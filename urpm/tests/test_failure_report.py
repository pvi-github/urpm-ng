"""A failed transaction must say what failed, and how much it withheld.

Eight call sites across erase, cleanup, history undo and install printed
the same block by hand, and every copy carried the same two defects.

The list was cut at 3, 5 or 10 with nothing said.  An operator seeing
exactly three failures at the end of a migration cannot tell whether
there were four or forty -- and one did see exactly three, which is the
cap.

Worse, seven of the eight read ``operations[0].errors`` and reached
``overall_error`` only through an ``elif``.  A queue holding several
operations whose *second* one failed therefore printed nothing at all:
the loop ran over an empty list and the ``elif`` never fired.  Not a
truncated diagnostic -- a bare "failed" with no diagnostic whatsoever.
``QueueResult.collect_errors`` was written for exactly this question;
these sites never asked it.
"""

from __future__ import annotations

import pytest

from urpm.cli.helpers.failure_report import print_errors
from urpm.core.transaction_queue import (
    OperationResult,
    OperationType,
    QueueResult,
)


def _op(op_id: str, success: bool, errors=None) -> OperationResult:
    return OperationResult(
        operation_id=op_id,
        op_type=OperationType.INSTALL,
        success=success,
        errors=list(errors or []),
    )


class TestOverflowIsAnnounced:

    def test_says_how_many_were_withheld(self, capsys):
        withheld = print_errors([f"error {i}" for i in range(5)], limit=3)
        out = capsys.readouterr().out
        assert withheld == 2
        assert "2" in out
        assert "error 4" not in out, "the 5th must not be printed"

    def test_says_nothing_when_nothing_is_withheld(self, capsys):
        """A trailing note on a complete list would be noise, and worse,
        would train the operator to ignore it."""
        withheld = print_errors(["only one"], limit=3)
        out = capsys.readouterr().out
        assert withheld == 0
        assert "more" not in out.lower()

    def test_exactly_at_the_cap_is_complete(self, capsys):
        """The off-by-one that matters: three errors under a cap of three
        is the whole story, and must not claim otherwise."""
        assert print_errors(["a", "b", "c"], limit=3) == 0
        assert "more" not in capsys.readouterr().out.lower()

    def test_prints_every_error_within_the_cap(self, capsys):
        print_errors(["first", "second"], limit=3)
        out = capsys.readouterr().out
        assert "first" in out and "second" in out


class TestEmptyAndOddInputs:

    def test_empty_prints_nothing(self, capsys):
        """Callers print their own headline first; a blank bullet under
        it would read as a corrupted message."""
        assert print_errors([], limit=3) == 0
        assert capsys.readouterr().out == ""

    def test_non_string_errors_are_rendered(self, capsys):
        """rpm problems arrive as objects on some paths."""
        class Problem:
            def __str__(self):
                return "file conflict on /usr/bin/foo"

        print_errors([Problem()], limit=3)
        assert "file conflict on /usr/bin/foo" in capsys.readouterr().out

    def test_indent_is_honoured(self, capsys):
        """History undo nests its report one level deeper."""
        print_errors(["boom"], limit=3, indent="    ")
        assert capsys.readouterr().out.startswith("    ")


class TestFailurePastTheFirstOperation:
    """The regression that printed nothing at all.

    ``if operations: for err in operations[0].errors`` / ``elif
    overall_error`` — when operation 0 succeeded and operation 1 failed,
    the loop iterated an empty list and the ``elif`` was never reached.
    """

    def test_second_operation_failure_is_reported(self, capsys):
        result = QueueResult(
            success=False,
            operations=[
                _op("op-0", True),
                _op("op-1", False, ["No space left on device"]),
            ],
        )
        print_errors(result.collect_errors(), limit=3)
        assert "No space left on device" in capsys.readouterr().out

    def test_errors_from_several_operations_are_all_collected(self, capsys):
        result = QueueResult(
            success=False,
            operations=[
                _op("op-0", False, ["first problem"]),
                _op("op-1", False, ["second problem"]),
            ],
        )
        print_errors(result.collect_errors(), limit=5)
        out = capsys.readouterr().out
        assert "first problem" in out and "second problem" in out

    def test_queue_level_error_survives_when_operations_exist(self, capsys):
        """The other half of the same narrowing: operations present but
        none carrying an error, the diagnostic being at queue level.
        The old ``elif`` skipped it because ``operations`` was truthy."""
        result = QueueResult(
            success=False,
            operations=[_op("op-0", True)],
            overall_error="[Errno 2] No such file or directory: /cache/x.rpm",
        )
        print_errors(result.collect_errors(), limit=3)
        assert "No such file" in capsys.readouterr().out

    def test_a_failure_is_never_silent(self, capsys):
        """The property the eight sites were meant to have all along."""
        for result in (
            QueueResult(success=False, operations=[_op("a", False, ["x"])]),
            QueueResult(success=False, operations=[_op("a", True),
                                                   _op("b", False, ["y"])]),
            QueueResult(success=False, overall_error="z"),
            QueueResult(success=False, operations=[_op("a", True)],
                        overall_error="w"),
        ):
            print_errors(result.collect_errors(), limit=3)
            assert capsys.readouterr().out.strip(), (
                f"failed queue printed nothing: {result}"
            )
