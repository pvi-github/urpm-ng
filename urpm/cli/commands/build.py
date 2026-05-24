"""Build commands: mkimage, build, cleanup."""

import argparse
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import _, ngettext
if TYPE_CHECKING:
    from ...core.database import PackageDatabase
    from ...core.container import Container

from ..helpers.package import resolve_target_arch
from .media import cmd_init
from .install import cmd_install


# Debug flag for mkimage
DEBUG_MKIMAGE = False
DEBUG_BUILD = True

def _get_profile_dirs() -> list:
    """Get profile directories, including dev mode path."""
    dirs = [
        Path('/usr/share/urpm/profiles'),
        Path('/etc/urpm/profiles'),
    ]
    # Dev mode: also check data/profiles/ relative to package
    dev_path = Path(__file__).parent.parent.parent.parent / 'data' / 'profiles'
    if dev_path.exists():
        dirs.insert(0, dev_path)
    return dirs


def load_profiles() -> dict:
    """Load mkimage profiles from YAML files.

    Searches in order:
    1. /usr/share/urpm/profiles/*.yaml (system, from package)
    2. /etc/urpm/profiles/*.yaml (local admin additions)

    If a local profile has the same name as a system profile,
    the local one is ignored with a warning.

    Returns:
        Dict mapping profile name to {'description': str, 'packages': list}
    """
    import yaml

    profiles = {}
    profile_dirs = _get_profile_dirs()

    for i, profile_dir in enumerate(profile_dirs):
        is_system = (i == 0)  # First dir has priority

        if not profile_dir.exists():
            continue

        for yaml_file in sorted(profile_dir.glob('*.yaml')):
            name = yaml_file.stem

            # Check for duplicate
            if name in profiles:
                if not is_system:
                    print(_("Warning: local profile '{name}' ignored "
                            "(system profile exists, use a different name)").format(name=name))
                continue

            try:
                with open(yaml_file, 'r') as f:
                    data = yaml.safe_load(f)

                if not isinstance(data, dict):
                    continue

                profiles[name] = {
                    'description': data.get('description', ''),
                    'packages': data.get('packages', []),
                }

            except Exception as e:
                print(_("Warning: failed to load profile {path}: {error}").format(path=yaml_file, error=e))

    return profiles


def get_profile_names() -> list:
    """Get list of available profile names for argument parser."""
    return list(load_profiles().keys())


