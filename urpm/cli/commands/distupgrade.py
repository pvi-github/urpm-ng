"""urpm distupgrade — full release-to-release migration (N → N+1).

Wired up to Stage 0 for the first testable slice (SPEC_DISTUPGRADE
§4.0).  Stages 1 through 5 will layer on subsequent tickets.

Currently supported CLI shapes :

- ``urpm distupgrade --to N``         — run Stage 0 targeting release N
- ``urpm distupgrade --to N --dry-run`` — Stage 0 without Phase A upgrade
- ``urpm distupgrade --resume``       — delegate to `cmd_recover` (Phase D)
- ``urpm distupgrade --abort``        — clear state + release lock

Once Stages 1+ land, `--to` will drive the full pipeline instead of
returning after Stage 0.
"""

import time
from typing import TYPE_CHECKING

from ...i18n import _
from .. import colors

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_distupgrade(args, db: 'PackageDatabase') -> int:
    """Handle `urpm distupgrade [--resume | --continue | --abort | --to N]`."""
    # Diag hook : ``--debug distupgrade`` (or ``--debug all``) turns on
    # the deep post-mortem tracer in ``urpm/core/_dup_diag.py`` — writes
    # per-stage JSONL + rpmdb snapshots under ``/var/log/dup-diag/``.
    # Env var, so ``os.execvp`` from Stage 3 propagates the flag to the
    # mga N+1 process without needing a second CLI arg.
    _debug = getattr(args, 'debug', None) or ''
    _debug_parts = {d.strip() for d in _debug.split(',')} if _debug else set()
    if 'distupgrade' in _debug_parts or 'all' in _debug_parts:
        from ...core import _dup_diag as _dupd
        _dupd.enable()

    if getattr(args, 'abort', False):
        return _cmd_abort(db)

    if getattr(args, 'resume', False):
        # SPEC_DISTUPGRADE §3.D : distupgrade-aware resume — reads
        # ``.state.stage`` and re-enters the pipeline at the failed
        # boundary instead of re-running Stage 0 → 1 → 2 from scratch.
        return _cmd_resume(db)

    if getattr(args, 'continue_', False):
        # SPEC_DISTUPGRADE §4.3 : entry point of the post-execvp
        # target-stack.  Reads the persisted Tx B plan out of
        # `.state`, commits Tx B under the target rpm/python/libsolv,
        # then hands off to Stage 4 + marks Stage 5 due.
        return _cmd_continue_after_execvp(args, db)

    # ``--to`` is optional : when absent, Stage 0 auto-detects via
    # the N → N+1 heuristic (see version.detect_target_release).
    to_arg = getattr(args, 'to', None)

    export_plan = getattr(args, 'export_plan', None)
    if export_plan:
        return _cmd_export_plan(args, db, to_arg=to_arg,
                                export_file=export_plan)

    return _cmd_run_to(args, db, to_arg=to_arg,
                      dry_run=getattr(args, 'dry_run', False))


def _cmd_resume(db: 'PackageDatabase') -> int:
    """Re-enter the distupgrade pipeline at the boundary saved in ``.state``.

    Dispatch by ``state.stage`` :

    - ``pre_check_done`` / ``stage1_running`` / ``media_swapped``
      / ``stage2_running`` / ``downloaded`` → re-run Stage 2 solve +
      Stage 3 Tx A (upstream stages are idempotent, so the cost is
      only a sync-metadata check + a solve).
    - ``tx_a_committing`` / ``tx_a_done`` → re-enter Tx A commit
      directly with the persisted plan + NEVRA→path map (no re-solve,
      no re-download).
    - ``tx_b_running`` → re-enter Tx B commit (post-execvp path).
    - ``stage4_running`` → re-run Stage 4 (Stage 3 already committed).

    ``.state`` absent → nothing to resume, print an info line and
    exit 0 so ``urpm distupgrade --resume`` is safe to invoke blindly
    at any time.
    """
    from ...core.distupgrade import read_state

    state = read_state(db)
    if state is None:
        print(colors.info(_(
            "No distupgrade state to resume from.")))
        return 0

    stage = state.get("stage", "?")
    print(colors.info(_(
        "Resuming distupgrade from stage '{stage}'...").format(stage=stage)))

    if stage in ("tx_a_committing", "tx_a_done"):
        return _resume_from_tx_a(db, state)
    if stage == "tx_b_running":
        # Same code path as ``--continue`` — Tx B + Stage 4 + marker.
        return _cmd_continue_after_execvp(None, db)
    if stage == "stage4_running":
        return _resume_from_stage4(db)

    # Pre-Tx-A boundary : fall through to a fresh ``--to`` run.
    version_to = state.get("version_to")
    if not version_to:
        print(colors.error(_(
            "cannot resume : ``.state`` lacks ``version_to``.  "
            "Run ``urpm distupgrade --abort`` and start over.")))
        return 1
    print(colors.dim(_(
        "  re-entering Stage 0 → target={t}").format(t=version_to)))

    # Synthesise an args-like namespace to re-enter ``_cmd_run_to``.
    class _Args:
        dry_run = False
    return _cmd_run_to(_Args(), db, to_arg=version_to, dry_run=False)


def _resume_from_tx_a(db, state: dict) -> int:
    """Re-enter :func:`run_stage3_tx_a` with the persisted plan."""
    from ...core.distupgrade import (
        SmokeTestFailure,
        Stage3Error,
        execvp_to_continue,
        run_stage3_tx_a,
    )

    tx_a_plan = state.get("tx_a_plan_ordered") or []
    nevra_to_path = state.get("nevra_to_path") or {}
    version_from = state.get("version_from", "unknown")
    version_to = state.get("version_to", "unknown")

    if not tx_a_plan:
        print(colors.error(_(
            "cannot resume Tx A : plan absent from .state.")))
        return 1

    try:
        run_stage3_tx_a(
            db,
            tx_a_plan=tx_a_plan,
            rpm_paths_by_nevra=nevra_to_path,
            version_from=version_from,
            version_to=version_to,
        )
    except (Stage3Error, SmokeTestFailure) as exc:
        print(colors.error(_("Tx A retry failed : {err}").format(err=exc)))
        return 1

    print(colors.success(_(
        "Tx A committed + smoke test passed on retry.  Handing off "
        "(execvp)…")))
    # Resume path : we lost the caller's original --auto flag (state
    # doesn't persist it).  Default to interactive : safer if the
    # user is watching a resume ; they can re-issue --yes if they
    # want non-interactive post-execvp.
    try:
        execvp_to_continue(auto=False)
    except OSError as exc:
        print(colors.error(_(
            "execvp handoff failed : {err}").format(err=exc)))
        return 1
    return 0


