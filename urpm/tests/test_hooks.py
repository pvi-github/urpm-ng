"""Tests for the post-operation hook rules.

The mechanism exists because none of rpm's own hooks can act once a
transaction is over: ``%post``, ``%posttrans`` and file triggers all run
inside it. The incident that proved it: the ``%post`` of
``urpm-ng-packagekit-backend`` restarted ``urpm-dbus.service``, which is
the service executing the transaction when the update comes from
Discover, so the package cut rpm off halfway through installing itself.

These tests cover the reading and matching half only, which performs
nothing. What matters here is that a rule fires on what was really
installed, that a file nobody can vouch for is refused, and that a
malformed rule is dropped instead of taking an operation down.
"""

import os

import pytest

from urpm.core import hooks
from urpm.core.hooks import (
    Action, Hook, OperationOutcome, hooks_for, load_hooks,
)


@pytest.fixture
def dirs(tmp_path):
    """A vendor directory and an administrator directory."""
    vendor = tmp_path / "usr" / "lib" / "urpm" / "hooks.d"
    admin = tmp_path / "etc" / "urpm" / "hooks.d"
    vendor.mkdir(parents=True)
    admin.mkdir(parents=True)
    return vendor, admin


@pytest.fixture
def trusted(monkeypatch):
    """Vouch for every file, since tests cannot own files as root.

    The refusal logic itself is covered by :class:`TestTrust`.
    """
    monkeypatch.setattr(hooks, "_refuse_untrusted", lambda path: None)


def _rule(identifier, watch="restart-on-completion", **extra):
    lines = [f"[on-completion:{identifier}]",
             f"watch = {watch}"]
    lines += [f"{key.replace('_', '-')} = {value}"
              for key, value in extra.items()]
    return "\n".join(lines) + "\n"


def _declared(capability="restart-on-completion", *values, **rest):
    """An outcome where the packages installed declared ``capability``."""
    return OperationOutcome(provides={capability: frozenset(values)}, **rest)


class TestDropInDirectories:
    """Vendor files and administrator overrides."""

    def test_both_directories_are_read(self, dirs, trusted):
        vendor, admin = dirs
        (vendor / "50-backend.cfg").write_text(_rule("from-vendor"))
        (admin / "90-local.cfg").write_text(_rule("from-admin"))

        report = load_hooks(vendor, admin)

        assert [h.identifier for h in report.hooks] == ["from-admin",
                                                        "from-vendor"]

    def test_an_administrator_file_masks_the_vendor_one(self, dirs, trusted):
        """Same name, same meaning as tmpfiles and sysusers."""
        vendor, admin = dirs
        (vendor / "50-backend.cfg").write_text(_rule("shipped"))
        (admin / "50-backend.cfg").write_text(_rule("overridden"))

        report = load_hooks(vendor, admin)

        assert [h.identifier for h in report.hooks] == ["overridden"]

    def test_masking_is_by_file_name_not_by_rule(self, dirs, trusted):
        """Masking replaces the file wholesale, rules included."""
        vendor, admin = dirs
        (vendor / "50-backend.cfg").write_text(
            _rule("kept-nowhere") + _rule("also-gone", watch="other"))
        (admin / "50-backend.cfg").write_text(_rule("the-only-one"))

        report = load_hooks(vendor, admin)

        assert [h.identifier for h in report.hooks] == ["the-only-one"]

    def test_files_are_read_in_name_order(self, dirs, trusted):
        vendor, _admin = dirs
        (vendor / "20-b.cfg").write_text(_rule("second"))
        (vendor / "10-a.cfg").write_text(_rule("first"))

        report = load_hooks(*dirs)

        assert {h.identifier for h in report.hooks} == {"first", "second"}

    def test_a_missing_directory_is_not_an_error(self, tmp_path, trusted):
        report = load_hooks(tmp_path / "nowhere", tmp_path / "neither")

        assert report.hooks == []
        assert report.rejected == []

    def test_only_cfg_files_count(self, dirs, trusted):
        vendor, _admin = dirs
        (vendor / "notes.txt").write_text(_rule("ignored"))
        (vendor / "real.cfg").write_text(_rule("kept"))

        report = load_hooks(*dirs)

        assert [h.identifier for h in report.hooks] == ["kept"]