def cmd_cleanup(args, db: 'PackageDatabase') -> int:
    """Handle cleanup command - unmount chroot filesystems."""
    from .. import colors

    urpm_root = getattr(args, 'urpm_root', None)
    if not urpm_root:
        print(colors.error(_("Error: --urpm-root is required for cleanup")))
        return 1

    root_path = Path(urpm_root)
    if not root_path.exists():
        print(colors.error(_("Error: {path} does not exist").format(path=urpm_root)))
        return 1

    print(_("Cleaning up mounts in {path}...").format(path=urpm_root))

    # Unmount in reverse order (most nested first)
    mounts_to_check = [
        root_path / 'dev/pts',
        root_path / 'dev/shm',
        root_path / 'dev/mqueue',
        root_path / 'dev/hugepages',
        root_path / 'proc',
        root_path / 'sys',
        root_path / 'dev',
    ]

    def is_mounted(path: Path) -> bool:
        """Check if path is a mount point."""
        try:
            with open('/proc/mounts', 'r') as f:
                path_str = str(path.resolve())
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == path_str:
                        return True
        except (OSError, IOError):
            pass
        return False

    unmounted = 0
    for mount_path in mounts_to_check:
        if mount_path.exists() and is_mounted(mount_path):
            result = subprocess.run(
                ['umount', str(mount_path)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(_("  Unmounted {path}").format(path=mount_path))
                unmounted += 1
            else:
                print(colors.warning(_("  Failed to unmount {path}: {error}").format(
                    path=mount_path, error=result.stderr.strip())))

    if unmounted == 0:
        print(_("  No mounts to clean up"))
    else:
        print(colors.success(ngettext(
            "  {count} filesystem unmounted",
            "  {count} filesystems unmounted",
            unmounted).format(count=unmounted)))

    return 0


def cmd_mkimage(args, db: 'PackageDatabase') -> int:
    """Create a minimal Docker/Podman image for RPM builds (two-phase)."""
    from ...core.container import detect_runtime, Container
    from .. import colors

    # Locale + systemd hint for scriptlets inside phase 1 chroot
    os.environ['LC_ALL'] = 'C'
    os.environ['LANGUAGE'] = 'C'
    os.environ['SYSTEMD_OFFLINE'] = '1'

    release = args.release
    arch = resolve_target_arch(args)
    tag = args.tag
    runtime_name = getattr(args, 'runtime', None)
    profile_name = getattr(args, 'profile', None) or 'build'
    extras_str = getattr(args, 'packages', None) or ''
    buildrequires_src = getattr(args, 'buildrequires', None)
    addmedia = getattr(args, 'addmedia', None) or []
    import_key = getattr(args, 'import_key', False)

    # Detect container runtime
    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)

    # Refuse to clobber an existing final image
    if container.image_exists(tag):
        print(colors.error(
            _("Image {tag} already exists (remove with: {rt} rmi {tag})").format(
                tag=tag, rt=runtime.name)))
        return 1

    # Resolve requested profile -> package list
    profiles = load_profiles()
    if profile_name not in profiles:
        print(colors.error(
            _("Unknown profile: {p}").format(p=profile_name)))
        return 1

    requested_packages = list(profiles[profile_name].get('packages', []))

    # Append --packages extras (comma-separated)
    for extra in extras_str.split(','):
        extra = extra.strip()
        if extra:
            requested_packages.append(extra)

    # Append BuildRequires from spec/srpm if asked
    if buildrequires_src:
        br = _extract_buildrequires(buildrequires_src)
        if br:
            requested_packages.extend(br)
        requested_packages.append('rpm-build')

    # Deduplicate preserving order
    seen = set()
    deduped = []
    for p in requested_packages:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    requested_packages = deduped

    # Compute phase 2 extras = requested minus bootstrap set
    bootstrap_profile = profiles.get('bootstrap', {})
    bootstrap_packages = list(bootstrap_profile.get('packages', []))
    bootstrap_set = set(bootstrap_packages)
    phase2_packages = [p for p in requested_packages if p not in bootstrap_set]

    # Cache key MUST include the arch: the bootstrap chroot is
    # arch-native (Phase 1 installs ``<arch>`` packages, so the
    # resulting rpmdb is arch-specific).  A bare ``{release}-minimal``
    # tag silently reused an x86_64 bootstrap for an i686 build and
    # ended up promoting an x86_64 rpmdb under a ``cauldron-i686``
    # tag — the upgrade then listed x86_64 packages because that is
    # all the rpmdb knew about.
    minimal_tag = f"{release}-{arch}-minimal"

    # Phase 1: bootstrap chroot -> minimal image (cached across runs)
    if not container.image_exists(minimal_tag):
        print(colors.bold(_(
            "[Phase 1/2] Building bootstrap chroot \u2192 {tag}").format(tag=minimal_tag)))
        rc = _phase1_bootstrap_chroot(
            args, release, arch, minimal_tag, container, db,
            bootstrap_packages=bootstrap_packages,
        )
        if rc != 0:
            return rc
    else:
        print(colors.dim(_(
            "[Phase 1/2] Reusing cached {tag} (remove with: podman rmi {tag})").format(tag=minimal_tag)))

    # Phase 2: promote minimal image to the final build image
    print(colors.bold(_(
        "[Phase 2/2] Promoting {src} \u2192 {dst}").format(src=minimal_tag, dst=tag)))
    return _phase2_container_promote(
        container, minimal_tag, tag, addmedia, import_key,
        phase2_packages, buildrequires_src,
    )


def _phase2_container_promote(
    container: 'Container',
    minimal_tag: str,
    final_tag: str,
    addmedia: list,
    import_key: bool,
    extra_packages: list,
    buildrequires_src: str | None,
) -> int:
    """Promote a minimal bootstrap image into a full build image.

    Boots the minimal image in a throwaway container with scriptlets active,
    adds custom media if requested, runs a full `urpm upgrade` (rejoue les
    scriptlets non tournés en Phase 1), installs the requested extras plus
    any BuildRequires, then commits the result as ``final_tag``.
    """
    from .. import colors

    cid = None
    try:
        print(_("  Booting minimal image {tag}...").format(tag=minimal_tag))
        cid = container.run(
            minimal_tag, ['sleep', 'infinity'],
            detach=True, rm=False, network='host',
        )
        print(_("  Container: {cid}").format(cid=cid[:12]))

        for name, url in addmedia:
            print(_("  Adding media {name}...").format(name=name))
            add_cmd = ['urpm', 'media', 'add', '--custom', name, name, url]
            if import_key:
                add_cmd.append('--import-key')
            ret = container.exec_stream(cid, add_cmd)
            if ret != 0:
                print(colors.error(
                    _("Failed to add media {name} ({url})").format(
                        name=name, url=url)))
                return 1

        print(_("  Updating media..."))
        ret = container.exec_stream(cid, ['urpm', 'media', 'update'])
        if ret != 0:
            print(colors.warning(_("  Warning: media update returned {code}").format(code=ret)))

        # Note: bootstrap scriptlets are NOT replayed in bulk here. A blanket
        # `rpm -Uvh --replacepkgs` over the bootstrap set breaks on packages
        # that own system-critical directories (e.g. filesystem owns /proc,
        # which cannot be rewritten while mounted). Phase 1 already bootstraps
        # the TLS trust store via `update-ca-trust extract`, which is the only
        # scriptlet known to be load-bearing for Phase 2 operations. The
        # `urpm upgrade` below will naturally replay scriptlets for any
        # bootstrap package that has a newer version available.
        print(_("  Upgrading packages (picks up any newer versions)..."))
        ret = container.exec_stream(cid, ['urpm', 'upgrade', '--auto'])
        if ret != 0:
            print(colors.warning(_("  Warning: upgrade returned {code}").format(code=ret)))

        if extra_packages:
            print(_("  Installing {n} extra packages...").format(n=len(extra_packages)))
            ret = container.exec_stream(
                cid, ['urpm', 'install', '--auto', '--without-recommends', *extra_packages])
            if ret != 0:
                print(colors.error(_("Failed to install extra packages")))
                return 1

        if buildrequires_src:
            src_path = Path(buildrequires_src).resolve()
            if not src_path.exists():
                print(colors.error(_("BuildRequires source not found: {p}").format(p=src_path)))
                return 1
            dst_in_container = f"/tmp/{src_path.name}"
            # Ensure /tmp exists (some minimal images lack it — filesystem's
            # %post didn't run in --noscripts Phase 1, and _cleanup_chroot_for_image
            # wiped tmp/* contents).
            container.exec(cid, ['mkdir', '-p', '/tmp'])
            print(_("  Copying {name} into container...").format(name=src_path.name))
            if not container.cp(str(src_path), f"{cid}:{dst_in_container}"):
                print(colors.error(_("Failed to copy BuildRequires source")))
                return 1
            print(_("  Installing BuildRequires..."))
            ret = container.exec_stream(
                cid, ['urpm', 'install', '--auto', '--without-recommends', '--buildrequires', dst_in_container])
            if ret != 0:
                print(colors.error(_("Failed to install BuildRequires")))
                return 1

        # Stop any lingering processes before commit
        container.exec(cid, ['sh', '-c', 'kill 1 2>/dev/null || true'])

        print(_("  Committing image {tag}...").format(tag=final_tag), end='', flush=True)
        if not container.commit(cid, final_tag):
            print()
            print(colors.error(_("Failed to commit image")))
            return 1
        print(_(" done"))
        return 0

    except Exception as e:
        print(colors.error(_("Phase 2 failed: {error}").format(error=e)))
        return 1

    finally:
        if cid:
            container.rm(cid, force=True)


def _find_local_urpm_rpm() -> Path | None:
    """Search standard locations for a local urpm-ng RPM (fallback when
    urpm-ng isn't yet in official repos)."""
    search_dirs = [
        Path.home() / 'Downloads',
        Path('./rpmbuild/RPMS'),
        Path.home() / 'rpmbuild/RPMS',
        Path('.'),
    ]
    for search_dir in search_dirs:
        if search_dir.exists():
            candidates = list(search_dir.glob('**/urpm-ng-core-*.rpm'))
            if not candidates:
                candidates = list(search_dir.glob('**/urpm-ng-*.rpm'))
            if candidates:
                return max(candidates, key=lambda p: p.stat().st_mtime)
    return None


def _ensure_system_accounts(chroot: str) -> None:
    """Pre-seed /etc/passwd and /etc/group with system accounts needed by
    bootstrap packages installed in --noscripts mode.

    Entries follow Mageia UID/GID conventions. Idempotent: lines are only
    appended when the name is not already present. No systemd dependency —
    plain text file manipulation only.
    """
    passwd_path = Path(chroot) / 'etc/passwd'
    group_path = Path(chroot) / 'etc/group'
    passwd_path.parent.mkdir(parents=True, exist_ok=True)
    if not passwd_path.exists():
        passwd_path.write_text(
            'root:x:0:0:root:/root:/bin/bash\n'
            'bin:x:1:1:bin:/bin:/sbin/nologin\n'
            'daemon:x:2:2:daemon:/sbin:/sbin/nologin\n'
        )
    if not group_path.exists():
        group_path.write_text('root:x:0:\nbin:x:1:\ndaemon:x:2:\n')

    # (name, uid, gid, gecos, home, shell) — matches Mageia rpm-mageia-setup
    users = [
        ('rpm',         37, 37, 'RPM database owner', '/var/lib/rpm', '/sbin/nologin'),
        ('messagebus',  81, 81, 'D-Bus system daemon', '/',            '/sbin/nologin'),
        ('polkitd',    997, 997, 'PolicyKit daemon',   '/',            '/sbin/nologin'),
    ]
    # (name, gid, members) — groups without a matching user
    groups = [
        ('shadow',          15, ''),
        ('utempter',        35, ''),
        ('systemd-journal', 190, ''),
    ]

    passwd_text = passwd_path.read_text()
    existing_users = {line.split(':', 1)[0] for line in passwd_text.splitlines() if line}
    new_passwd = []
    new_group_members = {}
    for name, uid, gid, gecos, home, shell in users:
        if name in existing_users:
            continue
        new_passwd.append(f"{name}:x:{uid}:{gid}:{gecos}:{home}:{shell}")
        new_group_members[name] = gid

    if new_passwd:
        with passwd_path.open('a') as f:
            f.write('\n'.join(new_passwd) + '\n')

    group_text = group_path.read_text()
    existing_groups = {line.split(':', 1)[0] for line in group_text.splitlines() if line}
    new_group = []
    for name, gid in new_group_members.items():
        if name not in existing_groups:
            new_group.append(f"{name}:x:{gid}:")
    for name, gid, members in groups:
        if name not in existing_groups:
            new_group.append(f"{name}:x:{gid}:{members}")

    if new_group:
        with group_path.open('a') as f:
            f.write('\n'.join(new_group) + '\n')


def _phase1_bootstrap_chroot(
    args,
    release: str,
    arch: str,
    minimal_tag: str,
    container: 'Container',
    db: 'PackageDatabase',
    bootstrap_packages: list | None = None,
) -> int:
    """Build a minimal bootstrap chroot and import it as ``minimal_tag``.

    Phase 1 of mkimage. Installs only the packages from the ``bootstrap``
    profile with ``--noscripts`` (setup, filesystem, glibc, bash,
    coreutils, grep/sed/awk/findutils, util-linux, shadow-utils, rpm,
    urpmi, ca-certificates, curl). Then installs urpm-ng separately
    (repo first, local RPM fallback) since it is not yet in Mageia's
    official repos. Phase 2 will boot this image and rejoue les
    scriptlets via `urpm upgrade` before installing any extras.
    """
    from ...core.database import PackageDatabase
    from .. import colors

    if bootstrap_packages is None:
        bootstrap = load_profiles().get('bootstrap')
        if bootstrap is None:
            print(colors.error(_("Error: 'bootstrap' profile not found")))
            return 1
        bootstrap_packages = list(bootstrap['packages'])
    else:
        bootstrap_packages = list(bootstrap_packages)

    keep_chroot = getattr(args, 'keep_chroot', False)

    workdir = getattr(args, 'workdir', None)
    if not workdir:
        xdg_cache = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
        workdir = os.path.join(xdg_cache, 'urpm', 'mkimage')
        os.makedirs(workdir, exist_ok=True)

    MIN_SPACE_GB = 2
    try:
        stat = os.statvfs(workdir)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < MIN_SPACE_GB:
            print(colors.error(_("Insufficient disk space in {path}").format(path=workdir)))
            print(colors.error(_("  Available: {available:.1f} GB, required: {required} GB").format(
                available=free_gb, required=MIN_SPACE_GB)))
            return 1
    except OSError as e:
        print(colors.warning(_("Could not check disk space: {error}").format(error=e)))

    tmpdir = tempfile.mkdtemp(prefix='urpm-mkimage-', dir=workdir)
    print(_("  Bootstrap chroot: {path}").format(path=tmpdir))

    try:
        (Path(tmpdir) / "tmp").mkdir(parents=True, exist_ok=True)
        chroot_db_path = Path(tmpdir) / "var/lib/urpm/packages.db"
        chroot_db_path.parent.mkdir(parents=True, exist_ok=True)
        chroot_db = PackageDatabase(db_path=chroot_db_path)

        print(_("  Initializing chroot media..."))
        init_args = argparse.Namespace(
            urpm_root=tmpdir, release=release, arch=arch,
            mirrorlist=None, auto=True, no_sync=False, no_mount=True,
        )
        ret = cmd_init(init_args, chroot_db)
        if ret != 0:
            print(colors.error(_("Failed to initialize chroot")))
            return ret

        if os.geteuid() == 0:
            proc_path = os.path.join(tmpdir, 'proc')
            sys_path = os.path.join(tmpdir, 'sys')
            os.makedirs(proc_path, exist_ok=True)
            os.makedirs(sys_path, exist_ok=True)
            subprocess.run(['mount', '-t', 'proc', 'proc', proc_path], check=True)
            subprocess.run(['mount', '-t', 'sysfs', 'sysfs', sys_path], check=True)

        os.environ['SYSTEMD_OFFLINE'] = '1'

        def _noscripts_install(pkgs: list, label: str, nosig: bool = False) -> int:
            print(_("  Installing {label}...").format(label=label))
            ns = argparse.Namespace(
                urpm_root=tmpdir, root=tmpdir,
                packages=pkgs, auto=True,
                without_recommends=True, with_suggests=False,
                download_only=False, nodeps=False, nosignature=nosig,
                noscripts=True, force=False, reinstall=False,
                debug=None, watched=None, prefer=None,
                all=False, test=False, sync=True,
                allow_no_root=True, config_policy='replace', no_readme=True,
                # Propagate the bootstrap arch so cmd_install does not
                # fall back to the host's ``uname -m`` and end up
                # trying to fetch x86_64 packages into an i686 chroot.
                arch=arch,
            )
            return cmd_install(ns, chroot_db)

        # Pre-seed system accounts that subsequent packages ship files owned by.
        # With --noscripts, the %pre of `setup` and friends does not run, so rpm
        # warns "user X does not exist - using root" and chowns to root. The
        # entries below match Mageia conventions; they are idempotent (skipped
        # if already present). Systemd-independent on purpose: no sysusers call.
        # Must run BEFORE the setup install: files shipped by packages pulled in
        # as deps of setup (e.g. shadow group references) also need these.
        # UsrMove: pre-create /bin, /sbin, /lib, /lib64 as symlinks into /usr/*
        # before the filesystem package runs. On distributions that still
        # ship %pretrans to perform this move (e.g. mga9), --noscripts skips
        # it and the chroot ends up with /bin and /usr/bin as separate dirs,
        # breaking any tool that hardcodes /usr/bin/<foo>. By establishing the
        # symlinks first, rpm extracts /bin/<foo> files straight into /usr/bin.
        # On systems where the filesystem package already ships the symlinks
        # (e.g. mga10), this is a no-op since the targets will match.
        root_path = Path(tmpdir)
        (root_path / 'usr/bin').mkdir(parents=True, exist_ok=True)
        (root_path / 'usr/sbin').mkdir(parents=True, exist_ok=True)
        (root_path / 'usr/lib').mkdir(parents=True, exist_ok=True)
        (root_path / 'usr/lib64').mkdir(parents=True, exist_ok=True)
        for name, target in [('bin', 'usr/bin'), ('sbin', 'usr/sbin'),
                             ('lib', 'usr/lib'), ('lib64', 'usr/lib64')]:
            link = root_path / name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(target)

        _ensure_system_accounts(tmpdir)
        if _noscripts_install(['setup'], 'setup') != 0:
            print(colors.error(_("Failed to install setup")))
            return 1
        # The setup package ships /etc/passwd and overwrites ours on first
        # install (config file, no previous version to diff against). Re-seed
        # so subsequent package extractions resolve rpm/messagebus/polkitd.
        _ensure_system_accounts(tmpdir)
        if _noscripts_install(['filesystem'], 'filesystem') != 0:
            print(colors.error(_("Failed to install filesystem")))
            return 1
        if _noscripts_install(['coreutils'], 'coreutils') != 0:
            print(colors.error(_("Failed to install coreutils")))
            return 1

        remaining = [p for p in bootstrap_packages
                     if p not in ('setup', 'filesystem', 'coreutils')]
        if remaining:
            label = _("bootstrap remainder ({n} pkgs)").format(n=len(remaining))
            if _noscripts_install(remaining, label) != 0:
                print(colors.error(_("Failed to install bootstrap packages")))
                return 1

        # urpm-ng: try repos, fallback to local RPM
        print(_("  Installing urpm-ng..."))
        if _noscripts_install(['urpm-ng'], 'urpm-ng from repos') != 0:
            print(_("    repos failed, looking for local urpm-ng RPM..."))
            local_rpm = _find_local_urpm_rpm()
            if local_rpm is not None:
                prompt = _("  Found: {path}\n  Press Enter to use, or provide another path: "
                           ).format(path=local_rpm)
                default_path = str(local_rpm)
            else:
                prompt = _("  Path to urpm-ng RPM file: ")
                default_path = ""
            user_input = input(prompt).strip()
            rpm_path = Path(user_input) if user_input else (Path(default_path) if default_path else None)
            if not rpm_path or not rpm_path.exists():
                print(colors.error(_("No urpm-ng RPM provided or file not found")))
                print(_("  Build it with: make rpm"))
                return 1
            if _noscripts_install([str(rpm_path.resolve())],
                                  _("urpm-ng from {name}").format(name=rpm_path.name),
                                  nosig=True) != 0:
                print(colors.error(_("Failed to install urpm-ng")))
                return 1
            print(colors.success(_("  Installed {name}").format(name=rpm_path.name)))

        # Bootstrap the TLS trust store inside the chroot so the committed
        # `<release>-minimal` image can do HTTPS out of the box (Phase 2's
        # own replay would otherwise face a chicken-and-egg: downloading the
        # RPMs to re-run ca scriptlets itself needs HTTPS). This is the ONLY
        # exec-in-chroot we allow in Phase 1 — it's the exact command the
        # `rootcerts` %post runs, and keeps the minimal image self-sufficient.
        print(_("  Bootstrapping TLS trust store..."))
        uct_bin = Path(tmpdir) / 'usr/bin/update-ca-trust'
        if uct_bin.exists():
            chroot_cmd = ['chroot', tmpdir, '/usr/bin/update-ca-trust', 'extract']
            if os.geteuid() != 0:
                chroot_cmd = ['podman', 'unshare'] + chroot_cmd
            uct = subprocess.run(chroot_cmd, capture_output=True, text=True)
            if uct.returncode != 0:
                print(colors.warning(_(
                    "  Warning: update-ca-trust returned {rc}: {err}").format(
                    rc=uct.returncode, err=uct.stderr.strip())))
        else:
            print(colors.warning(_(
                "  Warning: update-ca-trust not found in chroot (TLS may fail)")))

        print(_("  Cleaning up chroot..."))
        chroot_db.close()
        _cleanup_chroot_for_image(tmpdir)
        cmd_cleanup(argparse.Namespace(urpm_root=tmpdir), None)

        print(_("  Importing as {tag}...").format(tag=minimal_tag), end='', flush=True)
        if not container.import_from_dir(tmpdir, minimal_tag,
                                          use_unshare=(os.geteuid() != 0)):
            print()
            print(colors.error(_("Failed to import minimal image")))
            return 1
        print(_(" done"))
        return 0

    except Exception as e:
        print(colors.error(_("Phase 1 failed: {error}").format(error=e)))
        return 1

    finally:
        if not keep_chroot:
            cmd_cleanup(argparse.Namespace(urpm_root=tmpdir), None)
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(_("Chroot kept at: {path}").format(path=tmpdir))


def _cleanup_chroot_for_image(root: str):
    """Clean up chroot before creating container image.

    Removes caches, logs, and temporary files to reduce image size.
    """
    import glob

    from .. import colors

    cleanup_patterns = [
        'var/cache/urpmi/*',
        'var/cache/dnf/*',
        'var/lib/urpm/medias/**/*.rpm',     # Downloaded RPM packages (recursive)
        'var/lib/urpm/medias/**/*.cache',   # Cache files (recursive)
        'var/log/*',
        'tmp/*',
        'var/tmp/*',
        'root/.bash_history',
        'usr/share/doc/*',
        'usr/share/man/*',
        'usr/share/info/*',
        'etc/**/*.rpmnew',                  # Config file backups (new version)
        'etc/**/*.rpmold',                  # Config file backups (old version)
        'etc/**/*.rpmsave',                 # Config file backups (saved)
    ]

    removed = 0
    for pattern in cleanup_patterns:
        for path in glob.glob(os.path.join(root, pattern), recursive=True):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
                elif os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except (IOError, OSError):
                pass

    print(ngettext(
        "  Removed {count} cache/log entry",
        "  Removed {count} cache/log entries",
        removed).format(count=removed))

    # Ensure /var/tmp exists (required by RPM scriptlets)
    var_tmp = os.path.join(root, 'var', 'tmp')
    if not os.path.exists(var_tmp):
        os.makedirs(var_tmp, mode=0o1777, exist_ok=True)
        print(_("  Created /var/tmp"))

    # Fix PATH for Mageia 9 compatibility (/bin and /sbin are separate)
    # Must be in /etc/bashrc for non-login interactive shells (podman run -it)
    bashrc_path = os.path.join(root, 'etc', 'bashrc')
    if os.path.exists(bashrc_path):
        try:
            with open(bashrc_path, 'r') as f:
                bashrc_content = f.read()
            if 'path-compat' not in bashrc_content:
                with open(bashrc_path, 'a') as f:
                    f.write('\n# Mageia 9 compatibility: add /bin /sbin if not symlinks\n')
                    f.write('[ -d /bin ] && [ ! -L /bin ] && [[ ":$PATH:" != *":/bin:"* ]] && export PATH="$PATH:/bin"\n')
                    f.write('[ -d /sbin ] && [ ! -L /sbin ] && [[ ":$PATH:" != *":/sbin:"* ]] && export PATH="$PATH:/sbin"\n')
                print(_("  Added PATH fix to /etc/bashrc"))
        except (IOError, OSError):
            pass

    # Create /etc/machine-id if missing (required by systemd, dbus, etc.)
    machine_id_path = os.path.join(root, 'etc', 'machine-id')
    if not os.path.exists(machine_id_path):
        try:
            import uuid
            machine_id = uuid.uuid4().hex  # 32 hex chars, no dashes
            with open(machine_id_path, 'w') as f:
                f.write(machine_id + '\n')
            print(_("  Created /etc/machine-id"))
        except (IOError, OSError):
            pass


def cmd_build(args, db: 'PackageDatabase') -> int:
    """Build RPM package(s) in isolated containers."""
    import glob as globmod
    from ...core.container import detect_runtime, Container
    from .. import colors

    image = args.image
    sources = args.sources
    output_dir = Path(args.output) if args.output else Path('./build-output')
    parallel = getattr(args, 'parallel', 1)
    keep_container = getattr(args, 'keep_container', False)
    runtime_name = getattr(args, 'runtime', None)
    no_update = getattr(args, 'no_update', False)
    with_rpms_patterns = getattr(args, 'with_rpms', []) or []

    # Expand glob patterns for --with-rpms
    with_rpms = []
    for pattern in with_rpms_patterns:
        expanded = globmod.glob(pattern)
        if expanded:
            with_rpms.extend(Path(p) for p in expanded if p.endswith('.rpm'))
        elif Path(pattern).exists():
            with_rpms.append(Path(pattern))
        else:
            print(colors.warning(_("No RPMs found matching: {pattern}").format(pattern=pattern)))

    # Detect container runtime
    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)
    print(_("Using {name} {version}").format(name=runtime.name, version=runtime.version))

    # Check image exists
    if not container.image_exists(image):
        print(colors.error(_("Image not found: {image}").format(image=image)))
        print(colors.dim(_("Create one with: urpm mkimage --release 10 --tag <tag>")))
        return 1

    # Validate sources
    valid_sources = []
    for source in sources:
        source_path = Path(source)
        if not source_path.exists():
            print(colors.warning(_("Source not found: {source}").format(source=source)))
            continue
        # Accept .spec files or .src.rpm (source RPMs)
        if source_path.suffix == '.spec':
            valid_sources.append(source_path)
        elif source_path.suffix == '.rpm' and '.src.' in source_path.name:
            valid_sources.append(source_path)
        elif source_path.suffix == '.rpm':
            print(colors.warning(_("Binary RPM cannot be built: {source}").format(source=source)))
            print(colors.dim(_("  Use a .src.rpm or .spec file instead")))
            continue
        else:
            print(colors.warning(_("Unsupported source type: {source}").format(source=source)))
            continue

    if not valid_sources:
        print(colors.error(_("No valid sources to build")))
        return 1

    print("\n" + ngettext(
        "Building {count} package",
        "Building {count} packages",
        len(valid_sources)).format(count=len(valid_sources)))
    print("  " + _("Image:  {image}").format(image=image))
    if with_rpms:
        print("  " + ngettext(
            "Pre-install: {count} local RPM",
            "Pre-install: {count} local RPMs",
            len(with_rpms)).format(count=len(with_rpms)))
    if parallel > 1:
        print("  " + _("Parallel: {parallel}").format(parallel=parallel))

    results = []

    def build_one(source_path: Path) -> tuple:
        """Build a single package. Returns (source, success, message)."""
        return _build_single_package(
            container, image, source_path, output_dir, keep_container,
            with_rpms, no_update=no_update,
        )

    if parallel > 1 and len(valid_sources) > 1:
        # Parallel builds
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(build_one, src): src for src in valid_sources}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                source, success, msg = result
                status = colors.success(_("OK")) if success else colors.error(_("FAIL"))
                print(f"  [{status}] {source.name}: {msg}")
    else:
        # Sequential builds
        for source_path in valid_sources:
            print(f"\n{'='*60}")
            print(_("Building: {name}").format(name=source_path.name))
            print(f"{'='*60}")
            result = build_one(source_path)
            results.append(result)

    # Summary
    success_count = sum(1 for _n, ok, _m in results if ok)
    fail_count = len(results) - success_count

    print(f"\n{'='*60}")
    print(_("Build Summary"))
    print(f"{'='*60}")
    print("  " + _("Success: {count}").format(count=success_count))
    print("  " + _("Failed:  {count}").format(count=fail_count))
    print("  " + _("Output:  {path}").format(path=output_dir))

    if fail_count > 0:
        print(_("\nFailed packages:"))
        for source, success, msg in results:
            if not success:
                print(f"  {colors.error('X')} {source.name}: {msg}")

    return 0 if fail_count == 0 else 1


