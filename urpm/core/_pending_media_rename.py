"""One-shot cleanup of media display names polluted by the pre-3fafe62
``cmd_media_discover`` bug.

Before commit 3fafe62, ``urpm media discover`` overwrote the upstream
``name=`` field with a generated ``f"mga{m.version}-{m.short_name}"``
string regardless of what ``parse_media_cfg`` had produced.  The fix
stops the bleeding for new discovers but leaves existing databases
with mixed naming.

This module materialises a tiny one-shot queue **outside** the SQLite
schema:

    /var/lib/urpm/pending-name-cleanup.list   (one media_id per line)

* :func:`write_queue` is called from the urpm-ng RPM ``%post core``
  scriptlet.  It scans for any remaining buggy media row and lists
  their ids in the file.  No-op when none found (no file produced).

* :func:`drain_queue` is called from ``cmd_media_update`` after a
  successful sync — by then network has just been proven alive.  For
  each queued id, it tries
  :func:`urpm.core.media_cfg.resolve_display_name` to pull the
  upstream ``name=`` from the parent media.cfg; on success it
  rewrites the file with the queued id removed (atomic rename).
  When the last id is removed the file is deleted entirely.

When the file is gone, ``drain_queue`` is a single ``stat()`` —
zero work, no DB scan.

Lifetime
========

Pure temporary migration code.  Once the user base has worked through
the queue (typically one ``urpm media update`` after the upgrade),
the file is deleted and never comes back.  See
``doc/TODO_LATER.md`` — this module is scheduled for deletion two
urpm-ng releases out.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Standard urpm state directory.  Chosen rather than e.g.
# ``/run/urpm`` because the file MUST survive reboots — the user may
# not run ``urpm media update`` between the package upgrade and the
# next reboot.
QUEUE_PATH = Path("/var/lib/urpm/pending-name-cleanup.list")


def write_queue(db, *, queue_path: Path = QUEUE_PATH) -> int:
    """Scan ``db`` for media rows still carrying the obsolete
    ``mga{version}-{short_name}`` display name and list them in
    ``queue_path``.

    No-op (no file written) when nothing matches — keeps fresh
    installs untouched.

    Args:
        db: ``PackageDatabase`` instance.
        queue_path: Override for tests.

    Returns:
        Number of ids queued.  ``0`` means no file was written.
    """
    cursor = db._get_connection().execute(
        "SELECT id FROM media "
        "WHERE name LIKE ('mga' || mageia_version || '-%')"
    )
    ids = [str(row['id']) for row in cursor]
    if not ids:
        # No cleanup needed → no file → drain_queue stays a no-op
        # forever on this machine.
        return 0

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(queue_path, "\n".join(ids) + "\n")
    logger.info(
        "Queued %d media row(s) for display-name cleanup at next "
        "'urpm media update' (%s)",
        len(ids), queue_path,
    )
    return len(ids)


def drain_queue(db, *, queue_path: Path = QUEUE_PATH) -> None:
    """Process the cleanup queue file, if it exists.

    For each queued media id:
      * Compute a clean display name through
        :func:`urpm.core.media_cfg.resolve_display_name`
        (upstream-derived when possible, ``_make_display_name``
        fallback otherwise).
      * On rename success, drop the id from the queue file
        atomically.
      * On failure (rename collision, etc.), keep the id for the
        next round.

    When the queue file becomes empty, it is deleted: future calls
    short-circuit on the ``exists()`` check.
    """
    if not queue_path.exists():
        return

    try:
        raw = queue_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "pending-name-cleanup: cannot read %s: %s", queue_path, exc,
        )
        return

    ids = _parse_queue(raw)
    if not ids:
        # Empty or corrupt file — drop it.
        _safe_unlink(queue_path)
        return

    remaining: List[int] = []
    for media_id in ids:
        if _try_rename(db, media_id):
            continue
        remaining.append(media_id)

    if remaining:
        _atomic_write(
            queue_path,
            "\n".join(str(i) for i in remaining) + "\n",
        )
        logger.info(
            "pending-name-cleanup: %d/%d media still pending",
            len(remaining), len(ids),
        )
    else:
        _safe_unlink(queue_path)
        logger.info(
            "pending-name-cleanup: queue drained (%d media renamed)",
            len(ids),
        )


# ── Internals ─────────────────────────────────────────────────────────


def _try_rename(db, media_id: int) -> bool:
    """Attempt to give ``media_id`` a clean display name.

    Returns True on success (row updated or already clean — either
    way safe to drop from the queue), False on failure that warrants
    a retry next round (rename collision, etc.).
    """
    conn = db._get_connection()
    row = conn.execute(
        "SELECT id, name, short_name, mageia_version, relative_path "
        "FROM media WHERE id = ?",
        (media_id,),
    ).fetchone()
    if row is None:
        # Media was removed since the queue was written — drop it.
        return True

    expected_prefix = f"mga{row['mageia_version']}-"
    if not row['name'].startswith(expected_prefix):
        # Someone already renamed it (manually, via discover, ...).
        return True

    new_name = _resolve_clean_name(db, row)
    if not new_name or new_name == row['name']:
        # Couldn't derive anything better right now — keep queued
        # for the next round.
        return False

    existing = conn.execute(
        "SELECT id FROM media WHERE name = ? AND id != ?",
        (new_name, row['id']),
    ).fetchone()
    if existing is not None:
        logger.warning(
            "pending-name-cleanup: '%s' → '%s' collides with media "
            "#%d, keeping the buggy name", row['name'], new_name,
            existing['id'],
        )
        # Collision will not resolve itself — drop from queue so we
        # do not spin on it forever.
        return True

    conn.execute(
        "UPDATE media SET name = ? WHERE id = ?",
        (new_name, row['id']),
    )
    conn.commit()
    logger.info(
        "pending-name-cleanup: renamed '%s' → '%s'",
        row['name'], new_name,
    )
    return True


def _resolve_clean_name(db, row) -> Optional[str]:
    """Run the standard name-resolution cascade for the queued row.

    Looks up the upstream media.cfg through any enabled, non-
    blacklisted server linked to the media; falls back to
    ``_make_display_name(section)`` when none is reachable.  Returns
    ``None`` only in the degenerate case of a row with no
    ``relative_path`` AND no usable short_name.
    """
    from .media_cfg import resolve_display_name, _make_display_name

    rel = row['relative_path'] or ''
    idx = rel.rfind('/media/')
    section = rel[idx + len('/media/'):] if idx != -1 else rel

    server_row = db._get_connection().execute(
        "SELECT s.protocol, s.host, s.base_path "
        "FROM server s "
        "JOIN server_media sm ON sm.server_id = s.id "
        "WHERE sm.media_id = ? "
        "  AND s.enabled = 1 "
        "  AND s.blacklisted_at IS NULL "
        "ORDER BY s.priority DESC, s.id "
        "LIMIT 1",
        (row['id'],),
    ).fetchone()

    if server_row is not None and section:
        base_path = server_row['base_path'] or ''
        media_url = (
            f"{server_row['protocol']}://{server_row['host']}"
            f"{base_path}/{rel}/"
        )
        try:
            candidate = resolve_display_name(
                media_url=media_url,
                section=section,
                prefer="global",
            )
        except Exception as exc:
            logger.warning(
                "pending-name-cleanup: resolve_display_name(%s) "
                "failed: %s", media_url, exc,
            )
            candidate = None
        if candidate:
            return candidate

    if section:
        return _make_display_name(section)
    sn = row['short_name'] or ''
    return sn.replace('_', ' ').title().strip() or None


def _parse_queue(raw: str) -> List[int]:
    """Return the integer media ids found in the queue file body.

    Robust to blank lines / trailing whitespace / partial garbage —
    any line that does not parse as an integer is silently dropped.
    """
    out: List[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(int(line))
        except ValueError:
            logger.warning(
                "pending-name-cleanup: malformed queue line %r, "
                "dropping", line,
            )
    return out


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a same-directory ``.tmp``
    + ``rename`` so a crash mid-write leaves either the previous
    content intact or the new content fully present."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        logger.warning(
            "pending-name-cleanup: cannot remove %s: %s", path, exc,
        )
