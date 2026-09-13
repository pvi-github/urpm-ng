"""Tests for wiring the post-operation hooks into PackageOperations.

The split in two is the point. ``hooks_triggered_by`` only decides, so a
caller can report while its own caller is still listening; ``run_hooks``
acts, and belongs after that answer. Only the acting half has to come
last, and that is what lets a rule target the very process carrying it
out, which is the case the mechanism exists for.

The condition is matched against what the transaction *really*
installed, read back from the history where each package carries its own
verdict, so a partly applied transaction needs no special handling.
"""

from types import SimpleNamespace

import pytest

from urpm.core import init_system, operations
from urpm.core.hooks import Action, Hook, TriggeredHook
from urpm.core.init_system import Result, ServiceOutcome
from urpm.core.operations import PackageOperations


def _row(name, status="done", action="install"):
    return {"pkg_name": name, "pkg_nevra": f"{name}-1.0-1.mga10.x86_64",
            "action": action, "status": status}


@pytest.fixture
def ops(monkeypatch):
    """Operations over a stand-in database, with the rpmdb stubbed.

    ``history`` is what the transaction recorded; ``provides`` maps a
    package name to the capabilities it brings, each capability to its
    values, exactly as :func:`urpm.core.rpmdb.provides_of` does.
    """
    history = {"packages": []}
    provides = {}

    db = SimpleNamespace(get_transaction=lambda tid: history)
    instance = PackageOperations.__new__(PackageOperations)
    instance.db = db
    instance.audit = None

    def fake_provides_of(names, root="/"):
        brought = {}
        for name in names:
            for capability, values in provides.get(name, {}).items():
                brought.setdefault(capability, set()).update(values)
        return brought

    monkeypatch.setattr("urpm.core.rpmdb.provides_of", fake_provides_of)
    return instance, history, provides


@pytest.fixture
def rules(monkeypatch):
    """Control which rules are on the machine."""
    loaded = []
    monkeypatch.setattr(
        "urpm.core.hooks.load_hooks",
        lambda *args, **kwargs: SimpleNamespace(hooks=loaded, rejected=[]))
    return loaded


def _declares(service="urpm-dbus"):
    """What a package that asks to be restarted brings in."""
    return {"restart-on-completion": {service}}


class TestWhatTheOperationDid:
    """The outcome is read back from the history, not from the plan."""

    def test_a_rule_fires_on_what_was_installed(self, ops, rules):
        instance, history, provides = ops
        history["packages"] = [_row("urpm-ng-packagekit-backend")]
        provides["urpm-ng-packagekit-backend"] = _declares()
        rules.append(Hook("restart", "restart-on-completion"))

        triggered = instance.hooks_triggered_by(1)

        assert [t.hook.identifier for t in triggered] == ["restart"]

    def test_the_service_to_act_on_comes_from_the_package(self, ops, rules):
        """One shipped rule, and each package names its own service."""
        instance, history, provides = ops
        history["packages"] = [_row("urpm-ng-packagekit-backend"),
                               _row("some-daemon")]
        provides["urpm-ng-packagekit-backend"] = _declares("urpm-dbus")
        provides["some-daemon"] = _declares("somed")
        rules.append(Hook("declared-restarts", "restart-on-completion"))

        triggered = instance.hooks_triggered_by(1)

        assert triggered[0].subjects == {"urpm-dbus", "somed"}

    def test_a_package_that_failed_fires_nothing(self, ops, rules):
        """The partial-transaction case, with no special handling."""
        instance, history, provides = ops
        history["packages"] = [_row("urpm-ng-packagekit-backend",
                                    status="failed")]
        provides["urpm-ng-packagekit-backend"] = _declares()
        rules.append(Hook("restart", "restart-on-completion"))

        assert instance.hooks_triggered_by(1) == []

    def test_a_package_that_was_skipped_fires_nothing(self, ops, rules):
        instance, history, provides = ops
        history["packages"] = [_row("pkg", status="skipped")]
        provides["pkg"] = _declares()
        rules.append(Hook("restart", "restart-on-completion"))

        assert instance.hooks_triggered_by(1) == []

    def test_a_removal_brings_nothing_in(self, ops, rules):
        """Removing takes capabilities away; no vocabulary for that yet."""
        instance, history, provides = ops
        history["packages"] = [_row("pkg", action="remove")]
        provides["pkg"] = _declares()
        rules.append(Hook("restart", "restart-on-completion"))

        assert instance.hooks_triggered_by(1) == []

    def test_a_picky_rule_waits_for_every_package(self, ops, rules):
        instance, history, provides = ops
        history["packages"] = [_row("pkg"), _row("other", status="failed")]
        provides["pkg"] = _declares()
        rules.append(Hook("restart", "restart-on-completion",
                          only_on_full_success=True))

        assert instance.hooks_triggered_by(1) == []

    def test_the_same_rule_fires_when_everything_landed(self, ops, rules):
        instance, history, provides = ops
        history["packages"] = [_row("pkg"), _row("other")]
        provides["pkg"] = _declares()
        rules.append(Hook("restart", "restart-on-completion",
                          only_on_full_success=True))

        assert len(instance.hooks_triggered_by(1)) == 1