def _resume_from_stage4(db) -> int:
    """Re-run Stage 4 aggregation (marker + rpmnew + scriptlet report)."""
    from ...core.distupgrade import Stage4Error, run_stage4

    try:
        summary = run_stage4(db)
    except Stage4Error as exc:
        print(colors.error(_("Stage 4 retry failed : {err}").format(err=exc)))
        return 1
    print(colors.success(_(
        "Stage 4 complete : {n_rpmnew} .rpmnew file(s), "
        "{n_failed} failed scriptlet(s).").format(
            n_rpmnew=len(summary["rpmnew_files"]),
            n_failed=len(summary["failed_scriptlets"]))))
    print(colors.info(_(
        "Reboot recommended so Stage 5 post-boot fixups run.")))
    return 0


def _rollback_stage1(db: 'PackageDatabase') -> tuple:
    """Undo Stage 1 side-effects verbatim.

    Two reversal passes so the DB after rollback is bit-for-bit the
    state the user had before ``urpm distupgrade`` :

    1. **Delete every row Stage 1 created** — ``created_media_ids``
       and ``created_server_ids``.  ``server_media`` links cascade
       via the schema's ``ON DELETE CASCADE``.
    2. **Restore every row Stage 1 modified** — ``modified_media``
       snapshots carry the pre-mutation ``(enabled, disabled_by,
       name)`` tuple, restored verbatim.  ``modified_servers``
       snapshots carry the pre-refresh ``url_version`` value.

    Fallback for state files without an undo journal (pre-v0.9 stub
    attempts) : find rows tagged ``distupgrade`` /
    ``distupgrade_orphan`` and re-enable them.

    Returns ``(n_deleted_media, n_deleted_servers, n_restored)``.
    Caller is responsible for ``delete_state(db)`` afterwards.
    """
    from ...core.distupgrade import read_state
    from ...core.distupgrade.stage1 import _stripped_name

    state = read_state(db) or {}
    undo = state.get("stage1_undo")

    conn = db._get_connection()
    n_deleted_media = 0
    n_deleted_servers = 0
    n_restored = 0

    if undo is not None:
        with db._lock:
            for media_id in undo.get("created_media_ids") or []:
                cur = conn.execute(
                    "DELETE FROM media WHERE id = ?", (int(media_id),))
                n_deleted_media += cur.rowcount
            for server_id in undo.get("created_server_ids") or []:
                cur = conn.execute(
                    "DELETE FROM server WHERE id = ?", (int(server_id),))
                n_deleted_servers += cur.rowcount
            for snap in undo.get("modified_media") or []:
                cur = conn.execute("""
                    UPDATE media
                    SET enabled = ?, disabled_by = ?, name = ?
                    WHERE id = ?
                """, (
                    int(snap.get("enabled") or 0),
                    snap.get("disabled_by"),
                    snap.get("name"),
                    int(snap["id"]),
                ))
                n_restored += cur.rowcount
            for snap in undo.get("modified_servers") or []:
                cur = conn.execute("""
                    UPDATE server SET url_version = ? WHERE id = ?
                """, (
                    snap.get("url_version"),
                    int(snap["id"]),
                ))
                n_restored += cur.rowcount
            conn.commit()
    else:
        rows = conn.execute("""
            SELECT id, name FROM media
            WHERE disabled_by IN ('distupgrade', 'distupgrade_orphan')
        """).fetchall()
        with db._lock:
            for row in rows:
                conn.execute("""
                    UPDATE media
                    SET enabled = 1,
                        disabled_by = NULL,
                        name = ?
                    WHERE id = ?
                """, (_stripped_name(row["name"]), row["id"]))
            conn.commit()
        n_restored = len(rows)

    return n_deleted_media, n_deleted_servers, n_restored


def _cmd_abort(db: 'PackageDatabase') -> int:
    """Undo Stage 1 side-effects from ``.state.stage1_undo`` and clear
    state.  Thin wrapper around :func:`_rollback_stage1`."""
    from ...core.distupgrade import delete_state

    n_del_m, n_del_s, n_up = _rollback_stage1(db)
    delete_state(db)
    print(colors.success(_(
        "distupgrade state cleared : {n_del_m} target media + "
        "{n_del_s} server(s) removed, {n_up} row(s) restored.").format(
            n_del_m=n_del_m,
            n_del_s=n_del_s,
            n_up=n_up)))
    return 0


def _render_empty_plan_diagnosis(result) -> None:
    """Explain to the user why Stage 2 returned zero actions.

    Iterates ``result.skipped`` (populated by
    :meth:`Resolver.resolve_upgrade` and its distupgrade cousin) and
    prints each held package with its reason — the same info the
    ``urpm upgrade`` skipped-jobs report shows, minus the pretty
    tabulator (empty-plan is already a hard fail, not a live TTY
    moment).

    Called from the ``Stage2EmptyPlanError`` handler in
    :func:`cmd_distupgrade` before rollback.
    """
    from .. import colors

    print("\n" + colors.error(_(
        "distupgrade solve returned ZERO actions — every candidate "
        "was held by libsolv.  The system was NOT upgraded.")))
    skipped = getattr(result, "skipped", None) or []
    if not skipped:
        print(colors.warning(_(
            "  No skipped-jobs record either — this is a resolver "
            "anomaly.  Please report with the output of :")))
        print(colors.dim(
            "    urpm distupgrade --to <N+1> --dry-run 2>&1 | tee dg.log"))
        return
    print(colors.warning(_(
        "  {n} candidate(s) held :").format(n=len(skipped))))
    for sj in skipped[:20]:
        header = f"    {colors.error(sj.name)}"
        if getattr(sj, "evr", ""):
            header += f" {colors.dim(sj.evr)}"
        print(header)
        for line in (sj.reason or "").splitlines():
            if line.strip():
                print(f"      {colors.dim(line)}")
    if len(skipped) > 20:
        print(colors.dim(_(
            "    (+ {n} more)").format(n=len(skipped) - 20)))


