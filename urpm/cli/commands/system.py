"""`urpm system export` / `urpm system import` — clone a machine's
package selection + media/server catalogue onto another.

Export writes a JSON snapshot ; import reads one, backs up the current
state, then reconciles servers / media / packages towards the target.
See :mod:`urpm.core.system_profile` for the format and the diff logic.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

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
    if to_install or pkgs.remove_explicit:
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
        if pkgs.remove_explicit:
            print(colors.error(_(
                "    - remove ({n} explicit no longer in target)"
            ).format(n=len(pkgs.remove_explicit))))
            for name in pkgs.remove_explicit[:15]:
                print(f"        {name}")
            if len(pkgs.remove_explicit) > 15:
                print(colors.dim(_(
                    "        (+ {n} more)").format(
                        n=len(pkgs.remove_explicit) - 15)))

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
            db.add_media(
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

    pkgs = diff.packages
    to_install = (
        pkgs.install_explicit + pkgs.install_dependency
        + pkgs.install_buildrequires)
    if to_install:
        print(colors.info(_(
            "Installing {n} package(s)...").format(n=len(to_install))))
        rc = _run_urpm(["install", "--auto"] + to_install)
        if rc != 0:
            print(colors.warning(_(
                "  install returned {rc} — check the log for "
                "unresolvable packages").format(rc=rc)))

    if pkgs.remove_explicit:
        print(colors.info(_(
            "Removing {n} package(s) no longer in target profile..."
        ).format(n=len(pkgs.remove_explicit))))
        rc = _run_urpm(["remove", "--auto"] + pkgs.remove_explicit)
        if rc != 0:
            print(colors.warning(_(
                "  remove returned {rc}").format(rc=rc)))

    # 6. Restore install-reason flat files so ``urpm autoremove``
    # behaves the same on this box as on the source.
    _restore_reason_files(target.get("packages") or {})

    print(colors.success(_("System import complete.")))
    return 0