def _find_workspace(source_path: Path) -> tuple:
    """Find the workspace root and SOURCES directory for a spec file.

    Supports layouts:
    - workspace/SPECS/foo.spec + workspace/SOURCES/
    - workspace/foo.spec + workspace/SOURCES/
    - dir/foo.spec + dir/SOURCES/ (or dir/*.tar.gz)

    Returns:
        Tuple of (workspace_path, sources_dir, is_rpmbuild_layout)
        - workspace_path: Root of the workspace (for output)
        - sources_dir: Directory containing source files
        - is_rpmbuild_layout: True if SPECS/SOURCES layout
    """
    source_path = Path(source_path).resolve()
    parent = source_path.parent

    # Check if spec is in SPECS/ directory
    if parent.name == 'SPECS':
        workspace = parent.parent
        sources_dir = workspace / 'SOURCES'
        if sources_dir.is_dir():
            return (workspace, sources_dir, True)

    # Check for SOURCES/ in same directory as spec
    sources_dir = parent / 'SOURCES'
    if sources_dir.is_dir():
        return (parent, sources_dir, True)

    # Check for source files directly in same directory
    sources = list(parent.glob('*.tar.gz')) + list(parent.glob('*.tar.xz')) + \
              list(parent.glob('*.tar.bz2')) + list(parent.glob('*.tgz'))
    if sources:
        return (parent, parent, False)

    # No sources found - return parent anyway
    return (parent, None, False)