def _render_plan_and_confirm(result, *, source: str, target: str,
                             auto: bool) -> bool:
    """Show the Stage 2 plan (``urpm u``-style) and prompt Y/N.

    Categorises actions by ``action.value`` (upgrade / install /
    remove), sorts each bucket by name, prints via
    :func:`display.print_package_list` (multi-column, truncated to
    ~10 lines unless ``--show-all``), sums download size, then
    prompts the user unless ``auto=True``.  Returns ``True`` when
    the user proceeds, ``False`` on decline or Ctrl+C / EOF.

    All user-facing strings pass through ``_()`` — translations
    are updated in the ``.po`` files.
    """
    from ...i18n import _, confirm_yes, ngettext
    from .. import colors, display
    from ..display import format_size

    upgrades = [a for a in result.actions if a.action.value == "upgrade"]
    installs = [a for a in result.actions if a.action.value == "install"]
    removes = [a for a in result.actions if a.action.value == "remove"]

    # ``filesize`` = compressed RPM download size (SOLVABLE_DOWNLOADSIZE) ;
    # ``size`` = installed footprint (SOLVABLE_INSTALLSIZE).  Prefer
    # ``filesize`` for the download-total line ; fall back to ``size``
    # only when the resolver couldn't populate it.
    total_download = sum(
        (getattr(a, "filesize", 0) or getattr(a, "size", 0) or 0)
        for a in result.actions
        if a.action.value in ("install", "upgrade", "reinstall"))
    total_install = sum(
        getattr(a, "size", 0) or 0
        for a in result.actions
        if a.action.value in ("install", "upgrade", "reinstall"))

    print("\n" + colors.bold(_(
        "Distupgrade summary : Mageia {src} → {tgt}").format(
            src=source, tgt=target)))

    if upgrades:
        print("\n  " + colors.info(_(
            "Upgrade ({count}):").format(count=len(upgrades))))
        nevras = [a.nevra for a in sorted(upgrades,
                                          key=lambda x: x.name.lower())]
        display.print_package_list(
            nevras, indent=4, color_func=colors.info)

    if installs:
        print("\n  " + colors.success(_(
            "Install ({count}) - new dependencies:").format(
                count=len(installs))))
        nevras = [a.nevra for a in sorted(installs,
                                          key=lambda x: x.name.lower())]
        display.print_package_list(
            nevras, indent=4, color_func=colors.success)

    if removes:
        print("\n  " + colors.error(_(
            "Remove ({count}) - obsoleted / dropped in mga {tgt}:").format(
                count=len(removes), tgt=target)))
        nevras = [a.nevra for a in sorted(removes,
                                          key=lambda x: x.name.lower())]
        display.print_package_list(
            nevras, indent=4, color_func=colors.error)

    print("\n" + colors.bold(_(
        "Total : {n} package(s), download {dl}, "
        "installed footprint {inst}.").format(
            n=len(result.actions),
            dl=format_size(total_download),
            inst=format_size(total_install))))

    if auto:
        print(colors.dim(_(
            "  --yes / --auto : skipping confirmation.")))
        return True

    try:
        response = input(_("\nProceed with distupgrade? [y/N] "))
    except (KeyboardInterrupt, EOFError):
        print(_("\nAborted."))
        return False
    return confirm_yes(response)


def _cmd_export_plan(args, db, *, to_arg: str, export_file: str) -> int:
    """Solve the target-release plan and dump NEVRAs, restore DB after.

    Sequence :

    1. ``sqlite3.Connection.backup()`` the real ``packages.db`` to a
       sibling ``.export-plan-backup`` file.
    2. Run Stage 0 → Stage 1 → ``sync_all_media`` → Stage 2 solve on
       the real DB.  Solve only — no download, no Stage 3.
    3. Write one NEVRA per line to ``export_file``.
    4. ``finally`` : close the DB, ``shutil.copy2`` the backup back
       over the real ``packages.db``, ``unlink`` the backup.

    Net effect on the real DB : nothing (bit-for-bit identical row
    set after restore).  Side effect on ``/var/lib/urpm/medias/`` :
    the target-release synthesis files that ``sync_all_media`` fetched
    remain on disk — considered a *feature*, not garbage, since the
    real ``urpm distupgrade`` invocation will reuse them.

    Risks :

    - SIGKILL / power cut between step 2 and step 4 leaves the real
      DB in mid-Stage-1 state.  The distupgrade ``.state`` on the DB
      documents the boundary — ``urpm distupgrade --abort`` restores
      cleanly.  Rare, and consistent with the general ``--abort``
      recovery contract.
    - Concurrent ``urpm distupgrade`` : mesh check refuses writes
      while ``.state`` is present, and Stage 0 acquires the
      distupgrade lock, so a real distupgrade waits.
    """
    import shutil
    import sqlite3
    from pathlib import Path
    from ...core.distupgrade import (
        Stage0Error,
        Stage1Error,
        Stage2Error,
        run_stage0,
        run_stage1,
        solve_distupgrade,
    )

    export_path = Path(export_file)
    backup_path = Path(str(db.db_path) + ".export-plan-backup")

    # 1. Backup the real DB before we mutate anything.
    print(colors.info(_(
        "Backing up packages.db to {path}...").format(path=backup_path)))
    dst = sqlite3.connect(str(backup_path))
    try:
        db._get_connection().backup(dst)
    finally:
        dst.close()

    try:
        # 2. Same flow as `_cmd_run_to` up to and including solve,
        # then stop — no download, no Stage 3.
        try:
            stage0 = run_stage0(
                db, user_supplied_target=to_arg,
                skip_phase_a_upgrade=True,
            )
        except Stage0Error as exc:
            print(colors.error(str(exc)))
            return 1
        print(colors.success(_(
            "Stage 0 complete : current={cur}, target={tgt}.").format(
                cur=stage0.current or "unknown",
                tgt=stage0.target.display())))

        try:
            stage1_summary = run_stage1(
                db,
                source_identity=stage0.current or "unknown",
                target=stage0.target,
            )
        except Stage1Error as exc:
            print(colors.error(str(exc)))
            return 1
        print(colors.success(_(
            "Stage 1 complete : {n_new} target catalogue(s) "
            "inserted (ephemeral — DB will be restored).").format(
                n_new=len(stage1_summary["created_urls"]))))

        print(colors.info(_(
            "Refreshing target-release metadata...")))
        from ...core.sync import sync_all_media
        try:
            results = sync_all_media(db, force=True)
        except Exception as exc:  # noqa: BLE001
            print(colors.error(_(
                "Cannot sync target-release metadata: {err}").format(err=exc)))
            return 1
        n_ok = sum(1 for _n, r in results if getattr(r, "success", False))
        print(colors.dim(_(
            "  synced {n_ok}/{n_total} media").format(
                n_ok=n_ok, n_total=len(results))))

        try:
            result = solve_distupgrade(db, target=stage0.target)
        except Stage2Error as exc:
            print(colors.error(str(exc)))
            return 1

        # 3. Dump NEVRAs — one per line, install/upgrade/reinstall
        # only (remove actions carry no downloadable payload).
        downloadable = [
            a.nevra for a in result.actions
            if a.action.value in ("install", "upgrade", "reinstall")
        ]
        export_path.write_text("\n".join(downloadable) + "\n",
                               encoding="utf-8")
        print(colors.success(_(
            "Plan exported : {n} NEVRA(s) written to {path}").format(
                n=len(downloadable), path=export_path)))
        print(colors.info(_(
            "Preload a neighbour peer with :\n"
            "  urpm download --from-file {path}").format(path=export_path)))
        return 0

    finally:
        # 4. Restore : close every connection so SQLite releases its
        # file lock, delete the WAL/SHM sidecars so they don't get
        # combined with the restored main file (SQLite would read
        # inconsistent data → "database disk image is malformed"),
        # then bit-for-bit copy the backup back and unlink it.
        print(colors.info(_(
            "Restoring packages.db from backup...")))
        db_path_str = str(db.db_path)
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        # WAL sidecars survive the close() unless every connection was
        # cleanly checkpointed — worker threads in ``sync_all_media``
        # open their own thread-local connections that ``db.close()``
        # doesn't reach.  Deleting the sidecars forces SQLite to start
        # a fresh WAL on the next open, matching the restored main.
        for sfx in ("-wal", "-shm"):
            Path(db_path_str + sfx).unlink(missing_ok=True)
        shutil.copy2(str(backup_path), db_path_str)
        backup_path.unlink(missing_ok=True)
        print(colors.success(_(
            "packages.db restored — nothing persisted on your system.")))


