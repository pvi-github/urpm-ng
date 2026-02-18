"""Kernel and autoremove helper functions."""

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from .package import extract_pkg_name


# =============================================================================
# Configuration file management
# =============================================================================

CONFIG_FILE = Path('/etc/urpm/autoremove.conf')


def get_running_kernel() -> str:
    """Get the running kernel package name."""
    release = os.uname().release  # e.g., "6.6.58-1.mga9-desktop"
    # Extract version-release part to match against kernel packages
    return release


def get_root_fstype() -> str:
    """Get the filesystem type of the root partition."""
    try:
        result = subprocess.run(
            ['findmnt', '-n', '-o', 'FSTYPE', '/'],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().lower()
    except Exception:
        return 'ext4'  # Safe default


def read_config() -> dict:
    """Read the autoremove configuration file.

    Returns:
        Dict with 'blacklist', 'redlist' (sets) and 'kernel_keep' (int)
    """
    config = {
        'blacklist': set(),
        'redlist': set(),
        'kernel_keep': 2,
    }

    if not CONFIG_FILE.exists():
        return config

    try:
        current_section = None
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1].lower()
            elif '=' in line:
                key, value = line.split('=', 1)
                key = key.strip().lower()
                value = value.strip()
                if key == 'kernel_keep':
                    try:
                        config['kernel_keep'] = int(value)
                    except ValueError:
                        pass
            elif current_section in ('blacklist', 'redlist'):
                config[current_section].add(line)
    except Exception:
        pass

    return config


def write_config(config: dict) -> bool:
    """Write the autoremove configuration file.

    Args:
        config: Dict with 'blacklist', 'redlist' (sets) and 'kernel_keep' (int)

    Returns:
        True on success
    """
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            '# urpm autoremove configuration',
            '# Managed by urpm config command',
            '',
            f'kernel_keep = {config.get("kernel_keep", 2)}',
            '',
            '[blacklist]',
            '# Packages that must never be removed (in addition to built-in list)',
        ]
        for pkg in sorted(config.get('blacklist', [])):
            lines.append(pkg)

        lines.extend([
            '',
            '[redlist]',
            '# Packages requiring confirmation before removal (in addition to built-in list)',
        ])
        for pkg in sorted(config.get('redlist', [])):
            lines.append(pkg)

        CONFIG_FILE.write_text('\n'.join(lines) + '\n')
        return True
    except PermissionError:
        print(f"Error: Permission denied writing to {CONFIG_FILE}")
        print("Try running with sudo")
        return False
    except Exception as e:
        print(f"Error writing config: {e}")
        return False


def get_user_blacklist() -> set:
    """Get user-configured blacklist packages."""
    return read_config().get('blacklist', set())


def get_user_redlist() -> set:
    """Get user-configured redlist packages."""
    return read_config().get('redlist', set())


def get_kernel_keep() -> int:
    """Get the number of kernels to keep."""
    return read_config().get('kernel_keep', 2)


def is_running_kernel(pkg_name: str, pkg_version: str, pkg_release: str) -> bool:
    """Check if a package is the running kernel."""
    running = os.uname().release
    # Running kernel looks like "6.6.58-1.mga9-desktop"
    # Package version-release looks like "6.6.58-1.mga9"
    return running.startswith(f"{pkg_version}-{pkg_release}")


def get_blacklist() -> set:
    """Get the blacklist of critical packages that must never be removed.

    These packages, if removed, would make the system unbootable or unusable.
    """
    # Static blacklist - absolute minimum for a working system
    blacklist = {
        # Core system
        'glibc', 'basesystem', 'filesystem', 'setup',
        'systemd', 'systemd-libs', 'dbus', 'dbus-libs',
        'coreutils', 'bash', 'rpm', 'rpm-libs',
        'util-linux', 'util-linux-core',
        'shadow-utils', 'pam', 'pam-libs',
        # Boot
        'grub2', 'grub2-common', 'grub2-tools', 'grub2-efi',
        'dracut',
    }

    # Dynamic: running kernel
    running = get_running_kernel()
    # Add kernel packages matching running version
    # The kernel name pattern is kernel-<variant>-<version>-<release>.<arch>
    # Running kernel is like "6.6.58-1.mga9-desktop"
    # We protect packages where version-release matches

    # Dynamic: root filesystem tools
    fstype = get_root_fstype()
    fs_packages = {
        'ext4': {'e2fsprogs'},
        'ext3': {'e2fsprogs'},
        'ext2': {'e2fsprogs'},
        'xfs': {'xfsprogs'},
        'btrfs': {'btrfs-progs'},
        'f2fs': {'f2fs-tools'},
    }
    blacklist.update(fs_packages.get(fstype, set()))

    # Add user-configured blacklist
    blacklist.update(get_user_blacklist())

    return blacklist


