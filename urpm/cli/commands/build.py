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
        print("\nTo replace it, first remove the existing image:")
        print(f"  {runtime.name} rmi {tag}")
        print("\nThen run mkimage again.")
        return 1

    # Base packages for build image
    packages = [
        'filesystem',         # Must be first - creates base directory structure
        'basesystem-minimal',
        'coreutils',          # Essential: ls, cp, mv, cat, etc.
        'grep',               # Essential: used by bash profile scripts
        'sed',                # Essential: used by bash profile scripts
        'findutils',          # Essential: find, xargs
        'vim-minimal',
        'locales',
        'locales-en',
        'bash',
        'rpm',
        'curl',
        'wget',
        'ca-certificates',    # SSL certificates for pip/https
        'cronie',
        'urpmi',
    ]

    # Add extra packages if specified
    extra_packages = getattr(args, 'packages', None)
    if extra_packages:
        packages.extend(extra_packages.split(','))

    print(f"\nCreating image: {tag}")
    print(f"  Release: {release}")
    print(f"  Architecture: {arch}")
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
            print(colors.dim("  Use --workdir to specify a different location"))
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
        print("\n[1/5] Initializing chroot...")
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

        # Install filesystem FIRST in separate transaction
        # This ensures /bin -> usr/bin symlinks are created before other packages
        if use_noscripts:
            print("\n[2/5] Installing filesystem (--noscripts)...")
        else:
            print("\n[2/5] Installing filesystem...")
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
                print(colors.error("  DEBUG: RPM db dir does not exist!"))

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
            print(colors.success(f"  DEBUG: filesystem installed: {check.stdout.strip()}"))

        # Check symlinks
        bin_path = Path(tmpdir) / 'bin'
        if bin_path.is_symlink():
            if DEBUG_MKIMAGE:
                print(colors.success(f"  DEBUG: /bin is symlink -> {bin_path.resolve()}"))
        elif bin_path.exists():
            if DEBUG_MKIMAGE:
                print(colors.error("  DEBUG: /bin exists but is NOT a symlink!"))
        else:
            if DEBUG_MKIMAGE:
                print(colors.error("  DEBUG: /bin does not exist!"))

        # Now install remaining packages (filesystem already provides /bin -> usr/bin etc)
        remaining_packages = [p for p in packages if p != 'filesystem']
        if use_noscripts:
            print("\n[2.5/6] Installing packages (--noscripts for user namespace)...")
        else:
            print("\n[2.5/6] Installing packages...")
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
        )
        ret = cmd_install(install_args, chroot_db)
        if ret != 0:
            print(colors.error("Failed to install packages"))
            return ret

        # 3. Install urpm (this project)
        print("\n[3/6] Installing urpm...")

        # First try from repos (for when it's officially available)
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
        )
        ret = cmd_install(urpm_install_args, chroot_db)

        if ret != 0:
            # urpm not in repos - look for local RPM
            print("  urpm not found in repositories, looking for local RPM...")

            # Search common locations
            search_paths = [
                Path.home() / 'Downloads',
                Path('./rpmbuild/RPMS/noarch'),
                Path.home() / 'rpmbuild/RPMS/noarch',
                Path('.'),
            ]

            urpm_rpm = None
            for search_path in search_paths:
                if search_path.exists():
                    candidates = list(search_path.glob('urpm-ng-*.noarch.rpm'))
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

            # Install RPM using urpm with sync mode (waits for all scriptlets)
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
            )
            ret = cmd_install(urpm_local_args, chroot_db)
            if ret != 0:
                print(colors.error("Failed to install urpm"))
                return ret
            print(colors.success(f"  Installed {rpm_path.name}"))
        else:
            print(colors.success("  urpm installed from repositories"))

        # 4. Cleanup chroot to reduce image size
        print("\n[4/6] Cleaning up chroot...")
        # Close chroot database to flush all data before image creation
        chroot_db.close()
        _cleanup_chroot_for_image(tmpdir)

        # 5. Unmount filesystems
        print("\n[5/6] Unmounting filesystems...")
        cleanup_args = argparse.Namespace(urpm_root=tmpdir)
        cmd_cleanup(cleanup_args, db)

        # 6. Create container image
        print(f"\n[6/6] Creating container image {tag}...")
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
        print("  Archiving and importing (this may take a moment)...", end='', flush=True)
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
        print(colors.success("Image created successfully!"))
        print(colors.success(f"{'='*60}"))
        print(f"  Tag:  {tag}")
        print(f"  Size: {size}")
        print("\nUsage:")
        print(f"  {runtime.name} run -it {tag} /bin/bash")
        print(f"  urpm build --image {tag} ./package.src.rpm")

        return 0

    except Exception as e:
        print(colors.error(f"Error: {e}"))
        return 1

    finally:
        if not keep_chroot:
            print("\nCleaning up temporary directory...")
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"\nChroot kept at: {tmpdir}")