def _cmd_run_to(args, db, *, to_arg: str, dry_run: bool) -> int:
    """Drive Stage 0 up to `pre_check_done` and stop.

    Later tickets replace the stop with Stage 1+ once available.
    """
    from ...core.distupgrade import (
        Stage0Error,
        Stage1Error,
        Stage2Aborted,
        Stage2EmptyPlanError,
        Stage2Error,
        release_distupgrade_lock,
        run_stage0,
        run_stage1,
        run_stage2,
    )

    print(colors.info(_(
        "Preliminary checks (release detection, network, DB integrity)...")))

    # Resolve the target release EARLY (before Phase A upgrade
    # runs any download), so the maturity + multi-jump prompts
    # gate the run before any bandwidth is spent.
    from ...core.distupgrade.version import (
        detect_target_release, multi_version_jump,
        probe_target_maturity, read_current_release,
        VersionDetectionError,
    )
    from ...i18n import confirm_yes
    import platform as _platform
    try:
        _current = read_current_release()
        _target_early = detect_target_release(
            current=_current, user_supplied=to_arg)
    except VersionDetectionError as exc:
        print(colors.error(str(exc)))
        return 1
    print(colors.dim(_(
        "  Target : mga{tgt} (source : mga{cur})").format(
            tgt=_target_early.display(), cur=_current or "?")))

    # § pre-release maturity : probe media.cfg on the target
    # catalogue.  Ideally < 500 ms — one HTTP GET on the first
    # reachable official mirror.
    auto = getattr(args, "auto", False)
    maturity = probe_target_maturity(
        db, _target_early, _platform.machine(),
        source_identity=_current)
    if not auto:
        if maturity.probe_error:
            print(colors.warning(_(
                "Could not verify target release maturity : {err}. "
                "Cannot tell if mga{tgt} is stable or still in "
                "Alpha / Beta / RC.").format(
                    err=maturity.probe_error,
                    tgt=_target_early.display())))
            try:
                resp = input(_(
                    "Proceed without maturity verification ? [y/N] "))
            except (KeyboardInterrupt, EOFError):
                print(_("\nAborted."))
                return 1
            if not confirm_yes(resp):
                print(_("Aborted."))
                return 0
        elif not maturity.is_stable:
            print(colors.warning(_(
                "WARNING : target mga{tgt} advertises `branch={br}` on "
                "its mirror — not `Official`.  This release is not yet "
                "stable (Cauldron / Alpha / Beta / RC) ; distupgrading "
                "now may hit breakage a final release would fix.").format(
                    tgt=_target_early.display(),
                    br=maturity.branch or "?")))
            try:
                resp = input(_(
                    "Proceed with a pre-release target ? [y/N] "))
            except (KeyboardInterrupt, EOFError):
                print(_("\nAborted."))
                return 1
            if not confirm_yes(resp):
                print(_("Aborted."))
                return 0

    # § multi-version jump : same prompt logic, upfront so the user
    # confirms BEFORE Phase A eats time.
    skipped = multi_version_jump(current=_current or "",
                                 target=_target_early)
    if skipped > 0 and not auto:
        print(colors.warning(_(
            "WARNING : you are on Mageia {cur}, the target is "
            "Mageia {tgt} — you will skip {k} intermediate "
            "release(s).  Cross-release upgrades stack cumulative "
            "packaging changes and scriptlets that assume every "
            "intermediate release was crossed ; you may hit "
            "conflicts or breakage a stepwise upgrade would avoid."
        ).format(cur=_current, tgt=_target_early.display(),
                 k=skipped)))
        try:
            resp = input(_(
                "Proceed with this multi-version jump ? [y/N] "))
        except (KeyboardInterrupt, EOFError):
            print(_("\nAborted."))
            return 1
        if not confirm_yes(resp):
            print(_("Aborted."))
            return 0

    if not dry_run:
        print(colors.dim(_(
            "  Bringing the source release up to date first "
            "(applies pending mga N updates ; can take a few minutes "
            "on a stale system)...")))

    # Phase A live progress — same widgets ``urpm i`` / ``urpm u`` use.
    # Instantiated lazily : if there's nothing to upgrade in mga N,
    # ``on_plan_computed`` sees count=0 and neither callback fires.
    from .. import display as _display
    from ...core.settings import get_settings
    _pa = {"dl_display": None, "dl_start": None,
           "install_progress": None, "announced": False}

    def _pa_plan(count, dl_bytes, inst_bytes):
        if count == 0:
            print(colors.dim(_(
                "  Source release already up to date — nothing to "
                "install before the distupgrade.")))
            return
        _pa["announced"] = True
        print(colors.info(_(
            "  Phase A : {n} pending mga N update(s) to install first "
            "(download {dl}, installed footprint {inst}).").format(
                n=count,
                dl=_display.format_size(dl_bytes),
                inst=_display.format_size(inst_bytes))))
        # Prepare the install progress widget for later — the download
        # bar is created on first download callback.
        from ..helpers.progress import make_progress_callback
        _pa["install_progress"] = make_progress_callback(
            header_template=_("Phase A upgrade ({count} packages)"),
            total=count,
            full_sync=True,
        )

    def _pa_dl(name, pkg_num, pkg_total, bytes_done, bytes_total,
               item_bytes=None, item_total=None, slots_status=None,
               coordinator_speed=0.0):
        if _pa["dl_display"] is None:
            _pa["dl_display"] = _display.DownloadProgressDisplay(
                num_workers=get_settings().download.parallel)
            _pa["dl_start"] = time.monotonic()
        _pa["dl_display"].update(
            pkg_num, pkg_total, bytes_done, bytes_total,
            slots_status or [], coordinator_speed)

    def _pa_install(*args, **kwargs):
        if _pa["install_progress"] is not None:
            _pa["install_progress"](*args, **kwargs)

    try:
        stage0 = run_stage0(
            db,
            user_supplied_target=to_arg,
            skip_phase_a_upgrade=dry_run,
            phase_a_on_plan_computed=_pa_plan,
            phase_a_download_progress=_pa_dl,
            phase_a_install_progress=_pa_install,
        )
    except Stage0Error as exc:
        if _pa["dl_display"] is not None:
            _pa["dl_display"].finish()
        if _pa["install_progress"] is not None:
            _pa["install_progress"].cleanup()
        print(colors.error(str(exc)))
        return 1
    finally:
        if _pa["dl_display"] is not None:
            _pa["dl_display"].finish()
        if _pa["install_progress"] is not None:
            _pa["install_progress"].cleanup()
            # Erase the widget's 3-line region (header + bar + sub) so
        # the next print doesn't bleed into leftover ANSI-drawn bars.
        import sys
        if sys.stdout.isatty():
            print("\r\033[2A\033[J", end='', flush=True)
        else:
            print()

    print(colors.success(_(
        "Preliminary checks OK — current version : {cur}, target : {tgt}.").format(
            cur=stage0.current or "unknown",
            tgt=stage0.target.display())))

    # (maturity + multi-jump prompts already ran upfront before Phase A)

    if dry_run:
        print(colors.info(_(
            "--dry-run : stopping after Stage 0.  Release the lock "
            "and clear the state with `urpm distupgrade --abort`.")))
        return 0

    print(colors.info(_(
        "Switching repositories (mga{src} → mga{tgt})...").format(
            src=stage0.current or "?",
            tgt=stage0.target.display())))
    try:
        stage1_summary = run_stage1(
            db,
            source_identity=stage0.current or "unknown",
            target=stage0.target,
        )
    except Stage1Error as exc:
        print(colors.error(str(exc)))
        return 1

    n_src = len(stage1_summary["disabled_source"])
    n_orph = len(stage1_summary["disabled_orphan"])
    n_new = len(stage1_summary["created_urls"])
    n_failed = len(stage1_summary["failed_servers"])
    print(colors.success(_(
        "Repository switchover done : {n_src} source repositories "
        "disabled, {n_orph} third-party repositories with no target "
        "counterpart flagged, {n_new} target catalogue(s) added.").format(
            n_src=n_src, n_orph=n_orph, n_new=n_new)))
    if n_failed:
        print(colors.warning(_(
            "  {n} server(s) failed target-catalogue upsert : {names}").format(
                n=n_failed,
                names=", ".join(stage1_summary["failed_servers"]))))

    # List the third-party repositories that couldn't be transposed
    # to a mga N+1 counterpart, upfront — the user needs to know
    # BEFORE the Stage 2 prompt what they're losing, so they can
    # abort and re-run once the maintainer publishes the target
    # tree, or accept the loss knowingly.
    if stage1_summary["disabled_orphan"]:
        print(colors.warning(_(
            "  Third-party repositories with no mga{tgt} counterpart "
            "(will be unavailable after the upgrade) :").format(
                tgt=stage0.target.display())))
        for orph in stage1_summary["disabled_orphan"][:15]:
            print(f"    {colors.dim(orph['name'])}")
        if len(stage1_summary["disabled_orphan"]) > 15:
            print(colors.dim(_(
                "    (+ {n} more)").format(
                    n=len(stage1_summary["disabled_orphan"]) - 15)))

    # After Stage 1 the target-release media rows exist in the DB but
    # their synthesis metadata isn't on disk yet.  The libsolv pool
    # Stage 2 builds would then be empty on the target side and
    # SOLVER_DISTUPGRADE would find no work.  Sync the fresh media
    # first — same primitive `urpm media update` uses.
    print(colors.info(_("Refreshing target-release metadata...")))
    from ...core.sync import sync_all_media

    def _refresh_progress(media_name, stage, current, total):
        # Emit one line per media entering the "downloading" stage so
        # the user sees exactly which mga N+1 catalogues get synced.
        if stage == "downloading" and current == 0:
            print(colors.dim(f"  syncing {media_name}"))

    try:
        results = sync_all_media(
            db, force=True, progress_callback=_refresh_progress)
    except Exception as exc:  # noqa: BLE001
        print(colors.error(_(
            "Cannot sync target-release metadata: {err}").format(err=exc)))
        return 1
    n_ok = sum(1 for _n, r in results if getattr(r, "success", False))
    n_ko = len(results) - n_ok
    print(colors.dim(_(
        "  synced {n_ok}/{n_total} media ({n_ko} failed)").format(
            n_ok=n_ok, n_total=len(results), n_ko=n_ko)))

    print(colors.info(_(
        "Resolving the target-release package plan...")))

    def _stage2_confirm(result) -> bool:
        return _render_plan_and_confirm(
            result,
            source=stage0.current or "?",
            target=stage0.target.display(),
            auto=getattr(args, "auto", False))

    # Live download progress — same DownloadProgressDisplay ``urpm i``
    # / ``urpm u`` use.  Instantiated lazily inside the callback so
    # the preamble line lands only if we actually hit the download
    # phase (skipped when the plan is fully cached).
    from .. import display as _display
    from ...core.settings import get_settings
    _dp = {"display": None, "shown": False, "start": None}

    def _dl_progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                     item_bytes=None, item_total=None, slots_status=None,
                     coordinator_speed=0.0):
        if _dp["display"] is None:
            print(colors.info(_(
                "\nDownloading {count} package(s)...").format(
                    count=pkg_total)))
            _dp["display"] = _display.DownloadProgressDisplay(
                num_workers=get_settings().download.parallel)
            _dp["start"] = time.monotonic()
            _dp["shown"] = True
        _dp["display"].update(
            pkg_num, pkg_total, bytes_done, bytes_total,
            slots_status or [], coordinator_speed)

    try:
        stage2_summary = run_stage2(
            db, target=stage0.target,
            confirm_callback=_stage2_confirm,
            download_progress_callback=_dl_progress)
    except Stage2Aborted:
        if _dp["display"] is not None:
            _dp["display"].finish()
        print(colors.info(_(
            "distupgrade cancelled at the Stage 2 prompt — nothing "
            "downloaded, state not persisted.  Re-run to re-solve.")))
        return 0
    except Stage2EmptyPlanError as exc:
        # SAFETY : an empty plan means libsolv held every candidate.
        # We MUST NOT proceed to Stage 3 / Stage 4 — the transposed
        # mga N media would get flagged for deferred deletion while
        # the machine is still entirely on mga N, bricking it at
        # reboot.  Auto-rollback Stage 1 so the DB matches the
        # pre-distupgrade state.  User is left with a clear diagnosis
        # of the skipped packages, no cleanup needed on their side.
        if _dp["display"] is not None:
            _dp["display"].finish()
        _render_empty_plan_diagnosis(exc.result)
        print(colors.info(_(
            "Rolling back Stage 1 media changes so the DB matches the "
            "pre-distupgrade state...")))
        from ...core.distupgrade import (
            delete_state, release_distupgrade_lock,
        )
        try:
            n_del_m, n_del_s, n_up = _rollback_stage1(db)
            delete_state(db)
            release_distupgrade_lock()
            print(colors.success(_(
                "Rolled back : {n_del_m} target media + "
                "{n_del_s} server(s) removed, {n_up} row(s) restored."
            ).format(n_del_m=n_del_m, n_del_s=n_del_s, n_up=n_up)))
        except Exception as rb_exc:  # noqa: BLE001
            print(colors.error(_(
                "Rollback failed: {err}.  Run `urpm distupgrade --abort` "
                "manually to restore the pre-distupgrade state."
            ).format(err=rb_exc)))
        return 1
    except Stage2Error as exc:
        if _dp["display"] is not None:
            _dp["display"].finish()
        print(colors.error(str(exc)))
        return 1

    if _dp["display"] is not None:
        _dp["display"].finish()

    plan_size = stage2_summary["plan_size"]
    dl = stage2_summary["download"]
    if _dp["shown"] and _dp["start"] is not None:
        elapsed = _display.format_duration(time.monotonic() - _dp["start"])
        print("  " + colors.success(_(
            "{n_dl} downloaded ({n_peers} from peers, {n_up} from "
            "mirrors, {n_cached} from cache) in {time}").format(
                n_dl=dl["downloaded"],
                n_peers=dl.get("from_peers", 0),
                n_up=dl.get("from_upstream", 0),
                n_cached=dl["already_present"],
                time=elapsed)))
    else:
        print(colors.success(_(
            "All {n_req} package(s) already in cache — no download.").format(
                n_req=dl["requested"])))

    if dl["failed"]:
        print(colors.error(_(
            "Cannot proceed to install : {n} package(s) failed to "
            "download.  First failures : {head}").format(
                n=len(dl["failed"]),
                head=", ".join(dl["failed"][:3]))))
        return 1

    return _cmd_run_stage3_tx_a_and_execvp(
        db,
        plan=stage2_summary["plan"],
        nevra_to_path=stage2_summary["nevra_to_path"],
        resolver=stage2_summary.get("resolver"),
        version_from=stage0.current or "unknown",
        version_to=stage0.target.display(),
        auto=getattr(args, "auto", False),
    )