def get_redlist() -> set:
    """Get the redlist of packages that require confirmation before removal.

    These packages are generally useful and removing them might be a mistake.
    """
    redlist = {
        # Filesystem and storage tools
        'acl', 'attr', 'parted', 'gdisk', 'fdisk',
        'cryptsetup', 'lvm2', 'mdadm',
        'e2fsprogs', 'xfsprogs', 'btrfs-progs', 'dosfstools', 'ntfs-3g',
        'fuse', 'fuse3', 'udisks2',
        # Network
        'wireless-tools', 'iw', 'wpa_supplicant',
        'networkmanager', 'network-manager-applet',
        'dhcp-client', 'openssh-clients', 'openssh-server',
        # System administration
        'sudo', 'polkit',
        'msec', 'msec-gui', 'drakxtools', 'drakguard',
        # X11 drivers
        'x11-driver-input', 'x11-driver-video',
        'x11-driver-input-evdev', 'x11-driver-input-libinput',
        'x11-driver-input-synaptics', 'x11-driver-input-wacom',
        # Printing
        'cups', 'system-config-printer', 'hplip',
        # Desktop portals
        'xdg-desktop-portal', 'xdg-desktop-portal-gtk', 'xdg-desktop-portal-kde',
        'flatpak',
        # Fonts
        'fonts-ttf-dejavu', 'fonts-dejavu-common', 'fonts-ttf-liberation',
        # Sound
        'pulseaudio', 'pipewire', 'alsa-utils', 'alsa-plugins-pulseaudio',
    }

    # Add user-configured redlist
    redlist.update(get_user_redlist())

    return redlist


def find_old_kernels(keep_count: int = None) -> list:
    """Find old kernels that can be removed.

    Args:
        keep_count: Number of recent kernels to keep (in addition to running).
                    If None, uses the configured value from kernel-keep.

    Returns:
        List of (name, nevra, size) tuples for kernels to remove
    """
    import rpm
    from collections import defaultdict

    if keep_count is None:
        keep_count = get_kernel_keep()

    # Get running kernel version
    running_kernel = os.uname().release  # e.g., "6.12.57+deb13-amd64"

    ts = rpm.TransactionSet()
    kernels = []

    # Find all installed kernel packages
    for hdr in ts.dbMatch('name', 'kernel'):
        name = hdr[rpm.RPMTAG_NAME]
        version = hdr[rpm.RPMTAG_VERSION]
        release = hdr[rpm.RPMTAG_RELEASE]
        arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
        size = hdr[rpm.RPMTAG_SIZE] or 0

        # Build kernel version string to compare with running
        kernel_ver = f"{version}-{release}.{arch}"

        nevra = f"{name}-{version}-{release}.{arch}"
        kernels.append({
            'name': name,
            'nevra': nevra,
            'version': version,
            'release': release,
            'arch': arch,
            'size': size,
            'kernel_ver': kernel_ver,
            'is_running': running_kernel.startswith(f"{version}-{release}"),
        })

    # Also find kernel-desktop, kernel-server, etc.
    for variant in ['kernel-desktop', 'kernel-server', 'kernel-laptop',
                    'kernel-desktop-devel', 'kernel-server-devel']:
        for hdr in ts.dbMatch('name', variant):
            name = hdr[rpm.RPMTAG_NAME]
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
            size = hdr[rpm.RPMTAG_SIZE] or 0

            kernel_ver = f"{version}-{release}.{arch}"
            nevra = f"{name}-{version}-{release}.{arch}"

            kernels.append({
                'name': name,
                'nevra': nevra,
                'version': version,
                'release': release,
                'arch': arch,
                'size': size,
                'kernel_ver': kernel_ver,
                'is_running': running_kernel.startswith(f"{version}-{release}"),
            })

    if not kernels:
        return []

    # Group by base version (version-release)
    by_version = defaultdict(list)
    for k in kernels:
        ver_key = (k['version'], k['release'])
        by_version[ver_key].append(k)

    # Sort versions (newest first)
    sorted_versions = sorted(by_version.keys(), reverse=True)

    # Find versions to remove (skip running and keep_count newest)
    versions_to_keep = set()
    kept = 0
    for ver in sorted_versions:
        # Check if this version is running
        is_running = any(k['is_running'] for k in by_version[ver])
        if is_running:
            versions_to_keep.add(ver)
        elif kept < keep_count:
            versions_to_keep.add(ver)
            kept += 1

    # Collect kernels to remove
    to_remove = []
    for ver, pkgs in by_version.items():
        if ver not in versions_to_keep:
            for k in pkgs:
                to_remove.append((k['name'], k['nevra'], k['size']))

    return to_remove


def find_faildeps(db: 'PackageDatabase') -> tuple:
    """Find orphan deps from interrupted transactions.

    Returns:
        Tuple of (list of (name, nevra) tuples, list of transaction IDs to mark cleaned)
    """
    interrupted = db.get_interrupted_transactions()
    if not interrupted:
        return [], []

    all_orphans = []
    interrupted_ids = []

    for trans in interrupted:
        orphans = db.get_orphan_deps(trans['id'])
        if orphans:
            for nevra in orphans:
                name = extract_pkg_name(nevra)
                all_orphans.append((name, nevra))
            interrupted_ids.append(trans['id'])

    # Remove duplicates
    seen = set()
    unique = []
    for name, nevra in all_orphans:
        if nevra not in seen:
            seen.add(nevra)
            unique.append((name, nevra))

    return unique, interrupted_ids


# Backwards compatibility aliases (with underscore prefix)
_get_running_kernel = get_running_kernel
_get_root_fstype = get_root_fstype
_get_blacklist = get_blacklist
_get_redlist = get_redlist
_read_config = read_config
_write_config = write_config
_get_user_blacklist = get_user_blacklist
_get_user_redlist = get_user_redlist
_get_kernel_keep = get_kernel_keep
_is_running_kernel = is_running_kernel
_find_old_kernels = find_old_kernels
_find_faildeps = find_faildeps
