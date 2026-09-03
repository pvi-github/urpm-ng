"""``urpm audit`` — run every registered system integrity check.

The on-demand door into :mod:`urpm.core.audit`.  The same checks run
automatically after a ``distupgrade`` and on request via
``--check`` during install/upgrade/erase ; this verb exists for the
case with no operation attached — « is my system sound right now ? »,
typically after a crash, a manual rpm intervention, or a distupgrade
whose report scrolled past.

Reports only.  Every finding carries the command that would fix it,
but running it stays the operator's call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import colors
from ..helpers.audit_report import render_outcomes
from ...core.audit import available, run_all
from ...i18n import _, ngettext

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_audit(args, db: "PackageDatabase") -> int:
    """Run all checks and print a grouped report.

    Exit codes:
        * ``0`` — every check clean
        * ``1`` — at least one finding
    """
    checks = available()
    print(_("Running {n} check(s)…").format(n=len(checks)))
    for check in checks:
        print(colors.dim(f"  {check.name} — {check.summary}"))

    outcomes = run_all()
    total = render_outcomes(outcomes, show_clean=True)

    print()
    if total == 0:
        print(colors.success(_("Nothing to report.")))
        return 0

    print(colors.warning(ngettext(
        "{n} finding in total.",
        "{n} findings in total.",
        total,
    ).format(n=total)))
    return 1