def _which_anchors_in_plan(actions, *, resolver, anchors,
                            target_identity: str) -> dict:
    """Return ``{anchor: True/False}`` for each Tx A anchor.

    ``True`` when at least one plan solvable provides the anchor AND
    its ``release`` tag mentions the target release marker
    (``mgaN``).  This catches the case where the pool contains a
    stale mga N version of an anchor because no mga N+1 build was
    published — Tx A would then install a broken cross-mga stack.
    """
    marker = f"mga{target_identity}"
    plan_ids = {a.solvable_id: a for a in actions
                if getattr(a, "solvable_id", None) is not None}
    if resolver is None or not plan_ids:
        # Best-effort: treat every anchor as present (older tests /
        # dry-runs that don't have a pool).
        return {name: True for name in anchors}
    pool = resolver.pool
    result: dict = {}
    for name in anchors:
        try:
            dep = pool.Dep(name)
        except Exception:  # noqa: BLE001
            result[name] = False
            continue
        found = False
        for s in pool.whatprovides(dep):
            if s.id not in plan_ids:
                continue
            # Release tag must carry the target marker.
            if marker in (s.evr or ""):
                found = True
                break
        result[name] = found
    return result


def _cmd_run_stage3_tx_a_and_execvp(
    db,
    *,
    plan,
    nevra_to_path,
    resolver,
    version_from: str,
    version_to: str,
    auto: bool = False,
) -> int:
    """Commit Tx A, smoke-test the target stack, execvp into it.

    Never returns on success : ``execvp`` replaces the current
    process with a fresh urpm running under the target rpm/python.
    On failure returns 1 and leaves ``.state`` at the crash boundary
    so the user can `urpm recover`.
    """
    from ...core.distupgrade import (
        SmokeTestFailure,
        Stage3Error,
        execvp_to_continue,
        run_stage3_tx_a,
        split_plan_for_tx_a_and_b,
    )

    # Filter out REMOVE actions — the distupgrade solver produces them
    # for mga N-only packages that don't ship in mga N+1.  They have
    # no downloaded RPM and don't belong in the install-side plan ;
    # rpm's own obsoletes-processing handles them during Tx B, and any
    # residual (name-based) erase is passed via ``erase_names``.
    install_actions = [p for p in plan if p.action.value != 'remove']
    remove_names = [p.name for p in plan if p.action.value == 'remove']

    # Validate every Tx A anchor has a target-release counterpart in
    # the plan.  Missing an anchor is a fatal design mismatch — Tx A
    # will commit a partial critical stack, execvp will land on a
    # Python where the anchor's module tree isn't installed, and the
    # post-execvp instance can't run.  Refuse loudly with a
    # per-anchor status so the user knows what's missing.
    from ...core.distupgrade.manifest import TRANSACTION_A_PROVIDES
    plan_by_provides = _which_anchors_in_plan(
        install_actions,
        resolver=resolver,
        anchors=TRANSACTION_A_PROVIDES,
        target_identity=version_to.split(":", 1)[0],
    )
    missing = [n for n, ok in plan_by_provides.items() if not ok]
    if missing:
        print(colors.error(_(
            "Stage 3 refused : {n} critical Tx A anchor(s) missing "
            "from the target-release plan : {names}").format(
                n=len(missing), names=", ".join(missing))))
        print(colors.info(_(
            "The target-release repositories must ship a mga{v}-"
            "tagged build of each anchor.  Rebuild and publish the "
            "missing packages, then `urpm distupgrade --abort` and "
            "retry.").format(v=version_to.split(":", 1)[0])))
        return 1

    tx_a_plan, tx_b_plan = split_plan_for_tx_a_and_b(
        install_actions, resolver=resolver,
    )
    print(colors.info(_(
        "Install plan : {n_a} critical system component(s) first, then "
        "{n_b} remaining component(s), with {n_rm} package(s) to "
        "remove.").format(
            n_a=len(tx_a_plan), n_b=len(tx_b_plan),
            n_rm=len(remove_names))))

    try:
        from ...core import _dup_diag as _dupd
        _dupd.emit("stage3-split", "plan_computed", {
            "tx_a_count": len(tx_a_plan),
            "tx_b_count": len(tx_b_plan),
            "remove_count": len(remove_names),
            "tx_a_plan": list(tx_a_plan),
            "tx_b_plan": list(tx_b_plan),
            "erase_names": list(remove_names),
            "nevra_to_path_count": len(nevra_to_path),
        })
    except Exception:  # noqa: BLE001
        pass

    # Persist BOTH plans + the NEVRA→path map into `.state` before
    # anything mutating runs.  This gives the post-execvp instance
    # (loaded on the target stack) a complete picture without having
    # to re-solve.  ``persist_tx_a_plan`` inside ``run_stage3_tx_a``
    # merges this dict rather than overwriting.
    from ...core.distupgrade import read_state, write_state
    prior = read_state(db) or {}
    prior.update({
        "version_from": version_from,
        "version_to": version_to,
        "tx_b_plan_ordered": list(tx_b_plan),
        "nevra_to_path": {k: str(v) for k, v in nevra_to_path.items()},
        "erase_names": list(remove_names),
    })
    write_state(prior, db)

    # Live install progress — reuse ``urpm i`` / ``urpm u`` widget so
    # the critical-components install looks identical to a normal
    # transaction from the user's POV.
    from ..helpers.progress import make_progress_callback
    tx_a_progress = make_progress_callback(
        header_template=_(
            "Installing critical system components ({count} packages)"),
        total=len(tx_a_plan),
        full_sync=True,
    )
    try:
        run_stage3_tx_a(
            db,
            tx_a_plan=tx_a_plan,
            rpm_paths_by_nevra=nevra_to_path,
            version_from=version_from,
            version_to=version_to,
            progress_callback=tx_a_progress,
        )
    except (Stage3Error, SmokeTestFailure) as exc:
        tx_a_progress.cleanup()
        # Erase the widget's 3-line region (header + bar + sub) so
        # the next print doesn't bleed into leftover ANSI-drawn bars.
        import sys
        if sys.stdout.isatty():
            print("\r\033[2A\033[J", end='', flush=True)
        else:
            print()
        print(colors.error(_(
            "Installation of critical system components failed : "
            "{err}").format(err=exc)))
        print(colors.info(_(
            "Interrupted at the last failure boundary — run "
            "`urpm recover` once the underlying issue is fixed.")))
        return 1
    tx_a_progress.cleanup()
    print()  # finalise the progress-bar line

    print(colors.success(_(
        "Critical system components installed — handing off to the "
        "new urpm for the remaining components...")))

    # execvp replaces this process ; no code past this line runs on
    # success.  On failure (unusual — usually OSError) we bail out.
    # ``auto`` is propagated across the handoff so the post-execvp
    # instance keeps the caller's non-interactive semantic for the
    # Stage 4 drop-migrated-media prompt.
    try:
        execvp_to_continue(auto=auto)
    except OSError as exc:
        print(colors.error(_(
            "Handoff to new urpm failed : {err}.  Critical system "
            "components are installed ; run `urpm distupgrade "
            "--continue` manually.").format(err=exc)))
        return 1
    return 0  # unreachable


