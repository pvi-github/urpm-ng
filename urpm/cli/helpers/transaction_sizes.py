"""What a transaction costs, and where.

Two different numbers were being conflated under one field name and one
label.  ``PackageAction`` carries both:

* ``filesize`` — the compressed RPM, ``SOLVABLE_DOWNLOADSIZE``.  What
  crosses the network and lands in the payload directory, which may sit
  on another partition entirely.
* ``size`` — the installed footprint, ``SOLVABLE_INSTALLSIZE``.  What
  ``/`` has to hold once the package is unpacked.

The resolver filled ``install_size`` from ``filesize`` on the
distupgrade path and from ``size`` everywhere else, and ``upgrade``
printed the result under « Download size ».  On a cross-release upgrade
that announced a 13.4 GB download that was in fact the installed
footprint — a number that decides whether the operator starts at all,
and the wrong one.

A third figure was computed and never shown: what removals give back.
Without it the summary answers « how much arrives », never « will my
disk hold this », which is the question actually being asked.

``distupgrade`` had already worked this out inline.  Extracted here so
the definition lives once rather than being copied a fourth time — the
copies are what let the meanings drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ...i18n import _

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
        freed=sum(getattr(a, "size", 0) or 0 for a in outgoing),
    )


def format_totals(sizes: TransactionSizes, *, count: int) -> str:
    """One line summarising *sizes*, for *count* packages.

    The freed and net figures only appear when something is actually
    being removed: « freed 0 B » on an ordinary install is noise, and
    noise is what trains an operator to stop reading the line that
    matters.
    """
    from ..display import format_size

    if sizes.freed:
        return _(
            "Total : {n} package(s), download {dl}, "
            "installed footprint {inst}, freed {freed} "
            "(net {net}).").format(
                n=count,
                dl=format_size(sizes.download),
                inst=format_size(sizes.installed),
                freed=format_size(sizes.freed),
                net=_format_net(sizes.net))

    return _(
        "Total : {n} package(s), download {dl}, "
        "installed footprint {inst}.").format(
            n=count,
            dl=format_size(sizes.download),
            inst=format_size(sizes.installed))


def _format_net(net: int) -> str:
    """Signed, because the sign is the whole point of the figure."""
    from ..display import format_size

    if net < 0:
        return "-" + format_size(-net)
    return "+" + format_size(net)
