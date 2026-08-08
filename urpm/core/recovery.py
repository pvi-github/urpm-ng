"""Recovery from crashed / SIGKILLed / power-cut transactions.

SPEC_DISTUPGRADE §3.D Phase D.  Two entry points :

- :func:`check_orphaned_transactions` — **read-only** scan called at
  ``PackageDatabase.__init__``.  Returns transactions whose
  ``status='running'`` with a dead ``pid_running`` (crash or SIGKILL
  survived by the state row).  Feeds the startup warning banner
  (§3.D) so the user is told to run ``urpm recover``.
- :func:`reconcile_running_transactions` — **write** reconciliation
  called by ``urpm recover``.  For each orphaned row, cross-checks
  the rpmdb and flips ``status`` to ``'complete'`` or
  ``'interrupted'`` based on the presence of the target NEVRA.

Both share the low-level PID liveness helpers so a startup warning
never contradicts what ``urpm recover`` will decide.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Dict, Optional

if TYPE_CHECKING:
    from .database import PackageDatabase


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if a process with this PID exists.

    Same pattern as :mod:`urpm.core.sync_lock` and
    :mod:`urpm.cli.helpers.distupgrade_mesh`.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # EPERM = the process exists but we can't signal it
        return True
    return True


def _pid_is_urpm(pid: int) -> bool:
    """Return True when PID's cmdline looks like an urpm invocation.

    Cheap defense-in-depth against PID reuse across reboot on
    non-tmpfs setups.  Mirrors ``_pid_is_distupgrade`` in the mesh
    helper : find any argv element whose basename is ``'urpm'``
    (tolerates the shebang interpreter case where argv[0] is
    ``'python3'``).  Doesn't require the specific ``'distupgrade'``
    subcommand since any urpm write verb can leave an orphan row.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return False
    if argv and argv[-1] == b"":
        argv = argv[:-1]
    return any(
        os.path.basename(a) == b"urpm" for a in argv
    )


def check_orphaned_transactions(db: "PackageDatabase") -> List[Dict]:
    """Return every ``status='running'`` row with a dead owner PID.

    Read-only.  Cheap enough to run at every ``PackageDatabase``
    open : one SELECT plus one ``os.kill(pid, 0)`` per candidate.

    Returns:
        List of dicts ``{id, timestamp, action, command, user,
        pid_running}`` — the subset of :meth:`db.list_history` shape
        needed by the startup warning and by ``urpm recover``.
    """
    conn = db._get_connection()
    rows = conn.execute(
        """
        SELECT id, timestamp, action, command, user, pid_running
        FROM history
        WHERE status = 'running'
        ORDER BY id ASC
        """
    ).fetchall()

    orphans: List[Dict] = []
    for row in rows:
        pid = row["pid_running"]
        # A NULL pid_running on a status='running' row means the row
        # pre-dates the v33 migration.  We treat it as orphan on the
        # cautious side — the user will decide via `urpm recover`.
        if pid is None:
            orphans.append(dict(row))
            continue
        if not _pid_alive(pid):
            orphans.append(dict(row))
            continue
        # Live PID but repurposed for another program (post-reboot
        # PID reuse without tmpfs).  Cheap cross-check on cmdline.
        if not _pid_is_urpm(pid):
            orphans.append(dict(row))
    return orphans


def _pkg_installed_at_nevra(rpmdb_root: str, nevra: str) -> bool:
    """Return True if ``nevra`` is currently installed in the rpmdb.

    Uses the ``rpm`` bindings so we honour the exact same rpmdb the
    parent transaction was writing to.  ``nevra`` is matched by the
    rpm-provided ``Package.dbMatch`` filter, which handles epoch
    normalization and arch matching without any regex.
    """
    import rpm

    ts = rpm.TransactionSet(rpmdb_root)
    try:
        # rpm.MATCH_NEVRA doesn't exist as a public tag ; iterate
        # candidates by name and compare the full NEVRA string
        # produced by the header's own formatter to avoid substring
        # ambiguity on epochs.
        name = nevra.split("-", 1)[0]
        for hdr in ts.dbMatch("name", name):
            hdr_nevra = hdr.format("%{NEVRA}")
            if hdr_nevra == nevra:
                return True
        return False
    finally:
        # rpm.TransactionSet has no explicit close ; deleting the
        # ref lets rpm release the rpmdb lock promptly.
        del ts


def reconcile_running_transactions(db: "PackageDatabase",
                                   rpmdb_root: str = "/") -> Dict[int, str]:
    """Flip orphaned ``running`` rows to a terminal status.

    For each orphan, cross-check every :class:`history_packages`
    child NEVRA against the rpmdb :

    - Every ``install`` / ``upgrade`` action reached its target NEVRA
      → the whole transaction is marked ``'complete'``.
    - Otherwise (some rows missing, mixed state) →
      ``'interrupted'``.

    Called by ``urpm recover`` (Phase D verb).  Returns a mapping of
    ``{history_id: 'complete' | 'interrupted'}`` reflecting the
    decision taken.  Safe to call repeatedly — the second pass no
    longer sees the row as running.
    """
    conn = db._get_connection()
    decisions: Dict[int, str] = {}
    for orphan in check_orphaned_transactions(db):
        tx_id = orphan["id"]
        rows = conn.execute(
            """
            SELECT pkg_nevra, action
            FROM history_packages
            WHERE history_id = ?
            """,
            (tx_id,),
        ).fetchall()
        all_done = True
        for r in rows:
            action = r["action"]
            nevra = r["pkg_nevra"]
            if action in ("install", "upgrade", "downgrade"):
                if not _pkg_installed_at_nevra(rpmdb_root, nevra):
                    all_done = False
                    break
            elif action == "remove":
                if _pkg_installed_at_nevra(rpmdb_root, nevra):
                    all_done = False
                    break
        if all_done:
            db.complete_transaction(tx_id, return_code=0)
            decisions[tx_id] = "complete"
        else:
            db.abort_transaction(tx_id)
            decisions[tx_id] = "interrupted"
    return decisions