class TestDecidingIsFree:
    """``hooks_triggered_by`` must not touch the machine."""

    def test_no_rule_means_no_rpmdb_read(self, ops, rules, monkeypatch):
        """The common case pays nothing at all."""
        instance, history, _provides = ops
        history["packages"] = [_row("pkg")]

        def forbidden(*args, **kwargs):
            raise AssertionError("the rpmdb was read for nothing")

        monkeypatch.setattr("urpm.core.rpmdb.provides_of", forbidden)

        assert instance.hooks_triggered_by(1) == []

    def test_deciding_restarts_nothing(self, ops, rules, monkeypatch):
        instance, history, provides = ops
        history["packages"] = [_row("pkg")]
        provides["pkg"] = _declares()
        rules.append(Hook("restart", "restart-on-completion",
                          action=Action.RESTART_SERVICE))

        def forbidden(*args, **kwargs):
            raise AssertionError("a service was touched while only deciding")

        monkeypatch.setattr(init_system, "try_restart", forbidden)

        assert len(instance.hooks_triggered_by(1)) == 1

    def test_an_unknown_transaction_is_not_an_error(self, ops, rules):
        instance, _history, _provides = ops
        instance.db = SimpleNamespace(get_transaction=lambda tid: None)
        rules.append(Hook("restart", "restart-on-completion"))

        assert instance.hooks_triggered_by(1) == []


class TestActing:
    """``run_hooks`` is the half that changes the machine."""

    def test_a_restart_rule_restarts_what_it_was_given(self, ops, monkeypatch):
        instance, _history, _provides = ops
        asked = []
        monkeypatch.setattr(
            init_system, "try_restart",
            lambda service: asked.append(service) or ServiceOutcome(
                service, Result.RESTARTED))
        entry = TriggeredHook(
            Hook("restart", "restart-on-completion",
                 action=Action.RESTART_SERVICE),
            frozenset({"urpm-dbus"}))

        results = instance.run_hooks([entry])

        assert asked == ["urpm-dbus"]
        assert results[0][1][0].acted

    def test_a_rule_covering_several_services_restarts_them_all(
            self, ops, monkeypatch):
        instance, _history, _provides = ops
        asked = []
        monkeypatch.setattr(
            init_system, "try_restart",
            lambda service: asked.append(service) or ServiceOutcome(
                service, Result.RESTARTED))
        entry = TriggeredHook(
            Hook("restart", "restart-on-completion",
                 action=Action.RESTART_SERVICE),
            frozenset({"urpm-dbus", "somed"}))

        results = instance.run_hooks([entry])

        assert asked == ["somed", "urpm-dbus"]  # sorted, so reproducible
        assert len(results[0][1]) == 2

    def test_a_restart_with_nothing_to_restart_does_nothing(self, ops,
                                                            monkeypatch):
        """The capability was declared bare; the caller reports it."""
        instance, _history, _provides = ops

        def forbidden(*args, **kwargs):
            raise AssertionError("something nameless was restarted")

        monkeypatch.setattr(init_system, "try_restart", forbidden)
        entry = TriggeredHook(Hook("restart", "restart-on-completion",
                                   action=Action.RESTART_SERVICE))

        assert instance.run_hooks([entry]) == [(entry, [])]

    def test_a_reporting_rule_touches_nothing(self, ops, monkeypatch):
        """Reporting is the default, and it is inert."""
        instance, _history, _provides = ops

        def forbidden(*args, **kwargs):
            raise AssertionError("a report restarted something")

        monkeypatch.setattr(init_system, "try_restart", forbidden)
        entry = TriggeredHook(
            Hook("tell-me", "restart-on-completion", action=Action.REPORT),
            frozenset({"urpm-dbus"}))

        assert instance.run_hooks([entry]) == [(entry, [])]

    def test_a_hook_that_raises_does_not_stop_the_others(self, ops,
                                                         monkeypatch):
        """The operation is already committed; nothing here may undo it."""
        instance, _history, _provides = ops

        def explode(service):
            if service == "boom":
                raise RuntimeError("the init went sideways")
            return ServiceOutcome(service, Result.RESTARTED)

        monkeypatch.setattr(init_system, "try_restart", explode)
        first = TriggeredHook(Hook("a", "x", action=Action.RESTART_SERVICE),
                              frozenset({"boom"}))
        second = TriggeredHook(Hook("b", "x", action=Action.RESTART_SERVICE),
                               frozenset({"fine"}))

        results = instance.run_hooks([first, second])

        assert results[0][1][0].result == Result.FAILED
        assert results[1][1][0].acted

    def test_one_failing_service_does_not_cost_the_rule_its_others(
            self, ops, monkeypatch):
        instance, _history, _provides = ops

        def explode(service):
            if service == "boom":
                raise RuntimeError("the init went sideways")
            return ServiceOutcome(service, Result.RESTARTED)

        monkeypatch.setattr(init_system, "try_restart", explode)
        entry = TriggeredHook(Hook("a", "x", action=Action.RESTART_SERVICE),
                              frozenset({"boom", "fine"}))

        outcomes = instance.run_hooks([entry])[0][1]

        assert {o.service: o.result for o in outcomes} == {
            "boom": Result.FAILED, "fine": Result.RESTARTED}

    def test_every_rule_gets_an_answer(self, ops, monkeypatch):
        instance, _history, _provides = ops
        monkeypatch.setattr(
            init_system, "try_restart",
            lambda service: ServiceOutcome(service, Result.NOT_RUNNING))
        entries = [
            TriggeredHook(Hook("a", "x", action=Action.RESTART_SERVICE),
                          frozenset({"one"})),
            TriggeredHook(Hook("b", "x"), frozenset({"two"})),
        ]

        results = instance.run_hooks(entries)

        assert [e.hook.identifier for e, _outcomes in results] == ["a", "b"]
