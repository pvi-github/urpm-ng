"""Build commands: mkimage, build, cleanup."""

import argparse
import os
import platform
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase
    from ...core.container import Container

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
                    print(f"Warning: local profile '{name}' ignored "
                          f"(system profile exists, use a different name)")
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
                print(f"Warning: failed to load profile {yaml_file}: {e}")

    return profiles


def get_profile_names() -> list:
    """Get list of available profile names for argument parser."""
    return list(load_profiles().keys())


def cmd_cleanup(args, db: 'PackageDatabase') -> int:
    """Handle cleanup command - unmount chroot filesystems."""
    from .. import colors

    urpm_root = getattr(args, 'urpm_root', None)
    if not urpm_root:
        print(colors.error("Error: --urpm-root is required for cleanup"))
        return 1

    root_path = Path(urpm_root)
    if not root_path.exists():
        print(colors.error(f"Error: {urpm_root} does not exist"))
        return 1

    print(f"Cleaning up mounts in {urpm_root}...")

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
                print(f"  Unmounted {mount_path}")
                unmounted += 1
            else:
                print(colors.warning(f"  Failed to unmount {mount_path}: {result.stderr.strip()}"))

    if unmounted == 0:
        print("  No mounts to clean up")
    else:
        print(colors.success(f"  {unmounted} filesystem(s) unmounted"))

    return 0


