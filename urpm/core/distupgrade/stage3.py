"""Stage 3 — Tx A / smoke test / execvp / Tx B (SPEC_DISTUPGRADE §4.3).

Flow (§4.3 étapes 0-8) :

1. Persist ``stage=tx_a_committing + tx_a_plan_ordered`` in ``.state``.
2. Run Tx A via :meth:`PackageOperations.execute_install` with
   ``force=True + nodeps=True`` (matches the ``--replacepkgs
   --replacefiles --nodeps`` combo the spec justifies empirically :
   partial rpmdb + transient cross-package conflicts + libsolv already
   resolved deps).  Scriptlets + rpmnew_files come out of the queue
   result and get persisted (scriptlets via
   :meth:`PackageOperations.record_scriptlet_output`, rpmnew_files in
   ``.state.rpmnew_files_tx_a`` for Stage 4 to pick up).
3. Bump ``stage=tx_a_done``.
4. Smoke test post-Tx-A : subprocess Python invoked with ``-P``,
   ``PYTHONSAFEPATH=1``, ``cwd='/'``, ``LC_ALL=C`` — runs
   ``rpm.TransactionSet().dbMatch()`` and asserts non-empty.
5. Decision : execvp on success ; on failure, leave ``.state`` at
   ``tx_a_done`` and instruct the user to reboot + ``--resume``.
6. (Post-execvp) Persist Tx B plan → run Tx B.
7. Bump ``stage=transactions_done``.

No bespoke ``rpm.TransactionSet`` construction, no custom callback
classification, no diagnostic file : Stage 3 sits on top of the same
primitives ``cmd_install`` / ``cmd_upgrade`` use.  Distupgrade-specific
value = just the wiring + the smoke test + the execvp handoff.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..database import PackageDatabase


logger = logging.getLogger(__name__)


class Stage3Error(Exception):
    """Raised for any Stage 3 failure that isn't a smoke-test KO."""


class SmokeTestFailure(Exception):
    """Post-Tx-A smoke test failed ; the target stack is not
    loadable in the current process."""


# ── State persistence for Tx A / Tx B ──────────────────────────────


def persist_tx_a_plan(db: "PackageDatabase",
                      plan: List[str],
                      *,
                      version_from: str,
                      version_to: str,
                      started_at: Optional[str] = None) -> None:
    """Persist ``stage=tx_a_committing + tx_a_plan_ordered``.

    Single ``.state`` write (SQLite atomic) — a crash between plan
    persist and Tx A commit is caught by ``--resume`` reading the
    plan back.
    """
    from .state import read_state, write_state

    prior = read_state(db) or {}
    prior.update({
        "version_from": version_from,
        "version_to": version_to,
        "started_at": prior.get("started_at", started_at
                                or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime())),
        "stage": "tx_a_committing",
        "tx_a_plan_ordered": list(plan),
    })
    write_state(prior, db)


def persist_tx_b_plan(db: "PackageDatabase",
                      plan: List[str],
                      *,
                      version_from: str,
                      version_to: str) -> None:
    """Persist ``stage=tx_b_running + tx_b_plan_ordered``.

    Post-execvp only : the mga N+1 instance persists the plan before
    running any header (§4.3 étape 6).
    """
    from .state import read_state, write_state

    prior = read_state(db) or {}
    prior.update({
        "version_from": version_from,
        "version_to": version_to,
        "stage": "tx_b_running",
        "tx_b_plan_ordered": list(plan),
    })
    write_state(prior, db)


def bump_stage(new_stage: str, db: "PackageDatabase") -> None:
    """Update ``stage`` without touching other fields."""
    from .state import read_state, write_state

    prior = read_state(db) or {}
    prior["stage"] = new_stage
    write_state(prior, db)


def _strip_epoch(nevra: str) -> str:
    """Return ``nevra`` with the ``epoch:`` segment removed.

    Solver-produced NEVRAs carry epoch (``glibc-6:2.42-9.mga10.x86_64``).
    RPM filenames — and therefore the download-map keys built by
    :meth:`PackageOperations.download_packages` — don't.  This helper
    strips the ``-EPOCH:`` sandwich so the lookup succeeds on both
    forms.
    """
    stem, sep, arch = nevra.rpartition('.')
    if not sep or ':' not in stem:
        return nevra
    left, colon, ver_rel = stem.partition(':')
    name, dash, _epoch = left.rpartition('-')
    if not dash:
        return nevra
    return f"{name}-{ver_rel}.{arch}"