def _cmd_continue_after_execvp(args, db) -> int:
    """Post-execvp entry : commit Tx B → Stage 4 → mark Stage 5 due."""
    from ...core.distupgrade import (
        Stage3Error,
        Stage4Error,
        read_state,
        run_stage3_tx_b,
        run_stage4,
    )

    try:
        from ...core import _dup_diag as _dupd
        _dupd.dump_process_env("execvp", "post_execvp")
        _dupd.snapshot_rpmdb("execvp-post")
    except Exception:  # noqa: BLE001
        pass

    state = read_state(db)
    if state is None:
        print(colors.error(_(
            "no distupgrade state on disk : nothing to continue.")))
        return 1

    tx_b_plan = state.get("tx_b_plan_ordered") or []
    nevra_to_path = state.get("nevra_to_path") or {}
    version_from = state.get("version_from", "unknown")
    version_to = state.get("version_to", "unknown")
    erase_names = state.get("erase_names") or []

    try:
        from ...core import _dup_diag as _dupd
        _dupd.emit("execvp", "state_loaded", {
            "tx_b_plan_count": len(tx_b_plan),
            "tx_b_plan_sample": tx_b_plan[:20],
            "erase_names_count": len(erase_names),
            "erase_names_sample": erase_names[:20],
            "nevra_to_path_count": len(nevra_to_path),
            "version_from": version_from, "version_to": version_to,
        })
    except Exception:  # noqa: BLE001
        pass

    if not tx_b_plan and not erase_names:
        print(colors.info(_(
            "no Tx B plan persisted : nothing to install in phase B.  "
            "Continuing to Stage 4.")))
    else:
        from ..helpers.progress import make_progress_callback
        tx_b_progress = make_progress_callback(
            header_template=_(
                "Installing remaining components ({count} packages)"),
            total=len(tx_b_plan),
            full_sync=True,
        )
        try:
            run_stage3_tx_b(
                db,
                tx_b_plan=tx_b_plan,
                rpm_paths_by_nevra=nevra_to_path,
                version_from=version_from,
                version_to=version_to,
                erase_names=erase_names,
                progress_callback=tx_b_progress,
            )
        except Stage3Error as exc:
            tx_b_progress.cleanup()
            # Erase the widget's 3-line region (header + bar + sub) so
            # the next print doesn't bleed into leftover ANSI-drawn bars.
            import sys
            if sys.stdout.isatty():
                print("\r\033[2A\033[J", end='', flush=True)
            else:
                print()
            print(colors.error(_(
                "Installation of remaining components failed : "
                "{err}").format(err=exc)))
            return 1
        tx_b_progress.cleanup()
        # Erase the widget's 3-line region (header + bar + sub) so
        # the next print doesn't bleed into leftover ANSI-drawn bars.
        import sys
        if sys.stdout.isatty():
            print("\r\033[2A\033[J", end='', flush=True)
        else:
            print()
        print(colors.success(_(
            "Remaining components installed : {n}.").format(
                n=len(tx_b_plan))))

    try:
        summary = run_stage4(db)
    except Stage4Error as exc:
        print(colors.error(_(
            "Post-transaction report failed : {err}").format(err=exc)))
        return 1

    _render_stage4_report(summary, db=db,
                          auto=getattr(args, "auto", False))
    return 0


