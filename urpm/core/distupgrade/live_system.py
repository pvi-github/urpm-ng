"""What the running system cannot afford to lose, and in what order.

``rpm`` guarantees consistency *within* a transaction: files stay in
place until it commits.  Tx B does not run as one transaction — it is
sliced into batches so the disk peak stays bounded.  A package erased
in batch 3 is gone from disk while batches 4..N are still running, and
the X server, the display manager and the terminal running the upgrade
are still using it.

That is not a dependency problem, so libsolv's topological order says
nothing about it: the order is correct for *packages*, and the running
system is a separate constraint.  Interleaving erases at their libsolv
position without this froze a tester's desktop mid-migration.

Deferring *every* erase to the last batch is not the answer either: it
is what doubled the on-disk footprint of a cross-release upgrade and
filled a tester's root partition.  Both failures are real and neither
is acceptable, so erases are ranked rather than treated all-or-nothing:

``TIER_INTERLEAVED``
    Not running.  Travels at its libsolv position and frees space as
    the migration proceeds.  This is the bulk — measured at 85% of the
    installed volume on a fully loaded desktop, and more on a machine
    actually prepared for an upgrade.

``TIER_SACRIFICIAL``
    Running, but nothing depends on it staying alive: Firefox,
    Thunderbird, LibreOffice.  Killing them loses unsaved work at
    worst; it endangers neither the machine nor the migration.
    Released just before the core, so their volume comes back while
    there is still something to gain from it.

``TIER_CRITICAL``
    Its death breaks the machine, the operator's access to it, or the
    upgrade itself.  Final batch only.

The critical set is derived from the running system wherever that is
possible, because a hard-coded list rots:

* **our own process and every ancestor up to pid 1** — this is what
  protects the terminal.  A Konsole is indistinguishable from Firefox
  by uid or by session; what saves it is being our ancestor.  ``su``
  preserves the parent chain, so ``su -`` inside a terminal still
  works, and any terminal works without being named.
* **pid 1 and its direct children** — the init and the system daemons:
  display manager, sshd, network manager.
* **anything running as uid 0** — catches an Xorg started by a
  display-manager helper rather than by init.

One case resists derivation: under Wayland the compositor and Xwayland
run as the session user, children of the session, exactly like an
application.  No process attribute separates them from Firefox, so they
are named — see ``SESSION_CRITICAL_LOCKS`` in :mod:`manifest`, as
Provides like every other package identity in this codebase.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Set

logger = logging.getLogger(__name__)

#: May travel at its libsolv position.
TIER_INTERLEAVED = 0
#: Running but expendable : released just before the core.
TIER_SACRIFICIAL = 1
#: Released only in the final batch.
TIER_CRITICAL = 2


@dataclass(frozen=True)
class LiveSystemSnapshot:
    """Package names ranked by what removing them would cost.

    Taken once at the start of Tx B and held for the whole transaction:
    a package in use when we started stays protected even if its
    process exits half-way, and a snapshot drifting between batches
    would make the ranking unreproducible.

    Recomputed on ``--resume``, since a reboot changes what is running.
    """

    critical: FrozenSet[str]
    sacrificial: FrozenSet[str]

    def tier(self, package_name: str) -> int:
        if package_name in self.critical:
            return TIER_CRITICAL
        if package_name in self.sacrificial:
            return TIER_SACRIFICIAL
        return TIER_INTERLEAVED


def _proc_stat(pid: int) -> tuple:
    """``(comm, ppid)`` for *pid*, or ``(None, None)``.

    ``comm`` can contain spaces and parentheses, so the field is cut on
    the *last* ``)`` rather than split naively.
    """
    try:
        with open(f"/proc/{pid}/stat") as handle:
            data = handle.read()
        close = data.rindex(")")
        return data[data.index("(") + 1:close], int(data[close + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None, None


def _live_pids() -> List[int]:
    return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]


def _ancestors(pid: int) -> Set[int]:
    """*pid* and every parent up to init.

    Bounded by the visited set: a malformed /proc must not spin here,
    this runs at the head of a migration.
    """
    chain: Set[int] = set()
    current = pid
    while current and current not in chain:
        chain.add(current)
        if current == 1:
            break
        _comm, parent = _proc_stat(current)
        if parent is None:
            break
        current = parent
    return chain


def _critical_pids() -> Set[int]:
    """Processes whose death breaks the machine, access to it, or us."""
    pids = set(_ancestors(os.getpid()))
    pids.add(1)
    for pid in _live_pids():
        _comm, parent = _proc_stat(pid)
        if parent == 1:
            pids.add(pid)          # system daemons
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid == 0:
                pids.add(pid)      # Xorg under a display-manager helper
        except OSError:
            pass
    return pids


def _mapped_files(pids: Iterable[int]) -> Set[str]:
    """Absolute paths each process is executing or has mapped."""
    files: Set[str] = set()
    for pid in pids:
        try:
            files.add(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/maps") as handle:
                for line in handle:
                    fields = line.rstrip("\n").split(None, 5)
                    if len(fields) == 6 and fields[5].startswith("/"):
                        files.add(fields[5])
        except OSError:
            pass
    return files


def _owners(paths: Iterable[str], root: str = "/") -> Set[str]:
    """Package names owning *paths*, via the rpmdb.

    File paths live under ``RPMTAG_BASENAMES``; querying
    ``RPMTAG_PROVIDENAME`` for a path silently returns nothing, which
    would leave this whole module blind.
    """
    import rpm  # noqa: PLC0415 — matches the local-import idiom here
    from ..rpmdb import open_ts

    names: Set[str] = set()
    with open_ts(root) as ts:
        for path in paths:
            for header in ts.dbMatch(rpm.RPMTAG_BASENAMES, path):
                name = header[rpm.RPMTAG_NAME]
                names.add(name.decode() if isinstance(name, bytes) else name)
    return names


def _locked_by_manifest(root: str = "/") -> Set[str]:
    """Packages named by the manifest's never-remove capability lists.

    ``BOOT_CRITICAL_LOCKS_*`` has documented itself as a hard floor
    since it was written, and nothing ever consulted it.
    ``SESSION_CRITICAL_LOCKS`` covers what process attributes cannot
    separate.
    """
    import rpm  # noqa: PLC0415
    from ..rpmdb import open_ts
    from .manifest import SESSION_CRITICAL_LOCKS, boot_critical_locks

    names: Set[str] = set()
    capabilities = list(boot_critical_locks()) + list(SESSION_CRITICAL_LOCKS)
    with open_ts(root) as ts:
        for capability in capabilities:
            tag = (rpm.RPMTAG_BASENAMES if capability.startswith("/")
                   else rpm.RPMTAG_PROVIDENAME)
            for header in ts.dbMatch(tag, capability):
                name = header[rpm.RPMTAG_NAME]
                names.add(name.decode() if isinstance(name, bytes) else name)
    return names


def snapshot(root: str = "/") -> LiveSystemSnapshot:
    """Rank every running package by what losing it would cost.

    Best-effort by construction: a /proc that cannot be read yields a
    smaller critical set, never a wrong ranking, and the caller still
    gets a usable snapshot.  Costs about half a second on a loaded
    desktop — 1147 mapped files, 657 owning packages, measured.
    """
    try:
        critical_pids = _critical_pids()
        all_pids = set(_live_pids())
    except OSError as exc:  # pragma: no cover - /proc unavailable
        logger.warning("cannot read /proc, no erase will be deferred: %s", exc)
        return LiveSystemSnapshot(critical=frozenset(), sacrificial=frozenset())

    critical = _owners(_mapped_files(critical_pids), root)
    critical |= _locked_by_manifest(root)
    running = _owners(_mapped_files(all_pids - critical_pids), root)

    logger.info(
        "live system: %d critical package(s), %d expendable running package(s)",
        len(critical), len(running - critical))
    return LiveSystemSnapshot(
        critical=frozenset(critical),
        sacrificial=frozenset(running - critical),
    )