def cmd_mkimage(args, db: 'PackageDatabase') -> int:
    """Create a minimal Docker/Podman image for RPM builds."""
    from ...core.container import detect_runtime, Container
    from ...core.database import PackageDatabase
    from .. import colors

    # Set locale to C to avoid perl warnings in scriptlets
    os.environ['LC_ALL'] = 'C'
    os.environ['LANGUAGE'] = 'C'

    release = args.release
    arch = getattr(args, 'arch', None) or platform.machine()
    tag = args.tag
    keep_chroot = getattr(args, 'keep_chroot', False)
    runtime_name = getattr(args, 'runtime', None)

    # Detect container runtime
    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)
    print(f"Using {runtime.name} {runtime.version}")

    # Check if image already exists
    if container.image_exists(tag):
        print(colors.error(f"\nError: Image '{tag}' already exists."))
        print(f"\nTo replace it, first remove the existing image:")
        print(f"  {runtime.name} rmi {tag}")
        print(f"\nThen run mkimage again.")
        return 1

    # Load profile
    profile_name = getattr(args, 'profile', 'build')
    profiles = load_profiles()

    if profile_name not in profiles:
        print(colors.error(f"Error: unknown profile '{profile_name}'"))
        print(f"\nAvailable profiles:")
        for name, info in sorted(profiles.items()):
            print(f"  {name}: {info['description']}")
        return 1

    profile = profiles[profile_name]
    packages = list(profile['packages'])  # Copy to avoid modifying original

    # Add extra packages if specified
    extra_packages = getattr(args, 'packages', None)
    if extra_packages:
        packages.extend(extra_packages.split(','))

    print(f"\nCreating image: {tag}")
    print(f"  Release: {release}")
    print(f"  Architecture: {arch}")
    print(f"  Profile: {profile_name} ({profile['description']})")
    print(f"  Packages: {len(packages)}")

    # Determine working directory (default: ~/.cache/urpm/mkimage)
    workdir = getattr(args, 'workdir', None)
    if not workdir:
        # Use XDG cache directory as default (better than /tmp for large builds)
        xdg_cache = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
        workdir = os.path.join(xdg_cache, 'urpm', 'mkimage')
        os.makedirs(workdir, exist_ok=True)

    # Check available disk space (require at least 2 GB)
    MIN_SPACE_GB = 2
    try:
        stat = os.statvfs(workdir)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < MIN_SPACE_GB:
            print(colors.error(f"Insufficient disk space in {workdir}"))
            print(colors.error(f"  Available: {free_gb:.1f} GB, required: {MIN_SPACE_GB} GB"))
            print(colors.dim(f"  Use --workdir to specify a different location"))
            return 1
    except OSError as e:
        print(colors.warning(f"Could not check disk space: {e}"))

    # Create temporary directory for chroot
    tmpdir = tempfile.mkdtemp(prefix='urpm-mkimage-', dir=workdir)
    print(f"\nBuilding chroot in {tmpdir}...")

    try:
        # Create a PackageDatabase specific to the chroot
        # This ensures media configuration is stored IN the chroot, not on the host
        chroot_db_path = Path(tmpdir) / "var/lib/urpm/packages.db"
        chroot_db_path.parent.mkdir(parents=True, exist_ok=True)
        chroot_db = PackageDatabase(db_path=chroot_db_path)

        # 1. Initialize chroot with urpm
        print("\n[1/8] Initializing chroot...")
        init_args = argparse.Namespace(
            urpm_root=tmpdir,
            release=release,
            arch=arch,
            mirrorlist=None,
            auto=True,
            no_sync=False,
            no_mount=True,  # Skip mount operations - container runtime handles /dev, /proc
        )
        ret = cmd_init(init_args, chroot_db)
        if ret != 0:
            print(colors.error("Failed to initialize chroot"))
            return ret

        # 2. Install packages
        # Use noscripts when not root (user namespace) - scriptlets often fail
        use_noscripts = os.geteuid() != 0

        # Install setup+filesystem with --noscripts to avoid "group shadow does not exist"
        # The scriptlets of glibc (dependency) run before setup's files are in place
        print("\n[2/8] Installing setup (--noscripts)...")
        setup_args = argparse.Namespace(
            urpm_root=tmpdir,
            root=tmpdir,
            packages=['setup'],
            auto=True,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=False,
            noscripts=True,  # Always noscripts for bootstrap to avoid group warnings
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,
            allow_no_root=True,
            config_policy='replace',  # Replace config files in mkimage (no .rpmnew)
        )
        ret = cmd_install(setup_args, chroot_db)
        if ret != 0:
            print(colors.error("Failed to install setup"))
            return ret

        # Install filesystem to create directory structure and symlinks
        if use_noscripts:
            print("\n[3/8] Installing filesystem (--noscripts)...")
        else:
            print("\n[3/8] Installing filesystem...")
        fs_args = argparse.Namespace(
            urpm_root=tmpdir,
            root=tmpdir,
            packages=['filesystem'],
            auto=True,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=False,
            noscripts=use_noscripts,
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,
            allow_no_root=True,
            config_policy='replace',  # Replace config files in mkimage (no .rpmnew)
        )
        ret = cmd_install(fs_args, chroot_db)
        if ret != 0:
            print(colors.error("Failed to install filesystem"))
            return ret

        # DEBUG: Verify filesystem is actually installed
        rpm_db_dir = Path(tmpdir) / 'var/lib/rpm'
        if DEBUG_MKIMAGE:
            print(colors.dim(f"  DEBUG: RPM db dir: {rpm_db_dir}"))
        if rpm_db_dir.exists():
            db_files = list(rpm_db_dir.iterdir())
            if DEBUG_MKIMAGE:
                print(colors.dim(f"  DEBUG: RPM db files: {[f.name for f in db_files]}"))
            # Check if rpmdb.sqlite exists and has content
            rpmdb_sqlite = rpm_db_dir / 'rpmdb.sqlite'
            if rpmdb_sqlite.exists():
                if DEBUG_MKIMAGE:
                    print(colors.dim(f"  DEBUG: rpmdb.sqlite size: {rpmdb_sqlite.stat().st_size} bytes"))
        else:
            if DEBUG_MKIMAGE:
                print(colors.error(f"  DEBUG: RPM db dir does not exist!"))

        check = subprocess.run(
            ['rpm', '--root', tmpdir, '-q', 'filesystem'],
            capture_output=True, text=True
        )
        if check.returncode != 0:
            if DEBUG_MKIMAGE:
                print(colors.error(f"  DEBUG: filesystem NOT installed! rpm -q says: {check.stderr}"))
            # Also try rpm -qa to see what IS installed
            qa_result = subprocess.run(
                ['rpm', '--root', tmpdir, '-qa'],
                capture_output=True, text=True
            )
            pkg_count = len(qa_result.stdout.strip().split('\n')) if qa_result.stdout.strip() else 0
            if DEBUG_MKIMAGE:
                print(colors.dim(f"  DEBUG: rpm -qa shows {pkg_count} packages"))
                if pkg_count > 0 and pkg_count < 10:
                    print(colors.dim(f"  DEBUG: packages: {qa_result.stdout.strip()}"))
        else:
            if DEBUG_MKIMAGE:
                print(colors.success(f"  DEBUG: filesystem installed: {check.stdout.strip()}"))

        # Check symlinks
        bin_path = Path(tmpdir) / 'bin'
        if bin_path.is_symlink():
            if DEBUG_MKIMAGE:
                print(colors.success(f"  DEBUG: /bin is symlink -> {bin_path.resolve()}"))
        elif bin_path.exists():
            if DEBUG_MKIMAGE:
                print(colors.error(f"  DEBUG: /bin exists but is NOT a symlink!"))
        else:
            if DEBUG_MKIMAGE:
                print(colors.error(f"  DEBUG: /bin does not exist!"))

        # Install coreutils separately to ensure basename/dirname are available
        # for other packages' scriptlets
        print("\n[4/8] Installing coreutils...")
        coreutils_args = argparse.Namespace(
            urpm_root=tmpdir,
            root=tmpdir,
            packages=['coreutils'],
            auto=True,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=False,
            noscripts=use_noscripts,
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,
            allow_no_root=True,
            config_policy='replace',  # Replace config files in mkimage (no .rpmnew)
        )
        ret = cmd_install(coreutils_args, chroot_db)
        if ret != 0:
            print(colors.error("Failed to install coreutils"))
            return ret

        # Now install remaining packages
        remaining_packages = [p for p in packages if p not in ('setup', 'filesystem', 'coreutils')]
        if use_noscripts:
            print("\n[5/8] Installing packages (--noscripts for user namespace)...")
        else:
            print("\n[5/8] Installing packages...")
        install_args = argparse.Namespace(
            urpm_root=tmpdir,
            root=tmpdir,
            packages=remaining_packages,
            auto=True,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=False,
            noscripts=use_noscripts,
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,  # Wait for all scriptlets to complete
            allow_no_root=True,  # Installing to user-owned chroot
            config_policy='replace',  # Replace config files in mkimage (no .rpmnew)
        )
        ret = cmd_install(install_args, chroot_db)
        if ret != 0:
            print(colors.error("Failed to install packages"))
            return ret

        # 3. Install urpm (this project)
        print("\n[6/8] Installing urpm...")

        # First try from repos (for when it's officially available)
        # Use noscripts=True because urpm-ng's post-install runs autoconfig
        # which would conflict with the config already set up by cmd_init
        urpm_install_args = argparse.Namespace(
            urpm_root=tmpdir,
            root=tmpdir,
            packages=['urpm'],
            auto=True,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=False,
            noscripts=True,  # Skip post-install autoconfig (already done by cmd_init)
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,  # Wait for all scriptlets to complete
            allow_no_root=True,  # Installing to user-owned chroot
            config_policy='replace',  # Replace config files in mkimage (no .rpmnew)
        )
        ret = cmd_install(urpm_install_args, chroot_db)

        if ret != 0:
            # urpm not in repos - look for local RPM
            print("  urpm not found in repositories, looking for local RPM...")

            # Search common locations (all architectures)
            search_dirs = [
                Path.home() / 'Downloads',
                Path('./rpmbuild/RPMS'),
                Path.home() / 'rpmbuild/RPMS',
                Path('.'),
            ]

            urpm_rpm = None
            for search_dir in search_dirs:
                if search_dir.exists():
                    # Search recursively for urpm-ng-core or urpm-ng RPMs
                    candidates = list(search_dir.glob('**/urpm-ng-core-*.rpm'))
                    if not candidates:
                        candidates = list(search_dir.glob('**/urpm-ng-*.rpm'))
                    if candidates:
                        # Take most recent
                        urpm_rpm = max(candidates, key=lambda p: p.stat().st_mtime)
                        break

            if urpm_rpm:
                default_path = str(urpm_rpm)
                prompt = f"  Found: {default_path}\n  Press Enter to use, or provide another path: "
            else:
                default_path = ""
                prompt = "  Path to urpm RPM file: "

            user_input = input(prompt).strip()
            rpm_path = Path(user_input) if user_input else (Path(default_path) if default_path else None)

            if not rpm_path or not rpm_path.exists():
                print(colors.error("No urpm RPM provided or file not found"))
                print("  Build it with: make rpm")
                return 1

            # Install RPM using urpm with noscripts (post-install autoconfig
            # would conflict with config already set up by cmd_init)
            urpm_local_args = argparse.Namespace(
                urpm_root=tmpdir,
                root=tmpdir,
                packages=[str(rpm_path.resolve())],
                auto=True,
                without_recommends=True,
                with_suggests=False,
                download_only=False,
                nodeps=False,
                nosignature=True,  # Local build, no signature
                noscripts=True,  # Skip post-install autoconfig (already done by cmd_init)
                force=False,
                reinstall=False,
                debug=None,
                watched=None,
                prefer=None,
                all=False,
                test=False,
                sync=True,  # Wait for all scriptlets to complete
                allow_no_root=True,  # Installing to user-owned chroot
                config_policy='replace',  # Replace config files in mkimage (no .rpmnew)
            )
            ret = cmd_install(urpm_local_args, chroot_db)
            if ret != 0:
                print(colors.error(f"Failed to install urpm"))
                return ret
            print(colors.success(f"  Installed {rpm_path.name}"))
        else:
            print(colors.success("  urpm installed from repositories"))

        # 4. Cleanup chroot to reduce image size
        print("\n[7/8] Cleaning up chroot...")
        # Ensure database is closed (may already be closed before urpm install)
        chroot_db.close()
        _cleanup_chroot_for_image(tmpdir)

        # Unmount any filesystems
        cleanup_args = argparse.Namespace(urpm_root=tmpdir)
        cmd_cleanup(cleanup_args, db)

        # 5. Create container image
        print(f"\n[8/8] Creating container image {tag}...")
        # Estimate chroot size for user feedback
        try:
            total_size = sum(
                os.path.getsize(os.path.join(dirpath, filename))
                for dirpath, dirnames, filenames in os.walk(tmpdir)
                for filename in filenames
                if os.path.isfile(os.path.join(dirpath, filename))
            )
            size_mb = total_size / (1024 * 1024)
            print(f"  Chroot size: {size_mb:.1f} MB")
        except Exception:
            pass
        print(f"  Archiving and importing (this may take a moment)...", end='', flush=True)
        # Use podman unshare for import when not root (same UID/GID mapping as install)
        if not container.import_from_dir(tmpdir, tag, use_unshare=use_noscripts):
            print()  # newline after "..."
            print(colors.error("Failed to create container image"))
            return 1
        print(" done")

        # Get image size
        images = container.images(filter_name=tag)
        size = images[0]['size'] if images else 'unknown'

        print(colors.success(f"\n{'='*60}"))
        print(colors.success(f"Image created successfully!"))
        print(colors.success(f"{'='*60}"))
        print(f"  Tag:  {tag}")
        print(f"  Size: {size}")
        print(f"\nUsage:")
        print(f"  {runtime.name} run -it {tag} /bin/bash")
        print(f"  urpm build --image {tag} ./package.src.rpm")

        return 0

    except Exception as e:
        print(colors.error(f"Error: {e}"))
        return 1

    finally:
        if not keep_chroot:
            print(f"\nCleaning up temporary directory...")
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\nChroot kept at: {tmpdir}")


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

    print(f"  Removed {removed} cache/log entries")

    # Ensure /var/tmp exists (required by RPM scriptlets)
    var_tmp = os.path.join(root, 'var', 'tmp')
    if not os.path.exists(var_tmp):
        os.makedirs(var_tmp, mode=0o1777, exist_ok=True)
        print(f"  Created /var/tmp")

    # Fix PATH for Mageia 9 compatibility (/bin and /sbin are separate)
    profile_d = os.path.join(root, 'etc', 'profile.d')
    if os.path.isdir(profile_d):
        path_script = os.path.join(profile_d, 'zz-path-compat.sh')
        if not os.path.exists(path_script):
            try:
                with open(path_script, 'w') as f:
                    f.write('# Mageia 9 compatibility: add /bin /sbin if not symlinks\n')
                    f.write('[ -d /bin ] && [ ! -L /bin ] && export PATH="$PATH:/bin"\n')
                    f.write('[ -d /sbin ] && [ ! -L /sbin ] && export PATH="$PATH:/sbin"\n')
                os.chmod(path_script, 0o644)
                print(f"  Created /etc/profile.d/zz-path-compat.sh")
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
            print(f"  Created /etc/machine-id")
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
            print(colors.warning(f"No RPMs found matching: {pattern}"))

    # Detect container runtime
    try:
        runtime = detect_runtime(runtime_name)
    except RuntimeError as e:
        print(colors.error(str(e)))
        return 1

    container = Container(runtime)
    print(f"Using {runtime.name} {runtime.version}")

    # Check image exists
    if not container.image_exists(image):
        print(colors.error(f"Image not found: {image}"))
        print(colors.dim("Create one with: urpm mkimage --release 10 --tag <tag>"))
        return 1

    # Validate sources
    valid_sources = []
    for source in sources:
        source_path = Path(source)
        if not source_path.exists():
            print(colors.warning(f"Source not found: {source}"))
            continue
        # Accept .spec files or .src.rpm (source RPMs)
        if source_path.suffix == '.spec':
            valid_sources.append(source_path)
        elif source_path.suffix == '.rpm' and '.src.' in source_path.name:
            valid_sources.append(source_path)
        elif source_path.suffix == '.rpm':
            print(colors.warning(f"Binary RPM cannot be built: {source}"))
            print(colors.dim(f"  Use a .src.rpm or .spec file instead"))
            continue
        else:
            print(colors.warning(f"Unsupported source type: {source}"))
            continue

    if not valid_sources:
        print(colors.error("No valid sources to build"))
        return 1

    print(f"\nBuilding {len(valid_sources)} package(s)")
    print(f"  Image:  {image}")
    if with_rpms:
        print(f"  Pre-install: {len(with_rpms)} local RPM(s)")
    if parallel > 1:
        print(f"  Parallel: {parallel}")

    results = []

    def build_one(source_path: Path) -> tuple:
        """Build a single package. Returns (source, success, message)."""
        return _build_single_package(
            container, image, source_path, output_dir, keep_container, with_rpms
        )

    if parallel > 1 and len(valid_sources) > 1:
        # Parallel builds
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(build_one, src): src for src in valid_sources}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                source, success, msg = result
                status = colors.success("OK") if success else colors.error("FAIL")
                print(f"  [{status}] {source.name}: {msg}")
    else:
        # Sequential builds
        for source_path in valid_sources:
            print(f"\n{'='*60}")
            print(f"Building: {source_path.name}")
            print(f"{'='*60}")
            result = build_one(source_path)
            results.append(result)

    # Summary
    success_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - success_count

    print(f"\n{'='*60}")
    print("Build Summary")
    print(f"{'='*60}")
    print(f"  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"  Output:  {output_dir}")

    if fail_count > 0:
        print(f"\nFailed packages:")
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
    with_rpms: list = None
) -> tuple:
    """Build a single package in a container.

    Args:
        container: Container runtime wrapper
        image: Container image to use
        source_path: Path to .spec or .src.rpm file
        output_dir: Output directory for SRPM builds
        keep_container: Keep container after build for debugging
        with_rpms: List of local RPM paths to install before build

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
        print(f"  Container: {cid[:12]}")

        # 2. Prepare rpmbuild directories
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/SPECS'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/SOURCES'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/BUILD'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/RPMS'])
        container.exec(cid, ['mkdir', '-p', '/root/rpmbuild/SRPMS'])

        # DEBUG SSL IN BUILD... TODO: do that in mkimage
        container.exec(cid, ['/bin/update-ca-trust', 'extract'])

        # 3. Copy source into container
        print(f"  Copying source...")

        if source_path.suffix == '.rpm' and '.src.' in source_path.name:
            # Source RPM - install it to extract spec and sources
            if not container.cp(str(source_path), f"{cid}:/root/rpmbuild/SRPMS/"):
                return (source_path, False, "Failed to copy SRPM")

            print(f"  Installing SRPM...")
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
                print(f"  Copying {len(source_files)} source files from {sources_dir}...")
                # Copy entire directory content at once
                container.cp(f"{sources_dir}/.", f"{cid}:/root/rpmbuild/SOURCES/")
            else:
                print(colors.warning(f"  Warning: No SOURCES directory found"))

        else:
            return (source_path, False, f"Unsupported source type: {source_path.suffix}")

        # 4. Install rpm-build (provides rpmbuild)
        print(f"  Installing rpm-build...")
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--sync', 'rpm-build'
        ])
        if ret != 0:
            return (source_path, False, "Failed to install rpm-build")

        # 4b. Install local RPMs (dependencies built locally)
        if with_rpms:
            print(f"  Installing {len(with_rpms)} local RPM(s)...")
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
                'urpm', 'install', '--auto', '--sync', '--nosignature'
            ] + rpm_paths_in_container)
            if ret != 0:
                return (source_path, False, "Failed to install local RPMs")

        # 5. Install build dependencies
        print(f"  Installing BuildRequires...")
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--sync', '--builddeps', spec_path
        ])
        if ret != 0:
            return (source_path, False, f"BuildRequires install failed")

        # 6. Build the package
        print(f"  Building...")
        # Get package name from spec for log naming (log.<Name>)
        result = container.exec(cid, ['rpmspec', '-q', '--srpm', '--qf', '%{name}', spec_path])
        pkg_name = result.stdout.strip() if result.returncode == 0 else source_path.stem
        container_log = f'/tmp/log.{pkg_name}'
        result = container.exec_stream(cid, [
            'bash', '-c', f'rpmbuild -ba {spec_path} 2>&1 | tee {container_log}'
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
            print(f"  Build log: {log_file}")

        if build_failed:
            return (source_path, False, f"rpmbuild failed (see {log_file})")

        print(f"  Copying RPMs to {rpms_dir}/")
        container.cp(f"{cid}:/root/rpmbuild/RPMS/.", str(rpms_dir))

        print(f"  Copying SRPMs to {srpms_dir}/")
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