def _cleanup_chroot_for_image(root: str):
    """Clean up chroot before creating container image.

    Removes caches, logs, and temporary files to reduce image size.
    """
    import glob


    cleanup_patterns = [
        'var/cache/urpmi/*',
        'var/cache/dnf/*',
        'var/lib/urpm/medias/*/RPMS.*.cache',
        'var/log/*',
        'tmp/*',
        'var/tmp/*',
        'root/.bash_history',
        'usr/share/doc/*',
        'usr/share/man/*',
        'usr/share/info/*',
    ]

    removed = 0
    for pattern in cleanup_patterns:
        for path in glob.glob(os.path.join(root, pattern)):
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
        print("  Created /var/tmp")

    # Create /etc/machine-id if missing (required by systemd, dbus, etc.)
    machine_id_path = os.path.join(root, 'etc', 'machine-id')
    if not os.path.exists(machine_id_path):
        try:
            import uuid
            machine_id = uuid.uuid4().hex  # 32 hex chars, no dashes
            with open(machine_id_path, 'w') as f:
                f.write(machine_id + '\n')
            print("  Created /etc/machine-id")
        except (IOError, OSError):
            pass


def cmd_build(args, db: 'PackageDatabase') -> int:
    """Build RPM package(s) in isolated containers."""
    from ...core.container import detect_runtime, Container
    from .. import colors

    image = args.image
    sources = args.sources
    output_dir = Path(args.output)
    parallel = getattr(args, 'parallel', 1)
    keep_container = getattr(args, 'keep_container', False)
    runtime_name = getattr(args, 'runtime', None)

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
            print(colors.dim("  Use a .src.rpm or .spec file instead"))
            continue
        else:
            print(colors.warning(f"Unsupported source type: {source}"))
            continue

    if not valid_sources:
        print(colors.error("No valid sources to build"))
        return 1

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nBuilding {len(valid_sources)} package(s)")
    print(f"  Image:  {image}")
    print(f"  Output: {output_dir}")
    if parallel > 1:
        print(f"  Parallel: {parallel}")

    results = []

    def build_one(source_path: Path) -> tuple:
        """Build a single package. Returns (source, success, message)."""
        return _build_single_package(
            container, image, source_path, output_dir, keep_container
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
        print("\nFailed packages:")
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
    keep_container: bool
) -> tuple:
    """Build a single package in a container.

    Returns:
        Tuple of (source_path, success, message)
    """
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
        print("  Copying source...")

        if source_path.suffix == '.rpm' and '.src.' in source_path.name:
            # Source RPM - install it to extract spec and sources
            if not container.cp(str(source_path), f"{cid}:/root/rpmbuild/SRPMS/"):
                return (source_path, False, "Failed to copy SRPM")

            print("  Installing SRPM...")
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
                print(colors.warning("  Warning: No SOURCES directory found"))

        else:
            return (source_path, False, f"Unsupported source type: {source_path.suffix}")

        # 4. Install rpm-build (provides rpmbuild)
        print("  Installing rpm-build...")
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--sync', 'rpm-build'
        ])
        if ret != 0:
            return (source_path, False, "Failed to install rpm-build")

        # 5. Install build dependencies
        print("  Installing BuildRequires...")
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--sync', '--builddeps', spec_path
        ])
        if ret != 0:
            return (source_path, False, "BuildRequires install failed")

        # 6. Build the package
        print("  Building...")
        result = container.exec_stream(cid, [
            'rpmbuild', '-ba', spec_path
        ])
        if result != 0:
            return (source_path, False, "rpmbuild failed")

        # 7. Copy results out
        print("  Retrieving results...")

        # Determine output location
        if is_spec_build and workspace:
            # For spec builds, output to workspace/{RPMS,SRPMS}
            rpms_dir = workspace / 'RPMS'
            srpms_dir = workspace / 'SRPMS'
        else:
            # For SRPM builds, output to specified output_dir
            pkg_output = output_dir / source_path.stem.replace('.src', '')
            pkg_output.mkdir(parents=True, exist_ok=True)
            rpms_dir = pkg_output / 'RPMS'
            srpms_dir = pkg_output / 'SRPMS'

        rpms_dir.mkdir(parents=True, exist_ok=True)
        srpms_dir.mkdir(parents=True, exist_ok=True)

        container.cp(f"{cid}:/root/rpmbuild/RPMS/.", str(rpms_dir))
        container.cp(f"{cid}:/root/rpmbuild/SRPMS/.", str(srpms_dir))

        # 8. Copy build log (to SPECS directory for spec builds)
        if is_spec_build and workspace:
            log_dir = workspace / 'SPECS'
        else:
            log_dir = rpms_dir.parent
        # Get build.log if exists
        result = container.exec(cid, ['cat', '/root/rpmbuild/BUILD/build.log'])
        if result.returncode == 0 and result.stdout:
            log_file = log_dir / f"{source_path.stem}.build.log"
            log_file.write_text(result.stdout)

        # Count built packages
        rpm_count = len(list(rpms_dir.rglob('*.rpm')))
        srpm_count = len(list(srpms_dir.rglob('*.rpm')))

        output_location = workspace if (is_spec_build and workspace) else rpms_dir.parent
        return (source_path, True, f"{rpm_count} RPMs, {srpm_count} SRPMs -> {output_location}")

    except Exception as e:
        return (source_path, False, str(e))

    finally:
        # Always cleanup container unless --keep-container
        if cid and not keep_container:
            container.rm(cid)
