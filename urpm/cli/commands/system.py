"""`urpm system export` / `urpm system import` — clone a machine's
package selection + media/server catalogue onto another.

Export writes a JSON snapshot ; import reads one, backs up the current
state, then reconciles servers / media / packages towards the target.
See :mod:`urpm.core.system_profile` for the format and the diff logic.
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from ...i18n import _, confirm_yes
from .. import colors
from ...core import system_profile as sp

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _default_export_path() -> Path:
    """Default filename : ``system-<hostname>-<UTC-timestamp>.json`` in
    the user's home directory.  Timestamp keeps every dump unique."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return Path.home() / f"system-{socket.gethostname()}-{stamp}.json"


def cmd_system_export(args, db: 'PackageDatabase') -> int:
    """Serialise the current system to JSON at ``args.to``."""
    out_path = Path(args.to) if args.to else _default_export_path()

    print(colors.info(_("Exporting system state to {path}...").format(
        path=out_path)))
    try:
        profile = sp.export_profile(db)
    except Exception as exc:  # noqa: BLE001
        print(colors.error(_(
            "Export failed: {err}").format(err=exc)))
        return 1

    try:
        sp.save_profile(profile, out_path)
    except OSError as exc:
        print(colors.error(_(
            "Cannot write {path}: {err}").format(
                path=out_path, err=exc)))
        return 1

    n_expl = len(profile["packages"]["explicit"])
    n_dep = len(profile["packages"]["dependency"])
    n_br = len(profile["packages"]["buildrequires"])
    n_media = len(profile["media"])
    n_srv = len(profile["servers"])
    print(colors.success(_(
        "  {srv} server(s), {media} media, "
        "{expl} explicit / {dep} dependency / {br} buildrequires "
        "packages.").format(
            srv=n_srv, media=n_media,
            expl=n_expl, dep=n_dep, br=n_br)))
    print(_("Wrote {path}").format(path=out_path))
    return 0


# ---------------------------------------------------------------------------
# Import — helpers
# ---------------------------------------------------------------------------


def _render_summary(diff: sp.ProfileDiff) -> None:
    """Print the diff a user needs to see before confirming the apply."""
    print()
    print(colors.bold(_("Import plan")))

    def _section(title: str, added: list, removed: list) -> None:
        if not added and not removed:
            return
        print()
        print(colors.info(f"  {title}"))
        if added:
            print(colors.success(_(
                "    + add ({n})").format(n=len(added))))
            for row in added[:15]:
                print(f"        {row.get('name', row)}")
            if len(added) > 15:
                print(colors.dim(_(
                    "        (+ {n} more)").format(
                        n=len(added) - 15)))
        if removed:
            print(colors.error(_(
                "    - remove ({n})").format(n=len(removed))))
            for row in removed[:15]:
                print(f"        {row.get('name', row)}")
            if len(removed) > 15:
                print(colors.dim(_(
                    "        (+ {n} more)").format(
                        n=len(removed) - 15)))

    _section(_("Servers"), diff.servers.to_add, diff.servers.to_remove)
    _section(_("Media"), diff.media.to_add, diff.media.to_remove)

    pkgs = diff.packages
    to_install = (pkgs.install_explicit + pkgs.install_dependency
                  + pkgs.install_buildrequires)
    if to_install or pkgs.remove:
        print()
        print(colors.info(_("  Packages")))
        if to_install:
            print(colors.success(_(
                "    + install ({n} = {e} explicit + {d} deps + {b} BR)"
            ).format(
                n=len(to_install),
                e=len(pkgs.install_explicit),
                d=len(pkgs.install_dependency),
                b=len(pkgs.install_buildrequires))))
            for name in to_install[:15]:
                print(f"        {name}")
            if len(to_install) > 15:
                print(colors.dim(_(
                    "        (+ {n} more)").format(
                        n=len(to_install) - 15)))
        if pkgs.remove:
            print(colors.error(_(
                "    - remove ({n} not in target profile)"
            ).format(n=len(pkgs.remove))))
            for name in pkgs.remove[:15]:
                print(f"        {name}")
            if len(pkgs.remove) > 15:
                print(colors.dim(_(
                    "        (+ {n} more)").format(
                        n=len(pkgs.remove) - 15)))

    warnings = (diff.servers.warnings + diff.media.warnings
                + diff.packages.warnings)
    if warnings:
        print()
        print(colors.warning(_("  Warnings")))
        for w in warnings:
            print(f"    {colors.dim(w)}")

    print()