class TestTrust:
    """The files drive actions taken as root."""

    def test_a_file_not_owned_by_root_is_refused(self, dirs):
        vendor, _admin = dirs
        path = vendor / "50-backend.cfg"
        path.write_text(_rule("would-run-as-root"))

        report = load_hooks(*dirs)

        assert report.hooks == []
        assert "not owned by root" in report.rejected[0][1]

    def test_a_world_writable_file_is_refused(self, dirs, monkeypatch):
        """Ownership alone would not be enough."""
        vendor, _admin = dirs
        path = vendor / "50-backend.cfg"
        path.write_text(_rule("tampered"))
        path.chmod(0o666)
        # Pretend root owns it, so the mode is what decides.
        real_stat = os.stat

        class _RootOwned:
            def __init__(self, info):
                self.st_uid = 0
                self.st_mode = info.st_mode

        monkeypatch.setattr(type(path), "stat",
                            lambda self, **kw: _RootOwned(real_stat(self)))

        report = load_hooks(*dirs)

        assert report.hooks == []
        assert "writable by group or others" in report.rejected[0][1]

    def test_a_file_that_cannot_be_examined_is_refused(self, tmp_path):
        refusal = hooks._refuse_untrusted(tmp_path / "gone.cfg")

        assert refusal is not None
        assert "cannot be read" in refusal

    def test_an_entry_vanishing_mid_listing_costs_only_itself(self, dirs,
                                                              trusted,
                                                              monkeypatch):
        """A directory can be readable while one entry is not."""
        vendor, _admin = dirs
        (vendor / "10-doomed.cfg").write_text(_rule("never-seen"))
        (vendor / "20-fine.cfg").write_text(_rule("survivor"))

        real_is_file = type(vendor).is_file

        def flaky(self, *args, **kwargs):
            if self.name == "10-doomed.cfg":
                raise OSError(2, "vanished")
            return real_is_file(self, *args, **kwargs)

        monkeypatch.setattr(type(vendor), "is_file", flaky)

        report = load_hooks(*dirs)

        assert [h.identifier for h in report.hooks] == ["survivor"]


