"""What a transaction costs, and where.

Two different numbers were being conflated under one field name and one
label.  ``PackageAction`` carries both:

* ``filesize`` — the compressed RPM, ``SOLVABLE_DOWNLOADSIZE``.  What
  crosses the network and lands in the payload directory, which may sit
  on another partition entirely.
* ``size`` — the installed footprint, ``SOLVABLE_INSTALLSIZE`` for a
  candidate, ``RPMTAG_SIZE`` for something already installed.  What the
  root filesystem has to hold once the package is unpacked.

The resolver filled ``install_size`` from ``filesize`` on the
distupgrade path and from ``size`` everywhere else, and ``upgrade``
printed the result under « Download size ».  On a cross-release upgrade
that announced a 13.4 GB download that was in fact the installed
footprint — a number that decides whether the operator starts at all,
and the wrong one.

A third figure was computed and never shown: what removals give back.
Without it the summary answers « how much arrives », never « will my
disk hold this », which is the question actually being asked.

``freed`` counts two different things, and missing the second one is
what made a distupgrade pre-flight demand 14 GB where under 5 were
needed.  An explicit removal gives its bytes back — that part was
always counted.  But on a cross-release upgrade almost nothing is an
explicit removal: libsolv emits a single ``SOLVER_TRANSACTION_UPGRADE``
step carrying only the *new* solvable, and the version it replaces
never appears as an action at all.  Its space is freed just the same,
so ``PackageAction.from_size`` carries it and it is counted here.

This lives in ``core`` rather than next to its formatting because the
figures are no longer only for display: :mod:`urpm.core.distupgrade.
root_space` builds the pre-flight space verdict on them, and a core
module reaching into ``cli`` to sum integers would invert the layering.
:mod:`urpm.cli.helpers.transaction_sizes` re-exports both names and
keeps the rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

#: Actions that bring bytes in.  ``reinstall`` counts: the payload is
#: fetched and unpacked like any other, even though the net footprint
#: barely moves.
_INCOMING = ("install", "upgrade", "reinstall")


@dataclass(frozen=True)
class TransactionSizes:
    """Bytes moved by a transaction, split by where they land."""

    download: int
    installed: int
    freed: int

    @property
    def net(self) -> int:
        """Change in used space on ``/``.

        Negative when removals outweigh what arrives — which is common
        on a cleanup and not rare on a cross-release upgrade once
        obsolete packages go.
        """
        return self.installed - self.freed


def compute_sizes(actions: Iterable) -> TransactionSizes:
    """Sum the three figures over *actions*.

    ``filesize`` falls back to ``size`` when the resolver could not
    populate it — synthesis metadata is not always complete, and a
    download total that silently reads zero is worse than one that
    over-estimates.
    """
    actions = list(actions)
    incoming = [a for a in actions if getattr(a.action, "value", a.action) in _INCOMING]
    outgoing = [a for a in actions if getattr(a.action, "value", a.action) == "remove"]

    return TransactionSizes(
        download=sum((getattr(a, "filesize", 0) or getattr(a, "size", 0) or 0)
                     for a in incoming),
        installed=sum(getattr(a, "size", 0) or 0 for a in incoming),
        # Explicit removals, plus the versions the incoming upgrades
        # replace : both give their bytes back, and only the first kind
        # has an action of its own to be counted from.
        freed=(sum(getattr(a, "size", 0) or 0 for a in outgoing)
               + sum(getattr(a, "from_size", 0) or 0 for a in incoming)),
    )