def _apply_servers(db, diff: sp.SectionDiff) -> int:
    """Apply the server-section diff.  Returns the number of successful
    mutations ; on failure logs the specific server + continues."""
    n = 0
    for srv in diff.to_remove:
        name = srv.get("name")
        try:
            db.remove_server(name)
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(colors.error(_(
                "  server remove failed for '{name}': {err}").format(
                    name=name, err=exc)))
    for srv in diff.to_add:
        try:
            db.add_server(
                name=srv["name"],
                protocol=srv.get("protocol", "https"),
                host=srv.get("host", ""),
                base_path=srv.get("base_path", ""),
                is_official=bool(srv.get("is_official", True)),
                enabled=bool(srv.get("enabled", True)),
                priority=int(srv.get("priority", 50)),
                country=srv.get("country"),
                url_version=srv.get("url_version"),
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(colors.error(_(
                "  server add failed for '{name}': {err}").format(
                    name=srv.get("name"), err=exc)))
    return n


def _apply_media(db, diff: sp.SectionDiff) -> int:
    """Apply the media-section diff."""
    n = 0
    for m in diff.to_remove:
        try:
            db.remove_media(m["name"])
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(colors.error(_(
                "  media remove failed for '{name}': {err}").format(
                    name=m.get("name"), err=exc)))
    for m in diff.to_add:
        try:
            media_id = db.add_media(
                name=m["name"],
                short_name=m.get("short_name") or m["name"],
                mageia_version=m.get("mageia_version") or "",
                architecture=m.get("architecture") or "",
                relative_path=m.get("relative_path") or "",
                is_official=bool(m.get("is_official", True)),
                allow_unsigned=bool(m.get("allow_unsigned", False)),
                enabled=bool(m.get("enabled", True)),
                update_media=bool(m.get("update_media", False)),
                priority=int(m.get("priority", 50)),
                url=m.get("url"),
                mirrorlist=m.get("mirrorlist"),
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(colors.error(_(
                "  media add failed for '{name}': {err}").format(
                    name=m.get("name"), err=exc)))
            continue

        # Re-establish the server_media links so ``urpm media update``
        # has a server to pull the metadata from.  Without this the
        # media row is orphan and sync fails with « No server available ».
        for server_name in m.get("server_links") or []:
            srv = db.get_server(server_name)
            if srv is None:
                print(colors.warning(_(
                    "  media '{m}' expected server '{s}' but it is "
                    "not present in this DB — link skipped").format(
                        m=m.get("name"), s=server_name)))
                continue
            try:
                if not db.server_media_link_exists(srv["id"], media_id):
                    db.link_server_media(srv["id"], media_id)
            except Exception as exc:  # noqa: BLE001
                print(colors.warning(_(
                    "  link '{s}' → '{m}' failed: {err}").format(
                        s=server_name, m=m.get("name"), err=exc)))

    # Fill the official server ↔ media mesh.  Papoteur-style JSON
    # exports produced before ``server_links`` was serialised carry
    # zero links ; and even fresh exports lose their meaning here
    # because the SOURCE machine's servers are being *removed* on the
    # target.  The mesh call re-pairs every official media with every
    # official server present, matching what ``urpm init`` builds on
    # a first-run install and what makes ``urpm media update`` find
    # something to pull from.  Custom (non-official) rows keep their
    # explicit topology untouched.
    try:
        added = db.link_official_mesh()
        if added:
            print(colors.info(_(
                "  official server↔media mesh: {n} link(s) added"
            ).format(n=added)))
    except Exception as exc:  # noqa: BLE001
        print(colors.warning(_(
            "  official mesh refresh failed: {err}").format(err=exc)))
    return n


def _restore_reason_files(target_packages: dict, root: str = "/") -> None:
    """Rewrite ``/var/lib/rpm/installed-through-{deps,builddeps}.list``
    from the imported profile.

    These flat files are the single source of truth for install-reason
    classification (:mod:`urpm.core.resolution.orphans`).  Overwriting
    them here is what makes future ``urpm autoremove`` behave the same
    on the target as on the source.
    """
    deps_path = Path(root) / sp.DEPS_LIST_REL
    br_path = Path(root) / sp.BUILDDEPS_LIST_REL

    deps = sorted(set(target_packages.get("dependency") or []))
    br = target_packages.get("buildrequires") or {}

    try:
        deps_path.parent.mkdir(parents=True, exist_ok=True)
        deps_path.write_text(
            "\n".join(deps) + ("\n" if deps else ""))
    except OSError as exc:
        print(colors.warning(_(
            "  could not rewrite {path}: {err}").format(
                path=deps_path, err=exc)))

    try:
        br_path.parent.mkdir(parents=True, exist_ok=True)
        br_path.write_text(
            "\n".join(
                f"{name}\t{source}"
                for name, source in sorted(br.items())
            ) + ("\n" if br else ""))
    except OSError as exc:
        print(colors.warning(_(
            "  could not rewrite {path}: {err}").format(
                path=br_path, err=exc)))


class _ApplyResult:
    """Outcome of a single install-or-erase phase, consumed by
    :func:`_render_delta_report` to surface the exact drift."""

    def __init__(self):
        self.applied: List[str] = []
        self.transaction_error: Optional[str] = None
        # Names dropped from the erase list because kept pkgs still
        # need them (see :func:`_rescue_kept_deps`).  Set on the erase
        # phase only ; unused on install.
        self.rescued: List[Tuple[str, str, str]] = []
        # Names dropped from the erase list because they are
        # hardware-specific packages the target machine needs to keep
        # (see :func:`_detect_hw_specific`).  Set on the erase phase
        # only ; unused on install.
        self.hw_preserved: List[str] = []


# ---------------------------------------------------------------------------
# Hardware-specific package detection
# ---------------------------------------------------------------------------

# Regexes matching packages the target machine likely NEEDS regardless of
# what the source profile says.  A clone from a physical box onto a VM
# (or vice versa) would otherwise strip guest tools, kernel modules and
# firmware the target hardware / hypervisor depends on — a distupgrade
# will then boot into a broken graphics stack.
#
# Concrete failure mode observed (2026-08-22) : source profile lacks
# ``dkms-virtualbox``.  System import removes it on a VirtualBox target ;
# distupgrade installs a new kernel ; the ``vboxvideo`` DRM module is
# never rebuilt for that new kernel ; Xorg falls back to ``vmwgfx``,
# which reports ``device max cursor size = 0``.  gnome-shell spam-loops
# ``drmModeAtomicCommit`` failures, session hangs and dies.
#
# The list stays conservative — better to preserve a redundant package
# than to leave the target machine unbootable.  Add patterns here as
# new hardware categories surface.
_HW_SPECIFIC_PATTERNS: Tuple[str, ...] = (
    r'^dkms-.*',                        # DKMS kernel modules (drivers)
    r'^virtualbox-guest-additions.*',   # VirtualBox guest tools
    r'^open-vm-tools.*',                # VMware guest tools
    r'^spice-vdagent.*',                # QEMU/SPICE guest agent
    r'^qemu-guest-agent.*',             # QEMU guest agent
    r'^x11-driver-video-.*',            # Xorg video drivers (per-GPU)
    r'^x11-driver-input-.*',            # Xorg input drivers (touchpad,
                                        #                     wacom, ...)
    r'^kernel-firmware-.*',             # Firmware blobs (nonfree, iwlwifi,
                                        #                 amdgpu, ...)
    r'^microcode-.*',                   # CPU microcode
    r'^amd-ucode.*',                    # AMD microcode variants
    r'^nvidia.*',                       # NVIDIA proprietary drivers
    r'^lib64nvidia.*',                  # NVIDIA proprietary libs
    r'^broadcom-wl.*',                  # Broadcom WiFi
    r'^bumblebee.*',                    # Optimus dual-GPU
    r'^radeon-firmware.*',              # Radeon firmware (if outside
                                        # kernel-firmware pattern)
)
_HW_SPECIFIC_RE = re.compile(
    '|'.join(f'(?:{p})' for p in _HW_SPECIFIC_PATTERNS))


def _detect_hw_specific(names: List[str]) -> List[str]:
    """Return the subset of *names* matching any hardware-specific
    pattern (see :data:`_HW_SPECIFIC_PATTERNS`).  Preserves the input
    ordering so the caller can present the list to the user in the
    same order as the erase plan.
    """
    return [n for n in names if _HW_SPECIFIC_RE.match(n)]


def _render_preflight_gaps(install_missing: List[str],
                           erase_missing: List[str]) -> None:
    """Print the packages the profile references that are neither in
    the pool nor in rpmdb.  Called before asking the user whether to
    proceed anyway."""
    if not install_missing and not erase_missing:
        return
    print()
    print(colors.warning(_("Pre-flight gap detected")))
    if install_missing:
        print(colors.warning(_(
            "  {n} package(s) in the profile are not in any enabled "
            "medium's pool (cannot be installed) :").format(
                n=len(install_missing))))
        for name in install_missing[:15]:
            print(f"    {colors.dim(name)}")
        if len(install_missing) > 15:
            print(colors.dim(_(
                "    (+ {n} more)").format(
                    n=len(install_missing) - 15)))
    if erase_missing:
        print(colors.warning(_(
            "  {n} package(s) in the profile's remove-set are not "
            "currently installed (nothing to erase) :").format(
                n=len(erase_missing))))
        for name in erase_missing[:15]:
            print(f"    {colors.dim(name)}")
        if len(erase_missing) > 15:
            print(colors.dim(_(
                "    (+ {n} more)").format(
                    n=len(erase_missing) - 15)))
    print()


def _render_delta_report(*,
                        install_planned: List[str],
                        install_missing: List[str],
                        install_result: _ApplyResult,
                        erase_planned: List[str],
                        erase_missing: List[str],
                        erase_result: _ApplyResult,
                        allow_incoherent: bool = False) -> None:
    """Final summary block : what happened vs what was requested.
    Cloning is an exact-alignment operation ; any drop-off is
    surfaced prominently so the user can act."""
    print()
    print(colors.bold(_("Delta summary")))

    n_inst_ok = len(install_result.applied)
    n_inst_plan = len(install_planned)
    print(colors.info(_(
        "  Install : {ok}/{plan} applied").format(
            ok=n_inst_ok, plan=n_inst_plan)))
    if install_missing:
        print(colors.warning(_(
            "    - {n} not in pool (skipped) :").format(
                n=len(install_missing))))
        for name in install_missing[:10]:
            print(f"        {colors.dim(name)}")
        if len(install_missing) > 10:
            print(colors.dim(_(
                "        (+ {n} more)").format(
                    n=len(install_missing) - 10)))
    if install_result.transaction_error:
        print(colors.error(_(
            "    - transaction refused : {err}").format(
                err=install_result.transaction_error)))

    n_er_ok = len(erase_result.applied)
    n_er_plan = len(erase_planned)
    mode_note = ""
    if allow_incoherent:
        mode_note = "  " + _("[--allow-incoherent : forced, no rescue]")
    print(colors.info(_(
        "  Erase   : {ok}/{plan} applied").format(
            ok=n_er_ok, plan=n_er_plan) + mode_note))
    if erase_missing:
        print(colors.warning(_(
            "    - {n} not in rpmdb (skipped) :").format(
                n=len(erase_missing))))
        for name in erase_missing[:10]:
            print(f"        {colors.dim(name)}")
        if len(erase_missing) > 10:
            print(colors.dim(_(
                "        (+ {n} more)").format(
                    n=len(erase_missing) - 10)))
    if erase_result.rescued:
        rescued_names = sorted({r[2] for r in erase_result.rescued})
        print(colors.warning(_(
            "    - {n} rescued from erase (source profile "
            "incoherent — kept-pkgs still need them) :").format(
                n=len(rescued_names))))
        for name in rescued_names[:10]:
            print(f"        {colors.dim(name)}")
        if len(rescued_names) > 10:
            print(colors.dim(_(
                "        (+ {n} more)").format(
                    n=len(rescued_names) - 10)))
    if erase_result.hw_preserved:
        # Full list — the user explicitly authorised the preservation
        # (or opted in via --preserve-hw), no reason to truncate.
        print(colors.warning(_(
            "    - {n} hardware-specific package(s) preserved :").format(
                n=len(erase_result.hw_preserved))))
        for name in erase_result.hw_preserved:
            print(f"        {colors.dim(name)}")
    if erase_result.transaction_error:
        print(colors.error(_(
            "    - transaction refused : {err}").format(
                err=erase_result.transaction_error)))
    print()


def _filter_installable(db, names: list) -> tuple:
    """Split *names* into ``(known, skipped)`` where *known* is what
    the urpm-ng pool actually contains (case-sensitive Name match) and
    *skipped* is everything else.  Uses a single indexed SQL query
    against the packages table — O(N) DB round-trip regardless of
    list size."""
    if not names:
        return [], []
    placeholders = ",".join("?" * len(names))
    conn = db._get_connection()
    rows = conn.execute(
        f"SELECT DISTINCT name FROM packages WHERE name IN ({placeholders})",
        names,
    ).fetchall()
    found = {r[0] for r in rows}
    known = [n for n in names if n in found]
    skipped = [n for n in names if n not in found]
    return known, skipped


def _filter_removable(names: list) -> list:
    """Return the subset of *names* actually present in the rpmdb.

    Thin wrapper around :func:`urpm.core.rpmdb.is_installed` — kept
    as a local helper because callers want a stable-order list, not
    the set returned by the low-level function (delta reports rely
    on the ordering matching the source list).

    Case-sensitive : rpm names are case-sensitive.
    """
    if not names:
        return []
    from ...core import rpmdb
    installed = rpmdb.is_installed(tuple(names))
    return [n for n in names if n in installed]


def _rescue_kept_deps(
        erase_names: List[str]
) -> Tuple[List[str], List[Tuple[str, str, str]]]:
    """Drop from *erase_names* any provider still required by a pkg
    slated to stay installed.

    The source profile can be *incoherent* : list ``rhythmbox`` in the
    kept-set without listing ``libsoup3`` (which the local rhythmbox
    build needs at runtime).  Naïvely erasing libsoup3 leaves
    rhythmbox broken and the atomic RPM erase refuses the whole
    transaction at pre-check time.  Clone integrity outranks strict
    profile matching : we quietly keep the provider and print a
    visible warning about the profile gap.

    Algorithm — iterative closure :
        1. Read the live rpmdb once via two ``rpm -qa`` subprocess
           calls (Provides then Requires).  Bulk output, tabbed,
           parsed in Python — O(N) I/O, zero per-package overhead.
        2. Compute ``kept = installed − erase``.
        3. For each kept pkg, check every Require.  If **all**
           providers of that Require are in the erase set, rescue
           the alphabetically-first one (user-approved tie-break).
        4. Repeat until no new rescue — a rescued pkg has its own
           Requires that may in turn need rescuing.

    Args:
        erase_names: Names planned for removal (case-sensitive).

    Returns:
        Tuple ``(adjusted_erase, rescues)`` where ``adjusted_erase``
        is the input minus rescued names, and ``rescues`` is
        ``[(kept_pkg, missing_dep, rescued_provider), ...]``,
        preserving order of discovery for reporting.
    """
    if not erase_names:
        return [], []

    from ...core import rpmdb
    try:
        graph = rpmdb.get_provides_and_requires()
    except RuntimeError:
        # If we can't read rpmdb, don't rescue anything — better a
        # failed erase we can explain than silent tampering.
        return list(erase_names), []

    # Provides index : dep name → sorted list of (provider_pkg, PkgDep).
    # We keep the PkgDep alongside the name so :func:`rpmdb.satisfies`
    # can compare full versioned provides against versioned requires
    # (name-only matching would incorrectly consider ``lib64gcr-gir4``
    # a substitute for ``lib64gcr-gir3`` on a require of ``typelib(Gcr)
    # = 3``, defeating the whole point of the rescue).  Sorted by
    # provider pkg name for stable alpha tie-break on the rescue pick.
    providers_of: Dict[str, List[Tuple[str, "rpmdb.PkgDep"]]] = {}
    for pkg, deps in graph.items():
        for prov in deps.provides:
            providers_of.setdefault(prov.name, []).append((pkg, prov))
    for lst in providers_of.values():
        lst.sort(key=lambda pair: pair[0])

    all_installed: Set[str] = set(graph.keys())
    # Requires map : pkg → full versioned :class:`rpmdb.PkgDep` tuples.
    requires_of: Dict[str, Tuple["rpmdb.PkgDep", ...]] = {
        pkg: deps.requires for pkg, deps in graph.items()
    }

    erase_set: Set[str] = set(erase_names)
    rescued: Set[str] = set()
    rescues: List[Tuple[str, str, str]] = []

    while True:
        newly_rescued: Set[str] = set()
        # kept = every installed pkg that isn't (still) marked for
        # removal.  A rescued pkg re-enters kept on the next round.
        kept = all_installed - (erase_set - rescued)
        for pkg in kept:
            for req in requires_of.get(pkg, ()):
                candidates = providers_of.get(req.name)
                if not candidates:
                    # Dep is already unresolved even without our
                    # intervention — not our problem to invent one.
                    continue
                # A provider that (a) survives the erase and (b) actually
                # satisfies the require (name + version compare via
                # :func:`rpmdb.satisfies`) means the dep is fine ; else
                # we must rescue one that does satisfy.  This is the
                # version-aware step that name-only rescue got wrong
                # for e.g. ``typelib(Gcr) = 3`` where a name-alone
                # provider (``lib64gcr-gir4``, version 4) does NOT
                # satisfy a require pinned to version 3.
                surviving_satisfiers = [
                    (p, prov) for (p, prov) in candidates
                    if (p not in erase_set or p in rescued)
                    and rpmdb.satisfies(prov, req)
                ]
                if surviving_satisfiers:
                    continue
                # No surviving provider satisfies : pick from the
                # candidates that do satisfy but are marked for erase,
                # alpha-first (stable tie-break, user-approved).
                erased_satisfiers = [
                    (p, prov) for (p, prov) in candidates
                    if p in erase_set and p not in rescued
                    and rpmdb.satisfies(prov, req)
                ]
                if not erased_satisfiers:
                    # No candidate satisfies the require — the dep was
                    # unresolvable even before we came in, upstream
                    # profile / rpmdb inconsistency.  Nothing to do.
                    continue
                pick_pkg = erased_satisfiers[0][0]  # already alpha-sorted
                if pick_pkg in newly_rescued or pick_pkg in rescued:
                    continue
                newly_rescued.add(pick_pkg)
                rescues.append((pkg, str(req), pick_pkg))
        if not newly_rescued:
            break
        rescued |= newly_rescued

    adjusted = [n for n in erase_names if n not in rescued]
    return adjusted, rescues


def _run_urpm(argv: list) -> int:
    """Invoke ``/usr/bin/urpm`` in a child process.  Used to reuse the
    battle-tested install / remove pipelines without importing their
    command modules and reconstructing argparse namespaces.  No new
    flag is introduced — all args here are existing public CLI
    surface, so we don't hit the auto-referent-subprocess rule."""
    cmd = ["/usr/bin/urpm"] + argv
    try:
        rc = subprocess.call(cmd)
    except OSError as exc:
        print(colors.error(_(
            "  failed to invoke urpm: {err}").format(err=exc)))
        return 1
    return rc


# ---------------------------------------------------------------------------
# Import — main entry point
# ---------------------------------------------------------------------------


def cmd_system_import(args, db: 'PackageDatabase') -> int:
    """Load ``args.from_``, back up current state, compute diff, prompt,
    apply."""
    from ...auth.privileges import require_privileges
    require_privileges(action_id="org.mageia.urpm.media-manage")

    src_path = Path(args.from_)
    if not src_path.exists():
        print(colors.error(_(
            "Profile not found: {path}").format(path=src_path)))
        return 1

    # 1. Load + validate imported profile.
    try:
        target = sp.load_profile(src_path)
    except sp.ProfileError as exc:
        print(colors.error(str(exc)))
        return 1

    # 2. Auto-backup current system state (skippable — mostly for tests).
    if not getattr(args, "no_backup", False):
        backup_path = sp.timestamp_backup_path()
        try:
            current = sp.export_profile(db)
            sp.save_profile(current, backup_path)
            print(colors.info(_(
                "Backup of current state written to {path}").format(
                    path=backup_path)))
        except Exception as exc:  # noqa: BLE001
            # Backup is a safety net — refuse to proceed if we can't
            # write it, unless user explicitly opted out.
            print(colors.error(_(
                "Cannot write backup ({err}). Refusing to proceed. "
                "Re-run with --no-backup to override.").format(
                    err=exc)))
            return 1
    else:
        current = sp.export_profile(db)

    # 3. Compute the diff.
    merge_media = getattr(args, "merge_media", False)
    diff = sp.compute_diff(
        current, target,
        replace_media=not merge_media,
        replace_servers=not merge_media,
    )

    # 4. Show plan, prompt confirm.
    _render_summary(diff)

    if getattr(args, "dry_run", False):
        print(colors.info(_("--dry-run : no changes applied.")))
        return 0

    if not getattr(args, "auto", False):
        try:
            resp = input(_("Apply this plan ? [y/N] "))
        except (KeyboardInterrupt, EOFError):
            print(_("\nAborted."))
            return 0
        if not confirm_yes(resp):
            print(_("Aborted."))
            return 0

    # 5. Apply : servers → media → sync media → packages → reason files.
    n_srv = _apply_servers(db, diff.servers)
    print(colors.dim(_(
        "  {n} server(s) reconciled").format(n=n_srv)))

    n_med = _apply_media(db, diff.media)
    print(colors.dim(_(
        "  {n} media reconciled").format(n=n_med)))

    if diff.media.to_add:
        print(colors.info(_(
            "Syncing metadata for newly-added media...")))
        rc = _run_urpm(["media", "update"])
        if rc != 0:
            print(colors.warning(_(
                "  media sync returned {rc} — some packages may not "
                "resolve on install below").format(rc=rc)))

    # 6. Pre-flight package check — a clone must be strict.  Every name
    # in the install list should exist in the pool ; every name in the
    # erase list should be currently installed.  Missing = drift.  We
    # refuse to proceed unless the user explicitly opts in via
    # ``--force`` (or accepts the interactive prompt), so the delta is
    # surfaced up-front instead of hidden behind partial success.
    pkgs = diff.packages
    to_install = (
        pkgs.install_explicit + pkgs.install_dependency
        + pkgs.install_buildrequires)

    install_ok, install_missing = _filter_installable(db, to_install)
    erase_ok = _filter_removable(pkgs.remove)
    erase_missing = [n for n in pkgs.remove if n not in erase_ok]

    if install_missing or erase_missing:
        _render_preflight_gaps(install_missing, erase_missing)
        force = getattr(args, "force", False)
        if not force:
            if getattr(args, "auto", False):
                print(colors.error(_(
                    "System import refused : the profile references "
                    "packages that are neither in the pool nor in "
                    "rpmdb.  Re-run with --force to skip the missing "
                    "entries, or fix the profile / media list first."
                )))
                return 1
            try:
                resp = input(_(
                    "Proceed anyway (skipping the missing entries) ? "
                    "[y/N] "))
            except (KeyboardInterrupt, EOFError):
                print(_("\nAborted."))
                return 1
            if not confirm_yes(resp):
                print(_("Aborted."))
                return 1

    # 6bis. Hardware-specific preservation.  Detect kernel modules,
    # display / input drivers, hypervisor guest tools and firmware in
    # the erase list.  Removing these can leave the target machine
    # unbootable — most acutely when a subsequent distupgrade brings
    # in a new kernel that has no matching DKMS module to rebuild
    # against, forcing Xorg onto a wrong DRM driver (documented case
    # 2026-08-22 : ``dkms-virtualbox`` removed → vmwgfx used on a
    # VirtualBox target → gnome-shell locked in cursor-update failure
    # loop).  Interactive : prompt to preserve (Y default).  Non-
    # interactive (``--auto``) : preserve only when ``--preserve-hw``
    # is set — otherwise we honour the strict clone.
    hw_specific = _detect_hw_specific(erase_ok) if erase_ok else []
    hw_preserved: List[str] = []
    if hw_specific:
        preserve = getattr(args, "preserve_hw", False)
        if not preserve and not getattr(args, "auto", False):
            print()
            print(colors.warning(_(
                "⚠ {n} hardware-specific package(s) in the erase list — "
                "removing them can leave the target machine broken "
                "(kernel modules, drivers, guest tools, firmware)."
            ).format(n=len(hw_specific))))
            for name in hw_specific:
                print(f"    {colors.dim(name)}")
            try:
                resp = input(_(
                    "Preserve these {n} hardware-specific package(s) ? "
                    "[Y/n] ").format(n=len(hw_specific)))
            except (KeyboardInterrupt, EOFError):
                print(_("\nAborted."))
                return 1
            # Default is Y (preserve) — an empty response OR anything
            # starting with 'y'/'o' (yes/oui) accepts preservation.
            resp = resp.strip().lower()
            preserve = (resp == "" or resp[0:1] in ("y", "o"))
        if preserve:
            hw_preserved = list(hw_specific)
            hw_set = set(hw_specific)
            erase_ok = [n for n in erase_ok if n not in hw_set]

    # 7. Apply packages — atomic transactions (the default urpm
    # behaviour) so partial state can never happen.  Pre-flight
    # above already filtered to what's viable ; anything urpm still
    # refuses is a real dep-graph conflict the user has to see.
    install_result = _ApplyResult()
    erase_result = _ApplyResult()
    erase_result.hw_preserved = hw_preserved

    if install_ok:
        print(colors.info(_(
            "Installing {n} package(s)...").format(n=len(install_ok))))
        rc = _run_urpm(["install", "--auto"] + install_ok)
        if rc == 0:
            install_result.applied = install_ok
        else:
            install_result.transaction_error = _(
                "install refused (exit {rc}) — dep conflict, insufficient "
                "space, or signature issue ; see the urpm output above."
            ).format(rc=rc)

    # 8. Restore install-reason flat files BEFORE erase so libsolv's
    # orphan reasoning during erase uses the target's classification
    # (dep vs explicit).  Same source of truth as ``urpm autoremove``.
    _restore_reason_files(target.get("packages") or {})

    # 9. Bulk erase — one urpm subprocess = one libsolv solve that
    # handles interdependencies within the remove list and cascades
    # orphans.  If it refuses (real dep conflict against a kept pkg),
    # we don't retry / chunk / autoremove : we surface the urpm error
    # in the delta report and the user resolves manually.
    # Re-filter against live rpmdb : the install phase may have
    # obsoleted rows we planned to erase (pipewire-media-session →
    # pipewire-session-manager is the textbook case).  Handing those
    # to libsolv triggers a « Package not installed » that refuses
    # the whole atomic erase.  Names that vanished between the two
    # filters are attributed to install-phase obsoletes and folded
    # into applied — target state reached, delta stays explicable.
    #
    # Wait for the background install child to finish committing
    # rpmdb.  ``urpm install`` runs in ``smart_sync`` mode by default :
    # it releases the CLI prompt as soon as the RPM transaction is
    # scheduled and lets the forked grandchild run the tail (final
    # rpmdb commit + triggers) in background.  Our re-filter that
    # follows MUST see the settled rpmdb — otherwise it hands
    # libsolv a name RPM already removed and the erase transaction
    # is refused with « Package not installed ».
    #
    # ``InstallLock`` is the serialisation primitive urpm-ng uses
    # across install / undo / history / cleanup ; acquire it blocking
    # and release it right away — when the acquire returns, the
    # background child has finished its work and rpmdb is settled.
    # This is the same pattern used by ``urpm undo`` (see
    # :mod:`urpm.cli.commands.history`) for the exact same reason.
    from ...core.background_install import InstallLock
    from ...core import rpmdb as _rpmdb
    _lock = InstallLock()
    if not _lock.acquire(blocking=False):
        print(colors.dim(_(
            "  Waiting for background install to finish committing "
            "rpmdb...")))
        _lock.acquire(blocking=True)
    _lock.release()
    _rpmdb.invalidate_cache()  # rpmdb just moved — drop the cache
    erase_now = _filter_removable(erase_ok)
    obsoleted_during_install = [n for n in erase_ok if n not in erase_now]

    allow_incoherent = getattr(args, "allow_incoherent", False)

    if allow_incoherent:
        # Explicit user opt-in : reproduce the source state literally,
        # even when it leaves kept pkgs with unmet deps.  Zero rescue
        # + ``--force`` on the erase subprocess so RPM's pre-check
        # can't refuse.  Destructive by design — for bug repro only.
        print()
        print(colors.warning(_(
            "⚠ --allow-incoherent : dep rescue skipped, erase will be "
            "forced through RPM pre-checks — the system will be left "
            "with unmet dependencies")))
        print()
    else:
        # Guard against an incoherent source profile : some pkgs it
        # asks us to keep may need runtime deps whose only providers
        # are in the erase list.  Removing those would leave the kept
        # pkg broken and the atomic erase would refuse.  Rescue the
        # providers and warn — clone integrity outranks strict match.
        erase_now, rescues = _rescue_kept_deps(erase_now)
        erase_result.rescued = rescues
        if rescues:
            rescued_names = sorted({r[2] for r in rescues})
            print()
            print(colors.warning(_(
                "⚠ Source profile is incoherent : {n} package(s) kept "
                "from erase to satisfy runtime deps of retained pkgs"
            ).format(n=len(rescued_names))))
            for kept_pkg, dep, provider in rescues[:15]:
                print(f"    {provider}  "
                      + colors.dim(_("(required by {k} for {d})").format(
                          k=kept_pkg, d=dep)))
            if len(rescues) > 15:
                print(colors.dim(_(
                    "    (+ {n} more)").format(n=len(rescues) - 15)))
            print()

    if erase_now:
        print(colors.info(_(
            "Removing {n} package(s) no longer in target profile..."
        ).format(n=len(erase_now))))
        # ``--keep-orphans`` : the target profile is THE reference for
        # what should be installed at the end.  Without it, urpm/libsolv
        # would cascade-remove any pkg that becomes unreferenced by our
        # explicit erase list — and those cascade orphans routinely
        # include typelib / gir providers still needed by kept pkgs
        # (rescue can't see them, they're computed by libsolv INSIDE
        # the erase transaction, past the pre-erase snapshot our
        # rescue reads).  If the user wants to reclaim disk after the
        # clone, ``urpm autoremove`` is the tool for that — separate
        # from the clone semantic.
        erase_argv = ["erase", "--auto", "--keep-orphans"]
        if allow_incoherent:
            erase_argv.append("--force")
        rc = _run_urpm(erase_argv + erase_now)
        if rc == 0:
            erase_result.applied = erase_now
        else:
            erase_result.transaction_error = _(
                "erase refused (exit {rc}) — see urpm output above ; "
                "resolve manually then re-run."
            ).format(rc=rc)
    if obsoleted_during_install:
        erase_result.applied = (
            list(erase_result.applied) + obsoleted_during_install)

    # 10. Delta report — the user must see EXACTLY what happened vs
    # what was requested.  Cloning is not partial by design ; every
    # departure from the profile is worth surfacing.
    _render_delta_report(
        install_planned=to_install,
        install_missing=install_missing,
        install_result=install_result,
        erase_planned=pkgs.remove,
        erase_missing=erase_missing,
        erase_result=erase_result,
        allow_incoherent=allow_incoherent,
    )

    if install_result.transaction_error or erase_result.transaction_error:
        return 2
    if install_missing or erase_missing:
        return 3  # partial-success : proceeded with --force / user consent
    return 0