def _build_single_package(
    container: 'Container',
    image: str,
    source_path: Path,
    output_dir: Path,
    keep_container: bool,
    with_rpms: list = None,
    no_update: bool = False,
) -> tuple:
    """Build a single package in a container.

    Args:
        container: Container runtime wrapper
        image: Container image to use
        source_path: Path to .spec or .src.rpm file
        output_dir: Output directory for SRPM builds
        keep_container: Keep container after build for debugging
        with_rpms: List of local RPM paths to install before build
        no_update: Skip media sync and package update before building

    Returns:
        Tuple of (source_path, success, message)
    """
    if with_rpms is None:
        with_rpms = []
    from .. import colors

    cid = None
    workspace = None
    is_spec_build = source_path.suffix == '.spec'

    try:
        # 1. Start fresh container with host network (for urpmd P2P access)
        cid = container.run(
            image,
            ['sleep', 'infinity'],
            detach=True,
            rm=False,
            network='host'
        )
        print(_("  Container: {cid}").format(cid=cid[:12]))

        # 2. Prepare rpmbuild directories
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/SPECS'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/SOURCES'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/BUILD'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/RPMS'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/SRPMS'])
        container.exec(cid, ['mkdir', '-p', '/tmp'])

        # DEBUG SSL IN BUILD... TODO: do that in mkimage
        container.exec(cid, ['/bin/update-ca-trust', 'extract'])

        # 3. Copy source into container
        print(_("  Copying source..."))

        if source_path.suffix == '.rpm' and '.src.' in source_path.name:
            # Source RPM - install it to extract spec and sources
            if not container.cp(str(source_path), f"{cid}:/root/rpmbuild/SRPMS/"):
                return (source_path, False, "Failed to copy SRPM")

            print(_("  Installing SRPM..."))
            result = container.exec(cid, [
                'rpm', '-ivh', f'/root/rpmbuild/SRPMS/{source_path.name}'
            ])
            if result.returncode != 0:
                return (source_path, False, f"SRPM install failed: {result.stderr}")

            # Find spec file (name without version-release.src.rpm)
            name_parts = source_path.stem.replace('.src', '').rsplit('-', 2)
            spec_name = name_parts[0] + '.spec'
            spec_path = f'/root/rpmbuild/SPECS/{spec_name}'

        elif is_spec_build:
            # Spec file - need to copy spec and sources
            workspace, sources_dir, is_rpmbuild_layout = _find_workspace(source_path)

            # Copy spec file
            if not container.cp(str(source_path), f"{cid}:/root/rpmbuild/SPECS/"):
                return (source_path, False, "Failed to copy spec file")
            spec_path = f'/root/rpmbuild/SPECS/{source_path.name}'

            # Copy all sources (tar.gz, patches, license files, etc.)
            if sources_dir and sources_dir.exists():
                # Count files to copy
                source_files = [f for f in sources_dir.iterdir() if f.is_file()]
                print(ngettext(
                    "  Copying {count} source file from {path}...",
                    "  Copying {count} source files from {path}...",
                    len(source_files)).format(count=len(source_files), path=sources_dir))
                # Copy entire directory content at once
                container.cp(f"{sources_dir}/.", f"{cid}:/root/rpmbuild/SOURCES/")
            else:
                print(colors.warning(_("  Warning: No SOURCES directory found")))

        else:
            return (source_path, False, f"Unsupported source type: {source_path.suffix}")

        # 3b. Update media and packages (unless --no-update)
        if not no_update:
            print(_("  Updating media..."))
            ret = container.exec_stream(cid, ['urpm', 'media', 'update'])
            if ret != 0:
                print(colors.warning(_("  Warning: media update failed, continuing...")))
            print(_("  Updating packages..."))
            ret = container.exec_stream(cid, ['urpm', 'upgrade', '--auto'])
            if ret != 0:
                print(colors.warning(_("  Warning: package update failed, continuing...")))

        # 4. Install rpm-build (provides rpmbuild)
        print(_("  Installing rpm-build..."))
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--without-recommends', '--sync', 'rpm-build'
        ])
        if ret != 0:
            return (source_path, False, "Failed to install rpm-build")

        # 4b. Install local RPMs (dependencies built locally)
        if with_rpms:
            print(ngettext(
                "  Installing {count} local RPM...",
                "  Installing {count} local RPMs...",
                len(with_rpms)).format(count=len(with_rpms)))
            # Create temp directory for local RPMs
            container.exec(cid, ['mkdir', '-p', '/tmp/local-rpms'])
            # Copy all local RPMs and build list of paths
            rpm_paths_in_container = []
            for rpm_path in with_rpms:
                if not container.cp(str(rpm_path), f"{cid}:/tmp/local-rpms/"):
                    return (source_path, False, f"Failed to copy {rpm_path.name}")
                rpm_paths_in_container.append(f"/tmp/local-rpms/{rpm_path.name}")
            # Install all local RPMs at once
            ret = container.exec_stream(cid, [
                'urpm', 'install', '--auto', '--without-recommends', '--sync', '--nosignature'
            ] + rpm_paths_in_container)
            if ret != 0:
                return (source_path, False, "Failed to install local RPMs")

        # 5. Install static build dependencies (those declared verbatim in the spec)
        print(_("  Installing BuildRequires..."))
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--without-recommends', '--sync', '--buildrequires', spec_path
        ])
        if ret != 0:
            return (source_path, False, f"BuildRequires install failed")

        # Get package name from spec for log naming (log.<Name>)
        result = container.exec(cid, ['rpmspec', '-q', '--srpm', '--qf', '%{name}', spec_path])
        pkg_name = result.stdout.strip() if result.returncode == 0 else source_path.stem
        container_log = f'/tmp/log.{pkg_name}'

        # 5b. Resolve dynamic BuildRequires (rpm 4.15+ %generate_buildrequires).
        #
        # ANTI-PATTERN — adopted reluctantly to align with the ecosystem.
        # ---------------------------------------------------------------
        # What we'd *like*: parse all BuildRequires from the spec once, install
        # them, then run rpmbuild. Done.
        #
        # What rpm forces us to do: modern PEP 517 / Cargo / Meson backends
        # declare their dependencies programmatically (e.g. poetry-core reads
        # pyproject.toml at runtime to enumerate them). To learn what a project
        # needs you must execute its build backend; to execute the backend the
        # backend must already be installed. So rpm 4.15 introduced
        # %generate_buildrequires: rpmbuild runs the backend, writes newly
        # discovered BuildRequires into a *.buildreqs.nosrc.rpm sidecar, and
        # exits with code 11. The build tool installs those, retries, repeats
        # until convergence (typically 1-2 passes for Python).
        #
        # This is a retry-on-failure loop using an exit code as control flow.
        # It is the exact pattern documented by rpm upstream and implemented by
        # mock(1), dnf builddep, and koji. We do not have a cleaner option if
        # we want to build mainstream Python/Rust packages without forcing
        # every packager to duplicate pyproject.toml/Cargo.toml deps by hand
        # in their .spec — which would drift and rot.
        #
        # Refs:
        #   https://rpm-software-management.github.io/rpm/manual/dynamic_build_dependencies.html
        #   rpm source: RPMRC_MISSINGBUILDREQUIRES = 11
        import re
        RPMBUILD_MISSING_BR = 11        # rpm's documented "more BRs needed" exit code
        MAX_DYNBR_PASSES = 16           # safety cap; real builds converge in 1-3
        _ver_re = re.compile(r'\s*(?:>=|<=|=>|=<|[><=!])\s*\S+')

        # Output discipline: the per-iteration `rpmbuild -br` runs are an
        # implementation detail of dynamic BR resolution, not output we want
        # to surface. They are captured (not streamed) and appended to the
        # build log for post-mortem. The actual `urpm install` of the
        # discovered deps stays visible — that's a real install with progress
        # the user expects to see.

        for dynbr_pass in range(MAX_DYNBR_PASSES):
            # `rpmbuild -br` runs only %prep + %generate_buildrequires (cheap:
            # no %build, no %install). `set -o pipefail` so we observe
            # rpmbuild's exit code, not tee's. Output captured + logged.
            result = container.exec(cid, [
                'bash', '-c',
                f'set -o pipefail; rpmbuild -br {spec_path} 2>&1 | tee -a {container_log}'
            ])
            rc = result.returncode
            if rc == 0:
                break  # all BRs satisfied (or %generate_buildrequires absent)
            if rc != RPMBUILD_MISSING_BR:
                # Real failure — surface the captured output so the user
                # sees what broke without having to open the log.
                if result.stdout:
                    print(result.stdout, end='')
                return (source_path, False,
                        _("rpmbuild -br failed before %build (rc={rc}, see log)").format(rc=rc))

            # Read newly-required deps from the .buildreqs.nosrc.rpm sidecar.
            result = container.exec(cid, ['bash', '-c',
                'rpm -qp --requires /root/rpmbuild/SRPMS/*.buildreqs.nosrc.rpm 2>/dev/null'])
            if result.returncode != 0:
                return (source_path, False,
                        _("rpmbuild reported missing BRs but .buildreqs.nosrc.rpm could not be read"))

            # Preserve version constraints (``... >= 46``) verbatim:
            # they are essential for resolution.  Stripping them turned
            # an unsatisfiable build (a missing version, not a missing
            # capability) into an infinite "Nothing to do" loop because
            # ``urpm install`` resolved the bare capability to whatever
            # older version was on the mirror, while ``rpmbuild -br``
            # kept rejecting it.  ``urpm install`` accepts the full
            # ``name op version`` syntax directly.
            new_deps = []
            for line in result.stdout.splitlines():
                dep = line.strip()
                if dep and not dep.startswith('rpmlib('):
                    new_deps.append(dep)
            # Sort + dedupe for readable display.
            new_deps = sorted(set(new_deps))
            if not new_deps:
                return (source_path, False,
                        _("rpmbuild requested more BRs but emitted no installable requirements"))

            # Header in warning/orange; each dep on its own indented line.
            # Light purple for the dep names. Real packages stay at normal
            # intensity, virtual provides (anything containing '(' — i.e.
            # pkgconfig(), python3dist(), typelib(), …) are dimmed so the
            # eye latches onto the actual installable packages first.
            print(colors.warning(
                _("Getting dynamic buildrequires (round {n}), found :").format(
                    n=dynbr_pass + 1)))
            for dep in new_deps:
                if '(' in dep:
                    print("  " + colors.dim(colors.light_purple(dep)))
                else:
                    print("  " + colors.light_purple(dep))
            # Stream the install so the user sees the live progress
            # display.  On failure we re-query each requested dep
            # with ``urpm whatprovides`` to build the want/have report
            # — much cleaner than trying to parse the streamed output
            # with its ANSI cursor-control codes.
            ret = container.exec_stream(cid, [
                'urpm', 'install', '--auto', '--without-recommends', '--sync'
            ] + new_deps)
            if ret != 0:
                _diagnose_unsatisfied_buildrequires(cid, container, new_deps)
                return (source_path, False,
                        _("Dynamic BuildRequires install failed"))
        else:
            return (source_path, False,
                    _("Dynamic BuildRequires did not converge in {n} passes").format(n=MAX_DYNBR_PASSES))

        # 6. Build the package
        print(_("  Building..."))
        result = container.exec_stream(cid, [
            'bash', '-c', f'set -o pipefail; rpmbuild -ba {spec_path} 2>&1 | tee -a {container_log}'
        ])
        build_failed = result != 0

        # 7. Copy results out
        # Determine output location
        if is_spec_build and workspace:
            # For spec builds, output to workspace/{RPMS,SRPMS}
            rpms_dir = workspace / 'RPMS'
            srpms_dir = workspace / 'SRPMS'
            log_dir = workspace / 'SPECS'
        else:
            # For SRPM builds, output to specified output_dir
            pkg_output = output_dir / source_path.stem.replace('.src', '')
            pkg_output.mkdir(parents=True, exist_ok=True)
            rpms_dir = pkg_output / 'RPMS'
            srpms_dir = pkg_output / 'SRPMS'
            log_dir = pkg_output

        rpms_dir.mkdir(parents=True, exist_ok=True)
        srpms_dir.mkdir(parents=True, exist_ok=True)

        # Copy build log (always, even on failure)
        log_file = log_dir / f"log.{pkg_name}"
        if container.cp(f"{cid}:{container_log}", str(log_file)):
            print(_("  Build log: {path}").format(path=log_file))

        if build_failed:
            return (source_path, False, f"rpmbuild failed (see {log_file})")

        print(_("  Copying RPMs to {path}/").format(path=rpms_dir))
        container.cp(f"{cid}:/root/rpmbuild/RPMS/.", str(rpms_dir))

        print(_("  Copying SRPMs to {path}/").format(path=srpms_dir))
        container.cp(f"{cid}:/root/rpmbuild/SRPMS/.", str(srpms_dir))

        # Count built packages
        rpm_count = len(list(rpms_dir.rglob('*.rpm')))
        srpm_count = len(list(srpms_dir.rglob('*.rpm')))

        return (source_path, True, f"{rpm_count} RPMs, {srpm_count} SRPMs")

    except Exception as e:
        return (source_path, False, str(e))

    finally:
        # Always cleanup container unless --keep-container
        if cid and not keep_container:
            container.rm(cid)


