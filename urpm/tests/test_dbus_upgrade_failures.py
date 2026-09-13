"""An upgrade that does nothing must not be reported as a success.

Both failures here were seen at once, on a machine whose medium was a
``file://`` tree under a home directory that the service could not read.
Every download failed, and Discover was told the transaction had
succeeded, so it redrew the same list of pending updates with no error
anywhere. The journal agreed: *finished with success after 10899ms*.

Two separate defects produced that one silence, and they are the two
things these tests pin:

* a plan that had packages to put on the machine and came back with no
  file at all fell into the "nothing to upgrade" branch, which answers
  success. The empty plan is already answered earlier, so reaching that
  point with nothing downloaded can only mean a failure;
* the result of ``execute_upgrade`` was discarded, where the install
  path checks it. Whatever rpm said, the answer was success.

The third test is the reason the first one checks what it checks in the
order it does: an upgrade plan can carry removals, and going ahead with
them alone would erase the obsoleted packages while the ones meant to
replace them never arrived.
"""

from types import SimpleNamespace

import pytest

from urpm.core.resolver import TransactionType


#: What ``execute_upgrade`` answers when its queue turned out empty.
EMPTY_QUEUE = None

#: Distinguishes "the caller said nothing" from "the caller said
#: :data:`EMPTY_QUEUE`", which is a case under test.
_UNSET = object()


def _action(name, action=TransactionType.UPGRADE):
    return SimpleNamespace(name=name, action=action,
                           evr="1.0-1.mga10", arch="x86_64")


def _queue_result(success, error=""):
    return SimpleNamespace(success=success, overall_error=error,
                           operations=[])


class _Ops:
    """A stand-in for PackageOperations, recording what it was asked."""

    def __init__(self, download_items, downloaded_paths, upgrade_result):
        self._download_items = download_items
        self._downloaded_paths = downloaded_paths
        self._upgrade_result = upgrade_result
        self.calls = []

    def build_download_items(self, actions, resolver):
        return list(self._download_items), []

    def download_packages(self, items, progress_callback=None):
        results = [SimpleNamespace(path=path)
                   for path in self._downloaded_paths]
        # A download that failed still comes back, with no path.
        results += [SimpleNamespace(path=None)
                    for _ in range(len(items) - len(results))]
        return results, len(self._downloaded_paths), 0, None

    def begin_transaction(self, action, command, actions):
        self.calls.append('begin_transaction')
        return 42

    def execute_upgrade(self, rpm_paths, **kwargs):
        self.calls.append('execute_upgrade')
        return self._upgrade_result

    def abort_transaction(self, transaction_id):
        self.calls.append('abort_transaction')

    def complete_transaction(self, transaction_id):
        self.calls.append('complete_transaction')

    def mark_dependencies(self, resolver, actions):
        pass

    def notify_urpmd_cache_invalidate(self):
        pass

    def hooks_triggered_by(self, transaction_id):
        return []


def _run_upgrade(monkeypatch, actions, *, download_items=(),
                 downloaded_paths=(), upgrade_result=_UNSET):
    """Drive ``_run_upgrade`` over a stand-in world, return its answer."""
    from urpm.dbus import service as service_module

    if upgrade_result is _UNSET:
        upgrade_result = _queue_result(True)

    monkeypatch.setattr(
        "urpm.core.resolver.Resolver",
        lambda *args, **kwargs: SimpleNamespace(
            resolve_upgrade=lambda: SimpleNamespace(
                success=True, actions=list(actions), problems=[])))

    instance = service_module.UrpmDBusService()
    ops = _Ops(download_items, downloaded_paths, upgrade_result)
    instance._ops = ops

    completed = []
    instance._emit_progress = lambda *args, **kwargs: None
    instance._emit_complete = lambda op_id, ok, message="": completed.append(
        (ok, message))
    instance._return_invocation = lambda *args, **kwargs: None
    instance._progress_scale = lambda actions_, items: SimpleNamespace(
        downloading=lambda *a: 0, transacting=lambda *a: 0)
    instance._download_reporter = lambda *args, **kwargs: None
    instance._transaction_reporter = lambda *args, **kwargs: None

    instance._run_upgrade('op', None, None)
    return completed, ops


def test_a_plan_whose_downloads_all_failed_is_a_failure(monkeypatch):
    """What the machine with the unreadable medium actually hit."""
    completed, ops = _run_upgrade(
        monkeypatch, [_action("urpm-ng-core")],
        download_items=["urpm-ng-core.rpm"], downloaded_paths=())

    assert completed == [(False, "No packages downloaded")]
    assert 'begin_transaction' not in ops.calls


def test_failed_downloads_do_not_leave_the_removals_to_run_alone(monkeypatch):
    """Erasing the obsoleted packages on their own breaks the system."""
    actions = [_action("new-thing"),
               _action("old-thing", TransactionType.REMOVE)]

    completed, ops = _run_upgrade(
        monkeypatch, actions,
        download_items=["new-thing.rpm"], downloaded_paths=())

    assert completed == [(False, "No packages downloaded")]
    assert 'execute_upgrade' not in ops.calls


def test_a_failing_transaction_is_reported_and_aborted(monkeypatch):
    """The install path checks this; the upgrade path used not to."""
    completed, ops = _run_upgrade(
        monkeypatch, [_action("urpm-ng-core")],
        download_items=["urpm-ng-core.rpm"],
        downloaded_paths=["/var/cache/urpm/rpms/urpm-ng-core.rpm"],
        upgrade_result=_queue_result(False, "file conflict with lib64foo"))

    assert completed == [(False, "file conflict with lib64foo")]
    assert 'abort_transaction' in ops.calls
    assert 'complete_transaction' not in ops.calls


def test_an_upgrade_that_works_still_reports_success(monkeypatch):
    """Guard the guards: refusing everything would pass the tests above."""
    completed, ops = _run_upgrade(
        monkeypatch, [_action("urpm-ng-core")],
        download_items=["urpm-ng-core.rpm"],
        downloaded_paths=["/var/cache/urpm/rpms/urpm-ng-core.rpm"])

    ok, message = completed[0]
    assert ok
    assert 'complete_transaction' in ops.calls
    assert 'abort_transaction' not in ops.calls
    assert "Upgraded 1 package(s)" in message


def test_an_empty_queue_is_not_read_as_a_failure(monkeypatch):
    """``execute_upgrade`` answers None for an empty queue, as documented.

    Taking that for a failure would abort a transaction that had nothing
    to do, which is how the CLI pipeline reads it too.
    """
    completed, ops = _run_upgrade(
        monkeypatch, [_action("urpm-ng-core")],
        download_items=["urpm-ng-core.rpm"],
        downloaded_paths=["/var/cache/urpm/rpms/urpm-ng-core.rpm"],
        upgrade_result=EMPTY_QUEUE)

    assert completed[0][0] is True
    assert 'abort_transaction' not in ops.calls
