"""Stage 4 post-tx and Stage 5 post-boot (SPEC_DISTUPGRADE §4.4-§4.5).

Stage 4 runs immediately after Tx A + Tx B are both committed :

- Aggregates the ``rpmnew`` files that ``TransactionQueue`` recorded
  during Tx A and Tx B — persisted in ``.state.rpmnew_files_tx_{a,b}``
  by :mod:`stage3`.  No sentinel file, no ``find -newer`` scan : the
  queue already tracks this diff internally.
- Surfaces the scriptlet failures that ``TransactionQueue`` captured
  via ``PackageOperations.record_scriptlet_output`` — read from the
  standard history via :meth:`PackageDatabase.get_scriptlet_output`.
- Writes ``distupgrade-postboot.pending`` — the marker Stage 5
  consumes on first ``urpm`` invocation post-reboot.

Stage 5 fallback : check for the marker on every ``urpm`` startup
and run any pending post-boot script exactly once.  Removes the
marker + ``.state`` on success (SPEC_DISTUPGRADE §4.6 « fichier
`.state` supprimé quand : après Stage 5 post-boot exécuté avec
succès »).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ..database import PackageDatabase


logger = logging.getLogger(__name__)


POSTBOOT_MARKER_PATH = Path("/var/lib/urpm/distupgrade-postboot.pending")


class Stage4Error(Exception):
    """Raised for any Stage 4 failure."""


# ── Post-boot marker ─────────────────────────────────────────────


def write_postboot_marker(
    payload: List[str],
    *,
    path: Path = POSTBOOT_MARKER_PATH,
) -> None:
    """Persist the list of scripts Stage 5 must invoke.

    Written atomically (write-tmp + rename) so a Stage 4 crash
    leaves either the previous marker or no marker at all.  Empty
    payload is legitimate — the file's presence alone signals
    « Stage 5 must run » even when there's no per-package script.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pending.tmp")
    tmp.write_text("\n".join(payload) + ("\n" if payload else ""))
    tmp.replace(path)


