"""Deferred cleanup of media rows flagged for post-reboot deletion.

SPEC_DISTUPGRADE §4.4 — Stage 4 marks the old (transposed) mga N
media rows as ``disabled_by='pending_drop'`` and returns instantly.
The actual DB purge of those media (and their thousands of package /
requires / provides rows) happens *after* reboot, either at urpmd
startup or on the first ``urpm`` CLI invocation post-reboot.

Rationale : on a 40k-package DB, physically deleting 50 media takes
~11 seconds (dominated by requires/provides row deletion + index
maintenance).  That cost has no user value at Stage 4 : the user is
about to reboot anyway, and holds no interactive session waiting on
it.  Deferring it moves the wait behind the reboot barrier so no one
notices.

Serialization : ``fcntl.flock`` on ``/run/urpm/deferred_cleanup.lock``
prevents urpmd and a concurrent CLI invocation from both attempting
the sweep.  The loser just returns 0 — the winner does the work.

Hot-path cost : a single indexed ``SELECT 1 FROM media WHERE
disabled_by='pending_drop' LIMIT 1`` fires on every urpm invocation.
Post-sweep, that returns nothing and the function is a ~microsecond
no-op with no lock file touched.
"""
from __future__ import annotations

import fcntl
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import PackageDatabase

logger = logging.getLogger(__name__)

LOCK_PATH = Path("/run/urpm/deferred_cleanup.lock")

# Tables scoped by ``media_id`` — bulk deleted via WHERE media_id IN.
_MEDIA_DEP_TABLES = (
    "packages",
    "package_files",
    "cache_files",
    "media_update_deltas",
    "appstream_scan_cache",
    "files_xml_state",
    "server_media",
)

# Tables scoped by ``pkg_id`` via FK → packages.id.  Deleted first
# via subquery so the DELETE FROM packages below has no cascade to
# trigger (which would run per-row on 200k+ dep rows).
_PACKAGE_DEP_TABLES = (
    "requires", "provides", "conflicts", "obsoletes",
    "recommends", "suggests", "supplements", "enhances",
)


def sweep_pending_drop(db: "PackageDatabase") -> int:
    """Purge every media flagged ``disabled_by='pending_drop'``.

    Cheap no-op fast-path when nothing is pending (single indexed
    SELECT).  fcntl-serialized so parallel callers (urpmd startup +
    urpm CLI) don't collide — the loser returns 0.

    Returns the number of media rows actually dropped (0 when
    nothing was pending or the lock was busy).
    """
    conn = db._get_connection()
    if not conn.execute(
        "SELECT 1 FROM media WHERE disabled_by = 'pending_drop' LIMIT 1"
    ).fetchone():
        return 0

    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("deferred cleanup: cannot create %s: %s",
                     LOCK_PATH.parent, exc)
        return 0

    try:
        lock_fd = open(LOCK_PATH, "w")
    except OSError as exc:
        logger.debug("deferred cleanup: cannot open lock %s: %s",
                     LOCK_PATH, exc)
        return 0

    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.debug(
                "deferred cleanup: lock held by another sweeper — skipping")
            return 0

        # Re-query under the lock in case another sweeper drained
        # things between the fast-path SELECT and us grabbing the
        # lock.
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM media WHERE disabled_by = 'pending_drop'"
        ).fetchall()]
        if not ids:
            return 0

        return _purge_media_ids(db, conn, ids)
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def _purge_media_ids(db, conn, ids) -> int:
    """Bulk-delete every dep row + media row for ``ids``.

    Called under the caller's fcntl lock.  Uses the same optimized
    pattern as the previous synchronous Stage 4 code : PRAGMA tuning
    outside the transaction, subquery deletes on pkg-scoped tables
    first (kills the cascade), then media-scoped tables, then media.
    """
    # Filter dep tables down to those actually present in this DB
    # (older test fixtures may not have them all).
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    dep_tables = [t for t in _MEDIA_DEP_TABLES if t in existing]
    pkg_dep_tables = [t for t in _PACKAGE_DEP_TABLES if t in existing]

    with db._lock:
        # Save + tune session PRAGMAs.  All must be set OUTSIDE the
        # transaction (SQLite refuses ``foreign_keys`` mid-tx).
        # ``foreign_keys=OFF`` is safe here because we hand-delete
        # every dependent row in the right order — no dangling refs
        # can result.
        prev_sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        prev_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        prev_cache = conn.execute("PRAGMA cache_size").fetchone()[0]
        prev_temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA cache_size = -524288")  # 512 MB in-memory
        conn.execute("PRAGMA temp_store = MEMORY")

        t0 = time.monotonic()
        placeholders = ",".join("?" * len(ids))
        try:
            conn.execute("BEGIN")
            # Kill weak-dep rows first so DELETE FROM packages
            # below has zero cascade to fire.
            for dep in pkg_dep_tables:
                ta = time.monotonic()
                conn.execute(
                    f"DELETE FROM {dep} WHERE pkg_id IN "
                    f"(SELECT id FROM packages WHERE media_id IN "
                    f"({placeholders}))",
                    ids)
                logger.debug("deferred cleanup: DELETE %s in %.2fs",
                             dep, time.monotonic() - ta)
            for dep in dep_tables:
                ta = time.monotonic()
                conn.execute(
                    f"DELETE FROM {dep} WHERE media_id IN ({placeholders})",
                    ids)
                logger.debug("deferred cleanup: DELETE %s in %.2fs",
                             dep, time.monotonic() - ta)
            ta = time.monotonic()
            cur = conn.execute(
                f"DELETE FROM media WHERE id IN ({placeholders})", ids)
            logger.debug("deferred cleanup: DELETE media in %.2fs",
                         time.monotonic() - ta)
            dropped = cur.rowcount if cur.rowcount is not None else len(ids)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute(f"PRAGMA synchronous = {int(prev_sync)}")
            conn.execute(f"PRAGMA foreign_keys = {int(prev_fk)}")
            conn.execute(f"PRAGMA cache_size = {int(prev_cache)}")
            conn.execute(f"PRAGMA temp_store = {int(prev_temp_store)}")

        logger.info(
            "deferred cleanup: purged %d old media in %.2fs",
            dropped, time.monotonic() - t0)
        return dropped
