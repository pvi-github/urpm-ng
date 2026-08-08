"""urpm recover — reconcile interrupted transactions.

SPEC_DISTUPGRADE §3.D Phase D — verbe user-facing qui appelle
:func:`urpm.core.recovery.reconcile_running_transactions` sur les
lignes ``history.status='running'`` avec ``pid_running`` mort.
"""

from typing import TYPE_CHECKING

from ...i18n import _
from .. import colors

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_recover(args, db: 'PackageDatabase') -> int:
    """Reconcile every orphaned transaction against the rpmdb.

    - No orphan → exit 0 with a discreet info message.
    - Orphans reconciled → print each row's new status and exit 0.
    - Unexpected error → exit 1.
    """
    from ...core.recovery import (
        check_orphaned_transactions,
        reconcile_running_transactions,
    )

    orphans = check_orphaned_transactions(db)
    if not orphans:
        print(colors.info(_("No interrupted transaction to reconcile.")))
        return 0

    print(colors.info(_(
        "Reconciling {n} interrupted transaction(s) against the rpmdb..."
    ).format(n=len(orphans))))
    try:
        decisions = reconcile_running_transactions(db)
    except Exception as exc:  # noqa: BLE001 — top-level surface
        print(colors.error(_(
            "Reconciliation failed: {error}").format(error=exc)))
        return 1

    for tx_id, status in sorted(decisions.items()):
        if status == "complete":
            print(colors.success(_(
                "  transaction #{tid}: complete "
                "(rpmdb matches recorded intent)").format(tid=tx_id)))
        else:
            print(colors.warning(_(
                "  transaction #{tid}: interrupted "
                "(rpmdb diverges from recorded intent)").format(tid=tx_id)))
    return 0