def read_postboot_marker(
    *,
    path: Path = POSTBOOT_MARKER_PATH,
) -> List[str]:
    """Return the current pending list, or ``[]`` when absent."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    return [line for line in raw.splitlines() if line]


def clear_postboot_marker(*, path: Path = POSTBOOT_MARKER_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ── Stage 4 orchestrator ───────────────────────────────────────────


def run_stage4(
    db: "PackageDatabase",
    *,
    postboot_scripts: List[str] = None,
    marker_path: Path = None,
) -> dict:
    """Aggregate the post-Tx-A/B report and write the Stage 5 marker.

    Returns a summary dict :

    - ``rpmnew_files``      : list of ``.rpmnew`` paths TransactionQueue
      recorded across Tx A + Tx B.
    - ``failed_scriptlets`` : list of ``{pkg_name, script_type,
      status, output}`` dicts from ``history_scriptlets`` (v34+ table
      + legacy ``history_scriptlet_output``), filtered to
      ``status='failed'``.  Stage 4 does not attempt to classify
      atomic-fail vs warn — that requires script_type info the
      current TransactionQueue payload doesn't carry (deferred).
    - ``residuals``  : list of NEVRAs still installed and tagged
      ``.mga<source_version>``.  A handful is normal (SONAME-versioned
      libs waiting for a target-side rebuild) ; a large count deserves
      attention.  Best-effort — silent [] if rpm bindings absent.
    - ``orphan_media``  : list of media names disabled with
      ``disabled_by='distupgrade_orphan'`` by Stage 1.  Third-party
      repos that had no target-release equivalent.  The user should
      either wait for the maintainer to publish a target tree or
      ``urpm media remove`` them.
    - ``version_from`` / ``version_to`` : identities the report is
      built against.  Threaded through so the CLI can render them
      in the human-readable summary.
    """
    from .state import delete_state, read_state

    if marker_path is None:
        import urpm.core.distupgrade.stages_4_5 as _self
        marker_path = _self.POSTBOOT_MARKER_PATH

    try:
        from .. import _dup_diag as _dupd
        _dupd.snapshot_rpmdb("stage4-begin")
        _dupd.emit("stage4", "begin", {})
    except Exception:  # noqa: BLE001
        pass

    state = read_state(db) or {}
    version_from = state.get("version_from")
    version_to = state.get("version_to")

    rpmnew_files: List[str] = []
    rpmnew_files.extend(state.get("rpmnew_files_tx_a") or [])
    rpmnew_files.extend(state.get("rpmnew_files_tx_b") or [])

    failed_scriptlets: List[dict] = []
    for side in ("a", "b"):
        tx_id = state.get(f"tx_{side}_transaction_id")
        if tx_id is None:
            continue
        for row in db.get_scriptlet_output(int(tx_id)):
            if row.get("status") == "failed":
                failed_scriptlets.append(row)

    residuals = _residual_source_packages(version_from) if version_from else []
    orphan_media = _orphan_media_from_stage1(db)
    transposed_media = _transposed_media_from_stage1(db)

    # Stage 5 is fire-and-forget via the postboot marker file :
    # ``run_stage5_if_pending`` picks it up on next ``urpm`` startup
    # and needs no persisted state.  Clear ``.state`` here so the
    # distupgrade mesh reopens between Stage 4 and reboot — the
    # marker file alone signals « Stage 5 pending ».
    write_postboot_marker(postboot_scripts or [], path=marker_path)
    delete_state(db)

    try:
        from .. import _dup_diag as _dupd
        _dupd.snapshot_rpmdb("stage4-end")
        _dupd.emit("stage4", "end", {
            "rpmnew_count": len(rpmnew_files),
            "failed_scriptlets_count": len(failed_scriptlets),
            "failed_scriptlets_sample": [
                {"pkg": r.get("pkg_name"), "status": r.get("status")}
                for r in failed_scriptlets[:20]
            ],
            "residuals_count": len(residuals),
            "residuals_sample": residuals[:20],
            "orphan_media_count": len(orphan_media),
        })
    except Exception:  # noqa: BLE001
        pass

    return {
        "rpmnew_files": rpmnew_files,
        "failed_scriptlets": failed_scriptlets,
        "residuals": residuals,
        "orphan_media": orphan_media,
        "transposed_media": transposed_media,
        "version_from": version_from,
        "version_to": version_to,
    }


def _residual_source_packages(source_version: str) -> List[str]:
    """Return installed NEVRAs whose release tag matches ``.mgaN``.

    Best-effort : returns ``[]`` when rpm bindings are unavailable or
    ``source_version`` is empty.  Uses libsolv-adjacent ``.dbMatch()``
    to walk the installed rpmdb once — cheap even on a full system
    (~2000 packages, well under 100 ms).
    """
    if not source_version:
        return []
    marker = f".mga{source_version}"
    residuals: List[str] = []
    # rpmdb access via urpm.core.rpmdb — never open a librpm handle in
    # the parent (module contract, see :mod:`urpm.core.rpmdb`).  We
    # use the raw context manager because we filter on a substring of
    # RELEASE which no typed helper exposes.
    try:
        from .. import rpmdb
        import rpm
    except ImportError:
        return []
    with rpmdb.open_ts('/') as ts:
        for hdr in ts.dbMatch():
            release = hdr[rpm.RPMTAG_RELEASE] or ""
            if marker in release:
                name = hdr[rpm.RPMTAG_NAME] or ""
                evr = hdr[rpm.RPMTAG_EVR] or ""
                arch = hdr[rpm.RPMTAG_ARCH] or "noarch"
                residuals.append(f"{name}-{evr}.{arch}")
    residuals.sort()
    return residuals


def _orphan_media_from_stage1(db: "PackageDatabase") -> List[dict]:
    """Return media rows Stage 1 disabled as ``distupgrade_orphan``.

    Rows carry the ``[dg:N]`` suffix and their pre-swap state in the
    undo journal — but for reporting purposes we only need the
    display name and mageia_version.
    """
    try:
        conn = db._get_connection()
        rows = conn.execute("""
            SELECT name, short_name, mageia_version
            FROM media
            WHERE disabled_by = 'distupgrade_orphan'
            ORDER BY name
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _transposed_media_from_stage1(db: "PackageDatabase") -> List[dict]:
    """Return media rows Stage 1 disabled as ``distupgrade`` (transposed).

    These are the mga N originals that Stage 1 disabled *because* it
    inserted (or already had) a mga N+1 counterpart.  Safe to remove
    once the distupgrade succeeded — keeping them just clutters
    ``urpm media list --all``.
    """
    try:
        conn = db._get_connection()
        rows = conn.execute("""
            SELECT id, name, short_name, mageia_version
            FROM media
            WHERE disabled_by = 'distupgrade'
            ORDER BY name
        """).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


# ── Stage 5 fallback ──────────────────────────────────────────────


def run_stage5_if_pending(
    db: "PackageDatabase",
    *,
    marker_path: Path = POSTBOOT_MARKER_PATH,
) -> bool:
    """Execute pending post-boot scripts if the marker is present.

    Called on every ``urpm`` startup as the universal fallback
    (SPEC §4.5).  Returns ``True`` when Stage 5 ran (or attempted to
    run) ; ``False`` when there was nothing to do.
    """
    scripts = read_postboot_marker(path=marker_path)
    if not scripts and not marker_path.exists():
        return False

    logger.info("Stage 5 : running %d post-boot script(s)",
                len(scripts))
    for script in scripts:
        try:
            subprocess.run([script], check=False,
                           env={"LC_ALL": "C", "LANG": "C",
                                "LANGUAGE": "C"})
        except FileNotFoundError:
            logger.warning("post-boot script %s missing, skipping",
                           script)

    # On completion, clear the marker and the state so the mesh
    # reopens.  Individual script failures don't block the marker
    # clear — Stage 5 is fire-and-forget by design.
    clear_postboot_marker(path=marker_path)
    from .state import delete_state
    delete_state(db)
    return True
