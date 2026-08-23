"""Deep distupgrade diagnostic (opt-in via ``URPM_DUP_DIAG=1``).

Purpose
-------

Post-mortem of the papoteur beta case (mga9 → mga10) surfaced three
bugs that reproduce on real customer systems but resist inspection
by the normal urpm logging :

- Bug #99 : non-deterministic ``mga9 / mga10`` doublons (5 → 1801
  depending on the run).  Likely a race between Tx A's rpmdb commit
  and Tx B's opening of the same DB post-execvp.
- Bug #102 : ~435 packages from ``tx_b_plan_ordered`` are absent from
  the rpmdb post-Tx-B — silent skips that ``ts.run()`` doesn't
  surface as problems.
- Bug #103 : ``cannot access local variable 'exc'`` at Stage 4.

These bugs need in-context evidence : the state of ts, rpmdb, and
process at the exact hand-over points.  The normal ``urpm-background
.log`` is application-level narrative — not enough.

Contract
--------

This module is a **read-only inspector**.  It never mutates state
(never installs, never erases, never touches rpmdb) — even a bug
in the diagnostic must leave the transaction it observes untouched.

All emitters wrap in ``try/except`` and swallow their own errors :
a broken diagnostic must never propagate to the outer flow.

Storage
-------

Output goes to ``/var/log/dup-diag/`` — one file per stage :

- ``00-stage0.jsonl`` : Stage 0 checks + resolver plan
- ``10-stage1.jsonl`` : Stage 1 media swap + url_version updates
- ``20-stage2.jsonl`` : Stage 2 solve + download summary
- ``30-tx-a.jsonl``   : Tx A pre-order / post-order / post-run
- ``35-execvp.jsonl`` : execvp handoff (both sides)
- ``40-tx-b.jsonl``   : Tx B pre-order / post-order / post-run
- ``50-stage4.jsonl`` : Stage 4 finalisation
- ``rpmdb-snapshots/`` : per-phase rpmdb dumps (name + evr + arch)

Each JSONL line carries : ``{timestamp, pid, ppid, stage, event,
payload}``.  Machine-readable so post-mortem tooling can diff.

Enable
------

Set ``URPM_DUP_DIAG=1`` in the environment before running
``urpm distupgrade``.  ``os.execvp`` propagates env so both sides
enable together.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

_ROOT = Path("/var/log/dup-diag")
_ENV_KEY = "URPM_DUP_DIAG"


def is_enabled() -> bool:
    """Return True iff diagnostic dump is opt-in via env."""
    return os.environ.get(_ENV_KEY, "") not in ("", "0", "false", "False")


def _ensure_root() -> Optional[Path]:
    """Create the diagnostic root dir on first call.  Return path or None."""
    try:
        _ROOT.mkdir(parents=True, exist_ok=True)
        (_ROOT / "rpmdb-snapshots").mkdir(exist_ok=True)
        return _ROOT
    except Exception:  # noqa: BLE001
        return None


def emit(stage: str, event: str, payload: Optional[dict] = None) -> None:
    """Append one JSONL entry to the stage's log file.

    A diagnostic error must not propagate — wrap in try/except.

    Args:
        stage: Short slug picking the target file (``00-stage0``,
            ``30-tx-a``, etc.).  Free-form ; created on demand.
        event: Machine-readable event tag inside that stage
            (``pre_order``, ``post_run``, ``problems``, …).
        payload: Optional dict of extra data.  Values must be
            JSON-serialisable (basic Python types).
    """
    if not is_enabled():
        return
    root = _ensure_root()
    if root is None:
        return
    try:
        rec = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "stage": stage,
            "event": event,
            "payload": payload or {},
        }
        path = root / f"{stage}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def snapshot_rpmdb(tag: str) -> None:
    """Dump every installed (name, evr, arch, install_time) to a file.

    Names the file ``{tag}-{timestamp}.txt`` under the snapshots dir.
    Runs a fresh ``rpm.TransactionSet().dbMatch()`` — no reliance on
    any caller's shared state.
    """
    if not is_enabled():
        return
    root = _ensure_root()
    if root is None:
        return
    try:
        import rpm as _rpm
        ts_probe = _rpm.TransactionSet()
        ts_probe.setVSFlags(_rpm._RPMVSF_NOSIGNATURES)
        rows = []
        for h in ts_probe.dbMatch():
            rows.append((
                h["name"],
                f"{h['epoch'] or 0}:{h['version']}-{h['release']}",
                h["arch"] or "",
                int(h["installtime"] or 0),
            ))
        rows.sort()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = root / "rpmdb-snapshots" / f"{tag}-{stamp}-pid{os.getpid()}.txt"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# rpmdb snapshot tag={tag} pid={os.getpid()}\n")
            fh.write(f"# rows={len(rows)}\n")
            for name, evr, arch, tm in rows:
                fh.write(f"{name}\t{evr}\t{arch}\t{tm}\n")
    except Exception as exc:  # noqa: BLE001
        emit("errors", "snapshot_rpmdb_failed",
             {"tag": tag, "err": repr(exc)})


def dump_transaction_set(stage: str, event: str, ts) -> None:
    """Enumerate every te in ``ts`` and emit as one JSONL entry.

    Distinguishes ADDs (op == TR_ADDED) from REMOVEs (op == TR_REMOVED)
    and pairs them by ``(name, arch)`` to expose implicit-upgrade
    pairing — an ADD with a matching REMOVE means rpm will do the
    upgrade-and-erase in one step ; without match it will install
    alongside.
    """
    if not is_enabled():
        return
    try:
        import rpm as _rpm
        adds, removes = [], []
        for te in ts:
            entry = (te.N(), te.V(), te.R(), te.A())
            if te.Type() == _rpm.TR_ADDED:
                adds.append(entry)
            elif te.Type() == _rpm.TR_REMOVED:
                removes.append(entry)
        removes_by_key = {}
        for n, v, r, a in removes:
            removes_by_key.setdefault((n, a), []).append(f"{v}-{r}")
        paired, unpaired = [], []
        for n, v, r, a in adds:
            entry = f"{n}-{v}-{r}.{a}"
            if (n, a) in removes_by_key:
                paired.append(entry)
            else:
                unpaired.append(entry)
        emit(stage, event, {
            "adds_total": len(adds),
            "removes_total": len(removes),
            "adds_with_paired_remove": len(paired),
            "adds_without_paired_remove": len(unpaired),
            "unpaired_sample": unpaired[:50],
            "removes_sample": [
                f"{n}-{v}-{r}.{a}" for n, v, r, a in removes[:50]
            ],
        })
    except Exception as exc:  # noqa: BLE001
        emit("errors", f"{stage}_dump_ts_failed",
             {"event": event, "err": repr(exc)})


def dump_ts_problems(stage: str, event: str, problems) -> None:
    """Emit the raw ``ts.run()`` problem list.

    ``ts.run()`` returns None (success) or a list of ``rpm.problem``
    objects.  Each has ``.type``, ``.str`` etc.  We render as strings
    to survive JSON.
    """
    if not is_enabled():
        return
    try:
        rendered = []
        if problems:
            for p in problems:
                try:
                    rendered.append({
                        "type": int(getattr(p, "type", -1)),
                        "str": str(p),
                    })
                except Exception as exc:  # noqa: BLE001
                    rendered.append({"repr": repr(p), "err": repr(exc)})
        emit(stage, event, {
            "count": len(rendered),
            "problems": rendered,
        })
    except Exception as exc:  # noqa: BLE001
        emit("errors", f"{stage}_dump_problems_failed",
             {"event": event, "err": repr(exc)})


def dump_process_env(stage: str, event: str) -> None:
    """Emit process env + argv + cwd — helps trace execvp hand-over."""
    if not is_enabled():
        return
    try:
        import sys
        emit(stage, event, {
            "argv": list(sys.argv),
            "cwd": os.getcwd(),
            "python": sys.executable,
            "python_version": sys.version.split()[0],
            "env_urpm": {k: v for k, v in os.environ.items()
                         if "URPM" in k or "PYTHON" in k},
        })
    except Exception as exc:  # noqa: BLE001
        emit("errors", f"{stage}_dump_env_failed",
             {"event": event, "err": repr(exc)})
