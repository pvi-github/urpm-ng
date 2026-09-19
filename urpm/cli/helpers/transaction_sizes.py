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

    The third figure only appears when something is actually being
    given back: on an ordinary install the net change *is* the
    installed footprint, and repeating it under a second name is noise
    — noise is what trains an operator to stop reading the line that
    matters.

    When it does appear, it is the net, not the gross.  An upgrade
    frees the old version and immediately spends most of it again on
    the new one; announcing the gross told the operator a firefox
    upgrade would give back 390 MB, when the disk ends up a couple of
    megabytes lighter.  Only the net answers « will my disk hold
    this », which is the question being asked.
    """
    from ..display import format_size

    # The cache split modifies one quantity, so it is rendered into
    # that quantity rather than into four more variants of the whole
    # sentence.  Four templates times two states would be eight
    # msgids for one parenthesis.
    if sizes.cached:
        download = _("{dl} ({cached} already cached)").format(
            dl=format_size(sizes.to_fetch),
            cached=format_size(sizes.cached))
    else:
        download = format_size(sizes.download)

    if sizes.freed:
        if sizes.net < 0:
            template = _("Total : {n} package(s), download {dl}, "
                         "installed footprint {inst}, {delta} freed.")
        elif sizes.net > 0:
            template = _("Total : {n} package(s), download {dl}, "
                         "installed footprint {inst}, {delta} more used.")
        else:
            return _("Total : {n} package(s), download {dl}, "
                     "installed footprint {inst}, no net change.").format(
                         n=count,
                         dl=download,
                         inst=format_size(sizes.installed))
        return template.format(
            n=count,
            dl=download,
            inst=format_size(sizes.installed),
            delta=format_size(abs(sizes.net)))

    return _(
        "Total : {n} package(s), download {dl}, "
        "installed footprint {inst}.").format(
            n=count,
            dl=download,
            inst=format_size(sizes.installed))
