"""Rendering for the transaction size figures.

The figures themselves are computed in
:mod:`urpm.core.transaction_sizes` — they feed the distupgrade
root-filesystem pre-flight as well as this summary line, and core
cannot import from ``cli``.  ``TransactionSizes`` and ``compute_sizes``
are re-exported here so every caller keeps a single import.
"""

from __future__ import annotations

from ...core.transaction_sizes import TransactionSizes, compute_sizes
from ...i18n import _

__all__ = ["TransactionSizes", "compute_sizes", "format_totals"]


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