def _render_stage4_report(summary: dict, *, db=None, auto: bool = False) -> None:
    """Print the post-distupgrade report.

    Structured sections with counts, tail lists, and per-section
    action hints.  All strings pass through ``_()`` for i18n.

    When ``db`` is provided and there are transposed-old media rows
    left, prompt Y/N to drop them (skipped under ``auto=True`` — the
    user gets a hint to run ``urpm media remove --distupgraded``
    later).
    """
    from .. import display

    version_from = summary.get("version_from") or "?"
    version_to = summary.get("version_to") or "?"
    rpmnew = summary.get("rpmnew_files") or []
    failed = summary.get("failed_scriptlets") or []
    residuals = summary.get("residuals") or []
    orphan_media = summary.get("orphan_media") or []
    transposed_media = summary.get("transposed_media") or []

    print("\n" + colors.bold(_(
        "Distupgrade Mageia {src} → {tgt} complete.").format(
            src=version_from, tgt=version_to)))

    # ── Failed scriptlets ─────────────────────────────────────────
    if failed:
        print("\n  " + colors.error(_(
            "Failed scriptlets ({n}) - review carefully:").format(
                n=len(failed))))
        for row in failed[:5]:
            print(f"    {colors.error(row.get('pkg_name', '?'))} — "
                  f"{colors.dim(row.get('script_type', '?'))}")
        if len(failed) > 5:
            print(colors.dim(_(
                "    (+ {n} more — see `urpm history --scriptlets`)"
            ).format(n=len(failed) - 5)))

    # ── .rpmnew files ────────────────────────────────────────────
    if rpmnew:
        print("\n  " + colors.warning(_(
            ".rpmnew files ({n}) - config files where mga {tgt} "
            "ships a new default:").format(n=len(rpmnew), tgt=version_to)))
        display.print_package_list(
            [str(p) for p in rpmnew],
            indent=4, color_func=colors.warning)
        print(colors.dim(_(
            "    Review each with :  vimdiff <file> <file>.rpmnew")))

    # ── mga N residuals ──────────────────────────────────────────
    if residuals:
        print("\n  " + colors.info(_(
            "Residual mga{src} packages still installed ({n}):").format(
                n=len(residuals), src=version_from)))
        display.print_package_list(
            residuals, indent=4, color_func=colors.info)
        print(colors.dim(_(
            "    Most are SONAME-versioned libs waiting for a mga"
            "{tgt} rebuild — kept intentionally.").format(tgt=version_to)))
        print(colors.dim(_(
            "    Review orphan candidates with :  urpm autoremove "
            "--show-all")))

    # ── Orphan third-party media ──────────────────────────────────
    if orphan_media:
        print("\n  " + colors.warning(_(
            "Third-party media without a mga{tgt} equivalent "
            "({n}) - disabled:").format(
                n=len(orphan_media), tgt=version_to)))
        for m in orphan_media[:10]:
            print(f"    {colors.warning(m['name'])}")
        if len(orphan_media) > 10:
            print(colors.dim(_(
                "    (+ {n} more)").format(n=len(orphan_media) - 10)))
        print(colors.dim(_(
            "    Wait for a maintainer rebuild, or `urpm media "
            "remove <short_name>`.")))

    # ── Transposed old media (mga N → mga N+1) ────────────────────
    if transposed_media:
        print("\n  " + colors.info(_(
            "Old mga{src} repositories replaced by their mga{tgt} "
            "counterpart ({n}) - now obsolete:").format(
                n=len(transposed_media), src=version_from,
                tgt=version_to)))
        for m in transposed_media[:15]:
            print(f"    {colors.dim(m['name'])}")
        if len(transposed_media) > 15:
            print(colors.dim(_(
                "    (+ {n} more)").format(n=len(transposed_media) - 15)))
        _prompt_drop_transposed(db, transposed_media, auto=auto)

    # ── All-clear line ────────────────────────────────────────────
    if not (failed or rpmnew or residuals or orphan_media
            or transposed_media):
        print("\n  " + colors.success(_(
            "No .rpmnew, no failed scriptlet, no residual, no "
            "orphan media — clean upgrade.")))

    # ── Next-step recommendations ─────────────────────────────────
    print("\n" + colors.info(_(
        "Next :  reboot so the post-boot adjustments run on next "
        "startup.")))


