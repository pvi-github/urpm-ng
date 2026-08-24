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

Turn on with ``urpm distupgrade --debug distupgrade`` (or
``--debug all``).  The CLI sets ``URPM_DUP_DIAG=1`` on the current
process ; ``os.execvp`` from Stage 3 propagates env, so the
post-execvp mga N+1 process picks the flag up and continues writing
into the same ``/var/log/dup-diag/`` tree.
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
    """Return True iff diagnostic dump is turned on.

    Wired to ``urpm distupgrade --debug distupgrade`` (or
    ``--debug all``) — the CLI calls :func:`enable` which sets the
    ``URPM_DUP_DIAG`` env var.  Env is used (rather than a module
    global) because ``os.execvp`` from Stage 3 propagates env : the
    post-execvp mga N+1 process sees the same setting without needing
    a second CLI arg.
    """
    return os.environ.get(_ENV_KEY, "") not in ("", "0", "false", "False")


def enable() -> None:
    """Turn diagnostic dump on for this process and every ``execvp``ed
    child.  Idempotent."""
    os.environ[_ENV_KEY] = "1"


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


class MetricsSampler:
    """Background sampler emitting cpu%/mem/disk/io every ``interval_s``.

    Started right before ``ts.run()`` and stopped right after.  Zero
    cost when ``URPM_DUP_DIAG`` is unset : ``start()`` returns a
    no-op instance whose ``stop()`` does nothing.

    Each sample is one JSONL line in
    ``/var/log/dup-diag/<stage>-metrics.jsonl`` with keys :

    - ``cpu_percent`` : 0-100, from ``/proc/stat`` delta
    - ``mem_avail_kb`` / ``mem_total_kb`` : from ``/proc/meminfo``
    - ``disk_free_root_kb`` : from ``os.statvfs('/')``
    - ``load1`` : from ``os.getloadavg()``
    - ``io_wait_pct`` : from ``/proc/stat`` iowait delta

    Read-only : parses ``/proc``, never mutates anything.  All errors
    swallowed — a broken sampler must not crash the transaction.
    """

    def __init__(self, stage: str, interval_s: float = 10.0):
        self.stage = stage
        self.interval_s = interval_s
        self._thread = None
        self._stop_evt = None
        self._prev_cpu = None
        self._prev_iowait = None
        self._prev_total = None

    def _read_cpu(self):
        try:
            with open("/proc/stat", "r") as fh:
                for line in fh:
                    if line.startswith("cpu "):
                        parts = line.split()[1:]
                        vals = [int(v) for v in parts[:10]]
                        total = sum(vals)
                        idle = vals[3]
                        iowait = vals[4] if len(vals) > 4 else 0
                        return total, idle, iowait
        except Exception:  # noqa: BLE001
            pass
        return None, None, None

    def _sample_once(self):
        try:
            payload = {}
            total, idle, iowait = self._read_cpu()
            if total is not None and self._prev_total is not None:
                dt = total - self._prev_total
                di = idle - self._prev_cpu
                dw = iowait - self._prev_iowait
                if dt > 0:
                    payload["cpu_percent"] = round(100 * (1 - di / dt), 1)
                    payload["io_wait_pct"] = round(100 * dw / dt, 1)
            self._prev_total = total
            self._prev_cpu = idle
            self._prev_iowait = iowait

            try:
                with open("/proc/meminfo", "r") as fh:
                    mem = {}
                    for line in fh:
                        k, _, v = line.partition(":")
                        v = v.strip().split()
                        if v:
                            mem[k] = int(v[0])
                    payload["mem_avail_kb"] = mem.get("MemAvailable", 0)
                    payload["mem_total_kb"] = mem.get("MemTotal", 0)
                    payload["swap_used_kb"] = mem.get(
                        "SwapTotal", 0) - mem.get("SwapFree", 0)
            except Exception:  # noqa: BLE001
                pass

            try:
                st = os.statvfs("/")
                payload["disk_free_root_kb"] = int(st.f_bavail * st.f_frsize / 1024)
                payload["disk_total_root_kb"] = int(st.f_blocks * st.f_frsize / 1024)
            except Exception:  # noqa: BLE001
                pass

            try:
                l1, l5, l15 = os.getloadavg()
                payload["load1"] = round(l1, 2)
                payload["load5"] = round(l5, 2)
                payload["load15"] = round(l15, 2)
            except Exception:  # noqa: BLE001
                pass

            emit(self.stage + "-metrics", "sample", payload)
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        # Prime the CPU delta baseline.
        total, idle, iowait = self._read_cpu()
        self._prev_total = total
        self._prev_cpu = idle
        self._prev_iowait = iowait
        while not self._stop_evt.is_set():
            self._sample_once()
            self._stop_evt.wait(self.interval_s)

    def start(self):
        if not is_enabled():
            return self
        try:
            import threading
            self._stop_evt = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="dup-diag-metrics", daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001
            pass
        return self

    def stop(self):
        if self._stop_evt is None:
            return
        try:
            self._stop_evt.set()
            if self._thread is not None:
                self._thread.join(timeout=5.0)
        except Exception:  # noqa: BLE001
            pass


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
