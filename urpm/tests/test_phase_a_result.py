"""Phase A must not pretend a failed transaction succeeded.

Phase A brings the machine up to date before the migration proper
computes anything against it.  It committed its transaction and threw
the result away: only an exception stopped it.

rpm does not raise for per-package problems — it reports them on the
result.  An upgrade that half-failed therefore returned normally, and
``mark_dependencies`` and ``complete_transaction`` ran as though all
was well.  Stage 1 then started against a machine that was not up to
date, which is the one thing Phase A exists to guarantee.

These tests drive ``run_phase_a_upgrade`` itself rather than the
``QueueResult`` it reads: the whole defect was that the value never
reached a condition, so asserting on the value alone would prove
nothing.  The resolve and the download are stubbed; the commit and
everything after it are the real code path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from urpm.core.distupgrade.phase_a import PhaseAError, run_phase_a_upgrade
from urpm.core.transaction_queue import (
    OperationResult,
    OperationType,
    QueueResult,
)


def _op(success: bool, errors=None) -> OperationResult:
    return OperationResult(
        operation_id="op-0",
        op_type=OperationType.INSTALL,
        success=success,
        errors=list(errors or []),
    )


class FakeOps:
    """Stands in for PackageOperations, recording what ran after commit."""

    def __init__(self, db=None):
        self.queue_result = FakeOps.next_result
        self.completed = False
        self.aborted = False
        self.marked = False
        FakeOps.last = self

    #: Set by each test before calling.
    next_result: QueueResult = None
    last: "FakeOps" = None

    def begin_transaction(self, *a, **kw):
        return 42

    def build_download_items(self, actions, resolver):
        # Everything already cached : no download step to stub.
        return [], ["/cache/foo.rpm"]

    def execute_upgrade(self, **kw):
        return self.queue_result

    def mark_dependencies(self, *a, **kw):
        self.marked = True

    def complete_transaction(self, *a, **kw):
        self.completed = True

    def abort_transaction(self, *a, **kw):
        self.aborted = True


class FakeResolver:
    def __init__(self, db, arch=None):
        pass

    def resolve_upgrade(self, *a, **kw):
        action = SimpleNamespace(
            name="foo",
            action=SimpleNamespace(value="upgrade"),
            nevra="foo-2-1.mga10.x86_64",
            size=1024,
            filesize=512,
            evr="2-1",
        )
        return SimpleNamespace(success=True, actions=[action], problems=[])


@pytest.fixture
def run(monkeypatch):
    """Return a callable driving Phase A with a chosen commit outcome."""
    import urpm.core.operations as operations
    import urpm.core.resolver as resolver_mod

    monkeypatch.setattr(operations, "PackageOperations", FakeOps)
    monkeypatch.setattr(resolver_mod, "Resolver", FakeResolver)

    def _run(queue_result):
        FakeOps.next_result = queue_result
        return run_phase_a_upgrade(db=object())

    return _run


class TestFailedCommitStopsPhaseA:

    def test_raises_instead_of_continuing(self, run):
        with pytest.raises(PhaseAError):
            run(QueueResult(success=False,
                            operations=[_op(False, ["file conflict"])]))

    def test_does_not_complete_the_transaction(self, run):
        """``complete_transaction`` on a failed upgrade records it in the
        history as though it had worked."""
        with pytest.raises(PhaseAError):
            run(QueueResult(success=False, operations=[_op(False, ["boom"])]))
        assert not FakeOps.last.completed
        assert FakeOps.last.aborted

    def test_does_not_mark_dependencies(self, run):
        """Marking deps of packages that were not installed leaves the
        autoremove bookkeeping describing a machine that never existed."""
        with pytest.raises(PhaseAError):
            run(QueueResult(success=False, operations=[_op(False, ["boom"])]))
        assert not FakeOps.last.marked

    def test_the_rpm_problem_reaches_the_message(self, run):
        with pytest.raises(PhaseAError, match="No space left on device"):
            run(QueueResult(
                success=False,
                operations=[_op(False, ["No space left on device"])]))

    def test_failure_past_the_first_operation_is_seen(self, run):
        """The narrowing fixed in the CLI would bite here too: a failure
        on operation 2 must not read as success."""
        with pytest.raises(PhaseAError, match="scriptlet failed"):
            run(QueueResult(
                success=False,
                operations=[_op(True), _op(False, ["scriptlet failed"])]))

    def test_queue_level_error_reaches_the_message(self, run):
        with pytest.raises(PhaseAError, match="No such file"):
            run(QueueResult(
                success=False,
                operations=[_op(True)],
                overall_error="[Errno 2] No such file or directory"))

    def test_silent_failure_still_stops(self, run):
        """Silence is not success: the guard keys on ``success``, not on
        whether anything was reported."""
        with pytest.raises(PhaseAError):
            run(QueueResult(success=False, operations=[_op(False)]))

    def test_message_says_what_to_do_next(self, run):
        with pytest.raises(PhaseAError, match="urpm upgrade"):
            run(QueueResult(success=False, operations=[_op(False, ["boom"])]))


class TestSuccessfulCommitIsUntouched:

    def test_completes_normally(self, run):
        assert run(QueueResult(success=True, operations=[_op(True)])) == 0
        assert FakeOps.last.completed
        assert FakeOps.last.marked
        assert not FakeOps.last.aborted