def _prompt_drop_transposed(db, transposed_media, *, auto: bool) -> None:
    """Prompt Y/N (unless ``auto``) and delete transposed old media rows.

    The rows carry ``disabled_by='distupgrade'`` — they were disabled
    by Stage 1 because a mga N+1 counterpart was inserted alongside.
    Removing them cascades ``server_media`` links (FK ON DELETE
    CASCADE) so no orphan links are left behind.
    """
    from ...i18n import confirm_yes
    if db is None:
        return
    if auto:
        # --yes / --auto : auto-drop, don't prompt.  Matches the
        # « yes to every confirmation » semantic the user expects
        # when they explicitly asked for non-interactive.
        print(colors.dim(_(
            "  --yes / --auto : dropping the {n} old repositories "
            "without prompting.").format(n=len(transposed_media))))
        _drop_transposed_media(db, transposed_media)
        return
    try:
        response = input(_(
            "  Drop these old repositories now ? [y/N] "))
    except (KeyboardInterrupt, EOFError):
        print()
        return
    if not confirm_yes(response):
        print(colors.dim(_(
            "    Kept ; drop later with :  urpm media remove "
            "--distupgraded")))
        return
    _drop_transposed_media(db, transposed_media)


def _drop_transposed_media(db, transposed_media) -> int:
    """Mark transposed old media for deferred deletion (SPEC_DISTUPGRADE §4.4).

    Physical purge of these rows (and their thousands of packages /
    requires / provides deps) is deferred to urpmd startup or the
    next ``urpm`` CLI invocation — see
    :mod:`urpm.core.deferred_cleanup`.  Doing the ~10 s of DELETE
    work at Stage 4 blocks a user who is about to reboot anyway, for
    no user benefit ; deferring it moves the wait behind the reboot.

    Returns the number of media rows flagged.
    """
    if not transposed_media:
        return 0
    ids = [int(m['id']) for m in transposed_media if m.get('id') is not None]
    if not ids:
        return 0
    conn = db._get_connection()
    placeholders = ",".join("?" * len(ids))
    with db._lock:
        conn.execute(
            f"UPDATE media SET disabled_by = 'pending_drop' "
            f"WHERE id IN ({placeholders})", ids)
        conn.commit()
    print("  " + colors.success(_(
        "Flagged {n} old repositor(y|ies) for post-reboot cleanup.").format(
            n=len(ids))))
    return len(ids)