class TestParsing:
    """A rule the loader accepts, and the ones it refuses."""

    def test_a_minimal_rule_defaults_to_reporting(self, dirs, trusted):
        """Acting is opt-in; needrestart makes the same choice."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(_rule("tell-me"))

        hook = load_hooks(*dirs).hooks[0]

        assert hook.action == Action.REPORT
        assert hook.watch == "restart-on-completion"
        assert hook.service == ""
        assert hook.only_on_full_success is False
        assert hook.source.name == "50.cfg"

    def test_a_rule_watches_one_capability_only(self, dirs, trusted):
        """Two would make the subject ambiguous once it comes from a value."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(
            _rule("wide", watch="libfoo.so.1 libbar.so.2"))

        report = load_hooks(*dirs)

        assert report.hooks == []
        assert "takes one capability" in report.rejected[0][1]

    def test_a_restart_rule_may_name_its_own_service(self, dirs, trusted):
        """An administrator's local policy about a silent package."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(_rule(
            "restart-apache", watch="libssl.so.3",
            action="restart-service", service="httpd"))

        hook = load_hooks(*dirs).hooks[0]

        assert hook.action == Action.RESTART_SERVICE
        assert hook.service == "httpd"

    def test_a_restart_rule_needs_no_service_of_its_own(self, dirs, trusted):
        """The shipped rule: the subject comes from the watched value."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(
            _rule("declared-restarts", action="restart-service"))

        hook = load_hooks(*dirs).hooks[0]

        assert hook.action == Action.RESTART_SERVICE
        assert hook.service == ""

    def test_only_on_full_success_is_read(self, dirs, trusted):
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(
            _rule("picky", only_on_full_success="yes"))

        assert load_hooks(*dirs).hooks[0].only_on_full_success is True

    def test_an_unknown_action_is_refused_not_downgraded(self, dirs, trusted):
        """A rule asking for what we cannot do must not become something else."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(_rule("wishful", action="reboot"))

        report = load_hooks(*dirs)

        assert report.hooks == []
        assert "unknown action 'reboot'" in report.rejected[0][1]

    def test_a_rule_without_a_condition_is_refused(self, dirs, trusted):
        """It could never fire, so it is a mistake, not a rule."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(
            "[on-completion:silent]\naction = report\n")

        report = load_hooks(*dirs)

        assert report.hooks == []
        assert "could never fire" in report.rejected[0][1]

    def test_a_section_of_another_phase_is_left_alone(self, dirs, trusted):
        """TX-Shield will bring an entry phase; it is not ours to run."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(
            "[on-start:check-space]\nwatch = x\n"
            + _rule("ours"))

        report = load_hooks(*dirs)

        assert [h.identifier for h in report.hooks] == ["ours"]
        assert report.rejected == []

    def test_a_malformed_file_does_not_take_the_others_down(self, dirs,
                                                            trusted):
        vendor, _admin = dirs
        (vendor / "10-broken.cfg").write_text("[unclosed\nnonsense\n")
        (vendor / "20-fine.cfg").write_text(_rule("survivor"))

        report = load_hooks(*dirs)

        assert [h.identifier for h in report.hooks] == ["survivor"]
        assert len(report.rejected) == 1

    def test_an_unknown_key_is_tolerated(self, dirs, trusted):
        """A newer rule file must stay readable by an older urpm."""
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(_rule("forward", from_the_future="yes"))

        assert load_hooks(*dirs).hooks[0].identifier == "forward"


class TestMatching:
    """Which rules a finished operation satisfies."""

    def test_a_rule_fires_on_a_capability_that_was_installed(self):
        hook = Hook("restart", "restart-on-completion")
        outcome = OperationOutcome(provides={
            "restart-on-completion": frozenset({"urpm-dbus"}),
            "python3": frozenset(),
        })

        assert hook.matches(outcome)

    def test_a_capability_declared_without_a_value_still_fires(self):
        """Declaring is what matches; the value only says what to act on."""
        hook = Hook("restart", "restart-on-completion")

        assert hook.matches(_declared())

    def test_a_rule_stays_quiet_otherwise(self):
        hook = Hook("restart", "restart-on-completion")

        assert not hook.matches(_declared("firefox"))

    def test_a_package_that_never_reached_the_disk_fires_nothing(self):
        """The partial-transaction case, with no special flag for it.

        The outcome is built from what was really installed, so a package
        left behind simply is not in it.
        """
        hook = Hook("restart", "restart-on-completion")

        assert not hook.matches(OperationOutcome(fully_successful=False))

    def test_a_picky_rule_waits_for_the_whole_operation(self):
        hook = Hook("restart", "x", only_on_full_success=True)

        assert not hook.matches(_declared("x", fully_successful=False))
        assert hook.matches(_declared("x", fully_successful=True))

    def test_an_ordinary_rule_fires_on_a_partial_operation(self):
        """What it cares about did land, so it has work to do."""
        hook = Hook("restart", "x")

        assert hook.matches(_declared("x", fully_successful=False))

    def test_selection_keeps_the_loading_order(self):
        first = Hook("a-first", "x")
        skipped = Hook("b-other", "y")
        last = Hook("c-last", "x")

        triggered = hooks_for(_declared("x"), [first, skipped, last])

        assert [t.hook for t in triggered] == [first, last]

    def test_selection_resolves_the_subjects_once_and_for_all(self):
        """So whoever acts later needs neither the outcome nor the rpmdb."""
        hook = Hook("restart", "restart-on-completion")

        triggered = hooks_for(
            _declared("restart-on-completion", "urpm-dbus"), [hook])

        assert triggered[0].subjects == {"urpm-dbus"}

class TestSubjects:
    """What a rule that fired actually applies to.

    This is the half that lets one shipped rule serve every package: the
    subject is the value of the watched capability, so a package names
    its own service in its own spec.
    """

    def test_the_subject_comes_from_the_capability_value(self):
        hook = Hook("declared-restarts", "restart-on-completion",
                    action=Action.RESTART_SERVICE)

        subjects = hook.subjects(_declared("restart-on-completion",
                                           "urpm-dbus"))

        assert subjects == {"urpm-dbus"}

    def test_two_packages_declaring_it_give_two_subjects(self):
        hook = Hook("declared-restarts", "restart-on-completion")

        subjects = hook.subjects(_declared("restart-on-completion",
                                           "urpm-dbus", "urpmd"))

        assert subjects == {"urpm-dbus", "urpmd"}

    def test_a_service_named_by_the_rule_wins(self):
        """The administrator's local policy about a silent package."""
        hook = Hook("restart-apache", "libssl.so.3",
                    action=Action.RESTART_SERVICE, service="httpd")

        assert hook.subjects(_declared("libssl.so.3", "ignored")) == {"httpd"}

    def test_a_capability_without_a_value_leaves_no_subject(self):
        """The rule fired but says nothing to act on; the caller reports it."""
        hook = Hook("declared-restarts", "restart-on-completion")

        assert hook.subjects(_declared()) == frozenset()


class TestOrdering:
    """Rules are applied in a stated order, not in directory order."""

    def test_rules_are_ordered_by_identifier(self, dirs, trusted):
        vendor, _admin = dirs
        (vendor / "50.cfg").write_text(_rule("zulu") + _rule("alpha"))

        report = load_hooks(*dirs)

        assert [h.identifier for h in report.hooks] == ["alpha", "zulu"]
