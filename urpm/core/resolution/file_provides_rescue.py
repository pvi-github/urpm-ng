"""Reactive file-provides rescue for distupgrade resolution.

Chest-pack measure — not a permanent architectural feature.

Why this exists
---------------

Mageia's ``synthesis.hdlist.cz`` — the compact repo metadata urpm-ng
feeds to libsolv — deliberately omits file-provides such as
``/usr/bin/date`` or ``/usr/bin/pwscore``.  The full per-package
file list lives in a separate ``files.xml.lzma`` sidecar and is
never loaded into the solver pool.

In an ordinary ``urpm install`` this trou is invisible : the
installed rpmdb (``@System``) exposes every file its installed
packages own, so a target-media package that declares
``Requires: /usr/bin/date`` finds a provider through the installed
set.

A full-system distupgrade breaks that invariant.  Running
``SOLVER_DISTUPGRADE | SOLVER_SOLVABLE_ALL`` with
``SOLVER_FLAG_ALLOW_UNINSTALL`` mentally replaces the entire mga9
world with its mga10 counterpart ; the mga9 file-provides disappear
from the reachable set, and any mga10 package whose Requires depends
on a path only mga9 supplied becomes unresolvable.  libsolv reacts
by silently ejecting that package (and everything transitively
depending on it) from the transaction.

Observed on mga9→mga10 : ``cockpit-system`` requires
``/usr/bin/date`` and ``/usr/bin/pwscore``, ``elograf`` / ``kgeotag``
/ ``qrca`` require ``/usr/bin/xdg-open``.  Their loss then cascades
into ``cockpit`` and ``cockpit-networkmanager`` via cockpit-shell.

urpmi's Perl resolver has the same trou at the metadata level but is
permissive enough to sidestep it ; libsolv is stricter, so we
compensate here.  The long-term fix belongs upstream in Mageia's
``genhdlist2`` — teach it to expose file-provides that appear in any
package's Requires, exactly the rule DNF's ``primary.xml`` follows.

What this pass does
-------------------

Purely reactive : the module is dormant unless the first DUP solve
has already produced silent-drop victims.  When it fires :

1. Enumerate installed solvables the transaction marks ERASE with no
   paired target upgrade — while a same-Name target exists in the
   pool.  These are the silent drops.
2. Collect their target counterparts' unmet file-Requires (paths
   starting with ``/`` whose ``pool.whatprovides`` returns nothing
   outside ``@System``).
3. Stream each target-media ``files.xml.lzma`` once, filtering only
   on the collected paths, and register the missing paths as
   Provides on the matching target solvables.
4. Rebuild ``whatprovides`` and hand the caller back a re-solved
   transaction.

Cost is zero when nothing was dropped (early-exit before opening any
file).  When active, cost is one narrow-filter pass through each
target media's ``files.xml.lzma`` — hundreds of milliseconds on a
typical Mageia release.

Scope
-----

Wired only into :meth:`Resolver.resolve_distupgrade`.  Extension to
``resolve_install`` / ``resolve_upgrade`` is tracked separately :
those paths keep the installed set intact so ``@System`` still
covers the file-Requires in practice.
"""

import logging
import lzma
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from xml.etree.ElementTree import iterparse

import solv

logger = logging.getLogger(__name__)


def _lookup_all_requires(solvable) -> list:
    """Return Requires including entries after the PREREQ marker.

    Duplicated from :func:`urpm.core.resolution.pool.lookup_all_requires`
    to avoid a circular import ; the two must stay in sync.
    """
    normal = solvable.lookup_deparray(solv.SOLVABLE_REQUIRES)
    prereq = solvable.lookup_deparray(
        solv.SOLVABLE_REQUIRES, solv.SOLVABLE_PREREQMARKER,
    )
    return list(normal) + list(prereq)


def find_silent_drops(
    pool: solv.Pool, trans,
) -> List[Tuple['solv.Solvable', 'solv.Solvable']]:
    """Return ``(installed, target_counterpart)`` pairs the solver dropped silently.

    An installed package counts as a silent drop when the transaction
    marks it ERASE, ``trans.othersolvable(installed)`` returns None
    (no paired upgrade), and at least one same-Name package exists in
    a non-installed repo.  Pairs are returned with the highest-EVR
    target as the counterpart.
    """
    installed_repo = pool.installed
    if installed_repo is None:
        return []
    # Compare by numeric id : SWIG hands us a fresh Python proxy at
    # every ``.repo`` / ``pool.installed`` access, so ``is`` never
    # matches even for the same underlying C Repo.
    installed_id = installed_repo.id

    target_by_name: Dict[str, List['solv.Solvable']] = {}
    for s in pool.solvables_iter():
        if s.repo.id == installed_id:
            continue
        target_by_name.setdefault(s.name, []).append(s)

    drops: List[Tuple['solv.Solvable', 'solv.Solvable']] = []
    for step in trans.steps():
        if step.repo.id != installed_id:
            continue
        step_type = trans.steptype(
            step, solv.Transaction.SOLVER_TRANSACTION_RPM_ONLY,
        )
        if step_type != solv.Transaction.SOLVER_TRANSACTION_ERASE:
            continue
        if trans.othersolvable(step) is not None:
            continue
        candidates = target_by_name.get(step.name)
        if not candidates:
            continue
        # Pick the highest EVR — the solver would have preferred it too.
        target = max(candidates, key=lambda x: x.evrid)
        drops.append((step, target))
    return drops