def _diagnose_unsatisfied_buildrequires(
    cid: str, container, requested_deps: list[str],
) -> None:
    """Print a want/have report after a failed BuildRequires install.

    The resolver's terse ``Package not found: X`` line tells the user
    which capability could not be resolved, but not what *is*
    available.  In the most common failure mode — a Mageia mirror
    that hasn't yet rebuilt the upstream package with the required
    version — the actionable information is exactly that comparison:
    "I want ``python3dist(cryptography) >= 46`` but the medium ships
    45".

    We rerun ``urpm whatprovides`` inside the container for each
    requested dep that carries a version constraint, plus any bare
    capability that resolves to nothing, and print the highest
    priority provider next to the original request.

    Args:
        cid: Container id to run the diagnostic queries inside.
        container: ContainerRunner instance.
        requested_deps: the deps passed to the failing ``urpm install``.
    """
    import re as _re
    _ver_re = _re.compile(r'\s*(?:>=|<=|=>|=<|[><=!])\s*\S+')

    from .. import colors
    print()
    print(colors.error(_("Dynamic BuildRequires cannot be satisfied:")))

    for dep in requested_deps:
        bare = _ver_re.sub('', dep).strip()
        has_constraint = (dep != bare)
        wp = container.exec(cid, ['urpm', 'whatprovides', bare])
        available = (wp.stdout or '').strip()

        if available:
            # ``whatprovides`` may list one provider per arch / medium;
            # the first line is the highest-priority candidate.
            first = available.splitlines()[0].strip()
            if has_constraint:
                # The dep carries a version constraint and the bare
                # capability resolves to *something* — show both so
                # the user can spot the version gap.
                print(f"  {colors.warning(_('want'))}: {dep}")
                print(f"  {colors.dim(_('have'))}: {first}")
            # Bare capability that resolves to something is implicitly
            # satisfied: omit it from the diagnostic to keep the noise
            # down.
        else:
            print(f"  {colors.warning(dep)} → "
                  + colors.error(_("no provider in any enabled medium")))