def _persist_tx_result(db: "PackageDatabase",
                       *, side: str, transaction_id: int,
                       rpmnew_files: List[str]) -> None:
    """Record post-Tx-* artefacts into ``.state`` for Stage 4.

    Stage 4 reads ``.state.rpmnew_files_tx_{a,b}`` (Q5 : replaces the
    old sentinel + `find -newer` scan) and
    ``.state.tx_{a,b}_transaction_id`` (Q6 : lets Stage 4 read
    scriptlet outcomes via ``db.get_scriptlet_output(tx_id)``).
    """
    from .state import read_state, write_state

    prior = read_state(db) or {}
    prior[f"rpmnew_files_tx_{side}"] = list(rpmnew_files)
    prior[f"tx_{side}_transaction_id"] = int(transaction_id)
    write_state(prior, db)


# ── Smoke test (§4.3 F3) ──────────────────────────────────────────


SMOKE_SCRIPT = (
    "import itertools, rpm; "
    "ts = rpm.TransactionSet(); "
    "headers = list(itertools.islice(ts.dbMatch(), 5)); "
    "assert headers, 'rpmdb empty'; "
    "print('ok')"
)


def smoke_test_target_stack(python_bin: str = "/usr/bin/python3") -> None:
    """Prove the target stack loads by invoking a fresh Python subprocess.

    Uses ``-I`` (isolated mode : implies ``-P`` + ``-E`` + ``-s`` —
    ignores ``PYTHON*`` env vars, skips user site-packages, doesn't
    prepend cwd to ``sys.path``).  Portable to any Python ≥ 3.4 —
    ``-P`` alone would require ≥ 3.11 and break the pre-execvp check
    on the source release.

    ``cwd`` pinned to ``/`` so the loader ignores the invoker's
    directory.  ``LC_ALL=C`` so a parsable ``'ok'`` marker survives
    locale-mangled output.

    Raises :class:`SmokeTestFailure` on any non-zero exit or missing
    ``ok`` marker.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "LANGUAGE": "C",
        "PYTHONSAFEPATH": "1",
    }
    try:
        proc = subprocess.run(
            [python_bin, "-I", "-c", SMOKE_SCRIPT],
            env=env,
            cwd="/",
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SmokeTestFailure(
            f"target Python interpreter missing at {python_bin}") from exc

    if proc.returncode != 0 or "ok" not in proc.stdout:
        raise SmokeTestFailure(
            f"target stack smoke test failed "
            f"(rc={proc.returncode}) : "
            f"{proc.stderr.strip() or proc.stdout.strip()}")


# ── execvp handoff ────────────────────────────────────────────────


def execvp_to_continue(*, auto: bool = False) -> None:
    """Replace the current process with ``urpm distupgrade --continue``.

    Uses ``/usr/bin/urpm`` explicitly (no ``$PATH`` lookup) so a
    malicious ``%post`` that redirected ``PATH`` can't hijack the
    handoff.  ``argv[0]`` stays ``urpm`` for correct progress
    display.  Never returns on success.

    ``auto`` : when True, adds ``--yes`` to the target argv so the
    post-execvp instance keeps the caller's non-interactive semantic
    across the handoff (otherwise ``args.auto`` in
    ``_cmd_continue_after_execvp`` would default to False and Stage 4
    would prompt for the migrated-media drop).
    """
    argv = ["urpm", "distupgrade", "--continue"]
    if auto:
        argv.append("--yes")
    try:
        from .. import _dup_diag as _dupd
        _dupd.snapshot_rpmdb("execvp-pre")
        _dupd.dump_process_env("execvp", "pre_execvp")
        _dupd.emit("execvp", "about_to_execvp", {
            "target_argv": argv, "target_bin": "/usr/bin/urpm",
        })
    except Exception:  # noqa: BLE001
        pass
    os.execvp("/usr/bin/urpm", argv)
    # If we get here, something is very wrong.
    raise Stage3Error("execvp to /usr/bin/urpm returned unexpectedly")


# ── Stage 3 orchestrator ───────────────────────────────────────────


def _run_one_side(
    db: "PackageDatabase",
    *,
    side: str,
    plan: List[str],
    rpm_paths_by_nevra: dict,
    cmdline: str,
    erase_names: Optional[List[str]] = None,
    progress_callback=None,
) -> None:
    """Shared body of Tx A / Tx B commit.

    Chains the same primitives ``cmd_install`` uses :

    1. :meth:`PackageOperations.begin_transaction`
    2. :meth:`PackageOperations.execute_install` with
       ``force=True + nodeps=True`` (matches ``--replacepkgs
       --replacefiles --nodeps`` per SPEC §4.3 étape 5).
    3. :meth:`PackageOperations.record_scriptlet_output`
    4. :meth:`PackageOperations.complete_transaction`

    Rpmnew files + transaction_id land in ``.state`` for Stage 4
    to pick up.  On any failure the transaction is aborted via
    :meth:`PackageOperations.abort_transaction`.

    ``progress_callback`` : optional ``TransactionProgress`` sink
    forwarded to :meth:`execute_install` — same shape ``urpm i`` /
    ``urpm u`` build via :func:`cli.helpers.progress.make_progress_callback`.
    """
    from ..operations import InstallOptions, PackageOperations

    ordered_paths: List[str] = []
    for nevra in plan:
        # Try the plan's NEVRA verbatim first (may include epoch),
        # then a version without epoch — RPM filenames drop it, so
        # the download map is keyed both possibilities coalesced.
        rpm_path = (rpm_paths_by_nevra.get(nevra)
                    or rpm_paths_by_nevra.get(_strip_epoch(nevra)))
        if rpm_path is None:
            raise Stage3Error(
                f"cannot locate downloaded RPM for {nevra} "
                f"(Tx {side.upper()} cache)")
        ordered_paths.append(str(rpm_path))

    ops = PackageOperations(db)
    tx_id = ops.begin_transaction('distupgrade', cmdline, [])
    logger.info("Stage 3 Tx %s : begin_transaction id=%d, %d package(s)",
                side.upper(), tx_id, len(ordered_paths))

    try:
        from .. import _dup_diag as _dupd
        _dupd.snapshot_rpmdb(f"stage3-{side}-begin")
        _dupd.emit(f"stage3-{side}", "begin", {
            "side": side, "cmdline": cmdline,
            "n_packages": len(ordered_paths),
            "erase_names_count": len(erase_names or []),
            "erase_names_sample": (erase_names or [])[:20],
            "plan_sample": [str(p) for p in ordered_paths[:20]],
            "plan_count": len(ordered_paths),
        })
    except Exception:  # noqa: BLE001
        pass

    options = InstallOptions(
        verify_signatures=True,
        force=True,       # RPMPROB_FILTER_REPLACEPKG + REPLACENEWFILES + REPLACEOLDFILES
        nodeps=True,      # RPMTRANS_FLAG_NODEPS — libsolv resolved deps upstream
    )
    try:
        queue_result = ops.execute_install(
            rpm_paths=ordered_paths,
            options=options,
            progress_callback=progress_callback,
            full_sync=True,   # need scriptlets to complete before next step
            erase_names=erase_names or None,
        )
    except Exception as exc:  # noqa: BLE001
        ops.abort_transaction(tx_id)
        raise Stage3Error(
            f"Tx {side.upper()} commit failed: {exc}") from exc

    if queue_result is None or not getattr(queue_result, "success", False):
        ops.abort_transaction(tx_id)
        errs = "; ".join(str(e) for e in getattr(
            queue_result, "errors", None) or ["queue reported failure"])
        raise Stage3Error(
            f"Tx {side.upper()} did not converge: {errs}")

    # Capture scriptlet outputs + rpmnew list from the queue result.
    ops.record_scriptlet_output(tx_id, queue_result)
    rpmnew_files: List[str] = []
    ops_list = getattr(queue_result, "operations", None) or []
    if ops_list:
        rpmnew_files = list(getattr(ops_list[0], "rpmnew_files", None) or [])
    _persist_tx_result(
        db,
        side=side,
        transaction_id=tx_id,
        rpmnew_files=rpmnew_files,
    )
    ops.complete_transaction(tx_id)

    try:
        from .. import _dup_diag as _dupd
        _dupd.snapshot_rpmdb(f"stage3-{side}-end")
        _dupd.emit(f"stage3-{side}", "end", {
            "side": side, "tx_id": tx_id,
            "rpmnew_files_count": len(rpmnew_files),
            "queue_success": getattr(queue_result, "success", None),
            "queue_errors": [
                str(e) for e in getattr(queue_result, "errors", None) or []
            ],
        })
    except Exception:  # noqa: BLE001
        pass


def run_stage3_tx_a(
    db: "PackageDatabase",
    *,
    tx_a_plan: List[str],
    rpm_paths_by_nevra: dict,
    version_from: str,
    version_to: str,
    smoke=smoke_test_target_stack,
    progress_callback=None,
) -> None:
    """Persist plan → commit Tx A → smoke test → bump ``tx_a_done``.

    Raises :class:`Stage3Error` on commit failure or
    :class:`SmokeTestFailure` on smoke KO — the caller decides
    between reboot-and-resume vs abort.

    ``progress_callback`` : forwarded to :func:`_run_one_side` /
    :meth:`PackageOperations.execute_install`.
    """
    persist_tx_a_plan(
        db, tx_a_plan,
        version_from=version_from,
        version_to=version_to,
    )

    _run_one_side(
        db,
        side="a",
        plan=tx_a_plan,
        rpm_paths_by_nevra=rpm_paths_by_nevra,
        cmdline=f"urpm distupgrade tx-a {version_from}->{version_to}",
        progress_callback=progress_callback,
    )

    bump_stage("tx_a_done", db)
    logger.info("Stage 3 : smoke test post-Tx-A")
    smoke()


def _split_plan_by_size(
    plan: List[str],
    rpm_paths_by_nevra: dict,
    max_batch_bytes: int,
) -> List[List[str]]:
    """Slice ``plan`` into batches by cumulative on-disk .rpm size.

    The plan is already in libsolv's topological order.  Slicing
    contiguously preserves that order across batches : the Requires
    closure of every package in batch K lives entirely in batches
    ``[0..K-1]``, so ``rpm.ts.order()`` inside each batch can
    resolve dependencies without needing ``RPMTRANS_FLAG_NODEPS``.

    A single package larger than ``max_batch_bytes`` still lands in
    its own batch — we never bisect a single install.

    ``.rpm`` files unlinked earlier in the transaction (e.g. by the
    per-batch cache purge) or missing from ``rpm_paths_by_nevra``
    count as size 0 : batch boundaries chase disk peak, not plan
    size accounting.

    Returns a list of NEVRA-list batches ; empty list for an empty
    input plan.
    """
    import os as _os
    batches: List[List[str]] = []
    current: List[str] = []
    current_bytes = 0
    for nevra in plan:
        rpm_path = (rpm_paths_by_nevra.get(nevra)
                    or rpm_paths_by_nevra.get(_strip_epoch(nevra)))
        try:
            sz = _os.stat(rpm_path).st_size if rpm_path else 0
        except OSError:
            sz = 0
        if current and current_bytes + sz > max_batch_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(nevra)
        current_bytes += sz
    if current:
        batches.append(current)
    return batches


def _purge_installed_batch_rpms(
    batch: List[str],
    rpm_paths_by_nevra: dict,
) -> "tuple[int, int]":
    """Unlink cached .rpm files of packages in ``batch`` that are now
    committed to the rpmdb.

    Called right after each batch's ``ts.run()`` returns : whatever
    landed in the rpmdb doesn't need its payload again, whatever
    silently failed keeps its .rpm so the end-of-Tx-B retry doesn't
    have to re-download.

    Returns ``(freed_files, freed_bytes)``.
    """
    import os as _os
    installed = _installed_nevras_canonical()
    if not installed:
        return 0, 0
    freed_files = 0
    freed_bytes = 0
    for nevra in batch:
        canon = _canonical_nevra(nevra)
        if canon is None or canon not in installed:
            continue
        rpm_path = (rpm_paths_by_nevra.get(nevra)
                    or rpm_paths_by_nevra.get(_strip_epoch(nevra)))
        if not rpm_path:
            continue
        try:
            st = _os.stat(rpm_path)
            _os.unlink(rpm_path)
            freed_bytes += st.st_size
            freed_files += 1
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001
            logger.debug("batch purge : cannot unlink %s : %s",
                         rpm_path, exc)
    if freed_files:
        logger.info(
            "Stage 3 Tx B batch : purged %d .rpm from cache (%.1f MB)",
            freed_files, freed_bytes / (1024 * 1024))
    return freed_files, freed_bytes


def _canonical_nevra(nevra: str) -> Optional[str]:
    """Normalise ``name-EPOCH:version-release.arch`` for comparison.

    Handles both epoch-prefixed (``menu-messages-1:1-8.mga10.noarch``)
    and epoch-less (``menu-messages-1-8.mga10.noarch``) inputs.
    Returns ``name|epoch|version|release|arch`` — a tuple joined by
    ``|`` so it can be used as a set key without ambiguity.  Missing
    epoch normalises to ``0`` per RPM convention.

    Returns ``None`` for strings that don't match the shape at all —
    caller can treat them as opaque and compare verbatim.
    """
    import re
    # Split off trailing .arch
    m = re.match(r"^(.+)\.([^.]+)$", nevra)
    if not m:
        return None
    body, arch = m.group(1), m.group(2)
    # Split off release (last -N segment before arch)
    m = re.match(r"^(.+)-([^-]+)$", body)
    if not m:
        return None
    body, release = m.group(1), m.group(2)
    # Split off version (last -N segment, may have epoch prefix)
    m = re.match(r"^(.+)-(?:(\d+):)?([^-]+)$", body)
    if not m:
        return None
    name, epoch, version = m.group(1), m.group(2) or "0", m.group(3)
    return f"{name}|{epoch}|{version}|{release}|{arch}"


def _installed_nevras_canonical(root: str = "/") -> set:
    """Return the set of installed package NEVRAs in canonical form."""
    try:
        import rpm
        ts_probe = rpm.TransactionSet(root)
        ts_probe.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
        seen = set()
        for h in ts_probe.dbMatch():
            nevra_str = (
                f"{h['name']}-{h['epoch'] or 0}:{h['version']}-"
                f"{h['release']}.{h['arch'] or 'noarch'}"
            )
            canon = _canonical_nevra(nevra_str)
            if canon:
                seen.add(canon)
        return seen
    except Exception:  # noqa: BLE001
        return set()


def _retry_missing_installs(
    db: "PackageDatabase",
    *,
    planned_nevras: List[str],
    rpm_paths_by_nevra: dict,
    version_from: str,
    version_to: str,
) -> dict:
    """One-shot retry pass for packages Tx B silently failed to install.

    After Tx B's ``ts.run()`` returns without raising, rpm's callback
    machinery has fired INST_STOP for every package — including those
    whose cpio extraction failed with "Directory not empty",
    "No data available", or "No such file or directory".  These
    silent failures never reach the ``problems`` list ``ts.run()``
    returns, so ``_execute_install`` counts them as success.

    Compare the planned Tx B NEVRAs against the current rpmdb : any
    planned NEVRA absent from the rpmdb after commit is a silent
    failure.  Rebuild a mini-plan from ``rpm_paths_by_nevra``, run
    one ``execute_install`` on it with the same ``force=True +
    nodeps=True`` semantics as Tx B, and report what recovered.

    The retry is one-shot : if a package fails twice, it stays failed
    (avoids infinite loops on genuine incompatibilities).  Callers
    surface the still-missing set to the user in Stage 4.

    Returns ``{missing_before_retry, retry_recovered, still_missing}``
    where each value is a list of NEVRA strings.
    """
    from ..operations import InstallOptions, PackageOperations

    installed = _installed_nevras_canonical()
    if not installed:
        # rpm probe failed — refuse to guess.  Skip retry.
        return {
            "missing_before_retry": [],
            "retry_recovered": [],
            "still_missing": [],
        }

    missing_paths: list = []
    missing_nevras: list = []
    for nevra in planned_nevras:
        canon = _canonical_nevra(nevra)
        if canon is None or canon in installed:
            continue
        rpm_path = (rpm_paths_by_nevra.get(nevra)
                    or rpm_paths_by_nevra.get(_strip_epoch(nevra)))
        if rpm_path is None:
            # Downloaded artefact missing — nothing to retry with.
            missing_nevras.append(nevra)
            continue
        missing_paths.append(str(rpm_path))
        missing_nevras.append(nevra)

    if not missing_paths:
        return {
            "missing_before_retry": missing_nevras,
            "retry_recovered": [],
            "still_missing": missing_nevras,
        }

    # Free the .rpm cache of packages already in the rpmdb before the
    # retry — those payloads were consumed by Tx B, we don't need them
    # again.  On papoteur run 9 the disk was 100 % full during Tx B ;
    # freeing ~500 MB-1.5 GB of already-installed .rpm files here gives
    # the retry enough room to succeed on the big packages (R-base,
    # gimp, openblas, kernel-firmware-nonfree) that hit "No space left
    # on device" the first time.
    freed_bytes = 0
    freed_files = 0
    import os as _os
    for nevra, rpm_path in rpm_paths_by_nevra.items():
        canon = _canonical_nevra(nevra)
        if canon is None or canon not in installed:
            continue
        try:
            st = _os.stat(rpm_path)
            _os.unlink(rpm_path)
            freed_bytes += st.st_size
            freed_files += 1
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001
            logger.debug("cache purge : cannot unlink %s : %s",
                         rpm_path, exc)
    if freed_files:
        logger.info(
            "Stage 3 Tx B retry : purged %d already-installed .rpm "
            "from cache (%.1f MB freed) before retry",
            freed_files, freed_bytes / (1024 * 1024))

    logger.info(
        "Stage 3 Tx B retry : %d package(s) missing from rpmdb, "
        "attempting one retry pass", len(missing_paths))

    ops = PackageOperations(db)
    tx_id = ops.begin_transaction(
        'distupgrade',
        f"urpm distupgrade tx-b retry {version_from}->{version_to}",
        [])
    options = InstallOptions(
        verify_signatures=True, force=True, nodeps=True,
    )
    try:
        queue_result = ops.execute_install(
            rpm_paths=missing_paths, options=options,
            progress_callback=None, full_sync=True,
        )
    except Exception as exc:  # noqa: BLE001
        ops.abort_transaction(tx_id)
        logger.warning("Tx B retry raised : %s", exc)
        return {
            "missing_before_retry": missing_nevras,
            "retry_recovered": [],
            "still_missing": missing_nevras,
        }

    if queue_result is None or not getattr(queue_result, "success", False):
        ops.abort_transaction(tx_id)
    else:
        ops.record_scriptlet_output(tx_id, queue_result)
        ops.complete_transaction(tx_id)

    # Re-probe rpmdb to see what actually recovered.
    installed_after = _installed_nevras_canonical()
    recovered, still_missing = [], []
    for nevra in missing_nevras:
        canon = _canonical_nevra(nevra)
        if canon is not None and canon in installed_after:
            recovered.append(nevra)
        else:
            still_missing.append(nevra)
    return {
        "missing_before_retry": missing_nevras,
        "retry_recovered": recovered,
        "still_missing": still_missing,
    }


def run_stage3_tx_b(
    db: "PackageDatabase",
    *,
    tx_b_plan: List[str],
    rpm_paths_by_nevra: dict,
    version_from: str,
    version_to: str,
    erase_names: Optional[List[str]] = None,
    progress_callback=None,
) -> None:
    """Post-execvp : persist Tx B plan → commit → bump ``transactions_done``.

    ``erase_names`` — mga N package names the solver flagged REMOVE
    (dropped from mga N+1).  Passed to
    :meth:`PackageOperations.execute_install` so they're erased in
    the same transaction as the mga N+1 installs.

    ``progress_callback`` : forwarded to :func:`_run_one_side` /
    :meth:`PackageOperations.execute_install`.
    """
    persist_tx_b_plan(
        db, tx_b_plan,
        version_from=version_from,
        version_to=version_to,
    )

    # Batch the plan by cumulative .rpm size to cap the disk peak.
    # Slices follow the libsolv topological order — a package's Requires
    # closure lives entirely in earlier batches, so per-batch ts.order()
    # never sees a missing dependency and standard rpm dep enforcement
    # keeps working (no NODEPS trickery needed).  Per-batch cleanup of
    # already-committed .rpm files further limits the peak.
    batches = _split_plan_by_size(
        tx_b_plan, rpm_paths_by_nevra,
        max_batch_bytes=200 * 1024 * 1024,
    )
    logger.info(
        "Stage 3 Tx B : %d package(s) split into %d batch(es) "
        "(~%d MB max)", len(tx_b_plan), len(batches), 200)

    # Global progress across batches.  Without this, the caller's
    # progress bar resets to 0 at every batch boundary and the user
    # loses all sense of overall Tx B advancement.  We wrap the
    # incoming callback with a per-batch offset so ``packages_done``
    # and ``packages_total`` reflect the whole Tx B plan (installs +
    # last-batch erases), not the current batch alone.
    from dataclasses import replace
    from ..transaction_queue import TransactionPhase

    total_planned = len(tx_b_plan) + len(erase_names or [])
    _prog_state = {"done_before_batch": 0, "last_local_done": 0}

    def _global_progress(tp):
        # Track the last local packages_done seen so we can advance
        # ``done_before_batch`` by that amount at end of batch.  rpm
        # reports monotonically-increasing ``packages_done`` within a
        # single transaction, so the highest value is the count of
        # packages the batch actually processed (including cpio-failed
        # ones — those still fire INST_STOP, see the retry pass below).
        if tp.packages_done > _prog_state["last_local_done"]:
            _prog_state["last_local_done"] = tp.packages_done
        if progress_callback is None:
            return
        # Only rewrite the counters on phases whose ``packages_done`` is
        # a package-level index.  VERIFY / PREPARE run once per batch
        # over that batch's set — translating them to a global scale
        # would look like the bar jumps ahead during verification.
        if tp.phase in (TransactionPhase.INSTALL,
                        TransactionPhase.SCRIPT,
                        TransactionPhase.ERASE):
            adjusted = replace(
                tp,
                packages_done=(_prog_state["done_before_batch"]
                               + tp.packages_done),
                packages_total=total_planned,
            )
            progress_callback(adjusted)
        else:
            progress_callback(tp)

    for i, batch in enumerate(batches, start=1):
        # erase_names go with the LAST batch : the packages they name
        # are typically obsoleted by an install in the plan, so they
        # need to survive until that install runs.
        batch_erase = erase_names if i == len(batches) else None
        _prog_state["last_local_done"] = 0
        _run_one_side(
            db,
            side="b",
            plan=batch,
            rpm_paths_by_nevra=rpm_paths_by_nevra,
            cmdline=(f"urpm distupgrade tx-b batch {i}/{len(batches)} "
                     f"{version_from}->{version_to}"),
            erase_names=batch_erase,
            progress_callback=_global_progress,
        )
        _prog_state["done_before_batch"] += _prog_state["last_local_done"]
        # Free the .rpm cache of packages this batch successfully
        # committed to rpmdb : failed ones keep their .rpm so the
        # retry pass at end-of-Tx-B doesn't have to re-download.
        _purge_installed_batch_rpms(batch, rpm_paths_by_nevra)

    # Silent-fail retry pass : rpm fires INST_STOP even when a package's
    # cpio extraction failed with "Directory not empty" / "No data
    # available" / "No such file or directory" (errors are printed to
    # stderr but don't propagate as rpm problems).  A single retry of
    # each missing package, once the filesystem state has settled (mga N
    # counterparts of successful installs are gone, freeing conflicting
    # file paths), picks up most of these silent failures.
    retry_result = _retry_missing_installs(
        db, planned_nevras=tx_b_plan,
        rpm_paths_by_nevra=rpm_paths_by_nevra,
        version_from=version_from,
        version_to=version_to,
    )
    if retry_result["missing_before_retry"]:
        logger.info(
            "Stage 3 Tx B : %d package(s) missing after commit, "
            "retry recovered %d, %d still missing",
            len(retry_result["missing_before_retry"]),
            len(retry_result["retry_recovered"]),
            len(retry_result["still_missing"]),
        )

    bump_stage("transactions_done", db)
