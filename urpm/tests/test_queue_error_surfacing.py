"""A failed transaction must say what failed.

``QueueResult`` carries failures in two places — the per-operation
``errors`` lists, and a queue-level ``overall_error`` for exceptions
caught before any operation could report.  There is no ``errors``
attribute on the result itself, so ``getattr(result, "errors", None)``
always yields ``None``.

Two sites did exactly that, and both were on the distupgrade path.
Every failed Tx A/B printed the placeholder instead of the diagnostic,
and the ``URPM_DUP_DIAG`` post-mortem recorded an empty error list on
precisely the runs it exists for.

Measured cost, on one tester's mga9→mga10: an ENOSPC surfaced as
``Tx B did not converge: queue reported failure``.  They ran
``--resume``, which failed again — this time on a purged ``.rpm``,
message ``No such file or directory: …`` — and saw the same empty
string.  With no lead, they ran ``--abort``, which rolled the media
back and left the machine half-migrated.

The message was available both times.
"""

from __future__ import annotations

import pytest

from urpm.core.transaction_queue import (
    OperationResult,
    OperationType,
    QueueResult,
)


def _op(success: bool, errors=None) -> OperationResult:
    return OperationResult(
        operation_id="install",
        op_type=OperationType.INSTALL,
        success=success,
        errors=list(errors or []),
    )


class TestCollectErrors:

    def test_per_operation_errors_win(self):
        """They carry the real rpm problems — file conflicts, failed
        extractions — so they are what the operator needs first."""
        r = QueueResult(
            success=False,
            operations=[_op(False, ["libfoo.so conflicts with bar"])],
            overall_error="something generic",
        )
        assert r.collect_errors() == ["libfoo.so conflicts with bar"]

    def test_falls_back_to_overall_error(self):
        """The queue-level message covers exceptions raised before any
        operation could report — where the whole diagnostic lives."""
        r = QueueResult(
            success=False,
            operations=[],
            overall_error="[Errno 2] No such file or directory: /c/foo.rpm",
        )
        assert r.collect_errors() == [
            "[Errno 2] No such file or directory: /c/foo.rpm"]

    def test_errors_from_several_failed_operations_are_merged(self):
        r = QueueResult(
            success=False,
            operations=[_op(False, ["a"]), _op(False, ["b", "c"])],
        )
        assert r.collect_errors() == ["a", "b", "c"]

    def test_successful_operations_contribute_nothing(self):
        """A queue can fail with some operations having succeeded ; only
        the failures are worth showing."""
        r = QueueResult(
            success=False,
            operations=[_op(True, ["not an error"]), _op(False, ["real"])],
        )
        assert r.collect_errors() == ["real"]

    def test_empty_when_nothing_reported(self):
        """No placeholder is invented here — the caller decides what to
        print when a failure genuinely left no message."""
        r = QueueResult(success=False, operations=[], overall_error="")
        assert r.collect_errors() == []

    def test_success_yields_nothing(self):
        r = QueueResult(success=True, operations=[_op(True)])
        assert r.collect_errors() == []

    def test_queueresult_still_has_no_errors_attribute(self):
        """Pins the premise.  If someone ever adds an ``errors`` field,
        the two ``getattr`` sites this fix removed would start working
        by accident and the reason for ``collect_errors`` would be
        lost — better to fail here and revisit deliberately."""
        assert not hasattr(QueueResult(success=True), "errors")


class TestStage3SurfacesTheMessage:
    """End of the chain : what the operator actually reads."""

    def _raise_from(self, queue_result):
        """Reproduce the convergence check of ``_run_one_side``."""
        errs = "; ".join(
            queue_result.collect_errors() if queue_result is not None else []
        ) or "queue reported failure (no detail available)"
        return f"Tx B did not converge: {errs}"

    def test_enospc_reaches_the_operator(self):
        """The original failure on the tester's machine."""
        msg = self._raise_from(QueueResult(
            success=False, operations=[],
            overall_error="[Errno 28] No space left on device",
        ))
        assert "No space left on device" in msg
        assert "queue reported failure" not in msg

    def test_purged_rpm_reaches_the_operator(self):
        """The second failure, on ``--resume``.  This one names the
        missing file, which points straight at the batch purge."""
        msg = self._raise_from(QueueResult(
            success=False,
            operations=[_op(False, [
                "foo-1.0-1.mga10.x86_64.rpm: [Errno 2] "
                "No such file or directory"])],
        ))
        assert "No such file or directory" in msg
        assert "foo-1.0-1.mga10.x86_64.rpm" in msg

    def test_placeholder_only_when_truly_nothing(self):
        """Kept as a last resort, and worded so it reads as an absence
        of information rather than as the reason."""
        msg = self._raise_from(QueueResult(success=False, operations=[]))
        assert "no detail available" in msg

    def test_none_result_does_not_crash(self):
        """``execute_install`` returns None on an empty queue."""
        assert "no detail available" in self._raise_from(None)


class TestBuildTsCatchesMissingFile:
    """``_build_rpm_ts`` caught only ``rpm.error``.

    ``os.open`` on a purged ``.rpm`` raises ``FileNotFoundError``, an
    ``OSError`` — so the single path that most needed a clean, named
    error was the one escaping the handler.
    """

    def test_oserror_is_handled_alongside_rpm_error(self):
        import inspect
        from urpm.core.transaction_queue import TransactionQueue
        src = inspect.getsource(TransactionQueue._build_rpm_ts)
        assert "except (rpm.error, OSError)" in src, (
            "a missing cached .rpm must be reported, not propagated as "
            "an unhandled exception"
        )

    @pytest.mark.skipif(
        pytest.importorskip("rpm", reason="needs rpm bindings") is None,
        reason="needs rpm bindings",
    )
    def test_missing_path_yields_a_named_error(self, tmp_path):
        """Real call : a path that does not exist must come back as an
        error carrying the filename, not blow up."""
        from urpm.core.transaction_queue import (
            QueuedOperation, TransactionQueue,
        )
        q = TransactionQueue()
        op = QueuedOperation(
            op_type=OperationType.INSTALL,
            targets=[],
            operation_id="install",
            verify_signatures=False,
        )
        missing = tmp_path / "gone-1.0-1.mga10.x86_64.rpm"
        ts, errors = q._build_rpm_ts(op, [str(missing)], [], {})
        assert ts is None
        assert errors
        assert "gone-1.0-1.mga10.x86_64.rpm" in errors[0]
