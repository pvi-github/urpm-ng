"""Printing the errors of a failed transaction.

Lives in ``helpers/`` because eight call sites across four commands —
erase, cleanup, history undo, install/upgrade — printed the same block
by hand, and each copy had drifted.  Two defects were common to all of
them, and both cost a real operator real information.

**The list was cut without saying so.**  Each site stopped at 3, 5 or
10 errors and printed nothing further.  An operator seeing exactly
three failures at the end of a migration has no way to tell whether
there were four or forty.

**A failure past the first package printed nothing at all.**  Seven
sites read ``queue_result.operations[0].errors`` and fell back to
``overall_error`` only in an ``elif``.  When the queue held several
operations and the *second* one failed, the loop found an empty list
and the ``elif`` never ran: the operator got a bare "failed" and no
diagnostic whatsoever.  ``QueueResult.collect_errors`` exists precisely
to answer "what actually went wrong" across every operation plus the
queue-level fallback ; these sites simply never asked it.

Callers pass an already-collected list rather than a result object.
The two result types in play — ``QueueResult`` and ``InstallResult`` —
expose their errors differently, and hiding that behind a duck-typed
parameter would make it easy to reintroduce the narrowing this module
exists to remove.
"""

from typing import Sequence

from .. import colors
from ...i18n import _

#: Kept per call site rather than unified.  How many errors are worth
#: reading before the tail becomes noise depends on the operation: a
#: history undo lists ten, an erase three.  Unifying them is an
#: ergonomics decision, not a defect fix.
DEFAULT_LIMIT = 3


def print_errors(
    errors: Sequence[str],
    *,
    limit: int = DEFAULT_LIMIT,
    indent: str = "  ",
) -> int:
    """Print up to *limit* errors, then say how many were withheld.

    Returns the number withheld, so a caller can react to a large tail
    (none does today; it keeps the function honest about the fact that
    something was dropped).

    Prints nothing at all for an empty sequence: a caller that has
    already printed its own "failed" headline should not be forced to
    guard the call, and a blank bullet would be worse than silence.
    """
    if not errors:
        return 0

    for error in errors[:limit]:
        print(f"{indent}{colors.error(str(error))}")

    withheld = len(errors) - limit
    if withheld > 0:
        print(indent + colors.dim(
            _("... and {count} more").format(count=withheld)))
        return withheld
    return 0
