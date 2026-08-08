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

    _run_one_side(
        db,
        side="b",
        plan=tx_b_plan,
        rpm_paths_by_nevra=rpm_paths_by_nevra,
        cmdline=f"urpm distupgrade tx-b {version_from}->{version_to}",
        erase_names=erase_names,
        progress_callback=progress_callback,
    )

    bump_stage("transactions_done", db)
