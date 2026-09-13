"""Tests for driving services through the distribution's own wrapper.

urpm-ng carries no systemd dependency, so a hook rule says "this service
must restart" and never names an init. The translation is not ours
either: Mageia ships ``service`` (from ``initscripts``, required by
``basesystem-minimal``), which runs the SysV script when there is one and
redirects to systemd otherwise. Letting it decide keeps the answer the
distribution's.

The property that protects machines is pinned here: nothing in this
module ever *starts* a service. Restarting a display manager or sshd
underneath a live session is how a machine breaks, and task #105, sshd
dying during a distupgrade, says what that costs.
"""

import shutil
import subprocess

import pytest

from urpm.core import init_system
from urpm.core.init_system import Result, ServiceOutcome


@pytest.fixture
def commands(monkeypatch):
    """Record what would be run, and decide what it returns.

    ``codes`` maps a command line to its exit code, so a test states
    "status says yes, condrestart fails" without touching a service.
    """
    ran = []
    codes = {}

    def fake_run(command, timeout):
        ran.append((list(command), timeout))
        return codes.get(" ".join(command), 0)

    monkeypatch.setattr(init_system, "_run", fake_run)
    monkeypatch.setattr(init_system.shutil, "which", lambda name: "/usr/sbin/" + name)
    return ran, codes


@pytest.fixture
def no_wrapper(monkeypatch):
    """A machine with no ``service`` command at all."""
    monkeypatch.setattr(init_system.shutil, "which", lambda name: None)


class TestAvailability:
    """Whether there is anything here to drive services with."""

    def test_the_wrapper_is_looked_for_by_name(self, monkeypatch):
        seen = []
        monkeypatch.setattr(init_system.shutil, "which",
                            lambda name: seen.append(name) or "/usr/sbin/service")

        assert init_system.available()
        assert seen == ["service"]

    def test_its_absence_is_reported_not_guessed(self, no_wrapper):
        assert not init_system.available()

    def test_it_is_present_on_this_machine(self):
        """basesystem-minimal requires initscripts, so it should be."""
        assert shutil.which("service") is not None


class TestRunningState:
    """Asked before restarting, so the report can tell the two apart."""

    def test_the_wrapper_is_asked_for_the_status(self, commands):
        ran, _codes = commands

        assert init_system.is_running("urpm-dbus")
        assert ran[0][0] == ["service", "urpm-dbus", "status"]

    def test_a_non_zero_answer_means_not_running(self, commands):
        _ran, codes = commands
        codes["service urpm-dbus status"] = 3

        assert not init_system.is_running("urpm-dbus")

    def test_nothing_is_asked_without_a_wrapper(self, commands, no_wrapper):
        ran, _codes = commands

        assert not init_system.is_running("urpm-dbus")
        assert ran == []


class TestRestart:
    """Restart, never start."""

    def test_a_running_service_is_restarted(self, commands):
        ran, _codes = commands

        outcome = init_system.try_restart("urpm-dbus")

        assert outcome == ServiceOutcome("urpm-dbus", Result.RESTARTED)
        assert outcome.acted
        assert ran[-1][0] == ["service", "urpm-dbus", "condrestart"]

    def test_a_stopped_service_is_left_alone(self, commands):
        """The rule that protects machines: never start anything."""
        ran, codes = commands
        codes["service urpm-dbus status"] = 3

        outcome = init_system.try_restart("urpm-dbus")

        assert outcome.result == Result.NOT_RUNNING
        assert not outcome.acted
        assert len(ran) == 1, "it asked, and then did nothing"

    def test_without_a_wrapper_it_says_so(self, commands, no_wrapper):
        """Rather than hand back a failure that looks like the service's."""
        ran, _codes = commands

        outcome = init_system.try_restart("urpm-dbus")

        assert outcome.result == Result.NO_SERVICE_TOOL
        assert "not available" in outcome.message
        assert ran == []

    def test_a_refused_restart_is_reported_with_its_code(self, commands):
        _ran, codes = commands
        codes["service urpm-dbus condrestart"] = 1

        outcome = init_system.try_restart("urpm-dbus")

        assert outcome.result == Result.FAILED
        assert "exited with 1" in outcome.message

    def test_a_restart_gets_a_longer_allowance_than_a_question(self,
                                                               commands):
        ran, _codes = commands

        init_system.try_restart("urpm-dbus")

        query_timeout, restart_timeout = ran[0][1], ran[1][1]
        assert restart_timeout > query_timeout


class TestNoInitIsNamed:
    """The whole point: the wrapper decides, we do not."""

    def test_no_command_ever_names_an_init(self, commands):
        ran, _codes = commands

        init_system.is_running("urpm-dbus")
        init_system.try_restart("urpm-dbus")

        for command, _timeout in ran:
            assert command[0] == "service", command
            assert "systemctl" not in command
            assert "/etc/init.d" not in " ".join(command)

    def test_the_verb_is_the_conditional_one(self, commands):
        """``condrestart``, which systemd accepts as its ``try-restart``."""
        ran, _codes = commands

        init_system.try_restart("urpm-dbus")

        assert ran[-1][0][-1] == "condrestart"

    def test_the_service_is_named_bare(self, commands):
        """No ``.service`` suffix: the wrapper appends what it needs."""
        ran, _codes = commands

        init_system.is_running("urpm-dbus")

        assert ran[0][0][1] == "urpm-dbus"


class TestNeverRaises:
    """A hook runs after the operation is recorded; it may not undo it."""

    def test_a_timeout_becomes_a_code(self, monkeypatch):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="service", timeout=1)

        monkeypatch.setattr(subprocess, "run", timeout)

        assert init_system._run(["service", "x", "status"], 1) == 124

    def test_a_missing_command_becomes_a_code(self, monkeypatch):
        def missing(*args, **kwargs):
            raise FileNotFoundError(2, "no such file")

        monkeypatch.setattr(subprocess, "run", missing)

        assert init_system._run(["service", "x", "status"], 1) == 127

    def test_a_wrapper_that_disappears_mid_flight_is_survived(self,
                                                              monkeypatch):
        """Present when looked for, gone when run."""
        monkeypatch.setattr(init_system.shutil, "which",
                            lambda name: "/usr/sbin/service")

        def missing(*args, **kwargs):
            raise FileNotFoundError(2, "no such file")

        monkeypatch.setattr(subprocess, "run", missing)

        outcome = init_system.try_restart("urpm-dbus")

        assert outcome.result == Result.NOT_RUNNING