def _extract_buildrequires(path: str) -> list[str] | None:
    """Extract BuildRequires package names from a .spec or .src.rpm file.

    Uses ``rpmspec --parse`` for .spec files and ``rpm -qp --requires``
    for .src.rpm files, then strips version constraints to return bare
    package names.

    Returns:
        List of package names, or None on failure.
    """
    import re

    src = Path(path)
    if not src.exists():
        return None

    try:
        if src.suffix == '.spec':
            # Parse spec to expand macros, then grep BuildRequires
            result = subprocess.run(
                ['rpmspec', '--parse', str(src)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return None
            # Extract BuildRequires lines
            br_re = re.compile(r'^BuildRequires:\s*(.+)', re.IGNORECASE)
            raw_deps = []
            for line in result.stdout.splitlines():
                m = br_re.match(line.strip())
                if m:
                    raw_deps.append(m.group(1))

        elif '.src.rpm' in src.name or src.name.endswith('.src.rpm'):
            # Query SRPM for build requirements
            result = subprocess.run(
                ['rpm', '-qp', '--requires', str(src)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return None
            raw_deps = result.stdout.strip().splitlines()

        else:
            return None

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # Parse: strip version constraints (>=, <=, =, >, <) and pkgconfig()
    # "python3-devel >= 3.10" → "python3-devel"
    # "pkgconfig(libsolv)" → "pkgconfig(libsolv)" (keep as-is, rpm resolves it)
    packages = []
    ver_re = re.compile(r'\s*(?:>=|<=|=>|=<|[><=!])\s*\S+')
    for raw in raw_deps:
        # BuildRequires can list multiple deps separated by commas
        for dep in raw.split(','):
            dep = dep.strip()
            if not dep or dep.startswith('#'):
                continue
            # Strip version constraint
            dep = ver_re.sub('', dep).strip()
            if dep and dep != 'rpmlib(CompressedFileNames)':
                packages.append(dep)

    # Deduplicate
    return list(dict.fromkeys(packages))


def cmd_image_list(args, db: 'PackageDatabase') -> int:
    """List available container images."""
    from ...core.container import detect_runtime, Container
    from .. import colors

    runtime_name = getattr(args, 'runtime', None)

    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)
    images = container.images()

    if not images:
        print(_("No container images found."))
        return 0

    # Display as a table
    print(f"{'TAG':<40} {'ID':<14} {'SIZE':>10}")
    print(f"{'─' * 40} {'─' * 14} {'─' * 10}")
    for img in images:
        tag = img.get('tag', '<none>')
        img_id = img.get('id', '')[:12]
        size = img.get('size', '?')
        print(f"{tag:<40} {img_id:<14} {size:>10}")

    print(ngettext(
        "\n{count} image",
        "\n{count} images",
        len(images)).format(count=len(images)))
    return 0


def cmd_image_delete(args, db: 'PackageDatabase') -> int:
    """Delete a container image."""
    from ...core.container import detect_runtime, Container
    from .. import colors

    runtime_name = getattr(args, 'runtime', None)
    tags = args.tags
    force = getattr(args, 'force', False)

    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)
    errors = 0

    for tag in tags:
        if not container.image_exists(tag):
            print(colors.warning(
                _("Image not found: {tag}").format(tag=tag)))
            errors += 1
            continue

        if container.rmi(tag, force=force):
            print(_("Deleted: {tag}").format(tag=tag))
        else:
            print(colors.error(
                _("Failed to delete: {tag}").format(tag=tag)))
            errors += 1

    return 1 if errors else 0


def cmd_image_update(args, db: 'PackageDatabase') -> int:
    """Update a container image in-place (sync media + upgrade packages).

    Starts a temporary container from the image, runs ``urpm media update``
    and ``urpm upgrade --auto``, then commits the result as the same tag,
    replacing the old image.
    """
    from ...core.container import detect_runtime, Container
    from .. import colors

    tag = args.tag
    runtime_name = getattr(args, 'runtime', None)

    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)

    if not container.image_exists(tag):
        print(colors.error(
            _("Image not found: {tag}").format(tag=tag)))
        return 1

    # Remember old image ID so we can prune it after commit
    old_images = container.images(filter_name=tag)
    old_id = old_images[0]['id'] if old_images else None

    print(_("Updating image {tag}...").format(tag=tag))
    cid = None
    try:
        # Start temporary container
        cid = container.run(
            tag, ['sleep', 'infinity'],
            detach=True, rm=False, network='host',
        )
        print(_("  Container: {cid}").format(cid=cid[:12]))

        # Sync media
        print(_("  Updating media..."))
        ret = container.exec_stream(cid, ['urpm', 'media', 'update'])
        if ret != 0:
            print(colors.warning(
                _("  Warning: media update failed")))

        # Upgrade packages
        print(_("  Upgrading packages..."))
        ret = container.exec_stream(cid, ['urpm', 'upgrade', '--auto'])
        if ret != 0:
            print(colors.warning(
                _("  Warning: package update returned {code}").format(
                    code=ret)))

        # Stop the container before committing
        container.exec(cid, ['sh', '-c', 'kill 1 2>/dev/null || true'])

        # Commit as same tag (overwrites)
        print(_("  Committing..."), end='', flush=True)
        if not container.commit(cid, tag):
            print()
            print(colors.error(_("Failed to commit image")))
            return 1
        print(_(" done"))

        # Remove old image layer if ID changed
        new_images = container.images(filter_name=tag)
        new_id = new_images[0]['id'] if new_images else None
        if old_id and new_id and old_id != new_id:
            container.rmi(old_id, force=True)

        # Show result
        size = new_images[0]['size'] if new_images else '?'
        print(colors.success(
            _("\nImage {tag} updated ({size})").format(
                tag=tag, size=size)))
        return 0

    except Exception as e:
        print(colors.error(
            _("Error: {error}").format(error=e)))
        return 1

    finally:
        if cid:
            container.rm(cid)