def collect_unmet_file_requires(
    pool: solv.Pool, targets: Iterable['solv.Solvable'],
) -> Set[str]:
    """Gather target-side file-Requires that have no non-installed provider.

    File-Requires (paths starting with ``/``) are the sole class this
    rescue can address ; anything else falls outside the ``files.xml``
    fix and is left to a future extension.  The installed repo is
    excluded from the provider check on purpose — the whole point is
    that installed provides vanish once the DUP swaps the world.
    """
    installed_id = pool.installed.id if pool.installed else -1
    unmet: Set[str] = set()
    for target in targets:
        for dep in _lookup_all_requires(target):
            path = str(dep)
            if not path.startswith('/'):
                continue
            providers = pool.whatprovides(pool.str2id(path, 0))
            if any(p.repo.id != installed_id for p in providers):
                continue
            unmet.add(path)
    return unmet


def scan_files_xml(
    files_xml_path: Path, paths_of_interest: Set[str],
) -> Dict[str, List[str]]:
    """Stream one ``files.xml.lzma`` and return NEVRA → matched paths.

    Iterates the XML in end-only mode via ``iterparse`` and clears
    every parsed element to keep memory constant.  Only paths that
    appear in ``paths_of_interest`` are retained, so the returned
    dict stays small even on the largest media.
    """
    result: Dict[str, List[str]] = {}
    if not paths_of_interest or not files_xml_path.exists():
        return result

    try:
        stream = lzma.open(str(files_xml_path), 'rb')
    except OSError as exc:
        logger.warning("cannot open %s: %s", files_xml_path, exc)
        return result

    with stream:
        try:
            for _event, elem in iterparse(stream, events=("end",)):
                if elem.tag != "files":
                    continue
                nevra = elem.get("fn")
                text = elem.text or ""
                matched = [
                    line for line in text.splitlines()
                    if line in paths_of_interest
                ]
                if nevra and matched:
                    result[nevra] = matched
                elem.clear()
        except Exception as exc:  # noqa: BLE001 — best-effort, log & bail
            logger.warning("parse error on %s: %s", files_xml_path, exc)
    return result


def inject_provides(
    pool: solv.Pool, nevra_to_paths: Dict[str, List[str]],
) -> int:
    """Register each ``(nevra, [paths])`` as a file-Provides on its solvable.

    Only target (non-installed) solvables are eligible.  Returns the
    number of ``(solvable, path)`` pairs actually injected ; caller
    uses this to decide whether ``createwhatprovides()`` needs to run
    again.
    """
    if not nevra_to_paths:
        return 0

    installed_id = pool.installed.id if pool.installed else -1
    by_nevra: Dict[str, 'solv.Solvable'] = {}
    for s in pool.solvables_iter():
        if s.repo.id == installed_id:
            continue
        by_nevra[f"{s.name}-{s.evr}.{s.arch}"] = s

    injected = 0
    for nevra, paths in nevra_to_paths.items():
        target = by_nevra.get(nevra)
        if target is None:
            continue
        for path in paths:
            target.add_deparray(
                solv.SOLVABLE_PROVIDES, pool.Dep(path),
            )
            injected += 1
    return injected


def gather_paths_from_media(
    media_paths: Iterable[Path], paths_of_interest: Set[str],
) -> Dict[str, List[str]]:
    """Aggregate ``scan_files_xml`` results across every target media.

    ``media_paths`` are the media root directories — the sidecar is
    found at ``<media_path>/media_info/files.xml.lzma``.  Missing
    sidecars are silently skipped (some third-party media publish no
    files.xml at all).
    """
    merged: Dict[str, List[str]] = {}
    for media_path in media_paths:
        sidecar = media_path / "media_info" / "files.xml.lzma"
        found = scan_files_xml(sidecar, paths_of_interest)
        for nevra, paths in found.items():
            merged.setdefault(nevra, []).extend(paths)
    return merged
