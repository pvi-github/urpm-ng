"""
Main CLI entry point for urpm

Provides a modern CLI with short aliases:
- urpm install / urpm i  (like rpm -i, urpmi)
- urpm erase / urpm e    (like rpm -e, urpme)
- urpm rollback / urpm r
- urpm search / urpm s / urpm query / urpm q  (like rpm -q)
- urpm history / urpm h
- urpm media / urpm m
- etc.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__
from ..core.database import PackageDatabase
from .helpers.package import (
    extract_pkg_name as _extract_pkg_name,
    extract_family as _extract_family,
    get_installed_families as _get_installed_families,
    resolve_virtual_package as _resolve_virtual_package,
)
from .helpers.debug import (
    DEBUG_LAST_INSTALLED_DEPS,
    DEBUG_LAST_REMOVED_DEPS,
    DEBUG_INSTALLED_DEPS_COPY,
    DEBUG_PREV_INSTALLED_DEPS,
    write_debug_file as _write_debug_file,
    clear_debug_file as _clear_debug_file,
    copy_installed_deps_list as _copy_installed_deps_list,
    notify_urpmd_cache_invalidate as _notify_urpmd_cache_invalidate,
)
from .helpers.kernel import (
    CONFIG_FILE,
    get_running_kernel as _get_running_kernel,
    get_root_fstype as _get_root_fstype,
    get_blacklist as _get_blacklist,
    get_redlist as _get_redlist,
    read_config as _read_config,
    write_config as _write_config,
    get_user_blacklist as _get_user_blacklist,
    get_user_redlist as _get_user_redlist,
    get_kernel_keep as _get_kernel_keep,
    is_running_kernel as _is_running_kernel,
    find_old_kernels as _find_old_kernels,
    find_faildeps as _find_faildeps,
)
from .helpers.resolver import (
    extract_version as _extract_version,
    group_by_version as _group_by_version,
    create_resolver as _create_resolver,
)
from .helpers.media import (
    KNOWN_VERSIONS,
    KNOWN_ARCHES,
    KNOWN_CLASSES,
    KNOWN_TYPES,
    generate_media_name as _generate_media_name,
    generate_short_name as _generate_short_name,
    generate_server_name as _generate_server_name,
    parse_mageia_media_url,
    parse_custom_media_url,
    fetch_media_pubkey as _fetch_media_pubkey,
    get_gpg_key_info as _get_gpg_key_info,
    is_key_in_rpm_keyring as _is_key_in_rpm_keyring,
    import_gpg_key as _import_gpg_key,
)
from .commands.cache import (
    cmd_cache_info,
    cmd_cache_clean,
    cmd_cache_rebuild,
    cmd_cache_stats,
    cmd_cache_rebuild_fts,
)
from .commands.peer import cmd_peer
from .commands.config import cmd_config, cmd_key
from .commands.history import cmd_history, cmd_undo, cmd_rollback
from .commands.server import (
    cmd_server_list, cmd_server_add, cmd_server_remove,
    cmd_server_enable, cmd_server_disable, cmd_server_priority,
    cmd_server_test, cmd_server_ipmode, cmd_server_autoconfig,
)
from .commands.mirror import (
    cmd_mirror_status, cmd_mirror_enable, cmd_mirror_disable,
    cmd_mirror_quota, cmd_mirror_disable_version, cmd_mirror_enable_version,
    cmd_mirror_clean, cmd_mirror_sync, cmd_mirror_ratelimit,
)
from .commands.media import (
    cmd_media_list, cmd_init, cmd_media_add, cmd_media_remove,
    cmd_media_enable, cmd_media_disable, cmd_media_update,
    cmd_media_import, cmd_media_set, cmd_media_seed_info,
    cmd_media_link, cmd_media_autoconfig, parse_urpmi_cfg,
    STANDARD_MEDIA_TYPES,
)
from .commands.query import (
    cmd_search, cmd_show, cmd_list, cmd_provides, cmd_whatprovides, cmd_find,
)


# Debug flag for preferences matching - set to True to enable debug output
DEBUG_PREFERENCES = False
DEBUG_MKIMAGE = False
DEBUG_BUILD = True
DEBUG_INSTALL = False


def check_dependencies() -> list:
    """Check for required Python modules.

    Returns:
        List of missing module names (empty if all OK)
    """
    missing = []

    # Check libsolv (required for dependency resolution)
    try:
        import solv
    except ImportError:
        missing.append(('python3-solv', 'dependency resolution'))

    # Check zstandard (required for .cz decompression)
    try:
        import zstandard
    except ImportError:
        missing.append(('python3-zstandard', 'synthesis decompression'))

    return missing


def print_missing_dependencies(missing: list):
    """Print error message for missing dependencies."""
    print("ERROR: Missing required Python modules:\n", file=sys.stderr)
    for pkg, purpose in missing:
        print(f"  - {pkg} ({purpose})", file=sys.stderr)
    print(f"\nInstall with:", file=sys.stderr)
    print(f"  urpmi {' '.join(pkg for pkg, _ in missing)}", file=sys.stderr)


def print_quickstart_guide():
    """Print a quick start guide for new users with no media configured."""
    from . import colors

    media_add_cmd = 'sudo urpm media add Core <mirror_url>'

    print(f"""
{colors.bold('urpm - Modern package manager for Mageia Linux')}

{colors.warning('No media configured yet!')}

{colors.bold('Quick Start:')}

  1. Import media from existing urpmi configuration:
     {colors.success('sudo urpm media import')}

  2. Or add media manually:
     {colors.success(media_add_cmd)}

  3. Start the daemon for P2P sharing and background sync:
     {colors.success('sudo systemctl start urpmd')}

  4. Install packages:
     {colors.success('sudo urpm install <package>')}

{colors.bold('Documentation:')}
  /usr/share/doc/urpm-ng/QUICKSTART.md

{colors.bold('More help:')}
  urpm --help
""")


class AliasedSubParsersAction(argparse._SubParsersAction):
    """Custom action to support command aliases in argparse."""

    class _AliasedPseudoAction(argparse.Action):
        def __init__(self, name, aliases, help):
            dest = name
            if aliases:
                dest += f" ({', '.join(aliases)})"
            sup = super(AliasedSubParsersAction._AliasedPseudoAction, self)
            sup.__init__(option_strings=[], dest=dest, help=help)

    def add_parser(self, name, **kwargs):
        aliases = kwargs.pop('aliases', [])
        parser = super().add_parser(name, **kwargs)

        # Register aliases
        for alias in aliases:
            self._name_parser_map[alias] = parser

        return parser


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all commands and aliases."""

    parser = argparse.ArgumentParser(
        prog='urpm',
        description='Modern package manager for Mageia Linux',
        epilog='Use "urpm <command> --help" for command-specific help.'
    )

    parser.add_argument(
        '--version', '-V',
        action='version',
        version=f'urpm {__version__}'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet output'
    )

    parser.add_argument(
        '--nocolor',
        action='store_true',
        help='Disable colored output'
    )

    parser.add_argument(
        '--root',
        type=str,
        metavar='DIR',
        help='Use DIR as root for RPM install (chroot). urpm config from host system.'
    )

    parser.add_argument(
        '--urpm-root',
        type=str,
        metavar='DIR',
        dest='urpm_root',
        help='Use DIR as root for both urpm config and RPM install.'
    )

    # Parent parser for display options (inherited by subparsers)
    display_parent = argparse.ArgumentParser(add_help=False)
    display_parent.add_argument(
        '--json',
        action='store_true',
        help='JSON output for scripting'
    )
    display_parent.add_argument(
        '--flat',
        action='store_true',
        help='Flat output (one item per line, parsable)'
    )
    display_parent.add_argument(
        '--show-all',
        action='store_true',
        help='Show all items without truncation'
    )

    # Parent parser for debug options (inherited by install/upgrade/etc.)
    debug_parent = argparse.ArgumentParser(add_help=False)
    debug_parent.add_argument(
        '--debug',
        type=str,
        metavar='COMPONENT',
        help='Enable debug output (solver, download, all)'
    )
    debug_parent.add_argument(
        '--watched',
        type=str,
        metavar='PACKAGES',
        help='Watch specific packages during resolution (comma-separated)'
    )

    # Register custom action for aliases
    parser.register('action', 'parsers', AliasedSubParsersAction)

    subparsers = parser.add_subparsers(
        dest='command',
        title='commands',
        metavar='<command>'
    )

    # Store parents for use by subparsers
    parser._display_parent = display_parent
    parser._debug_parent = debug_parent

    # =========================================================================
    # init - Initialize urpm setup (for bootstrap/chroot)
    # =========================================================================
    init_parser = subparsers.add_parser(
        'init',
        help='Initialize urpm setup (for bootstrap/chroot)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Initialize a new urpm setup with standard Mageia media.

Used for creating chroot environments or bootstrapping new systems.

Examples:
  urpm --urpm-root /tmp/rootfs init --release 10
  urpm --urpm-root /tmp/rootfs init --release cauldron --arch x86_64
  urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'
'''
    )
    init_parser.add_argument(
        '--mirrorlist',
        metavar='URL',
        help='URL to fetch mirror list (auto-generated from --release if not provided)'
    )
    init_parser.add_argument(
        '--arch',
        metavar='ARCH',
        help='Target architecture (default: current system)'
    )
    init_parser.add_argument(
        '--release',
        metavar='VERSION',
        help='Target Mageia version (default: detect from mirrorlist URL or system)'
    )
    init_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='Non-interactive mode'
    )
    init_parser.add_argument(
        '--no-sync',
        action='store_true',
        help='Do not sync media after adding (just configure)'
    )

    # =========================================================================
    # cleanup - Unmount chroot filesystems
    # =========================================================================
    cleanup_parser = subparsers.add_parser(
        'cleanup',
        help='Unmount chroot filesystems (/dev, /proc)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Unmount filesystems mounted by 'urpm init' in a chroot.

Examples:
  urpm --urpm-root /tmp/rootfs cleanup
'''
    )

    # =========================================================================
    # install / i
    # =========================================================================
    install_parser = subparsers.add_parser(
        'install', aliases=['i'],
        help='Install packages',
        parents=[display_parent, debug_parent]
    )
    install_parser.add_argument(
        'packages', nargs='*',
        help='Package names to install (optional with --builddeps)'
    )
    install_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )
    install_parser.add_argument(
        '--test',
        action='store_true',
        help='Dry run (simulation)'
    )
    install_parser.add_argument(
        '--without-recommends',
        action='store_true',
        help='Skip recommended packages'
    )
    install_parser.add_argument(
        '--with-suggests',
        action='store_true',
        help='Also install suggested packages'
    )
    install_parser.add_argument(
        '--all',
        action='store_true',
        help='Install for all matching families (e.g., both php8.4 and php8.5)'
    )
    install_parser.add_argument(
        '--prefer',
        type=str,
        help='Comma-separated preferences for alternatives (e.g., 8.5,fpm,nginx)'
    )
    install_parser.add_argument(
        '--nosignature',
        action='store_true',
        help='Skip GPG signature verification (not recommended)'
    )
    install_parser.add_argument(
        '--noscripts',
        action='store_true',
        help='Skip pre/post install scripts (for chroot/container builds)'
    )
    install_parser.add_argument(
        '--no-peers',
        action='store_true',
        help='Disable P2P download from LAN peers'
    )
    install_parser.add_argument(
        '--only-peers',
        action='store_true',
        help='Only download from LAN peers, no upstream mirrors'
    )
    install_parser.add_argument(
        '--force',
        action='store_true',
        help='Force install despite dependency problems or conflicts'
    )
    install_parser.add_argument(
        '--reinstall',
        action='store_true',
        help='Reinstall already installed packages (repair)'
    )
    install_parser.add_argument(
        '--download-only',
        action='store_true',
        help='Download packages to cache but do not install them'
    )
    install_parser.add_argument(
        '--nodeps',
        action='store_true',
        help='Skip dependency resolution (use with --download-only)'
    )
    install_parser.add_argument(
        '--builddeps', '-b',
        nargs='?',
        const='AUTO',
        metavar='SPEC_OR_SRPM',
        help='Install build dependencies from spec file or SRPM'
    )
    install_parser.add_argument(
        '--allow-arch',
        type=str,
        action='append',
        metavar='ARCH',
        help='Allow additional architectures (e.g., --allow-arch i686 for wine/steam). Can be repeated.'
    )
    install_parser.add_argument(
        '--sync',
        action='store_true',
        help='Wait for all scriptlets and triggers to complete before returning'
    )

    # =========================================================================
    # download / dl - Download packages without installing
    # =========================================================================
    download_parser = subparsers.add_parser(
        'download', aliases=['dl'],
        help='Download packages to cache without installing',
        parents=[display_parent, debug_parent]
    )
    download_parser.add_argument(
        'packages', nargs='*',
        help='Package names to download (optional with --builddeps)'
    )
    download_parser.add_argument(
        '--release', '-r',
        type=str,
        help='Target release (e.g., 10, cauldron). Downloads for this release.'
    )
    download_parser.add_argument(
        '--arch',
        type=str,
        help='Target architecture (default: host arch)'
    )
    download_parser.add_argument(
        '--builddeps', '-b',
        nargs='?',
        const='AUTO',
        metavar='SPEC_OR_SRPM',
        help='Download build dependencies. Auto-detect .spec or specify path.'
    )
    download_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )
    download_parser.add_argument(
        '--without-recommends',
        action='store_true',
        help='Skip recommended packages'
    )
    download_parser.add_argument(
        '--no-peers',
        action='store_true',
        help='Do not use P2P downloads from peers'
    )
    download_parser.add_argument(
        '--only-peers',
        action='store_true',
        help='Only download from LAN peers, no upstream mirrors'
    )
    download_parser.add_argument(
        '--nodeps',
        action='store_true',
        help='Download only specified packages, no dependencies'
    )
    download_parser.add_argument(
        '--allow-arch',
        type=str,
        action='append',
        metavar='ARCH',
        help='Allow additional architectures (e.g., --allow-arch i686 for wine/steam)'
    )

    # =========================================================================
    # mkimage - Create minimal Docker/Podman image for RPM builds
    # =========================================================================
    mkimage_parser = subparsers.add_parser(
        'mkimage',
        help='Create a minimal Docker/Podman image for RPM builds',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Create a minimal Mageia Docker/Podman image for RPM builds.

The image contains a minimal system with urpmi configured to use
the official Mageia mirrors. Use with 'urpm build' for isolated builds.

Examples:
  urpm mkimage --release 10 --tag mageia:10-build
  urpm mkimage --release cauldron --tag mageia:cauldron-build --runtime podman
'''
    )
    mkimage_parser.add_argument(
        '--release', '-r',
        required=True,
        help='Mageia release (e.g., 10, cauldron)'
    )
    mkimage_parser.add_argument(
        '--tag', '-t',
        required=True,
        help='Docker/Podman image tag (e.g., mageia:10-build)'
    )
    mkimage_parser.add_argument(
        '--arch',
        help='Target architecture (default: host arch)'
    )
    mkimage_parser.add_argument(
        '--packages', '-p',
        help='Additional packages to install (comma-separated)'
    )
    mkimage_parser.add_argument(
        '--runtime',
        choices=['docker', 'podman'],
        help='Container runtime (default: auto-detect, prefers podman)'
    )
    mkimage_parser.add_argument(
        '--keep-chroot',
        action='store_true',
        help='Keep temporary chroot directory after image creation'
    )
    mkimage_parser.add_argument(
        '--workdir', '-w',
        help='Working directory for chroot (default: ~/.cache/urpm/mkimage)'
    )

    # =========================================================================
    # build - Build RPM packages in isolated container
    # =========================================================================
    build_parser = subparsers.add_parser(
        'build',
        help='Build RPM package(s) in isolated container',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Build RPM packages in isolated containers.

Each build runs in a fresh container that is destroyed after completion,
ensuring a clean build environment. Results are copied to --output directory.

Examples:
  urpm build --image mageia:10-build ./foo-1.0-1.mga10.src.rpm
  urpm build --image mageia:10-build ./foo.spec
  urpm build --image mageia:10-build *.src.rpm --output ~/results/
'''
    )
    build_parser.add_argument(
        'sources', nargs='+',
        help='Source RPM files (.src.rpm) or spec files (.spec) to build'
    )
    build_parser.add_argument(
        '--image', '-i',
        required=True,
        help='Docker/Podman image to use for builds'
    )
    build_parser.add_argument(
        '--output', '-o',
        default='./build-output',
        help='Output directory for built RPMs (default: ./build-output)'
    )
    build_parser.add_argument(
        '--runtime',
        choices=['docker', 'podman'],
        help='Container runtime (default: auto-detect, prefers podman)'
    )
    build_parser.add_argument(
        '--parallel', '-j',
        type=int,
        default=1,
        help='Number of parallel builds (default: 1)'
    )
    build_parser.add_argument(
        '--keep-container',
        action='store_true',
        help='Keep container after build (for debugging)'
    )

    # =========================================================================
    # erase / e (like rpm -e, urpme)
    # =========================================================================
    erase_parser = subparsers.add_parser(
        'erase', aliases=['e'],
        help='Erase (remove) packages',
        parents=[display_parent]
    )
    erase_parser.add_argument(
        'packages', nargs='*',
        help='Package names to erase (optional with --auto-orphans)'
    )
    erase_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )
    erase_parser.add_argument(
        '--test',
        action='store_true',
        help='Dry run (simulation)'
    )
    erase_parser.add_argument(
        '--auto-orphans',
        action='store_true',
        help='Also remove orphan dependencies (implied by -y unless --keep-orphans)'
    )
    erase_parser.add_argument(
        '--keep-orphans',
        action='store_true',
        help='Do not remove orphan dependencies'
    )
    erase_parser.add_argument(
        '--force',
        action='store_true',
        help='Force erase despite dependency problems'
    )
    erase_parser.add_argument(
        '--erase-recommends',
        action='store_true',
        help='Also erase packages recommended by remaining packages'
    )
    erase_parser.add_argument(
        '--keep-suggests',
        action='store_true',
        help='Keep packages suggested by remaining packages'
    )
    erase_parser.add_argument(
        '--debug',
        choices=['solver', 'all'],
        help='Enable debug output (solver, all)'
    )

    # =========================================================================
    # search / s / query / q
    # =========================================================================
    search_parser = subparsers.add_parser(
        'search', aliases=['s', 'query', 'q'],
        help='Search packages',
        parents=[display_parent]
    )
    search_parser.add_argument(
        'pattern', nargs='?', default='',
        help='Search pattern (optional with --unavailable)'
    )
    search_parser.add_argument(
        '--installed',
        action='store_true',
        help='Search only installed packages'
    )
    search_parser.add_argument(
        '--unavailable',
        action='store_true',
        help='List installed packages not available in any media'
    )

    # =========================================================================
    # show / sh / info
    # =========================================================================
    show_parser = subparsers.add_parser(
        'show', aliases=['sh', 'info'],
        help='Show package details',
        parents=[display_parent]
    )
    show_parser.add_argument(
        'package',
        help='Package name'
    )
    show_parser.add_argument(
        '--files',
        action='store_true',
        help='Show file list'
    )
    show_parser.add_argument(
        '--changelog',
        action='store_true',
        help='Show changelog'
    )

    # =========================================================================
    # list / l
    # =========================================================================
    list_parser = subparsers.add_parser(
        'list', aliases=['l'],
        help='List packages',
        parents=[display_parent]
    )
    list_parser.add_argument(
        'filter',
        nargs='?',
        choices=['installed', 'available', 'updates', 'upgradable', 'all'],
        default='installed',
        help='Filter type (default: installed)'
    )

    # =========================================================================
    # provides / p
    # =========================================================================
    provides_parser = subparsers.add_parser(
        'provides', aliases=['p'],
        help='Show what a package provides',
        parents=[display_parent]
    )
    provides_parser.add_argument(
        'package',
        help='Package name'
    )

    # =========================================================================
    # whatprovides / wp
    # =========================================================================
    whatprovides_parser = subparsers.add_parser(
        'whatprovides', aliases=['wp'],
        help='Find packages providing a capability',
        parents=[display_parent]
    )
    whatprovides_parser.add_argument(
        'capability',
        help='Capability or file path to search'
    )

    # =========================================================================
    # find / f (search in files, compat with urpmf)
    # =========================================================================
    find_parser = subparsers.add_parser(
        'find', aliases=['f'],
        help='Find which package contains a file',
        parents=[display_parent]
    )
    find_parser.add_argument(
        'pattern',
        help='File pattern'
    )
    find_parser.add_argument(
        '--available', '-a',
        action='store_true',
        help='Search only in available packages (requires files.xml, see: urpm media update --files)'
    )
    find_parser.add_argument(
        '--installed', '-i',
        action='store_true',
        help='Search only in installed packages (default: search both)'
    )
    find_parser.add_argument(
        '--limit', '-l',
        type=int, default=100,
        help='Maximum number of results (default: 100)'
    )

    # =========================================================================
    # depends / d / requires
    # =========================================================================
    depends_parser = subparsers.add_parser(
        'depends', aliases=['d', 'requires', 'req'],
        help='Show package dependencies',
        parents=[display_parent]
    )
    depends_parser.add_argument(
        'package',
        help='Package name'
    )
    depends_parser.add_argument(
        '--tree',
        action='store_true',
        help='Show as recursive tree'
    )
    depends_parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Show all dependencies recursively (flat list)'
    )
    depends_parser.add_argument(
        '--legacy',
        action='store_true',
        help='Show raw capabilities (like urpmq/dnf)'
    )
    depends_parser.add_argument(
        '--prefer',
        type=str,
        help='Comma-separated list of preferences for alternatives (e.g., php8.5,nginx,fpm)'
    )
    depends_parser.add_argument(
        '--pager',
        action='store_true',
        help='Use pager for long output (less)'
    )
    depends_parser.add_argument(
        '--no-libs',
        action='store_true',
        help='Hide library packages (lib*, glibc) in tree view'
    )
    depends_parser.add_argument(
        '--depth',
        type=int,
        default=5,
        help='Maximum tree depth (default: 5)'
    )

    # =========================================================================
    # rdepends / rd / whatrequires
    # =========================================================================
    rdepends_parser = subparsers.add_parser(
        'rdepends', aliases=['rd', 'whatrequires', 'wr'],
        help='Show reverse dependencies',
        parents=[display_parent]
    )
    rdepends_parser.add_argument(
        'package',
        help='Package name'
    )
    rdepends_parser.add_argument(
        '--tree',
        action='store_true',
        help='Show as recursive tree'
    )
    rdepends_parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Show all reverse dependencies recursively (flat list)'
    )
    rdepends_parser.add_argument(
        '--depth',
        type=int,
        default=3,
        help='Maximum depth for tree display (default: 3)'
    )
    rdepends_parser.add_argument(
        '--hide-uninstalled',
        action='store_true',
        help='Only show installed packages in tree'
    )

    # =========================================================================
    # recommends
    # =========================================================================
    recommends_parser = subparsers.add_parser(
        'recommends',
        help='Show packages recommended by a package',
        parents=[display_parent]
    )
    recommends_parser.add_argument(
        'package',
        help='Package name'
    )

    # =========================================================================
    # whatrecommends
    # =========================================================================
    whatrecommends_parser = subparsers.add_parser(
        'whatrecommends',
        help='Show packages that recommend a package',
        parents=[display_parent]
    )
    whatrecommends_parser.add_argument(
        'package',
        help='Package name'
    )

    # =========================================================================
    # suggests
    # =========================================================================
    suggests_parser = subparsers.add_parser(
        'suggests',
        help='Show packages suggested by a package',
        parents=[display_parent]
    )
    suggests_parser.add_argument(
        'package',
        help='Package name'
    )

    # =========================================================================
    # whatsuggests
    # =========================================================================
    whatsuggests_parser = subparsers.add_parser(
        'whatsuggests',
        help='Show packages that suggest a package',
        parents=[display_parent]
    )
    whatsuggests_parser.add_argument(
        'package',
        help='Package name'
    )

    # =========================================================================
    # why
    # =========================================================================
    why_parser = subparsers.add_parser(
        'why',
        help='Explain why a package is installed',
        parents=[display_parent]
    )
    why_parser.add_argument(
        'package',
        help='Package name'
    )

    # =========================================================================
    # update - metadata only (apt-style)
    # =========================================================================
    update_parser = subparsers.add_parser(
        'update',
        help='Update media metadata (apt-style: use "upgrade" for packages)',
        parents=[display_parent, debug_parent]
    )
    update_parser.add_argument(
        'name', nargs='?',
        help='Media name to update (default: all)'
    )
    update_parser.add_argument(
        '--files',
        action='store_true',
        help='Also sync files.xml for media with sync_files enabled'
    )

    # =========================================================================
    # upgrade / u
    # =========================================================================
    upgrade_parser = subparsers.add_parser(
        'upgrade', aliases=['u'],
        help='Upgrade packages (all if none specified)',
        parents=[display_parent, debug_parent]
    )
    upgrade_parser.add_argument(
        'packages', nargs='*',
        help='Packages to upgrade (empty = all)'
    )
    upgrade_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )
    upgrade_parser.add_argument(
        '--noerase-orphans',
        action='store_true',
        help='Keep orphaned dependencies (do not remove them)'
    )
    upgrade_parser.add_argument(
        '--test',
        action='store_true',
        help='Dry run - show what would be done'
    )
    upgrade_parser.add_argument(
        '--nosignature',
        action='store_true',
        help='Skip GPG signature verification (not recommended)'
    )
    upgrade_parser.add_argument(
        '--with-recommends',
        action='store_true',
        help='Install recommended packages (not installed by default for upgrades)'
    )
    upgrade_parser.add_argument(
        '--with-suggests',
        action='store_true',
        help='Also install suggested packages'
    )
    upgrade_parser.add_argument(
        '--no-peers',
        action='store_true',
        help='Disable P2P download from LAN peers'
    )
    upgrade_parser.add_argument(
        '--only-peers',
        action='store_true',
        help='Only download from LAN peers, no upstream mirrors'
    )
    upgrade_parser.add_argument(
        '--force',
        action='store_true',
        help='Force upgrade despite dependency problems or conflicts'
    )
    upgrade_parser.add_argument(
        '--allow-arch',
        type=str,
        action='append',
        metavar='ARCH',
        help='Allow additional architectures (e.g., --allow-arch i686 for wine/steam)'
    )

    # =========================================================================
    # autoremove / ar
    # =========================================================================
    autoremove_parser = subparsers.add_parser(
        'autoremove', aliases=['ar'],
        help='Remove orphaned packages, old kernels, or failed deps',
        parents=[display_parent]
    )
    autoremove_parser.add_argument(
        '--orphans', '-o',
        action='store_true',
        help='Remove orphaned packages (default if no selector)'
    )
    autoremove_parser.add_argument(
        '--kernels', '-k',
        action='store_true',
        help='Remove old kernels (keeps running + N recent)'
    )
    autoremove_parser.add_argument(
        '--faildeps', '-f',
        action='store_true',
        help='Remove orphan deps from interrupted transactions'
    )
    autoremove_parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='All of the above'
    )
    autoremove_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )

    # =========================================================================
    # mark
    # =========================================================================
    mark_parser = subparsers.add_parser(
        'mark',
        help='Mark packages as manual or auto-installed',
        parents=[display_parent]
    )
    mark_subparsers = mark_parser.add_subparsers(
        dest='mark_command',
        metavar='<subcommand>'
    )

    mark_manual = mark_subparsers.add_parser(
        'manual', aliases=['m', 'explicit'],
        help='Mark package as manually installed (protects from autoremove)'
    )
    mark_manual.add_argument(
        'packages', nargs='+', metavar='PACKAGE',
        help='Package names to mark as manual'
    )

    mark_auto = mark_subparsers.add_parser(
        'auto', aliases=['a', 'dep'],
        help='Mark package as auto-installed (can be autoremoved)'
    )
    mark_auto.add_argument(
        'packages', nargs='+', metavar='PACKAGE',
        help='Package names to mark as auto'
    )

    mark_show = mark_subparsers.add_parser(
        'show', aliases=['s', 'list'],
        help='Show install reason for packages'
    )
    mark_show.add_argument(
        'packages', nargs='*', metavar='PACKAGE',
        help='Package names to check (or all if empty)'
    )

    # =========================================================================
    # hold / unhold
    # =========================================================================
    hold_parser = subparsers.add_parser(
        'hold',
        help='Hold packages (prevent upgrades and obsoletes replacement)',
        parents=[display_parent]
    )
    hold_parser.add_argument(
        'packages', nargs='*', metavar='PACKAGE',
        help='Package names to hold (or list holds if empty)'
    )
    hold_parser.add_argument(
        '-r', '--reason',
        help='Reason for holding the package'
    )
    hold_parser.add_argument(
        '-l', '--list', action='store_true', dest='list_holds',
        help='List held packages'
    )

    unhold_parser = subparsers.add_parser(
        'unhold',
        help='Remove hold from packages',
        parents=[display_parent]
    )
    unhold_parser.add_argument(
        'packages', nargs='+', metavar='PACKAGE',
        help='Package names to unhold'
    )

    # =========================================================================
    # media / m
    # =========================================================================
    media_parser = subparsers.add_parser(
        'media', aliases=['m'],
        help='Manage media sources',
        parents=[display_parent]
    )
    media_subparsers = media_parser.add_subparsers(
        dest='media_command',
        metavar='<subcommand>'
    )

    # media list / l / ls
    media_list = media_subparsers.add_parser(
        'list', aliases=['l', 'ls'],
        help='List media sources'
    )
    media_list.add_argument(
        '--all', '-a',
        action='store_true',
        help='Show all media (including disabled)'
    )

    # media add / a
    media_add = media_subparsers.add_parser(
        'add', aliases=['a'],
        help='Add media source',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Add a media source.

For official Mageia media, just provide the URL:
  urpm media add https://mirrors.mageia.org/mageia/9/x86_64/media/core/release/

For custom/third-party media, use --custom with name and short_name:
  urpm media add --custom "My Repo" myrepo https://example.com/repo/x86_64/

For legacy mode (non-Mageia URL with explicit name):
  urpm media add --name "My Media" https://example.com/repo/
'''
    )
    media_add.add_argument('url', help='Media URL')
    media_add.add_argument(
        '--name',
        help='Media name (legacy mode, for non-Mageia URLs without --custom)'
    )
    media_add.add_argument(
        '--custom',
        nargs=2,
        metavar=('NAME', 'SHORT_NAME'),
        help='Add as custom media with display name and short identifier'
    )
    media_add.add_argument(
        '--update',
        action='store_true',
        help='Mark as update media'
    )
    media_add.add_argument(
        '--disabled',
        action='store_true',
        help='Add as disabled'
    )
    media_add.add_argument(
        '--auto', '-y',
        action='store_true',
        help='Non-interactive mode (auto-confirm prompts)'
    )
    media_add.add_argument(
        '--import-key',
        action='store_true',
        help='Import GPG key from media'
    )
    media_add.add_argument(
        '--allow-unsigned',
        action='store_true',
        help='Allow unsigned packages (custom media only)'
    )

    # media remove / r
    media_remove = media_subparsers.add_parser(
        'remove', aliases=['r'],
        help='Remove media source'
    )
    media_remove.add_argument('name', help='Media name')

    # media enable / e
    media_enable = media_subparsers.add_parser(
        'enable', aliases=['e'],
        help='Enable media source'
    )
    media_enable.add_argument('name', help='Media name')

    # media disable / d
    media_disable = media_subparsers.add_parser(
        'disable', aliases=['d'],
        help='Disable media source'
    )
    media_disable.add_argument('name', help='Media name')

    # media update / u
    media_update = media_subparsers.add_parser(
        'update', aliases=['u'],
        help='Update media metadata'
    )
    media_update.add_argument(
        'name', nargs='?',
        help='Media name (empty = all)'
    )
    media_update.add_argument(
        '--files', '-f',
        action='store_true',
        help='Also download and index files.xml.lzma (enables file search in available packages)'
    )
    media_update.add_argument(
        '--no-appstream',
        action='store_true',
        help='Skip AppStream metadata sync'
    )

    # media import
    media_import = media_subparsers.add_parser(
        'import',
        help='Import media from urpmi.cfg'
    )
    media_import.add_argument(
        'file', nargs='?',
        default='/etc/urpmi/urpmi.cfg',
        help='Path to urpmi.cfg (default: /etc/urpmi/urpmi.cfg)'
    )
    media_import.add_argument(
        '--replace',
        action='store_true',
        help='Replace existing media with same name'
    )
    media_import.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )

    # media set / s
    media_set = media_subparsers.add_parser(
        'set', aliases=['s'],
        help='Modify media settings'
    )
    media_set.add_argument('name', nargs='?', help='Media name (or use --all)')
    media_set.add_argument(
        '--all', '-a',
        action='store_true',
        help='Apply to all enabled media'
    )
    media_set.add_argument(
        '--shared',
        choices=['yes', 'no'],
        help='Enable/disable sharing this media with peers'
    )
    media_set.add_argument(
        '--replication',
        metavar='POLICY',
        help='Replication policy: none, on_demand, seed'
    )
    media_set.add_argument(
        '--seeds',
        metavar='SECTIONS',
        help='rpmsrate sections for seed replication (comma-separated), e.g., INSTALL,CAT_PLASMA5,CAT_GNOME'
    )
    media_set.add_argument(
        '--quota',
        metavar='SIZE',
        help='Per-media quota (e.g., 5G, 500M)'
    )
    media_set.add_argument(
        '--retention',
        metavar='DAYS', type=int,
        help='Days to keep cached packages'
    )
    media_set.add_argument(
        '--priority',
        metavar='N', type=int,
        help='Media priority (higher = preferred)'
    )
    # sync_files: mutually exclusive --sync-files / --no-sync-files
    sync_files_group = media_set.add_mutually_exclusive_group()
    sync_files_group.add_argument(
        '--sync-files',
        dest='sync_files', action='store_true', default=None,
        help='Enable auto-sync of files.xml for urpm find'
    )
    sync_files_group.add_argument(
        '--no-sync-files',
        dest='sync_files', action='store_false',
        help='Disable auto-sync of files.xml'
    )

    # media seed-info
    media_seed_info = media_subparsers.add_parser(
        'seed-info',
        help='Show seed set info for a media'
    )
    media_seed_info.add_argument(
        'name',
        help='Media name'
    )

    # media link
    media_link = media_subparsers.add_parser(
        'link',
        help='Link/unlink servers to a media',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Link or unlink servers to a media source.

Use +server to add a server, -server to remove it.
Use +all/-all to add/remove all servers.

Examples:
  urpm media link "Core Release" +mirror1 +mirror2
  urpm media link "Core Updates" -oldserver
  urpm media link "Core Release" +newserver -oldserver
  urpm media link "Core Release" +all
  urpm media link "Core Release" -all +preferred_mirror
'''
    )
    media_link.add_argument('name', help='Media name')
    media_link.add_argument(
        'changes', nargs='+', metavar='+/-server',
        help='Server changes: +name to add, -name to remove, +all/-all for all'
    )

    # media autoconfig / auto / ac
    media_autoconfig = media_subparsers.add_parser(
        'autoconfig', aliases=['auto', 'ac'],
        help='Auto-add official Mageia media for a release',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Auto-configure all official Mageia media for a release.

Uses the official mirrorlist to discover mirrors and adds all standard media:
- core/release, core/updates
- nonfree/release, nonfree/updates
- tainted/release, tainted/updates

Examples:
  urpm media autoconfig --release 10 --arch x86_64
  urpm media autoconfig -r cauldron
  urpm media ac -r 10   # Short form
'''
    )
    media_autoconfig.add_argument(
        '--release', '-r',
        required=True,
        help='Mageia release (e.g., 10, cauldron)'
    )
    media_autoconfig.add_argument(
        '--arch',
        help='Architecture (default: host architecture)'
    )
    media_autoconfig.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Show what would be added without making changes'
    )
    media_autoconfig.add_argument(
        '--no-nonfree',
        action='store_true',
        help='Skip nonfree media'
    )
    media_autoconfig.add_argument(
        '--no-tainted',
        action='store_true',
        help='Skip tainted media'
    )

    # =========================================================================
    # server / srv
    # =========================================================================
    server_parser = subparsers.add_parser(
        'server', aliases=['srv'],
        help='Manage servers',
        parents=[display_parent]
    )
    server_subparsers = server_parser.add_subparsers(
        dest='server_command',
        metavar='<subcommand>'
    )

    # server list / l / ls
    server_list = server_subparsers.add_parser(
        'list', aliases=['l', 'ls'],
        help='List servers'
    )
    server_list.add_argument(
        '--all', '-a',
        action='store_true',
        help='Show all servers (including disabled)'
    )

    # server add / a
    server_add = server_subparsers.add_parser(
        'add', aliases=['a'],
        help='Add a server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Add a mirror server.

Examples:
  urpm server add "Belnet" https://ftp.belnet.be/mageia/distrib/
  urpm server add "Local" file:///mnt/repo/
'''
    )
    server_add.add_argument('name', help='Server display name')
    server_add.add_argument('url', help='Server base URL (https://host/path/ or file:///path/)')
    server_add.add_argument(
        '--priority', '-p', type=int, default=50,
        help='Server priority (higher = preferred, default: 50)'
    )
    server_add.add_argument(
        '--disabled',
        action='store_true',
        help='Add as disabled'
    )
    server_add.add_argument(
        '--custom',
        action='store_true',
        help='Mark as non-official server'
    )

    # server remove / r / rm
    server_remove = server_subparsers.add_parser(
        'remove', aliases=['r', 'rm'],
        help='Remove a server'
    )
    server_remove.add_argument('name', help='Server name')

    # server enable / e
    server_enable = server_subparsers.add_parser(
        'enable', aliases=['e'],
        help='Enable a server'
    )
    server_enable.add_argument('name', help='Server name')

    # server disable / d
    server_disable = server_subparsers.add_parser(
        'disable', aliases=['d'],
        help='Disable a server'
    )
    server_disable.add_argument('name', help='Server name')

    # server priority
    server_priority = server_subparsers.add_parser(
        'priority',
        help='Set server priority'
    )
    server_priority.add_argument('name', help='Server name')
    server_priority.add_argument('priority', type=int, help='Priority (higher = preferred)')

    # server test / t
    server_test = server_subparsers.add_parser(
        'test', aliases=['t'],
        help='Test server connectivity and detect IP mode'
    )
    server_test.add_argument(
        'name', nargs='?',
        help='Server name (empty = test all enabled servers)'
    )

    # server ip-mode
    server_ipmode = server_subparsers.add_parser(
        'ip-mode',
        help='Set server IP mode manually'
    )
    server_ipmode.add_argument('name', help='Server name')
    server_ipmode.add_argument(
        'mode', choices=['auto', 'ipv4', 'ipv6', 'dual'],
        help='IP mode: auto, ipv4, ipv6, or dual (dual = prefer ipv4)'
    )

    # server autoconfig
    server_autoconfig = server_subparsers.add_parser(
        'autoconfig', aliases=['auto'],
        help='Auto-discover and add servers from Mageia mirrorlist'
    )
    server_autoconfig.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Show what would be added without making changes'
    )
    server_autoconfig.add_argument(
        '--release', '-r',
        help='Override detected Mageia version (e.g., 9)'
    )

    # =========================================================================
    # mirror (local package mirroring)
    # =========================================================================
    mirror_parser = subparsers.add_parser(
        'mirror',
        help='Manage local package mirroring',
        aliases=['proxy'],  # backward compatibility
        parents=[display_parent]
    )
    mirror_subparsers = mirror_parser.add_subparsers(
        dest='mirror_command',
        metavar='<subcommand>'
    )

    # mirror status
    mirror_subparsers.add_parser('status', help='Show mirror status and quotas')

    # mirror enable
    mirror_subparsers.add_parser('enable', help='Enable mirroring (serve packages to peers)')

    # mirror disable
    mirror_subparsers.add_parser('disable', help='Disable mirroring')

    # mirror quota
    mirror_quota = mirror_subparsers.add_parser('quota', help='Set global cache quota')
    mirror_quota.add_argument(
        'size', nargs='?',
        help='Quota size (e.g., 10G, 500M) or empty to show current'
    )

    # mirror disable-version
    mirror_disable_ver = mirror_subparsers.add_parser(
        'disable-version',
        help='Stop serving a Mageia version to peers'
    )
    mirror_disable_ver.add_argument(
        'versions',
        help='Comma-separated version numbers (e.g., 8,9)'
    )

    # mirror enable-version
    mirror_enable_ver = mirror_subparsers.add_parser(
        'enable-version',
        help='Resume serving a Mageia version to peers'
    )
    mirror_enable_ver.add_argument(
        'versions',
        help='Comma-separated version numbers (e.g., 9)'
    )

    # mirror clean
    mirror_clean = mirror_subparsers.add_parser(
        'clean',
        help='Enforce quotas and retention policies'
    )
    mirror_clean.add_argument(
        '--dry-run', '-n', action='store_true',
        help='Show what would be deleted without deleting'
    )

    # mirror sync
    mirror_sync = mirror_subparsers.add_parser(
        'sync',
        help='Force sync according to replication policies'
    )
    mirror_sync.add_argument(
        'media', nargs='?',
        help='Specific media to sync (default: all with seed policy)'
    )
    mirror_sync.add_argument(
        '--latest-only', action='store_true',
        help='Only download latest version of each package (smaller, DVD-like)'
    )

    # mirror rate-limit
    mirror_ratelimit = mirror_subparsers.add_parser(
        'rate-limit',
        help='Configure rate limiting'
    )
    mirror_ratelimit.add_argument(
        'setting',
        nargs='?',
        help='on, off, or N/min (e.g., 60/min)'
    )

    # =========================================================================
    # cache / c
    # =========================================================================
    cache_parser = subparsers.add_parser(
        'cache', aliases=['c'],
        help='Manage cache',
        parents=[display_parent]
    )
    cache_subparsers = cache_parser.add_subparsers(
        dest='cache_command',
        metavar='<subcommand>'
    )

    cache_subparsers.add_parser('info', help='Cache information')

    cache_clean_parser = cache_subparsers.add_parser('clean', help='Clean orphan RPMs from cache')
    cache_clean_parser.add_argument(
        '--dry-run', '-n', action='store_true',
        help='Show what would be removed without removing'
    )
    cache_clean_parser.add_argument(
        '--auto', '-y', action='store_true',
        help='Do not ask for confirmation'
    )
    cache_clean_parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='List all orphan files'
    )

    cache_subparsers.add_parser('rebuild', help='Rebuild database from synthesis files')
    cache_subparsers.add_parser('stats', help='Detailed cache statistics')
    cache_subparsers.add_parser('rebuild-fts', help='Rebuild FTS index for fast file search')

    # =========================================================================
    # history / h
    # =========================================================================
    history_parser = subparsers.add_parser(
        'history', aliases=['h'],
        help='Show transaction history',
        parents=[display_parent]
    )
    history_parser.add_argument(
        'count', nargs='?', type=int, default=20,
        help='Number of transactions to show (default: 20)'
    )
    history_parser.add_argument(
        '--install', '-i', action='store_true',
        help='Show only install transactions'
    )
    history_parser.add_argument(
        '--remove', '-r', action='store_true',
        help='Show only remove transactions'
    )
    history_parser.add_argument(
        '--detail', '-d', type=int, metavar='ID',
        help='Show details of transaction ID'
    )
    history_parser.add_argument(
        '--delete', type=int, nargs='+', metavar='ID',
        help='Delete transaction(s) from history'
    )

    # =========================================================================
    # rollback / r
    # =========================================================================
    rollback_parser = subparsers.add_parser(
        'rollback', aliases=['r'],
        help='Rollback transactions: "rollback 5" (last 5), "rollback to 42" (to #42), "rollback to 26/11/2025"',
        parents=[display_parent]
    )
    rollback_parser.add_argument(
        'args', nargs='*',
        help='N (last N transactions), or "to N" (to transaction #N), or "to DATE"'
    )
    rollback_parser.add_argument(
        '--auto', '-y', action='store_true',
        help='No confirmation'
    )

    # =========================================================================
    # undo / u
    # =========================================================================
    undo_parser = subparsers.add_parser(
        'undo',
        help='Undo last transaction, or a specific one',
        parents=[display_parent]
    )
    undo_parser.add_argument(
        'transaction_id', nargs='?', type=int,
        help='Transaction ID to undo (default: last)'
    )
    undo_parser.add_argument(
        '--auto', '-y', action='store_true',
        help='No confirmation'
    )

    # =========================================================================
    # cleandeps / cd (alias for autoremove --faildeps)
    # =========================================================================
    cleandeps_parser = subparsers.add_parser(
        'cleandeps', aliases=['cd'],
        help='Remove orphan deps from interrupted transactions (alias: autoremove --faildeps)',
        parents=[display_parent]
    )
    cleandeps_parser.add_argument(
        '--auto', '-y', action='store_true',
        help='No confirmation'
    )

    # =========================================================================
    # config - Configuration management
    # =========================================================================
    config_parser = subparsers.add_parser(
        'config', aliases=['cfg'],
        help='Manage urpm configuration',
        parents=[display_parent]
    )
    config_subparsers = config_parser.add_subparsers(dest='config_cmd', metavar='COMMAND')

    # config blacklist
    blacklist_parser = config_subparsers.add_parser(
        'blacklist', aliases=['bl'],
        help='Manage blacklist (critical packages never removed)'
    )
    blacklist_subparsers = blacklist_parser.add_subparsers(dest='blacklist_cmd', metavar='ACTION')

    blacklist_subparsers.add_parser('list', aliases=['ls'], help='Show blacklist')
    bl_add = blacklist_subparsers.add_parser('add', aliases=['a'], help='Add package to blacklist')
    bl_add.add_argument('package', help='Package name to add')
    bl_remove = blacklist_subparsers.add_parser('remove', aliases=['rm'], help='Remove package from blacklist')
    bl_remove.add_argument('package', help='Package name to remove')

    # config redlist
    redlist_parser = config_subparsers.add_parser(
        'redlist', aliases=['rl'],
        help='Manage redlist (packages requiring confirmation)'
    )
    redlist_subparsers = redlist_parser.add_subparsers(dest='redlist_cmd', metavar='ACTION')

    redlist_subparsers.add_parser('list', aliases=['ls'], help='Show redlist')
    rl_add = redlist_subparsers.add_parser('add', aliases=['a'], help='Add package to redlist')
    rl_add.add_argument('package', help='Package name to add')
    rl_remove = redlist_subparsers.add_parser('remove', aliases=['rm'], help='Remove package from redlist')
    rl_remove.add_argument('package', help='Package name to remove')

    # config kernel-keep
    kernel_keep_parser = config_subparsers.add_parser(
        'kernel-keep', aliases=['kk'],
        help='Number of old kernels to keep (in addition to running)'
    )
    kernel_keep_parser.add_argument('count', nargs='?', type=int, help='Number of kernels to keep (show current if omitted)')

    # config version-mode
    version_mode_parser = config_subparsers.add_parser(
        'version-mode', aliases=['vm'],
        help='Choose between system version and cauldron when both are enabled'
    )
    version_mode_parser.add_argument(
        'mode', nargs='?', choices=['system', 'cauldron', 'auto'],
        help='system=use system version, cauldron=use cauldron, auto=remove preference (show current if omitted)'
    )

    # =========================================================================
    # key - GPG key management
    # =========================================================================
    key_parser = subparsers.add_parser(
        'key', aliases=['k'],
        help='Manage GPG keys for package verification',
        parents=[display_parent]
    )
    key_subparsers = key_parser.add_subparsers(dest='key_cmd', metavar='COMMAND')

    key_subparsers.add_parser('list', aliases=['ls', 'l'], help='List installed GPG keys')

    key_import = key_subparsers.add_parser('import', aliases=['i', 'add'], help='Import GPG key')
    key_import.add_argument('keyfile', help='Path to key file or HTTPS URL')

    key_remove = key_subparsers.add_parser('remove', aliases=['rm', 'del'], help='Remove GPG key')
    key_remove.add_argument('keyid', help='Key ID to remove (e.g., 80420f66)')

    # =========================================================================
    # peer - P2P peer management
    # =========================================================================
    peer_parser = subparsers.add_parser(
        'peer',
        help='Manage P2P peers (provenance, blacklist)',
        parents=[display_parent]
    )
    peer_subparsers = peer_parser.add_subparsers(
        dest='peer_command',
        metavar='<subcommand>'
    )

    # peer list / ls - list known peers and their stats
    peer_list = peer_subparsers.add_parser(
        'list', aliases=['ls'],
        help='List peers and download statistics'
    )

    # peer downloads - list packages downloaded from peers
    peer_downloads = peer_subparsers.add_parser(
        'downloads', aliases=['dl'],
        help='List packages downloaded from peers'
    )
    peer_downloads.add_argument(
        'host', nargs='?',
        help='Filter by peer host (optional)'
    )
    peer_downloads.add_argument(
        '--limit', '-n', type=int, default=50,
        help='Max entries to show (default: 50)'
    )

    # peer blacklist - manage blacklist
    peer_blacklist = peer_subparsers.add_parser(
        'blacklist', aliases=['bl', 'block'],
        help='Blacklist a peer'
    )
    peer_blacklist.add_argument('host', help='Peer host to blacklist')
    peer_blacklist.add_argument(
        '--port', '-p', type=int,
        help='Specific port (default: all ports)'
    )
    peer_blacklist.add_argument(
        '--reason', '-r',
        help='Reason for blacklisting'
    )

    # peer unblacklist - remove from blacklist
    peer_unblacklist = peer_subparsers.add_parser(
        'unblacklist', aliases=['unbl', 'unblock'],
        help='Remove peer from blacklist'
    )
    peer_unblacklist.add_argument('host', help='Peer host to unblacklist')
    peer_unblacklist.add_argument(
        '--port', '-p', type=int,
        help='Specific port (default: all ports)'
    )

    # peer clean - delete files from a peer and purge records
    peer_clean = peer_subparsers.add_parser(
        'clean',
        help='Delete RPMs downloaded from a peer (use after blacklist)'
    )
    peer_clean.add_argument('host', help='Peer host to clean')
    peer_clean.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Do not prompt for confirmation'
    )
    peer_clean.add_argument(
        '--show-all', '-a',
        action='store_true',
        help='Show all files (do not truncate list)'
    )

    # =========================================================================
    # appstream
    # =========================================================================
    appstream_parser = subparsers.add_parser(
        'appstream',
        help='Manage AppStream metadata for software centers (Discover, GNOME Software)'
    )
    appstream_subparsers = appstream_parser.add_subparsers(
        dest='appstream_command',
        metavar='<subcommand>'
    )

    # appstream generate
    appstream_generate = appstream_subparsers.add_parser(
        'generate', aliases=['gen'],
        help='Generate AppStream catalog from package database'
    )
    appstream_generate.add_argument(
        '--output', '-o',
        help='Output file (default: /var/cache/swcatalog/xml/mageia-{version}.xml.gz)'
    )
    appstream_generate.add_argument(
        '--no-compress',
        action='store_true',
        help='Do not gzip the output file'
    )
    appstream_generate.add_argument(
        '--media', '-m',
        help='Generate for specific media only'
    )

    # appstream status
    appstream_status = appstream_subparsers.add_parser(
        'status',
        help='Show AppStream status for all media'
    )

    # appstream merge
    appstream_merge = appstream_subparsers.add_parser(
        'merge',
        help='Merge per-media AppStream files into unified catalog'
    )
    appstream_merge.add_argument(
        '--refresh', '-r',
        action='store_true',
        help='Also refresh system AppStream cache'
    )

    # appstream init-distro
    appstream_init = appstream_subparsers.add_parser(
        'init-distro',
        help='Create OS metainfo file for AppStream (required for Discover/GNOME Software)'
    )
    appstream_init.add_argument(
        '--force', '-f',
        action='store_true',
        help='Overwrite existing metainfo file'
    )

    return parser


# =============================================================================
# Command handlers
# =============================================================================

def cmd_cleanup(args, db: PackageDatabase) -> int:
    """Handle cleanup command - unmount chroot filesystems."""
    import subprocess
    from pathlib import Path
    from . import colors

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


def cmd_install(args, db: PackageDatabase) -> int:
    """Handle install command."""
    import signal
    import solv
    from ..core.resolver import Resolver, Resolution, format_size, set_solver_debug, PackageAction, TransactionType
    from ..core.operations import PackageOperations, InstallOptions
    from ..core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from . import colors

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    watched_pkgs = getattr(args, 'watched', None)
    if watched_pkgs:
        watched_pkgs = [p.strip() for p in watched_pkgs.split(',')]
    if debug_solver or watched_pkgs:
        set_solver_debug(enabled=debug_solver, watched=watched_pkgs)

    # Check for previous background install errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # Debug: save previous state and clear debug files at start
    _copy_installed_deps_list(dest=DEBUG_PREV_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_INSTALLED_DEPS)

    # Check --nodeps flag
    nodeps = getattr(args, 'nodeps', False)
    download_only = getattr(args, 'download_only', False)
    if nodeps and not download_only:
        print(colors.error("Error: --nodeps requires --download-only"))
        return 1

    # Check root privileges early (unless allowed to skip for mkimage)
    from ..core.install import check_root
    allow_no_root = getattr(args, 'allow_no_root', False)
    if not download_only and not allow_no_root and not check_root():
        print(colors.error("Error: root privileges required for installation"))
        print("Try: sudo urpm install <packages>")
        return 1

    # Handle --builddeps option (install build dependencies from spec/SRPM)
    builddeps = getattr(args, 'builddeps', None)
    if builddeps:
        from ..core.buildrequires import get_buildrequires, list_specs_in_workdir, rpm_dep_to_solver_format

        try:
            if builddeps == 'AUTO':
                # Auto-detect mode
                specs = list_specs_in_workdir()
                if len(specs) > 1:
                    print(colors.info("Multiple .spec files found:"))
                    for i, spec in enumerate(specs, 1):
                        print(f"  {i}. {spec.name}")
                    if getattr(args, 'auto', False):
                        print(colors.error("Error: Multiple .spec files found. Specify which one to use."))
                        return 1
                    try:
                        choice = input("Select spec file (number): ").strip()
                        idx = int(choice) - 1
                        if 0 <= idx < len(specs):
                            builddeps = str(specs[idx])
                        else:
                            print(colors.error("Invalid choice"))
                            return 1
                    except (ValueError, KeyboardInterrupt):
                        print("\nAborted.")
                        return 1
                else:
                    builddeps = 'AUTO'

            target = None if builddeps == 'AUTO' else builddeps
            reqs, source = get_buildrequires(target)
            print(colors.info(f"Build dependencies from: {source}"))
            print(f"  Found {len(reqs)} BuildRequires")

            # Replace packages list with build requirements (convert to solver format)
            args.packages = [rpm_dep_to_solver_format(req) for req in reqs]

        except FileNotFoundError as e:
            print(colors.error(f"Error: {e}"))
            return 1
        except ValueError as e:
            print(colors.error(f"Error: {e}"))
            return 1

    # Check that we have something to install
    if not args.packages and not builddeps:
        print(colors.error("Error: No packages specified"))
        print("Usage: urpm install <packages> or urpm install --builddeps <spec>")
        return 1

    # Separate local RPM files from package names
    from pathlib import Path
    from ..core.rpm import is_local_rpm, read_rpm_header
    from ..core.download import verify_rpm_signature

    local_rpm_paths = []
    local_rpm_infos = []
    package_names = []
    verify_sigs = not getattr(args, 'nosignature', False)

    for pkg in args.packages:
        if is_local_rpm(pkg):
            path = Path(pkg)
            if not path.exists():
                print(colors.error(f"Error: file not found: {pkg}"))
                return 1
            # Read RPM header
            info = read_rpm_header(path)
            if not info:
                print(colors.error(f"Error: cannot read RPM file: {pkg}"))
                return 1
            # Verify signature
            if verify_sigs:
                valid, error = verify_rpm_signature(path)
                if not valid:
                    print(colors.error(f"Error: signature verification failed for {pkg}"))
                    print(colors.error(f"  {error}"))
                    print(colors.dim("  Use --nosignature to skip verification (not recommended)"))
                    return 1
            local_rpm_paths.append(str(path.resolve()))
            local_rpm_infos.append(info)
        else:
            package_names.append(pkg)

    # If we have local RPMs, show what we're installing
    if local_rpm_infos:
        print(f"Local RPM files ({len(local_rpm_infos)}):")
        for info in local_rpm_infos:
            print(f"  {info['nevra']}")

    # Resolve virtual packages to concrete packages
    # This handles cases like php-opcache → php8.5-opcache based on what's installed
    auto_mode = getattr(args, 'auto', False)
    install_all = getattr(args, 'all', False)

    resolved_packages = []
    # Initialize choices dict early to track virtual package resolutions
    # This prevents the resolver from asking again about already-resolved providers
    choices = {}
    # Add local RPM names to the list
    for info in local_rpm_infos:
        resolved_packages.append(info['name'])
    # Resolve virtual packages from command line
    for pkg in package_names:
        pkg_name = _extract_pkg_name(pkg)
        concrete = _resolve_virtual_package(db, pkg_name, auto_mode, install_all)
        resolved_packages.extend(concrete)
        # Record the choice so resolver doesn't ask again for this capability
        # Only record if single provider was selected (not "All")
        if len(concrete) == 1 and concrete[0] != pkg_name:
            choices[pkg_name] = concrete[0]

    # Remove duplicates while preserving order
    seen = set()
    unique_packages = []
    for p in resolved_packages:
        if p.lower() not in seen:
            seen.add(p.lower())
            unique_packages.append(p)
    resolved_packages = unique_packages

    if not resolved_packages:
        print("Aborted.")
        return 1

    from ..core.resolver import InstallReason

    # Get CLI options for recommends/suggests
    without_recommends = getattr(args, 'without_recommends', False)
    with_suggests = getattr(args, 'with_suggests', False)

    # Parse --prefer using PreferencesMatcher
    prefer_str = getattr(args, 'prefer', None)
    preferences = PreferencesMatcher(prefer_str)

    # Determine initial recommends behavior:
    # - Auto mode: no recommends (never ask)
    # - Interactive mode: yes unless --without-recommends (will ask user)
    if args.auto:
        initial_recommends = False
    else:
        initial_recommends = not without_recommends

    resolver = _create_resolver(db, args, install_recommends=initial_recommends)
    # choices dict was initialized earlier (line ~4818) with virtual package resolutions

    # Add local RPMs to resolver pool before resolution
    if local_rpm_infos:
        resolver.add_local_rpms(local_rpm_infos)

    if nodeps:
        # --nodeps: build actions directly without dependency resolution
        from ..core.resolver import PackageAction, TransactionType, Resolution
        actions = []
        not_found = []
        for pkg_spec in resolved_packages:
            pkg = db.get_package_smart(pkg_spec)
            if not pkg:
                not_found.append(pkg_spec)
                continue
            media = db.get_media_by_id(pkg['media_id'])
            media_name = media.get('name', 'unknown') if media else 'unknown'
            epoch = pkg.get('epoch', 0) or 0
            evr = f"{epoch}:{pkg['version']}-{pkg['release']}" if epoch else f"{pkg['version']}-{pkg['release']}"
            actions.append(PackageAction(
                action=TransactionType.INSTALL,
                name=pkg['name'],
                evr=evr,
                arch=pkg['arch'],
                nevra=pkg['nevra'],
                size=pkg.get('filesize', 0) or 0,
                media_name=media_name,
                reason=InstallReason.EXPLICIT
            ))
        if not_found:
            print(colors.error(f"Packages not found ({len(not_found)}):"))
            for p in not_found[:10]:
                print(f"  {p}")
            if len(not_found) > 10:
                print(f"  ... and {len(not_found) - 10} more")
            return 1
        result = Resolution(success=True, actions=actions, problems=[])
        aborted = False
    else:
        # Normal resolution with user choices for alternatives
        # Build set of local package names for SOLVER_UPDATE
        local_pkg_names = {info['name'] for info in local_rpm_infos}
        result, aborted = _resolve_with_alternatives(
            resolver, resolved_packages, choices, args.auto, preferences,
            local_packages=local_pkg_names
        )
    if aborted:
        return 1

    if not result.success:
        print("Resolution failed:")
        for p in result.problems:
            print(f"  {p}")
        return 1

    # Handle --reinstall for local RPMs that are already installed at same version
    reinstall_mode = getattr(args, 'reinstall', False)
    if reinstall_mode and local_rpm_infos:
        from ..core.resolver import PackageAction, TransactionType
        actions_names = {a.name for a in result.actions}
        for info in local_rpm_infos:
            if info['name'] not in actions_names:
                # Package not in actions = already installed at same version
                # Add as REINSTALL action
                epoch = info.get('epoch', 0) or 0
                evr = f"{epoch}:{info['version']}-{info['release']}" if epoch else f"{info['version']}-{info['release']}"
                reinstall_action = PackageAction(
                    action=TransactionType.REINSTALL,
                    name=info['name'],
                    evr=evr,
                    arch=info['arch'],
                    nevra=info['nevra'],
                    size=info.get('filesize', 0) or 0,
                    media_name='@LocalRPMs',
                    reason=InstallReason.EXPLICIT
                )
                result.actions.append(reinstall_action)

    if not result.actions:
        print("Nothing to do")
        return 0

    # Categorize packages by install reason
    rec_pkgs = [a for a in result.actions if a.reason == InstallReason.RECOMMENDED]

    # Find available suggests only if --with-suggests is specified
    # Iterate to find suggests of suggests (e.g., digikam -> marble -> marble-qt)
    all_to_install = [a.name for a in result.actions]
    if with_suggests:
        suggests = []
        suggest_alternatives = []
        packages_to_check = all_to_install[:]
        checked_packages = set(p.lower() for p in all_to_install)
        max_iterations = 10  # Safety limit against infinite loops

        for _iteration in range(max_iterations):
            new_suggests, new_alternatives = resolver.find_available_suggests(
                packages_to_check, choices=choices, resolved_packages=list(checked_packages)
            )

            if not new_suggests and not new_alternatives:
                break

            # Handle alternatives for this iteration
            new_packages_from_alternatives = []

            if new_alternatives and not args.auto:
                for alt in new_alternatives:
                    if alt.capability in choices:
                        continue

                    # Filter providers based on preferences
                    filtered = preferences.filter_providers(alt.providers)

                    # If only one after filtering, auto-select
                    if len(filtered) == 1:
                        chosen_pkg = filtered[0]
                        choices[alt.capability] = chosen_pkg
                        sel = resolver.pool.select(chosen_pkg, solv.Selection.SELECTION_NAME)
                        for s in sel.solvables():
                            if s.repo and s.repo.name != '@System':
                                from ..core.resolver import InstallReason
                                pkg_action = PackageAction(
                                    action=TransactionType.INSTALL,
                                    name=s.name,
                                    evr=s.evr,
                                    arch=s.arch,
                                    nevra=f"{s.name}-{s.evr}.{s.arch}",
                                    size=s.size,
                                    media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                    reason=InstallReason.SUGGESTED,
                                )
                                if s.name.lower() not in checked_packages:
                                    new_suggests.append(pkg_action)
                                    new_packages_from_alternatives.append(s.name)
                                break
                        continue

                    # Ask user to choose
                    print(f"\n{alt.capability} ({alt.required_by}):")
                    for i, provider in enumerate(filtered, 1):
                        print(f"  {i}) {provider}")
                    print(f"  {len(filtered) + 1}) All")

                    try:
                        choice = input(f"\nChoice [1]: ").strip() or "1"
                        if choice == str(len(filtered) + 1):
                            # "All" selected - add all providers
                            for prov_name in filtered:
                                choices[alt.capability] = prov_name
                                sel = resolver.pool.select(prov_name, solv.Selection.SELECTION_NAME)
                                for s in sel.solvables():
                                    if s.repo and s.repo.name != '@System':
                                        from ..core.resolver import InstallReason
                                        pkg_action = PackageAction(
                                            action=TransactionType.INSTALL,
                                            name=s.name,
                                            evr=s.evr,
                                            arch=s.arch,
                                            nevra=f"{s.name}-{s.evr}.{s.arch}",
                                            size=s.size,
                                            media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                            reason=InstallReason.SUGGESTED,
                                        )
                                        if s.name.lower() not in checked_packages:
                                            new_suggests.append(pkg_action)
                                            new_packages_from_alternatives.append(s.name)
                                        break
                        else:
                            idx = int(choice) - 1
                            if 0 <= idx < len(filtered):
                                chosen_pkg = filtered[idx]
                                choices[alt.capability] = chosen_pkg
                                sel = resolver.pool.select(chosen_pkg, solv.Selection.SELECTION_NAME)
                                for s in sel.solvables():
                                    if s.repo and s.repo.name != '@System':
                                        from ..core.resolver import InstallReason
                                        pkg_action = PackageAction(
                                            action=TransactionType.INSTALL,
                                            name=s.name,
                                            evr=s.evr,
                                            arch=s.arch,
                                            nevra=f"{s.name}-{s.evr}.{s.arch}",
                                            size=s.size,
                                            media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                            reason=InstallReason.SUGGESTED,
                                        )
                                        if s.name.lower() not in checked_packages:
                                            new_suggests.append(pkg_action)
                                            new_packages_from_alternatives.append(s.name)
                                        break
                    except (ValueError, EOFError, KeyboardInterrupt):
                        print("\nAborted")
                        return 1

            elif new_alternatives and args.auto:
                # Auto mode: select first provider (already sorted by missing deps count)
                for alt in new_alternatives:
                    if alt.capability in choices:
                        continue

                    filtered = preferences.filter_providers(alt.providers)
                    if not filtered:
                        continue

                    chosen_pkg = filtered[0]
                    choices[alt.capability] = chosen_pkg

                    sel = resolver.pool.select(chosen_pkg, solv.Selection.SELECTION_NAME)
                    for s in sel.solvables():
                        if s.repo and s.repo.name != '@System':
                            from ..core.resolver import InstallReason
                            pkg_action = PackageAction(
                                action=TransactionType.INSTALL,
                                name=s.name,
                                evr=s.evr,
                                arch=s.arch,
                                nevra=f"{s.name}-{s.evr}.{s.arch}",
                                size=s.size,
                                media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                reason=InstallReason.SUGGESTED,
                            )
                            if s.name.lower() not in checked_packages:
                                new_suggests.append(pkg_action)
                                new_packages_from_alternatives.append(s.name)
                            break

            # Collect new suggests (not already checked)
            next_packages = []
            for s in new_suggests:
                if s.name.lower() not in checked_packages:
                    suggests.append(s)
                    checked_packages.add(s.name.lower())
                    next_packages.append(s.name)

                    # Also resolve dependencies of this suggest to check their suggests
                    # e.g., konq-plugins requires konqueror, konqueror suggests konqueror-handbook
                    sel = resolver.pool.select(s.name, solv.Selection.SELECTION_NAME)
                    for solv_pkg in sel.solvables():
                        if solv_pkg.repo and solv_pkg.repo.name != '@System':
                            for dep in solv_pkg.lookup_deparray(solv.SOLVABLE_REQUIRES):
                                dep_str = str(dep).split()[0]
                                if dep_str.startswith(('rpmlib(', '/', 'config(')):
                                    continue
                                # Find provider of this dependency
                                dep_obj = resolver.pool.Dep(dep_str)
                                for provider in resolver.pool.whatprovides(dep_obj):
                                    if provider.repo and provider.repo.name != '@System':
                                        if provider.name.lower() not in checked_packages:
                                            checked_packages.add(provider.name.lower())
                                            next_packages.append(provider.name)
                                        break
                            break

            # Add packages from alternatives to next check
            for pkg_name in new_packages_from_alternatives:
                if pkg_name.lower() not in checked_packages:
                    checked_packages.add(pkg_name.lower())
                    next_packages.append(pkg_name)

            # Next iteration: check newly found suggests
            packages_to_check = next_packages
            if not packages_to_check:
                break
    else:
        suggests = []
        suggest_alternatives = []

    # Calculate sizes for initial display
    rec_size = sum(a.size for a in rec_pkgs)
    sug_size = sum(a.size for a in suggests)

    # Determine final recommends/suggests behavior
    install_recommends_final = initial_recommends
    install_suggests = with_suggests

    # In interactive mode: ask about recommends (unless --without-recommends)
    if rec_pkgs and not args.auto and not without_recommends:
        print(f"\n{colors.success(f'Recommended packages ({len(rec_pkgs)})')} - {format_size(rec_size)}")
        from . import display
        rec_names = [f"{a.name}-{a.evr}" for a in rec_pkgs]
        display.print_package_list(rec_names, max_lines=5)
        try:
            answer = input(f"\nInstall recommended packages? [Y/n] ")
            install_recommends_final = answer.lower() not in ('n', 'no')
        except EOFError:
            print("\nAborted")
            return 1

    # In interactive mode with --with-suggests: ask about suggests
    if suggests and not args.auto:
        print(f"\n{colors.warning(f'Suggested packages ({len(suggests)})')} - {format_size(sug_size)}")
        from . import display
        sug_names = [f"{a.name}-{a.evr}" for a in suggests]
        display.print_package_list(sug_names, max_lines=5)
        try:
            answer = input(f"\nInstall suggested packages? [Y/n] ")
            install_suggests = answer.lower() not in ('n', 'no')
        except EOFError:
            print("\nAborted")
            return 1

    # Re-resolve with final preferences (recommends + suggests)
    need_reresolve = False
    final_packages = list(resolved_packages)

    if not install_recommends_final and rec_pkgs:
        need_reresolve = True

    if install_suggests and suggests:
        suggest_names = [s.name for s in suggests]
        final_packages = resolved_packages + suggest_names
        need_reresolve = True

    if need_reresolve:
        resolver = _create_resolver(db, args, install_recommends=install_recommends_final)
        if local_rpm_infos:
            resolver.add_local_rpms(local_rpm_infos)
        result, aborted = _resolve_with_alternatives(
            resolver, final_packages, choices, args.auto, preferences,
            local_packages=local_pkg_names
        )
        if aborted:
            return 1

        # If resolution failed and we have suggests, try removing problematic suggests
        skipped_suggests = {}  # suggest_name -> reason
        if not result.success and install_suggests and suggests:
            suggest_names_set = set(suggest_names)

            # Find suggests mentioned in problems and store the reason
            for prob in result.problems:
                prob_str = str(prob)
                for sug_name in suggest_names:
                    if sug_name in prob_str:
                        skipped_suggests[sug_name] = prob_str

            # If we found problematic suggests, retry without them
            if skipped_suggests:
                remaining_suggests = [s for s in suggest_names if s not in skipped_suggests]
                retry_packages = resolved_packages + remaining_suggests

                # Retry resolution
                resolver = _create_resolver(db, args, install_recommends=install_recommends_final)
                if local_rpm_infos:
                    resolver.add_local_rpms(local_rpm_infos)
                result, aborted = _resolve_with_alternatives(
                    resolver, retry_packages, choices, args.auto, preferences,
                    local_packages=local_pkg_names
                )
                if aborted:
                    return 1

                # Update suggest_names for marking below
                suggest_names = remaining_suggests

        if not result.success:
            print("Resolution failed:")
            for p in result.problems:
                print(f"  {p}")
            return 1

        # Show skipped suggests with reasons
        if skipped_suggests:
            from . import colors
            print(f"\n{colors.warning('Skipped suggests:')}")
            for sug in sorted(skipped_suggests.keys()):
                reason = skipped_suggests[sug]
                print(f"  {colors.dim(sug)}: {reason}")

        # Mark the suggest packages with the right reason
        if install_suggests and suggests:
            for action in result.actions:
                if action.name in suggest_names:
                    action.reason = InstallReason.SUGGESTED

    final_actions = list(result.actions)

    # Separate packages being removed (obsoleted) from packages being installed
    remove_pkgs = [a for a in final_actions if a.action == TransactionType.REMOVE]
    install_actions = [a for a in final_actions if a.action != TransactionType.REMOVE]

    # Categorize install packages by install reason
    explicit_pkgs = [a for a in install_actions if a.reason == InstallReason.EXPLICIT]
    dep_pkgs = [a for a in install_actions if a.reason == InstallReason.DEPENDENCY]
    rec_pkgs = [a for a in install_actions if a.reason == InstallReason.RECOMMENDED]
    sug_pkgs = [a for a in install_actions if a.reason == InstallReason.SUGGESTED]

    # Build set of explicit package names for history recording
    explicit_names = set(a.name.lower() for a in explicit_pkgs)

    # Calculate final sizes
    explicit_size = sum(a.size for a in explicit_pkgs)
    dep_size = sum(a.size for a in dep_pkgs)
    rec_size = sum(a.size for a in rec_pkgs)
    sug_size = sum(a.size for a in sug_pkgs)
    total_size = sum(a.size for a in final_actions if a.action.value in ('install', 'upgrade', 'reinstall'))

    # Show final transaction summary
    print(f"\n{colors.bold('Transaction summary:')}\n")
    from . import display

    if explicit_pkgs:
        print(f"  {colors.info(f'Requested ({len(explicit_pkgs)})')} - {format_size(explicit_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in explicit_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if dep_pkgs:
        print(f"  {colors.dim(f'Dependencies ({len(dep_pkgs)})')} - {format_size(dep_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in dep_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if rec_pkgs:
        print(f"  {colors.success(f'Recommended ({len(rec_pkgs)})')} - {format_size(rec_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in rec_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if sug_pkgs:
        print(f"  {colors.warning(f'Suggested ({len(sug_pkgs)})')} - {format_size(sug_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in sug_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if remove_pkgs:
        remove_size = sum(a.size for a in remove_pkgs)
        print(f"  {colors.error(f'Obsoleted ({len(remove_pkgs)})')} - {format_size(remove_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in remove_pkgs]
        display.print_package_list(pkg_names, indent=4)

    # Final confirmation
    if remove_pkgs:
        print(f"\n{colors.bold(f'Total: {len(install_actions)} to install, {len(remove_pkgs)} to remove')} ({format_size(total_size)})")
    else:
        print(f"\n{colors.bold(f'Total: {len(install_actions)} packages')} ({format_size(total_size)})")

    if not args.auto:
        try:
            answer = input("\nProceed with installation? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Update result.actions with final list
    result = Resolution(
        success=True,
        actions=final_actions,
        problems=[],
        install_size=total_size
    )

    if args.test:
        print("\n(dry run - no changes made)")
        return 0

    # Build download items (skip local RPMs - we already have them)
    ops = PackageOperations(db)
    download_items, local_action_paths = ops.build_download_items(
        result.actions, resolver, local_rpm_infos
    )

    # Download remote packages (if any)
    dl_results = []
    downloaded = 0
    cached = 0
    peer_stats = {}

    if download_items:
        print(colors.info("\nDownloading packages..."))
        dl_opts = InstallOptions(
            use_peers=not getattr(args, 'no_peers', False),
            only_peers=getattr(args, 'only_peers', False),
        )

        # Multi-line progress display using DownloadProgressDisplay
        from . import display
        progress_display = display.DownloadProgressDisplay(num_workers=4)

        def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                     item_bytes=None, item_total=None, slots_status=None):
            # Calculate global speed from all active downloads
            global_speed = 0.0
            if slots_status:
                for slot, prog in slots_status:
                    if prog is not None:
                        global_speed += prog.get_speed()

            progress_display.update(
                pkg_num, pkg_total, bytes_done, bytes_total,
                slots_status or [], global_speed
            )

        download_start = time.time()
        dl_results, downloaded, cached, peer_stats = ops.download_packages(
            download_items, options=dl_opts, progress_callback=progress,
            urpm_root=getattr(args, 'urpm_root', None)
        )
        download_elapsed = time.time() - download_start
        progress_display.finish()

        # Check for failures
        failed = [r for r in dl_results if not r.success]
        if failed:
            print(colors.error(f"\n{len(failed)} download(s) failed:"))
            for r in failed[:5]:
                print(f"  {colors.error(r.item.name)}: {r.error}")
            return 1

        # Download summary with P2P stats and timing
        cache_str = colors.warning(str(cached)) if cached > 0 else colors.dim(str(cached))
        from_peers = peer_stats.get('from_peers', 0)
        from_upstream = peer_stats.get('from_upstream', 0)
        time_str = display.format_duration(download_elapsed)
        if from_peers > 0:
            print(f"  {colors.success(f'{downloaded} downloaded')} ({from_peers} from peers, {from_upstream} from mirrors), {cache_str} from cache in {time_str}")
        else:
            print(f"  {colors.success(f'{downloaded} downloaded')}, {cache_str} from cache in {time_str}")

        # Notify urpmd to invalidate cache index (so new downloads are visible to peers)
        if downloaded > 0:
            PackageOperations.notify_urpmd_cache_invalidate()

    # Handle --download-only mode
    download_only = getattr(args, 'download_only', False)
    if download_only:
        print(colors.success("\nPackages downloaded to cache. Use 'urpm install' to install them later."))
        return 0

    # Collect RPM paths for installation (downloaded + local)
    rpm_paths = [r.path for r in dl_results if r.success and r.path]
    rpm_paths.extend(local_action_paths)  # Add local RPM files

    # DEBUG: show what packages are in rpm_paths
    if DEBUG_INSTALL:
        print(colors.dim(f"  DEBUG rpm_paths ({len(rpm_paths)}):"))
        for rp in rpm_paths:
            print(colors.dim(f"    {Path(rp).name}"))

    if not rpm_paths:
        print("No packages to install")
        return 0

    # Begin transaction for history
    cmd_line = "urpm install " + " ".join(args.packages)
    transaction_id = ops.begin_transaction('install', cmd_line, result.actions)

    # Setup Ctrl+C handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            # Second Ctrl+C - force abort
            print("\n\nForce abort!")
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")
            print("Press Ctrl+C again to force abort (may leave system inconsistent)")

    signal.signal(signal.SIGINT, sigint_handler)

    print(colors.info(f"\nInstalling {len(rpm_paths)} packages..."))

    # Check if another install is in progress
    # Use root path for lock file when installing to chroot
    install_root = getattr(args, 'root', None) or getattr(args, 'urpm_root', None)
    lock = InstallLock(root=install_root)
    if not lock.acquire(blocking=False):
        print(colors.warning("  RPM database is locked by another process."))
        print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))

        def wait_cb(pid):
            pass  # Just wait silently, message already shown

        lock.acquire(blocking=True, wait_callback=wait_cb)
    lock.release()  # Release - child will acquire its own lock

    last_shown = [None]

    try:
        from ..core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        install_opts = InstallOptions(
            verify_signatures=not getattr(args, 'nosignature', False),
            force=getattr(args, 'force', False),
            test=getattr(args, 'test', False),
            reinstall=getattr(args, 'reinstall', False),
            noscripts=getattr(args, 'noscripts', False),
            root=rpm_root or "/",
            use_userns=bool(getattr(args, 'allow_no_root', False) and rpm_root),
            sync=getattr(args, 'sync', False),
        )

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_shown[0] = name

        queue_result = ops.execute_install(
            rpm_paths, options=install_opts, progress_callback=queue_progress
        )

        # Print done
        print(f"\r\033[K  [{len(rpm_paths)}/{len(rpm_paths)}] done")

        if not queue_result.success:
            print(colors.error(f"\nInstallation failed:"))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            ops.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Installation interrupted"))
            ops.abort_transaction(transaction_id)
            return 130

        installed_count = queue_result.operations[0].count if queue_result.operations else len(rpm_paths)
        if remove_pkgs:
            print(colors.success(f"  {installed_count} packages installed, {len(remove_pkgs)} removed"))
        else:
            print(colors.success(f"  {installed_count} packages installed"))
        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        ops.mark_dependencies(resolver, result.actions)
        dep_packages = [a.name for a in result.actions
                        if a.reason != InstallReason.EXPLICIT]
        if dep_packages:
            _write_debug_file(DEBUG_LAST_INSTALLED_DEPS, dep_packages)

        # Debug: copy the installed-through-deps.list for inspection
        _copy_installed_deps_list()

        return 0

    except Exception as e:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_download(args, db: PackageDatabase) -> int:
    """Handle download command - download packages without installing.

    Downloads packages and their dependencies to the local cache.
    Uses ignore_installed=True to resolve all dependencies, even if already installed.
    """
    import time
    import platform
    from pathlib import Path

    from ..core.resolver import Resolver, Resolution, format_size, set_solver_debug, PackageAction
    from ..core.download import Downloader, DownloadItem
    from ..core.config import get_base_dir
    from . import colors

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    watched_pkgs = getattr(args, 'watched', None)
    if watched_pkgs:
        watched_pkgs = [p.strip() for p in watched_pkgs.split(',')]
    if debug_solver or watched_pkgs:
        set_solver_debug(enabled=debug_solver, watched=watched_pkgs)

    # Collect packages to download
    packages = list(args.packages) if args.packages else []

    # Handle --builddeps option
    builddeps = getattr(args, 'builddeps', None)
    if builddeps:
        from ..core.buildrequires import get_buildrequires, list_specs_in_workdir, rpm_dep_to_solver_format

        try:
            if builddeps == 'AUTO':
                # Auto-detect mode
                specs = list_specs_in_workdir()
                if len(specs) > 1:
                    # Multiple specs found - ask user
                    print(colors.info("Multiple .spec files found:"))
                    for i, spec in enumerate(specs, 1):
                        print(f"  {i}. {spec.name}")
                    if getattr(args, 'auto', False):
                        print(colors.error("Error: Multiple .spec files found. Specify which one to use."))
                        return 1
                    try:
                        choice = input("Select spec file (number): ").strip()
                        idx = int(choice) - 1
                        if 0 <= idx < len(specs):
                            builddeps = str(specs[idx])
                        else:
                            print(colors.error("Invalid choice"))
                            return 1
                    except (ValueError, KeyboardInterrupt):
                        print("\nAborted.")
                        return 1
                else:
                    builddeps = 'AUTO'  # Let get_buildrequires handle it

            target = None if builddeps == 'AUTO' else builddeps
            reqs, source = get_buildrequires(target)
            print(colors.info(f"Build dependencies from: {source}"))
            print(f"  Found {len(reqs)} BuildRequires")
            # Convert to solver format and add to packages
            packages.extend(rpm_dep_to_solver_format(req) for req in reqs)

        except FileNotFoundError as e:
            print(colors.error(f"Error: {e}"))
            return 1
        except ValueError as e:
            print(colors.error(f"Error: {e}"))
            return 1

    if not packages:
        print(colors.error("Error: No packages specified"))
        print("Usage: urpm download [packages...] [--builddeps [spec]]")
        return 1

    # Get target release/arch
    target_release = getattr(args, 'release', None)
    target_arch = getattr(args, 'arch', None) or platform.machine()

    # Get CLI options
    without_recommends = getattr(args, 'without_recommends', False)
    nodeps = getattr(args, 'nodeps', False)
    auto_mode = getattr(args, 'auto', False)

    # Show what we're downloading
    print(colors.info(f"\nResolving packages for download..."))
    if target_release:
        print(f"  Target release: {target_release}")
    print(f"  Target arch: {target_arch}")
    print(f"  Packages: {', '.join(packages[:5])}" + (f" ... (+{len(packages)-5} more)" if len(packages) > 5 else ""))

    # Create resolver with ignore_installed=True (resolves all deps)
    resolver = _create_resolver(
        db, args,
        arch=target_arch,
        install_recommends=not without_recommends,
        ignore_installed=True
    )

    if nodeps:
        # --nodeps: download only specified packages, no dependency resolution
        from ..core.resolver import PackageAction, TransactionType, Resolution, InstallReason
        actions = []
        not_found = []

        for pkg_spec in packages:
            # Clean package name (remove version constraints for lookup)
            pkg_name = pkg_spec.split()[0] if ' ' in pkg_spec else pkg_spec
            pkg = db.get_package_smart(pkg_name)
            if not pkg:
                not_found.append(pkg_spec)
                continue

            media = db.get_media_by_id(pkg['media_id'])
            media_name = media.get('name', 'unknown') if media else 'unknown'
            epoch = pkg.get('epoch', 0) or 0
            evr = f"{epoch}:{pkg['version']}-{pkg['release']}" if epoch else f"{pkg['version']}-{pkg['release']}"
            actions.append(PackageAction(
                action=TransactionType.INSTALL,
                name=pkg['name'],
                evr=evr,
                arch=pkg['arch'],
                nevra=pkg['nevra'],
                size=pkg.get('filesize', 0) or 0,
                media_name=media_name,
                reason=InstallReason.EXPLICIT
            ))
            print(f"Insert {pkg['name']} {pkg.get('filesize',0)}")

        if not_found:
            print(colors.error(f"Packages not found ({len(not_found)}):"))
            for p in not_found[:10]:
                print(f"  {p}")
            if len(not_found) > 10:
                print(f"  ... and {len(not_found) - 10} more")
            return 1

        result = Resolution(success=True, actions=actions, problems=[])
    else:
        # Normal resolution with alternatives handling
        result, aborted = _resolve_with_alternatives(resolver, packages, {}, auto_mode)
        if aborted:
            return 1

    if not result.success:
        print(colors.error("Resolution failed:"))
        for p in result.problems:
            print(f"  {p}")
        return 1

    # Filter to only install actions
    install_actions = [a for a in result.actions if a.action.name in ('INSTALL', 'UPGRADE', 'DOWNGRADE')]

    if not install_actions:
        print(colors.success("Nothing to download - all packages already available."))
        return 0

    # Calculate total size
    total_size = sum(a.size for a in install_actions if a.size)

    # Show summary
    print(colors.info(f"\nPackages to download ({len(install_actions)}):"))
    for action in install_actions[:20]:
        size_str = format_size(action.size) if action.size else "?"
        print(f"  {action.nevra} ({size_str})")
    if len(install_actions) > 20:
        print(f"  ... and {len(install_actions) - 20} more")
    print(f"\nTotal download size: {format_size(total_size)}")

    # Confirm unless --auto
    if not auto_mode:
        try:
            confirm = input("\nProceed with download? [Y/n] ").strip().lower()
            if confirm and confirm not in ('y', 'yes', 'o', 'oui'):
                print("Aborted.")
                return 0
        except KeyboardInterrupt:
            print("\nAborted.")
            return 0

    # Build download items
    download_items = []
    media_cache = {}
    servers_cache = {}

    for action in install_actions:
        media_name = action.media_name
        if media_name not in media_cache:
            media = db.get_media(media_name)
            media_cache[media_name] = media
            if media and media.get('id'):
                servers_cache[media['id']] = db.get_servers_for_media(
                    media['id'], enabled_only=True
                )

        media = media_cache[media_name]
        if not media:
            print(f"  Warning: media '{media_name}' not found")
            continue

        # Parse EVR
        evr = action.evr
        if ':' in evr:
            evr = evr.split(':', 1)[1]
        version, release = evr.rsplit('-', 1) if '-' in evr else (evr, '1')

        if media.get('relative_path'):
            servers = servers_cache.get(media['id'], [])
            servers = [dict(s) for s in servers]
            download_items.append(DownloadItem(
                name=action.name,
                version=version,
                release=release,
                arch=action.arch,
                media_id=media['id'],
                relative_path=media['relative_path'],
                is_official=bool(media.get('is_official', 1)),
                servers=servers,
                media_name=media_name,
                size=action.size,
            ))
        elif media.get('url'):
            download_items.append(DownloadItem(
                name=action.name,
                version=version,
                release=release,
                arch=action.arch,
                media_url=media['url'],
                media_name=media_name,
                size=action.size,
            ))
        else:
            print(f"  Warning: no URL or servers for media '{media_name}'")

    if not download_items:
        print(colors.error("No packages to download"))
        return 1

    # Download packages
    print(colors.info("\nDownloading packages..."))
    use_peers = not getattr(args, 'no_peers', False)
    only_peers = getattr(args, 'only_peers', False)
    cache_dir = get_base_dir(urpm_root=getattr(args, 'urpm_root', None))
    downloader = Downloader(cache_dir=cache_dir, use_peers=use_peers, only_peers=only_peers, db=db)

    # Progress display
    from . import display
    progress_display = display.DownloadProgressDisplay(num_workers=4)

    def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                 item_bytes=None, item_total=None, slots_status=None):
        global_speed = 0.0
        if slots_status:
            for slot, prog in slots_status:
                if prog is not None:
                    global_speed += prog.get_speed()
        progress_display.update(
            pkg_num, pkg_total, bytes_done, bytes_total,
            slots_status or [], global_speed
        )

    download_start = time.time()
    dl_results, downloaded, cached, peer_stats = downloader.download_all(download_items, progress)
    download_elapsed = time.time() - download_start
    progress_display.finish()

    # Check for failures
    failed = [r for r in dl_results if not r.success]
    if failed:
        print(colors.error(f"\n{len(failed)} download(s) failed:"))
        for r in failed[:5]:
            print(f"  {colors.error(r.item.name)}: {r.error}")
        return 1

    # Summary
    from_peers = peer_stats.get('from_peers', 0)
    from_upstream = peer_stats.get('from_upstream', 0)
    time_str = display.format_duration(download_elapsed)

    print(f"\n{colors.success('Download complete')}:")
    print(f"  {downloaded} downloaded, {cached} from cache in {time_str}")
    if from_peers > 0:
        print(f"  P2P: {from_peers} from peers, {from_upstream} from upstream")

    # Notify urpmd to invalidate cache index (so new downloads are visible to peers)
    if downloaded > 0:
        _notify_urpmd_cache_invalidate()

    print(colors.success(f"\nPackages saved to cache. Use 'urpm install' to install them."))
    return 0


def cmd_mkimage(args, db: PackageDatabase) -> int:
    """Create a minimal Docker/Podman image for RPM builds."""
    import argparse
    import os
    import platform
    import shutil
    import tempfile
    from pathlib import Path

    from ..core.container import detect_runtime, Container
    from . import colors

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
        from ..core.database import PackageDatabase
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
        import subprocess
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
                print(colors.error(f"Failed to install urpm"))
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
            import os
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
    import os
    import shutil

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
        print(f"  Created /var/tmp")

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


def cmd_build(args, db: PackageDatabase) -> int:
    """Build RPM package(s) in isolated containers."""
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ..core.container import detect_runtime, Container
    from . import colors

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
            print(colors.dim(f"  Use a .src.rpm or .spec file instead"))
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
        print(f"\nFailed packages:")
        for source, success, msg in results:
            if not success:
                print(f"  {colors.error('X')} {source.name}: {msg}")

    return 0 if fail_count == 0 else 1


def _find_workspace(source_path: 'Path') -> tuple:
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
    from pathlib import Path

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
    source_path: 'Path',
    output_dir: 'Path',
    keep_container: bool
) -> tuple:
    """Build a single package in a container.

    Returns:
        Tuple of (source_path, success, message)
    """
    from pathlib import Path
    from . import colors

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

        # 5. Install build dependencies
        print(f"  Installing BuildRequires...")
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--sync', '--builddeps', spec_path
        ])
        if ret != 0:
            return (source_path, False, f"BuildRequires install failed")

        # 6. Build the package
        print(f"  Building...")
        result = container.exec_stream(cid, [
            'rpmbuild', '-ba', spec_path
        ])
        if result != 0:
            return (source_path, False, "rpmbuild failed")

        # 7. Copy results out
        print(f"  Retrieving results...")

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


def cmd_erase(args, db: PackageDatabase) -> int:
    """Handle erase (remove) command."""
    import platform
    import signal

    from ..core.resolver import Resolver, format_size, set_solver_debug
    from ..core.install import check_root
    from ..core.operations import PackageOperations, InstallOptions
    from ..core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from . import colors

    # Check for previous background errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # If --auto-orphans without packages, delegate to cmd_autoremove (urpme compat)
    clean_deps = getattr(args, 'auto_orphans', False)
    if clean_deps and not args.packages:
        return cmd_autoremove(args, db)

    # Must have packages if not --auto-orphans
    if not args.packages:
        print(colors.error("Error: no packages specified"))
        print(colors.dim("  Use --auto-orphans to remove orphan dependencies"))
        return 1

    # Debug: save previous state and clear debug files at start
    _copy_installed_deps_list(dest=DEBUG_PREV_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_REMOVED_DEPS)

    # Check root
    if not check_root():
        print(colors.error("Error: erase requires root privileges"))
        return 1

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    if debug_solver:
        set_solver_debug(enabled=True)

    # Resolve what to remove
    resolver = _create_resolver(db, args)
    result = resolver.resolve_remove(args.packages, clean_deps=False)

    if not result.success:
        print(colors.error("Resolution failed:"))
        for prob in result.problems:
            print(f"  {colors.error(prob)}")
        return 1

    if not result.actions:
        print(colors.info("Nothing to erase."))
        return 0

    # Separate explicit requests from reverse dependencies
    explicit_names = set()
    for pkg in args.packages:
        pkg_name = _extract_pkg_name(pkg).lower()
        explicit_names.add(pkg_name)

    # Also include packages that provide what the user requested
    for action in result.actions:
        pkg_info = db.get_package(action.name)
        if pkg_info and pkg_info.get('provides'):
            for prov in pkg_info['provides']:
                prov_name = prov.split('[')[0].split('=')[0].split('<')[0].split('>')[0].strip().lower()
                if prov_name in explicit_names:
                    explicit_names.add(action.name.lower())
                    break

    explicit = [a for a in result.actions if a.name.lower() in explicit_names]
    deps = [a for a in result.actions if a.name.lower() not in explicit_names]

    # Find orphaned dependencies unless --keep-orphans
    keep_orphans = getattr(args, 'keep_orphans', False)
    orphans = []
    include_orphans = False

    if not keep_orphans:
        erase_names = [a.name for a in result.actions]
        orphans = resolver.find_erase_orphans(
            erase_names,
            erase_recommends=getattr(args, 'erase_recommends', False),
            keep_suggests=getattr(args, 'keep_suggests', False)
        )

    all_actions = list(result.actions)
    total_size = result.remove_size

    # Show what will be erased (without orphans first)
    print(f"\n{colors.bold(f'The following {len(all_actions)} package(s) will be erased:')}")
    from . import display

    if explicit:
        print(f"\n  {colors.info(f'Requested ({len(explicit)}):')}")
        pkg_names = [a.nevra for a in explicit]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)

    if deps:
        print(f"\n  {colors.warning(f'Reverse dependencies ({len(deps)}):')}")
        pkg_names = [a.nevra for a in deps]
        display.print_package_list(pkg_names, indent=4, color_func=colors.warning)

    # Handle orphans: ask or auto-include
    if orphans:
        # Determine if we should auto-include orphans
        # --auto-orphans OR (--auto AND NOT --keep-orphans)
        auto_include_orphans = clean_deps or (args.auto and not keep_orphans)

        if auto_include_orphans:
            # Include orphans automatically
            include_orphans = True
            print(f"\n  {colors.warning(f'Orphaned dependencies ({len(orphans)}):')}")
            pkg_names = [a.nevra for a in orphans]
            display.print_package_list(pkg_names, indent=4, color_func=colors.warning)
        else:
            # Ask user about orphans
            print(f"\n  {colors.dim(f'Orphaned dependencies that could be removed ({len(orphans)}):')}")
            pkg_names = [a.nevra for a in orphans]
            display.print_package_list(pkg_names, indent=4, color_func=colors.dim)
            try:
                response = input(f"\n  Also remove these {len(orphans)} orphaned packages? [y/N] ")
                include_orphans = response.lower() in ('y', 'yes')
                if include_orphans:
                    print(colors.success("  Orphans will be removed"))
                else:
                    print(colors.dim("  Orphans will be kept"))
            except (KeyboardInterrupt, EOFError):
                print("\n  Orphans will be kept")
                include_orphans = False

    # Add orphans to the removal if confirmed
    if include_orphans and orphans:
        all_actions = all_actions + orphans
        for o in orphans:
            total_size += o.size

    if total_size > 0:
        print(f"\nDisk space freed: {colors.success(format_size(total_size))}")

    # Confirmation
    if not args.auto:
        try:
            response = input("\nProceed with removal? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    if args.test:
        print("\n(dry run - no changes made)")
        return 0

    # Set correct reasons on actions for transaction history
    from ..core.resolver import InstallReason
    for action in all_actions:
        if action.name.lower() in explicit_names:
            action.reason = InstallReason.EXPLICIT
        else:
            action.reason = InstallReason.DEPENDENCY

    # Record transaction
    ops = PackageOperations(db)
    cmd_line = ' '.join(['urpm', 'erase'] + args.packages)
    transaction_id = ops.begin_transaction('erase', cmd_line, all_actions)

    # Setup Ctrl+C handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            print("\n\nForce abort!")
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")
            print("Press Ctrl+C again to force abort (may leave system inconsistent)")

    signal.signal(signal.SIGINT, sigint_handler)

    # Erase packages (all from resolution, including reverse deps and orphans)
    print(colors.info(f"\nErasing {len(all_actions)} packages..."))
    packages_to_erase = [action.name for action in all_actions]

    # Check if another operation is in progress
    lock = InstallLock()
    if not lock.acquire(blocking=False):
        print(colors.warning("  RPM database is locked by another process."))
        print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
        lock.acquire(blocking=True)
    lock.release()  # Release - child will acquire its own lock

    last_erase_shown = [None]

    try:
        from ..core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        erase_opts = InstallOptions(
            force=getattr(args, 'force', False),
            test=getattr(args, 'test', False),
            root=rpm_root or "/",
            sync=getattr(args, 'sync', False),
        )

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_erase_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_erase_shown[0] = name

        queue_result = ops.execute_erase(
            packages_to_erase, options=erase_opts, progress_callback=queue_progress
        )

        # Print done
        print(f"\r\033[K  [{len(packages_to_erase)}/{len(packages_to_erase)}] done")

        if not queue_result.success:
            print(colors.error(f"\nErase failed:"))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            if not erase_opts.force:
                print(colors.dim("  Use --force to ignore dependency problems"))
            ops.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Erase interrupted"))
            ops.abort_transaction(transaction_id)
            return 130

        erased_count = queue_result.operations[0].count if queue_result.operations else len(packages_to_erase)
        print(colors.success(f"  {erased_count} packages erased"))
        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        erased_packages = [action.name for action in all_actions]
        resolver.unmark_packages(erased_packages)

        # Debug: write orphans that were removed
        orphan_names = [o.name for o in orphans]
        if orphan_names:
            _write_debug_file(DEBUG_LAST_REMOVED_DEPS, orphan_names)

        # Debug: copy the installed-through-deps.list for inspection
        _copy_installed_deps_list()

        return 0

    except Exception as e:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_upgrade(args, db: PackageDatabase) -> int:
    """Handle upgrade command - upgrade packages."""
    import platform
    import signal

    from . import colors
    from ..core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from ..core.operations import PackageOperations, InstallOptions

    # Check for previous background install errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # Debug: save previous state and clear debug files at start
    _copy_installed_deps_list(dest=DEBUG_PREV_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_REMOVED_DEPS)

    from ..core.resolver import Resolver, format_size, set_solver_debug
    from ..core.install import check_root
    from pathlib import Path
    from ..core.rpm import is_local_rpm, read_rpm_header
    from ..core.download import verify_rpm_signature

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    watched_pkgs = getattr(args, 'watched', None)
    if watched_pkgs:
        watched_pkgs = [p.strip() for p in watched_pkgs.split(',')]
    if debug_solver or watched_pkgs:
        set_solver_debug(enabled=debug_solver, watched=watched_pkgs)

    # Determine what to upgrade
    packages = getattr(args, 'packages', []) or []
    # No packages = full system upgrade (apt-style: urpm upgrade = upgrade all)
    upgrade_all = not packages

    # Separate local RPM files from package names
    local_rpm_paths = []
    local_rpm_infos = []
    package_names = []
    verify_sigs = not getattr(args, 'nosignature', False)

    for pkg in packages:
        if is_local_rpm(pkg):
            path = Path(pkg)
            if not path.exists():
                print(colors.error(f"Error: file not found: {pkg}"))
                return 1
            # Read RPM header
            info = read_rpm_header(path)
            if not info:
                print(colors.error(f"Error: cannot read RPM file: {pkg}"))
                return 1
            # Verify signature
            if verify_sigs:
                valid, error = verify_rpm_signature(path)
                if not valid:
                    print(colors.error(f"Error: signature verification failed for {pkg}"))
                    print(colors.error(f"  {error}"))
                    print(colors.dim("  Use --nosignature to skip verification (not recommended)"))
                    return 1
            local_rpm_paths.append(str(path.resolve()))
            local_rpm_infos.append(info)
        else:
            package_names.append(pkg)

    # If we have local RPMs, show what we're upgrading
    if local_rpm_infos:
        print(f"Local RPM files ({len(local_rpm_infos)}):")
        for info in local_rpm_infos:
            print(f"  {info['nevra']}")

    # Check root
    if not check_root():
        print(colors.error("Error: upgrade requires root privileges"))
        return 1

    # Resolve upgrades
    # For upgrades, don't install recommends by default (unlike install)
    # This matches urpmi --auto-update behavior
    install_recommends = getattr(args, 'with_recommends', False)
    resolver = _create_resolver(db, args, install_recommends=install_recommends)

    # Add local RPMs to resolver pool before resolution
    if local_rpm_infos:
        resolver.add_local_rpms(local_rpm_infos)
        # Add local package names to the list
        for info in local_rpm_infos:
            package_names.append(info['name'])

    if upgrade_all:
        print("Resolving system upgrade...")
        result = resolver.resolve_upgrade()
    else:
        print(f"Resolving upgrade for: {', '.join(package_names)}")
        # Build set of local package names for special handling
        local_pkg_names = {info['name'] for info in local_rpm_infos}
        result = resolver.resolve_upgrade(package_names, local_packages=local_pkg_names)

    if not result.success:
        print(colors.error("Resolution failed:"))
        for prob in result.problems:
            print(f"  {colors.error(prob)}")
        return 1

    # Show warnings for held packages
    held_count = 0
    if hasattr(resolver, '_held_upgrade_warnings') and resolver._held_upgrade_warnings:
        held_count += len(resolver._held_upgrade_warnings)
    if hasattr(resolver, '_held_obsolete_warnings') and resolver._held_obsolete_warnings:
        held_count += len(resolver._held_obsolete_warnings)

    if held_count > 0:
        print(colors.warning(f"\nHeld packages ({held_count}) skipped:"))
        if hasattr(resolver, '_held_upgrade_warnings') and resolver._held_upgrade_warnings:
            for held_pkg in resolver._held_upgrade_warnings:
                print(f"  {colors.warning(held_pkg)} (upgrade skipped)")
        if hasattr(resolver, '_held_obsolete_warnings') and resolver._held_obsolete_warnings:
            for held_pkg, obsoleting_pkg in resolver._held_obsolete_warnings:
                print(f"  {colors.warning(held_pkg)} (would be obsoleted by {obsoleting_pkg})")
        print(f"\n  Use '{colors.dim('urpm unhold <package>')}' to allow changes.")

    if not result.actions:
        print(colors.success("All packages are up to date."))
        return 0

    # Categorize actions
    upgrades = [a for a in result.actions if a.action.value == 'upgrade']
    installs = [a for a in result.actions if a.action.value == 'install']
    removes = [a for a in result.actions if a.action.value == 'remove']
    downgrades = [a for a in result.actions if a.action.value == 'downgrade']

    # Find orphaned dependencies (unless --noerase-orphans)
    # Exclude packages already in removes to avoid duplicates
    orphans = []
    if upgrades and not getattr(args, 'noerase_orphans', False):
        removes_names = {a.name for a in removes}
        orphans = [o for o in resolver.find_upgrade_orphans(upgrades)
                   if o.name not in removes_names]

    # Show packages by category
    from . import colors, display
    print(f"\n{colors.bold('Transaction summary:')}")
    if upgrades:
        print(f"\n  {colors.info(f'Upgrade ({len(upgrades)}):')}")
        pkg_names = [a.nevra for a in sorted(upgrades, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.info)
    if installs:
        print(f"\n  {colors.success(f'Install ({len(installs)}) - new dependencies:')}")
        pkg_names = [a.nevra for a in sorted(installs, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.success)
    if removes:
        print(f"\n  {colors.error(f'Remove ({len(removes)}) - obsoleted:')}")
        pkg_names = [a.nevra for a in sorted(removes, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)
    if downgrades:
        print(f"\n  {colors.warning(f'Downgrade ({len(downgrades)}):')}")
        pkg_names = [a.nevra for a in sorted(downgrades, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.warning)
    if orphans:
        print(f"\n  {colors.error(f'Remove ({len(orphans)}) - orphaned dependencies:')}")
        pkg_names = [a.nevra for a in sorted(orphans, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)

    if result.install_size > 0:
        print(f"\nDownload size: {format_size(result.install_size)}")

    # Confirmation
    if not getattr(args, 'auto', False):
        try:
            response = input("\nProceed with upgrade? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    if getattr(args, 'test', False):
        print("\n(dry run - no changes made)")
        return 0

    # Build download items and download
    ops = PackageOperations(db)
    download_items, local_action_paths = ops.build_download_items(
        result.actions, resolver, local_rpm_infos
    )

    dl_results = []
    downloaded = 0

    if download_items:
        print(f"\nDownloading {len(download_items)} packages...")
        dl_opts = InstallOptions(
            use_peers=not getattr(args, 'no_peers', False),
            only_peers=getattr(args, 'only_peers', False),
        )

        # Multi-line progress display using DownloadProgressDisplay
        from . import display
        progress_display = display.DownloadProgressDisplay(num_workers=4)

        def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                     item_bytes=None, item_total=None, slots_status=None):
            global_speed = 0.0
            if slots_status:
                for slot, prog in slots_status:
                    if prog is not None:
                        global_speed += prog.get_speed()
            progress_display.update(
                pkg_num, pkg_total, bytes_done, bytes_total,
                slots_status or [], global_speed
            )

        download_start = time.time()
        dl_results, downloaded, cached, peer_stats = ops.download_packages(
            download_items, options=dl_opts, progress_callback=progress,
            urpm_root=getattr(args, 'urpm_root', None)
        )
        download_elapsed = time.time() - download_start
        progress_display.finish()

        # Check failures
        failed = [r for r in dl_results if not r.success]
        if failed:
            print(colors.error(f"\n{len(failed)} download(s) failed:"))
            for r in failed[:5]:
                print(f"  {colors.error(r.item.name)}: {r.error}")
            return 1

        # Download summary with P2P stats and timing
        cache_str = colors.warning(str(cached)) if cached > 0 else colors.dim(str(cached))
        from_peers = peer_stats.get('from_peers', 0)
        from_upstream = peer_stats.get('from_upstream', 0)
        time_str = display.format_duration(download_elapsed)
        if from_peers > 0:
            print(f"  {colors.success(f'{downloaded} downloaded')} ({from_peers} from peers, {from_upstream} from mirrors), {cache_str} from cache in {time_str}")
        else:
            print(f"  {colors.success(f'{downloaded} downloaded')}, {cache_str} from cache in {time_str}")

        if downloaded > 0:
            PackageOperations.notify_urpmd_cache_invalidate()

    rpm_paths = [r.path for r in dl_results if r.success and r.path]
    rpm_paths.extend(local_action_paths)

    # Set correct reasons on actions for transaction history
    from ..core.resolver import InstallReason
    explicit_names = set(p.lower() for p in package_names) if package_names else set()
    for action in result.actions:
        if action.name.lower() in explicit_names or upgrade_all:
            action.reason = InstallReason.EXPLICIT
        else:
            action.reason = InstallReason.DEPENDENCY

    # Include orphans in transaction recording (with 'orphan' reason)
    all_record_actions = list(result.actions)
    orphan_names = [a.name for a in orphans] if orphans else []
    if orphans:
        for o in orphans:
            o.reason = 'orphan'
        all_record_actions.extend(orphans)

    # Record transaction
    if upgrade_all:
        cmd_line = "urpm upgrade"
    else:
        cmd_line = "urpm update " + " ".join(package_names)
    transaction_id = ops.begin_transaction('upgrade', cmd_line, all_record_actions)

    # Setup interrupt handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            print("\n\nForce abort!")
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        from ..core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        upgrade_opts = InstallOptions(
            verify_signatures=not getattr(args, 'nosignature', False),
            force=getattr(args, 'force', False),
            test=getattr(args, 'test', False),
            root=rpm_root or "/",
            sync=getattr(args, 'sync', False),
        )

        remove_names = [a.name for a in removes] if removes else []

        if not rpm_paths and not remove_names and not orphan_names:
            ops.complete_transaction(transaction_id)
            return 0

        # Check if another install is in progress
        lock = InstallLock()
        if not lock.acquire(blocking=False):
            print(colors.warning("  RPM database is locked by another process."))
            print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
            lock.acquire(blocking=True)
        lock.release()  # Release - child will acquire its own lock

        # Progress tracking
        last_shown = [None]
        current_phase = [""]

        def queue_progress(op_id: str, name: str, current: int, total: int):
            if current_phase[0] != op_id:
                current_phase[0] = op_id
                if op_id == "upgrade":
                    print(f"\nUpgrading {total} packages...")
                last_shown[0] = None
            if last_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_shown[0] = name

        queue_result = ops.execute_upgrade(
            rpm_paths,
            erase_names=remove_names,
            orphan_names=orphan_names or None,
            options=upgrade_opts,
            progress_callback=queue_progress,
        )

        # Clear the line after last progress
        print(f"\r\033[K", end='')

        if interrupted[0]:
            print(colors.warning(f"\n  Operation interrupted"))
            ops.abort_transaction(transaction_id)
            return 130

        # Process results for each operation
        upgrade_success = True

        for op_result in queue_result.operations:
            if op_result.operation_id == "upgrade":
                if op_result.success:
                    msg_parts = []
                    if op_result.count > 0:
                        msg_parts.append(f"{op_result.count} packages upgraded")
                    if remove_names:
                        msg_parts.append(f"{len(remove_names)} removed (obsoleted)")
                    if msg_parts:
                        print(colors.success(f"  {', '.join(msg_parts)}"))
                else:
                    upgrade_success = False
                    print(colors.error(f"\nUpgrade failed:"))
                    for err in op_result.errors[:3]:
                        print(f"  {colors.error(err)}")

        # Orphan cleanup runs in background - just display status
        if orphan_names:
            print(colors.dim(f"  {len(orphan_names)} orphaned packages being removed in background..."))
            resolver.unmark_packages(orphan_names)

        if not upgrade_success:
            ops.abort_transaction(transaction_id)
            return 1

        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        new_deps = [a.name for a in result.actions if a.action.value == 'install']
        if new_deps:
            resolver.mark_as_dependency(new_deps)
            _write_debug_file(DEBUG_LAST_INSTALLED_DEPS, new_deps)
        removed = [a.name for a in result.actions if a.action.value == 'remove']
        if removed:
            resolver.unmark_packages(removed)

        # Debug: write orphans that were removed
        if orphan_names:
            _write_debug_file(DEBUG_LAST_REMOVED_DEPS, orphan_names)

        # Debug: copy the installed-through-deps.list for inspection
        _copy_installed_deps_list()

        return 0

    except Exception as e:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_autoremove(args, db: PackageDatabase) -> int:
    """Handle autoremove command - unified cleanup."""
    import platform
    import signal

    from . import colors
    from ..core.resolver import Resolver, format_size
    from ..core.install import check_root
    from ..core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from ..core.transaction_queue import TransactionQueue

    # Check for previous background errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # Determine which selectors are active
    do_orphans = getattr(args, 'orphans', False)
    do_kernels = getattr(args, 'kernels', False)
    do_faildeps = getattr(args, 'faildeps', False)
    do_all = getattr(args, 'all', False)

    # --all enables everything
    if do_all:
        do_orphans = do_kernels = do_faildeps = True

    # Default to --orphans if no selector specified
    if not (do_orphans or do_kernels or do_faildeps):
        do_orphans = True

    arch = platform.machine()
    resolver = Resolver(db, arch=arch)

    # Collect packages to remove from each selector
    packages_to_remove = []  # List of (name, nevra, size, reason)
    faildeps_trans_ids = []  # Transaction IDs to mark as cleaned

    # --orphans: orphaned packages
    if do_orphans:
        print("Searching for orphaned packages...")
        orphans = resolver.find_all_orphans()
        for o in orphans:
            packages_to_remove.append((o.name, o.nevra, o.size, 'orphan'))
        if orphans:
            print(f"  Found {colors.warning(str(len(orphans)))} orphaned package(s)")
        else:
            print(colors.success("  No orphaned packages found"))

    # --kernels: old kernels
    if do_kernels:
        print("Searching for old kernels...")
        old_kernels = _find_old_kernels(keep_count=2)
        for name, nevra, size in old_kernels:
            packages_to_remove.append((name, nevra, size, 'old-kernel'))
        if old_kernels:
            print(f"  Found {colors.warning(str(len(old_kernels)))} old kernel package(s)")
        else:
            print(colors.success("  No old kernels found"))

    # --faildeps: orphan deps from interrupted transactions
    if do_faildeps:
        print("Searching for failed dependencies...")
        faildeps, faildeps_trans_ids = _find_faildeps(db)
        for name, nevra in faildeps:
            packages_to_remove.append((name, nevra, 0, 'faildep'))
        if faildeps:
            print(f"  Found {colors.warning(str(len(faildeps)))} failed dependency package(s)")
        else:
            print(colors.success("  No failed dependencies found"))

    # Remove duplicates (keep first occurrence)
    seen = set()
    unique_packages = []
    for name, nevra, size, reason in packages_to_remove:
        if name not in seen:
            seen.add(name)
            unique_packages.append((name, nevra, size, reason))
    packages_to_remove = unique_packages

    if not packages_to_remove:
        print(colors.success("\nNothing to remove."))
        return 0

    # Apply blacklist and redlist protection
    blacklist = _get_blacklist()
    redlist = _get_redlist()

    blocked = []
    warned = []
    safe = []

    for pkg in packages_to_remove:
        name = pkg[0]
        if name in blacklist:
            blocked.append(pkg)
        elif name in redlist:
            warned.append(pkg)
        else:
            safe.append(pkg)

    # Report blocked packages
    if blocked:
        print(f"\n  {colors.error(f'BLOCKED ({len(blocked)})')} - critical system packages:")
        for name, nevra, _, _ in blocked:
            print(f"    {colors.error(nevra)}")
        print(colors.error("  These packages cannot be removed (system would be unusable)"))

    # Handle warned packages
    if warned and not getattr(args, 'auto', False):
        print(f"\n  {colors.warning(f'WARNING ({len(warned)})')} - generally useful packages:")
        for name, nevra, _, _ in warned:
            print(f"    {colors.warning(nevra)}")
        try:
            response = input("\n  Remove these warned packages anyway? [y/N] ")
            if response.lower() in ('y', 'yes'):
                safe.extend(warned)
            else:
                print("  Warned packages will be kept")
        except (KeyboardInterrupt, EOFError):
            print("\n  Warned packages will be kept")

    packages_to_remove = safe

    if not packages_to_remove:
        print(colors.success("\nNothing safe to remove."))
        return 0

    # Display summary
    total_size = sum(size for _, _, size, _ in packages_to_remove)
    print(f"\n{colors.bold(f'The following {len(packages_to_remove)} package(s) will be removed:')}")
    from . import display

    # Group by reason for display
    by_reason = {}
    for name, nevra, size, reason in packages_to_remove:
        if reason not in by_reason:
            by_reason[reason] = []
        by_reason[reason].append(nevra)

    reason_labels = {
        'orphan': 'Orphaned packages',
        'old-kernel': 'Old kernels',
        'faildep': 'Failed dependencies',
    }

    for reason, nevras in by_reason.items():
        label = reason_labels.get(reason, reason)
        print(f"\n  {colors.error(f'{label} ({len(nevras)}):')}")
        display.print_package_list(sorted(nevras), indent=4, color_func=colors.error)

    print(f"\nDisk space to free: {format_size(total_size)}")

    # Confirmation
    if not getattr(args, 'auto', False):
        try:
            response = input("\nRemove these packages? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    # Check root
    if not check_root():
        print(colors.error("Error: autoremove requires root privileges"))
        return 1

    # Build command line for history
    cmd_parts = ["urpm", "autoremove"]
    if do_orphans and not do_all:
        cmd_parts.append("--orphans")
    if do_kernels and not do_all:
        cmd_parts.append("--kernels")
    if do_faildeps and not do_all:
        cmd_parts.append("--faildeps")
    if do_all:
        cmd_parts.append("--all")
    cmd_line = " ".join(cmd_parts)

    transaction_id = db.begin_transaction('autoremove', cmd_line)

    # Record packages
    for name, nevra, size, reason in packages_to_remove:
        db.record_package(transaction_id, nevra, name, 'remove', reason)

    # Setup interrupt handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            print("\n\nForce abort!")
            db.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        print(f"\nRemoving {len(packages_to_remove)} packages...")
        package_names = [name for name, _, _, _ in packages_to_remove]

        # Check if another operation is in progress
        lock = InstallLock()
        if not lock.acquire(blocking=False):
            print(colors.warning("  RPM database is locked by another process."))
            print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
            lock.acquire(blocking=True)
        lock.release()  # Release - child will acquire its own lock

        last_erase_shown = [None]

        # Build transaction queue
        from ..core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        queue = TransactionQueue(root=rpm_root or "/")
        queue.add_erase(package_names, operation_id="autoremove")

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_erase_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_erase_shown[0] = name

        # Execute the queue
        sync_mode = getattr(args, 'sync', False)
        queue_result = queue.execute(progress_callback=queue_progress, sync=sync_mode)

        # Print done
        print(f"\r\033[K  [{len(package_names)}/{len(package_names)}] done")

        if not queue_result.success:
            print(colors.error(f"\nRemoval failed:"))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Autoremove interrupted"))
            db.abort_transaction(transaction_id)
            return 130

        removed_count = queue_result.operations[0].count if queue_result.operations else len(package_names)
        print(colors.success(f"  {removed_count} packages removed"))

        # Mark faildeps transactions as cleaned
        if faildeps_trans_ids:
            for tid in faildeps_trans_ids:
                db.conn.execute(
                    "UPDATE history SET status = 'cleaned' WHERE id = ?",
                    (tid,)
                )
            db.conn.commit()

        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        resolver.unmark_packages(package_names)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_mark(args, db: PackageDatabase) -> int:
    """Handle mark command - mark packages as manual or auto-installed."""
    from . import colors
    from ..core.resolver import Resolver

    resolver = Resolver(db)

    if args.mark_command in ('manual', 'm', 'explicit'):
        # Mark packages as explicitly installed (remove from unrequested)
        packages = args.packages
        unrequested = resolver._get_unrequested_packages()

        marked = []
        already_manual = []
        not_installed = []

        # Check which packages are installed
        try:
            import rpm
            ts = rpm.TransactionSet()
            installed = set()
            for hdr in ts.dbMatch():
                installed.add(hdr[rpm.RPMTAG_NAME].lower())
        except ImportError:
            print(colors.error("Error: rpm module not available"))
            return 1

        for pkg in packages:
            pkg_lower = pkg.lower()
            if pkg_lower not in installed:
                not_installed.append(pkg)
            elif pkg_lower not in unrequested:
                already_manual.append(pkg)
            else:
                marked.append(pkg)

        if not_installed:
            print(colors.warning(f"Not installed: {', '.join(not_installed)}"))

        if already_manual:
            print(f"Already manual: {', '.join(already_manual)}")

        if marked:
            resolver.mark_as_explicit(marked)
            print(colors.success(f"Marked as manual: {', '.join(marked)}"))
            print("These packages are now protected from autoremove.")

        return 0 if marked or already_manual else 1

    elif args.mark_command in ('auto', 'a', 'dep'):
        # Mark packages as auto-installed (add to unrequested)
        packages = args.packages
        unrequested = resolver._get_unrequested_packages()

        marked = []
        already_auto = []
        not_installed = []

        # Check which packages are installed
        try:
            import rpm
            ts = rpm.TransactionSet()
            installed = set()
            for hdr in ts.dbMatch():
                installed.add(hdr[rpm.RPMTAG_NAME].lower())
        except ImportError:
            print(colors.error("Error: rpm module not available"))
            return 1

        for pkg in packages:
            pkg_lower = pkg.lower()
            if pkg_lower not in installed:
                not_installed.append(pkg)
            elif pkg_lower in unrequested:
                already_auto.append(pkg)
            else:
                marked.append(pkg)

        if not_installed:
            print(colors.warning(f"Not installed: {', '.join(not_installed)}"))

        if already_auto:
            print(f"Already auto: {', '.join(already_auto)}")

        if marked:
            resolver.mark_as_dependency(marked)
            print(colors.success(f"Marked as auto: {', '.join(marked)}"))
            print("These packages can now be autoremoved if no longer needed.")

        return 0 if marked or already_auto else 1

    elif args.mark_command in ('show', 's', 'list'):
        # Show install reason for packages
        unrequested = resolver._get_unrequested_packages()

        try:
            import rpm
            ts = rpm.TransactionSet()
            installed = {}
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                installed[name.lower()] = name
        except ImportError:
            print(colors.error("Error: rpm module not available"))
            return 1

        packages = args.packages if args.packages else sorted(installed.keys())

        manual_count = 0
        auto_count = 0

        for pkg in packages:
            pkg_lower = pkg.lower()
            if pkg_lower not in installed:
                print(f"{pkg}: {colors.warning('not installed')}")
            elif pkg_lower in unrequested:
                print(f"{installed[pkg_lower]}: {colors.info('auto')}")
                auto_count += 1
            else:
                print(f"{installed[pkg_lower]}: {colors.success('manual')}")
                manual_count += 1

        if not args.packages:
            print(f"\nTotal: {manual_count} manual, {auto_count} auto")

        return 0

    else:
        print("Usage: urpm mark <manual|auto|show> [packages...]")
        return 1


def cmd_hold(args, db: PackageDatabase) -> int:
    """Handle hold command - hold packages to prevent upgrades and obsoletes."""
    from . import colors
    from datetime import datetime

    # List holds if no packages or --list
    if args.list_holds or not args.packages:
        holds = db.list_holds()
        if not holds:
            print("No packages are held.")
            return 0

        print(f"Held packages ({len(holds)}):\n")
        for hold in holds:
            ts = datetime.fromtimestamp(hold['added_timestamp'])
            reason = f" - {hold['reason']}" if hold['reason'] else ""
            print(f"  {colors.warning(hold['package_name'])}{reason}")
            print(f"    (held since {ts.strftime('%Y-%m-%d %H:%M')})")
        return 0

    # Hold packages
    added = []
    already_held = []

    for pkg in args.packages:
        if db.add_hold(pkg, args.reason):
            added.append(pkg)
        else:
            already_held.append(pkg)

    if already_held:
        print(f"Already held: {', '.join(already_held)}")

    if added:
        print(colors.success(f"Held: {', '.join(added)}"))
        print("These packages will be protected from upgrades and obsoletes replacement.")

    return 0 if added or already_held else 1


def cmd_unhold(args, db: PackageDatabase) -> int:
    """Handle unhold command - remove hold from packages."""
    from . import colors

    removed = []
    not_held = []

    for pkg in args.packages:
        if db.remove_hold(pkg):
            removed.append(pkg)
        else:
            not_held.append(pkg)

    if not_held:
        print(f"Not held: {', '.join(not_held)}")

    if removed:
        print(colors.success(f"Unheld: {', '.join(removed)}"))
        print("These packages can now be upgraded and replaced by obsoletes.")

    return 0 if removed else 1


# History commands moved to urpm/cli/commands/history.py

# Config and key commands moved to urpm/cli/commands/config.py

# Peer commands moved to urpm/cli/commands/peer.py


def cmd_appstream(args, db: PackageDatabase) -> int:
    """Handle appstream command - manage AppStream metadata."""
    import gzip
    from datetime import datetime
    from pathlib import Path
    from ..core.config import get_system_version, get_base_dir
    from ..core.appstream import AppStreamManager
    from . import colors

    appstream_mgr = AppStreamManager(db, get_base_dir())

    if args.appstream_command in ('generate', 'gen', None):
        media_name = getattr(args, 'media', None)

        if media_name:
            # Generate for specific media
            media = db.get_media(media_name)
            if not media:
                print(colors.error(f"Media '{media_name}' not found"))
                return 1

            print(f"Generating AppStream for {media_name}...")
            xml_str, count = appstream_mgr.generate_for_media(
                media['id'], media_name
            )

            output_path = appstream_mgr.get_media_appstream_path(media_name)
            appstream_mgr._ensure_dirs()
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(xml_str)

            print(colors.ok(f"Generated {count} components -> {output_path}"))
            return 0

        else:
            # Generate for all enabled media and merge
            print("Generating AppStream for all enabled media...")

            media_list = db.list_media()
            enabled_media = [m for m in media_list if m['enabled']]

            total = 0
            for media in enabled_media:
                xml_str, count = appstream_mgr.generate_for_media(
                    media['id'], media['name']
                )

                output_path = appstream_mgr.get_media_appstream_path(media['name'])
                appstream_mgr._ensure_dirs()
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(xml_str)

                print(f"  {media['name']}: {count} components")
                total += count

            # Merge all catalogs
            print("\nMerging catalogs...")
            total_merged, media_count = appstream_mgr.merge_all_catalogs()
            print(colors.ok(f"Merged {total_merged} components from {media_count} media"))
            print(f"Output: {appstream_mgr.catalog_path}")

            print("\nTo refresh the AppStream cache, run:")
            print("  sudo appstreamcli refresh-cache --force")
            return 0

    elif args.appstream_command == 'status':
        # Show AppStream status for all media
        status_list = appstream_mgr.get_status()

        if not status_list:
            print("No media configured")
            return 0

        # Header
        print(f"{'Media':<30} {'Source':<12} {'Components':>10} {'Last Updated':<20}")
        print("-" * 75)

        for item in status_list:
            name = item['media_name'][:29]
            source = item['source']
            count = item['component_count']
            mtime = item['last_updated']

            if mtime > 0:
                updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            else:
                updated = '-'

            # Color source
            if source == 'upstream':
                source_str = colors.ok(source)
            elif source == 'generated':
                source_str = colors.warning(source)
            elif source == 'missing':
                source_str = colors.error(source)
            else:
                source_str = source

            print(f"{name:<30} {source_str:<21} {count:>10} {updated:<20}")

        # Summary
        print("-" * 75)
        total = sum(s['component_count'] for s in status_list)
        upstream = sum(1 for s in status_list if s['source'] == 'upstream')
        generated = sum(1 for s in status_list if s['source'] == 'generated')
        missing = sum(1 for s in status_list if s['source'] == 'missing')

        print(f"Total: {total} components | upstream: {upstream}, generated: {generated}, missing: {missing}")

        # Check merged catalog
        if appstream_mgr.catalog_path.exists():
            mtime = appstream_mgr.catalog_path.stat().st_mtime
            updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            print(f"\nMerged catalog: {appstream_mgr.catalog_path} (updated: {updated})")
        else:
            print(f"\nMerged catalog: {colors.warning('not found')} (run 'urpm appstream merge')")

        return 0

    elif args.appstream_command == 'merge':
        # Merge per-media files into unified catalog
        print("Merging AppStream catalogs...")

        total, media_count = appstream_mgr.merge_all_catalogs(
            progress_callback=lambda msg: print(f"  {msg}")
        )

        if total == 0:
            print(colors.warning("No components found. Run 'urpm media update' first."))
            return 1

        print(colors.ok(f"Merged {total} components from {media_count} media"))
        print(f"Output: {appstream_mgr.catalog_path}")

        # Refresh system cache if requested
        if getattr(args, 'refresh', False):
            print("\nRefreshing system AppStream cache...")
            if appstream_mgr.refresh_system_cache():
                print(colors.ok("Cache refreshed"))
            else:
                print(colors.warning("Cache refresh failed (appstreamcli may not be installed)"))

        return 0

    elif args.appstream_command == 'init-distro':
        # Create OS metainfo file for AppStream
        metainfo_dir = Path('/usr/share/metainfo')
        metainfo_file = metainfo_dir / 'org.mageia.mageia.metainfo.xml'

        if metainfo_file.exists() and not getattr(args, 'force', False):
            print(f"OS metainfo file already exists: {metainfo_file}")
            print("Use --force to overwrite")
            return 1

        # Get system version
        version = get_system_version() or 'unknown'

        metainfo_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<component type="operating-system">
  <id>org.mageia.mageia</id>
  <name>Mageia</name>
  <summary>Mageia Linux Distribution</summary>
  <description>
    <p>Mageia is a GNU/Linux-based, Free Software operating system.
    It is a community project, supported by a nonprofit organization
    of elected contributors.</p>
  </description>
  <url type="homepage">https://www.mageia.org</url>
  <metadata_license>CC0-1.0</metadata_license>
  <releases>
    <release version="{version}" />
  </releases>
</component>
'''
        try:
            metainfo_dir.mkdir(parents=True, exist_ok=True)
            with open(metainfo_file, 'w', encoding='utf-8') as f:
                f.write(metainfo_content)
            print(colors.ok(f"OS metainfo file created: {metainfo_file}"))
            return 0
        except PermissionError:
            print(colors.error(f"Permission denied. Run with sudo."))
            return 1
        except Exception as e:
            print(colors.error(f"Failed to create metainfo: {e}"))
            return 1

    else:
        print(f"Unknown appstream command: {args.appstream_command}")
        return 1


# undo command moved to urpm/cli/commands/history.py


def cmd_cleandeps(args, db: PackageDatabase) -> int:
    """Handle cleandeps command - remove orphan deps from interrupted transactions."""
    import signal
    import platform
    from ..core.install import check_root
    from ..core.resolver import Resolver
    from ..core.transaction_queue import TransactionQueue
    from ..core.background_install import InstallLock
    from . import colors

    interrupted = db.get_interrupted_transactions()

    if not interrupted:
        print("No interrupted transactions found")
        return 0

    # Collect all orphan deps
    all_orphans = []
    interrupted_ids = []
    for trans in interrupted:
        orphans = db.get_orphan_deps(trans['id'])
        if orphans:
            all_orphans.extend(orphans)
            interrupted_ids.append(trans['id'])

    if not all_orphans:
        print("No orphan dependencies to clean")
        return 0

    # Remove duplicates while preserving order
    seen = set()
    unique_orphans = []
    for nevra in all_orphans:
        if nevra not in seen:
            seen.add(nevra)
            unique_orphans.append(nevra)
    all_orphans = unique_orphans

    print(f"\nFound {len(all_orphans)} orphan dependencies from {len(interrupted)} interrupted transaction(s):")
    from . import display
    display.print_package_list(all_orphans, max_lines=10)

    if not args.auto:
        try:
            answer = input("\nRemove these packages? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Check root
    if not check_root():
        print("Error: cleandeps requires root privileges")
        return 1

    # Record transaction
    cmd_line = 'urpm cleandeps'
    transaction_id = db.begin_transaction('cleandeps', cmd_line)

    # Setup Ctrl+C handler
    interrupted_flag = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted_flag[0]:
            print("\n\nForce abort!")
            db.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted_flag[0] = True
            print("\n\nInterrupt requested - finishing current package...")

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # Extract package names from NEVRAs for removal
        packages_to_erase = []
        for nevra in all_orphans:
            # Extract name from nevra (e.g., "foo-1.0-1.mga9.x86_64" -> "foo")
            name = _extract_pkg_name(nevra)
            packages_to_erase.append(name)
            # Record in transaction
            db.record_package(transaction_id, nevra, name, 'remove', 'cleandeps')

        # Check if another operation is in progress
        lock = InstallLock()
        if not lock.acquire(blocking=False):
            print(colors.warning("  RPM database is locked by another process."))
            print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
            lock.acquire(blocking=True)
        lock.release()  # Release - child will acquire its own lock

        # Erase packages
        print(f"\nErasing {len(packages_to_erase)} orphan dependencies...")

        last_erase_shown = [None]

        # Build transaction queue
        from ..core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        queue = TransactionQueue(root=rpm_root or "/")
        queue.add_erase(packages_to_erase, operation_id="cleandeps")

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_erase_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_erase_shown[0] = name

        # Execute the queue
        sync_mode = getattr(args, 'sync', False)
        queue_result = queue.execute(progress_callback=queue_progress, sync=sync_mode)

        # Print done
        print(f"\r\033[K  [{len(packages_to_erase)}/{len(packages_to_erase)}] done")

        if not queue_result.success:
            print(colors.error(f"\nErase failed:"))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:5]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted_flag[0]:
            erased_count = queue_result.operations[0].count if queue_result.operations else 0
            print(colors.warning(f"\n  Erase interrupted after {erased_count} packages"))
            db.abort_transaction(transaction_id)
            return 130

        erased_count = queue_result.operations[0].count if queue_result.operations else len(packages_to_erase)
        print(colors.success(f"  {erased_count} packages erased"))

        # Mark interrupted transactions as cleaned
        for tid in interrupted_ids:
            db.conn.execute(
                "UPDATE history SET status = 'cleaned' WHERE id = ?",
                (tid,)
            )
        db.conn.commit()

        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        if packages_to_erase:
            arch = platform.machine()
            resolver = Resolver(db, arch=arch)
            resolver.unmark_packages(packages_to_erase)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_depends(args, db: PackageDatabase) -> int:
    """Handle depends command - show package dependencies."""
    from ..core.resolver import Resolver
    from . import colors

    package = args.package
    pkg_name = _extract_pkg_name(package)
    show_tree = getattr(args, 'tree', False)
    legacy = getattr(args, 'legacy', False)
    show_all = getattr(args, 'all', False)
    prefer_str = getattr(args, 'prefer', None)

    # Parse --prefer using PreferencesMatcher
    preferences = PreferencesMatcher(prefer_str)

    # Create resolver for provider lookups
    resolver = Resolver(db)

    # Get package requires using resolver
    requires = resolver.get_package_requires(pkg_name)

    if not requires:
        # Fallback: try installed package via rpm
        try:
            import rpm
            ts = rpm.TransactionSet()
            mi = ts.dbMatch('name', pkg_name)
            for hdr in mi:
                req_names = hdr[rpm.RPMTAG_REQUIRENAME] or []
                requires = [r for r in req_names
                           if not r.startswith('/') and not r.startswith('rpmlib(')]
                break
        except:
            pass

    if not requires:
        pkg = db.get_package_smart(package)
        if not pkg:
            print(f"Package '{package}' not found")
            return 1
        print(f"{package}: no dependencies")
        return 0

    # Build dependency info with providers
    # dep_info: { capability: { 'providers': [...], 'chosen': str|None, 'is_alternative': bool } }
    dep_info = {}
    choices_made = {}  # Track choices for tree display

    def match_preference(provider_name: str) -> bool:
        """Check if a provider matches any preference."""
        return preferences.match_provider_name(provider_name)

    # First pass: identify capabilities that have multiple providers (alternatives)
    alternative_caps = []

    for cap in requires:
        # Extract base capability (remove version constraints like [>= 1.0])
        cap_base = cap.split('[')[0].split()[0] if '[' in cap else cap.split()[0]
        # Don't strip () for library capabilities (.so files) - they need the full name
        # e.g., libncursesw.so.6()(64bit) must stay as-is
        if '(' in cap_base and '.so' not in cap_base:
            cap_base = cap_base.split('(')[0]

        providers = resolver.get_providers(cap_base, include_installed=True)
        providers = [p for p in providers if p != pkg_name]

        if not providers:
            dep_info[cap_base] = {'providers': [], 'chosen': None, 'is_alternative': False}
        elif len(providers) == 1:
            dep_info[cap_base] = {'providers': providers, 'chosen': providers[0], 'is_alternative': False}
        else:
            dep_info[cap_base] = {'providers': providers, 'chosen': None, 'is_alternative': True}
            alternative_caps.append(cap_base)

    # For non-tree modes, apply preference matching to direct requires
    # (Tree mode handles everything via _resolve_with_alternatives)
    if not show_tree:
        for cap_base, info in dep_info.items():
            if info['is_alternative'] and not info['chosen']:
                for prov in info['providers']:
                    if match_preference(prov):
                        info['chosen'] = prov
                        choices_made[cap_base] = prov
                        break

    # Display based on mode
    use_pager = getattr(args, 'pager', False)

    if legacy:
        # --legacy: raw capabilities
        print(f"Dependencies of {package} ({len(requires)}):")
        for cap in sorted(requires):
            print(f"  {cap}")
    elif show_tree:
        # --tree: show actual dependency tree (what the package requires)
        no_libs = getattr(args, 'no_libs', False)
        max_depth = getattr(args, 'depth', 5)

        # Build set of installed packages for coloring
        installed_pkgs = set()
        try:
            import rpm
            ts = rpm.TransactionSet()
            for hdr in ts.dbMatch():
                installed_pkgs.add(hdr[rpm.RPMTAG_NAME])
        except ImportError:
            pass

        def is_lib_package(name: str) -> bool:
            """Check if package is a library package."""
            return (name.startswith('lib') or
                    name in ('glibc', 'glibc-devel', 'filesystem', 'setup', 'basesystem'))

        def print_requires_tree(pkg: str, visited: set, prefix: str, depth: int):
            """Recursively print package requirements as a tree."""
            if depth > max_depth:
                print(f"{prefix}└── {colors.dim('... (max depth)')}")
                return

            pkg_requires = resolver.get_package_requires(pkg)
            if not pkg_requires:
                return

            # Resolve capabilities to package names
            deps = []
            for cap in pkg_requires:
                cap_base = cap.split('[')[0].split()[0] if '[' in cap else cap.split()[0]
                if '(' in cap_base and not cap_base.startswith('lib'):
                    cap_base = cap_base.split('(')[0]
                providers = resolver.get_providers(cap_base, include_installed=True)
                providers = [p for p in providers if p != pkg]
                if providers:
                    # Choose provider based on preference or first
                    chosen = None
                    for p in providers:
                        if match_preference(p):
                            chosen = p
                            break
                    if not chosen:
                        chosen = providers[0]
                    if chosen not in deps:
                        # Filter libs if --no-libs
                        if no_libs and is_lib_package(chosen):
                            continue
                        deps.append(chosen)

            for i, dep in enumerate(sorted(deps)):
                is_last = (i == len(deps) - 1)
                connector = "└── " if is_last else "├── "
                child_prefix = prefix + ("    " if is_last else "│   ")

                # Color: green if installed, normal if not
                if dep in installed_pkgs:
                    dep_display = colors.success(dep)
                else:
                    dep_display = dep

                if dep in visited:
                    print(f"{prefix}{connector}{colors.dim(dep)} ⨂")
                else:
                    print(f"{prefix}{connector}{dep_display}")
                    visited.add(dep)
                    print_requires_tree(dep, visited, child_prefix, depth + 1)

        def do_print_tree():
            print(f"\n{pkg_name}")
            print_requires_tree(pkg_name, {pkg_name}, "", 0)

        if use_pager:
            import io
            import subprocess
            old_stdout = sys.stdout
            sys.stdout = buffer = io.StringIO()
            try:
                do_print_tree()
            finally:
                sys.stdout = old_stdout
            output = buffer.getvalue()
            try:
                proc = subprocess.Popen(['less', '-R'], stdin=subprocess.PIPE)
                proc.communicate(input=output.encode())
            except (FileNotFoundError, BrokenPipeError):
                print(output, end='')
        else:
            do_print_tree()
    elif show_all:
        # --all: flat list of all recursive dependencies
        all_deps = set()
        for cap, info in dep_info.items():
            if info['chosen']:
                all_deps.add(info['chosen'])

        visited = {pkg_name}
        to_process = list(all_deps)

        while to_process:
            prov = to_process.pop(0)
            if prov in visited:
                continue
            visited.add(prov)

            sub_requires = resolver.get_package_requires(prov)
            for cap in sub_requires:
                cap_base = cap.split('[')[0].split()[0] if '[' in cap else cap.split()[0]
                if '(' in cap_base:
                    cap_base = cap_base.split('(')[0]

                providers = resolver.get_providers(cap_base, include_installed=True)
                providers = [p for p in providers if p not in visited]
                if providers:
                    # Use first provider or preference
                    chosen = None
                    for p in providers:
                        if match_preference(p):
                            chosen = p
                            break
                    if not chosen:
                        chosen = providers[0]
                    all_deps.add(chosen)
                    to_process.append(chosen)

        print(f"All dependencies of {package}: {len(all_deps)} packages\n")
        for prov in sorted(all_deps):
            print(f"  {prov}")
    else:
        # Default: flat list with alternatives shown
        single_providers = []
        alternatives = []

        for cap, info in sorted(dep_info.items()):
            if not info['providers']:
                continue
            if info['is_alternative']:
                alternatives.append((cap, info))
            else:
                single_providers.append(info['chosen'])

        # Print single-provider deps
        if single_providers:
            unique_deps = sorted(set(single_providers))
            print(f"Dependencies of {package}: {len(unique_deps)} packages\n")
            for prov in unique_deps:
                print(f"  {prov}")

        # Print alternatives
        if alternatives:
            print(f"\nAlternatives ({len(alternatives)} capabilities with choices):\n")
            for cap, info in alternatives:
                providers_str = ' | '.join(info['providers'][:5])
                if len(info['providers']) > 5:
                    providers_str += f" (+{len(info['providers']) - 5})"
                print(f"  {colors.warning(cap)}")
                print(f"    → {colors.dim(providers_str)}")

    return 0


class PreferencesMatcher:
    """Parse and match --prefer preferences.

    Format: --prefer=capability:version,pattern,...
    Examples:
        --prefer=php:8.4,nginx      -> PHP 8.4, nginx-based
        --prefer=python:3.11,gtk    -> Python 3.11, GTK-based
        --prefer=php-fpm            -> packages that provide php-fpm
    """

    def __init__(self, prefer_str: str = None):
        self.version_constraints = {}  # {capability: version}
        self.name_patterns = []  # [pattern, ...]
        self.negative_patterns = []  # [pattern, ...] - patterns to DISFAVOR
        self.resolved_packages = set()  # Packages resolved from patterns via whatprovides
        self.disfavored_packages = set()  # Packages to explicitly disfavor
        self._compatible_providers = set()  # Packages that require something resolved_packages provide
        if prefer_str:
            for part in prefer_str.split(','):
                part = part.strip()
                if not part:
                    continue
                # Negative preference: -pattern means DISFAVOR
                if part.startswith('-'):
                    self.negative_patterns.append(part[1:].lower())
                elif ':' in part:
                    # capability:version format
                    cap, ver = part.split(':', 1)
                    self.version_constraints[cap.lower()] = ver.lower()
                else:
                    # Simple pattern
                    self.name_patterns.append(part.lower())

    def resolve_patterns(self, pool) -> None:
        """Resolve name patterns to actual package names using libsolv.

        Uses whatprovides() to find packages that provide each capability.
        When multiple patterns have overlapping candidates, computes their
        intersection (e.g., php:8.4 + php-fpm → php8.4-fpm only).

        Args:
            pool: libsolv Pool instance
        """
        import re

        def get_candidates(cap: str, version: str = None) -> set:
            """Get candidate packages for a capability via whatprovides."""
            candidates = set()
            dep = pool.Dep(cap)
            for p in pool.whatprovides(dep):
                if p.repo and p.repo.name != '@System':
                    name_lower = p.name.lower()
                    if version is None:
                        candidates.add(name_lower)
                    else:
                        # Filter by version in package name
                        match = re.search(r'(\d+\.\d+)', name_lower)
                        if match and match.group(1) == version:
                            candidates.add(name_lower)
            return candidates

        # Collect candidates for each pattern
        all_candidate_sets = []

        for pattern in self.name_patterns:
            candidates = get_candidates(pattern)
            if candidates:
                all_candidate_sets.append(candidates)

        for cap, version in self.version_constraints.items():
            candidates = get_candidates(cap, version)
            if candidates:
                all_candidate_sets.append(candidates)

        if not all_candidate_sets:
            return

        # Group sets that overlap (share candidates) and intersect them
        # Sets that don't overlap are kept separate
        result = set()
        processed = [False] * len(all_candidate_sets)

        for i, set_i in enumerate(all_candidate_sets):
            if processed[i]:
                continue

            # Find all sets that overlap with this one
            group = set_i.copy()
            processed[i] = True

            for j, set_j in enumerate(all_candidate_sets):
                if i != j and not processed[j]:
                    if group & set_j:  # If they overlap
                        group = group & set_j  # Intersect
                        processed[j] = True

            result.update(group)

        self.resolved_packages = result

        # Resolve negative patterns to disfavored_packages
        for neg_pattern in self.negative_patterns:
            # Try as capability first
            candidates = get_candidates(neg_pattern)
            if candidates:
                self.disfavored_packages.update(candidates)
            else:
                # Try as glob pattern on package names
                import fnmatch
                for s in pool.solvables_iter():
                    if s.repo and s.repo.name != '@System':
                        name_lower = s.name.lower()
                        # Match if pattern is substring or glob
                        if neg_pattern in name_lower or fnmatch.fnmatch(name_lower, f'*{neg_pattern}*'):
                            self.disfavored_packages.add(name_lower)

        # Now find packages that are compatible with resolved_packages
        # A package is compatible if it requires something that a resolved package provides
        self._find_compatible_providers(pool)

    def _find_compatible_providers(self, pool) -> None:
        """Find packages that require capabilities provided by resolved_packages.

        Excludes packages that are alternatives to resolved_packages (provide
        the same capabilities without requiring them).
        Also filters by version to only include packages matching the preferred versions.
        """
        import solv
        import re

        if not self.resolved_packages:
            return

        # Extract versions from resolved packages (e.g., php8.4-fpm -> 8.4)
        preferred_versions = set()
        for pkg_name in self.resolved_packages:
            match = re.search(r'(\d+\.\d+)', pkg_name)
            if match:
                preferred_versions.add(match.group(1))

        # Collect capabilities provided by resolved packages
        provided_caps = set()
        for pkg_name in self.resolved_packages:
            sel = pool.select(pkg_name, solv.Selection.SELECTION_NAME)
            for s in sel.solvables():
                if s.repo and s.repo.name != '@System':
                    for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                        cap = str(dep).split()[0]
                        if not cap.startswith(('rpmlib(', '/', 'lib')):
                            provided_caps.add(cap)

        # Find packages that require capabilities from resolved_packages
        # but exclude alternatives (packages that provide same caps without requiring them)
        for s in pool.solvables_iter():
            if not s.repo or s.repo.name == '@System':
                continue
            name_lower = s.name.lower()
            if name_lower in self.resolved_packages:
                continue

            # Filter by version: if resolved packages have versions, only accept
            # compatible providers with matching versions
            if preferred_versions:
                pkg_version_match = re.search(r'(\d+\.\d+)', name_lower)
                if pkg_version_match:
                    pkg_version = pkg_version_match.group(1)
                    if pkg_version not in preferred_versions:
                        continue  # Skip packages with wrong version

            # Get this package's requires and provides
            pkg_requires = set()
            pkg_provides = set()
            for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                pkg_requires.add(str(dep).split()[0])
            for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                cap = str(dep).split()[0]
                if not cap.startswith(('rpmlib(', '/', 'lib')):
                    pkg_provides.add(cap)

            # Check if it requires something from resolved_packages
            requires_preferred = bool(pkg_requires & provided_caps)

            # Check if it's an alternative (provides same caps but doesn't require them)
            is_alternative = bool(pkg_provides & provided_caps) and not requires_preferred

            if requires_preferred and not is_alternative:
                self._compatible_providers.add(name_lower)

        if DEBUG_PREFERENCES:
            if 'php8.4-fpm-apache' in self._compatible_providers:
                print(f"DEBUG: php8.4-fpm-apache IS in _compatible_providers")
            else:
                print(f"DEBUG: php8.4-fpm-apache NOT in _compatible_providers")
                print(f"DEBUG: provided_caps sample: {list(provided_caps)[:10]}")

    def match_bloc_version(self, bloc_defining_caps: dict, bloc_key: str) -> bool:
        """Check if a bloc matches version constraints or resolved packages.

        Args:
            bloc_defining_caps: Dict of {capability: [versions]}
            bloc_key: The bloc version key (e.g., "3:8.4")

        Returns:
            True if this bloc matches a version constraint
        """
        import re

        # Extract version from bloc_key (e.g., "3:8.4" -> "8.4")
        bloc_version = bloc_key.split(':')[-1] if ':' in bloc_key else bloc_key

        # Check explicit version constraints (php:8.4)
        for cap, ver in self.version_constraints.items():
            for bloc_cap in bloc_defining_caps.keys():
                if cap in bloc_cap.lower() or bloc_cap.lower() in cap:
                    if ver in bloc_version or bloc_version in ver:
                        return True

        # Check versions extracted from resolved package names (php8.4-fpm)
        for pkg_name in self.resolved_packages:
            # Extract version from package name (e.g., php8.4-fpm -> 8.4)
            match = re.search(r'(\d+\.\d+)', pkg_name)
            if match:
                pkg_version = match.group(1)
                if pkg_version in bloc_version or bloc_version.endswith(pkg_version):
                    return True

        return False

    def match_provider_name(self, provider_name: str) -> bool:
        """Check if a provider name matches preferences.

        Checks in order:
        1. If provider is in resolved_packages (from whatprovides)
        2. Glob patterns (*nginx*, etc.)
        3. Exact or prefix match (nginx matches nginx-common)

        Args:
            provider_name: The provider package name

        Returns:
            True if name matches preferences
        """
        name_lower = provider_name.lower()

        # Check if provider is in resolved packages
        if name_lower in self.resolved_packages:
            return True

        # Check if provider requires something that a resolved package provides
        # This is set by resolve_patterns when it has access to the pool
        if name_lower in self._compatible_providers:
            return True

        return False

    def has_version_constraints(self) -> bool:
        return bool(self.version_constraints)

    def has_name_patterns(self) -> bool:
        return bool(self.name_patterns)

    def filter_providers(self, providers: list) -> list:
        """Filter and sort providers based on preferences.

        Removes providers that are incompatible with stated preferences,
        and puts preferred providers first.
        E.g., if --prefer=qt, puts qt-based providers first and removes gtk conflicts.

        Args:
            providers: List of provider names

        Returns:
            Filtered and sorted list (never empty - returns original if all filtered)
        """
        if not self.name_patterns:
            return providers

        # Known conflicts: if one is preferred, filter the other
        conflicts = {
            'nginx': ['apache', 'lighttpd'],
            'apache': ['nginx', 'lighttpd'],
            'lighttpd': ['apache', 'nginx'],
            'fpm': ['cgi', 'cli'],
            'cgi': ['fpm'],
            'qt': ['gtk'],
            'gtk': ['qt'],
        }

        # Build set of things to exclude based on preferences
        exclude_patterns = set()
        prefer_patterns = []
        for pattern in self.name_patterns:
            pattern_lower = pattern.lower().replace('*', '').replace('?', '')
            prefer_patterns.append(pattern_lower)
            if pattern_lower in conflicts:
                exclude_patterns.update(conflicts[pattern_lower])

        # Filter providers
        filtered = []
        for prov in providers:
            prov_lower = prov.lower()
            excluded = False
            for excl in exclude_patterns:
                if excl in prov_lower:
                    excluded = True
                    break
            if not excluded:
                filtered.append(prov)

        # Never return empty list - fallback to original
        result = filtered if filtered else providers

        # Sort: preferred providers first
        def preference_key(prov):
            prov_lower = prov.lower()
            for i, pref in enumerate(prefer_patterns):
                if pref in prov_lower:
                    return (0, i, prov)  # Preferred: sort by preference order
            return (1, 0, prov)  # Not preferred: keep original order

        return sorted(result, key=preference_key)


def _handle_bloc_choices(bloc_info: dict, preferences: 'PreferencesMatcher',
                         choices_made: dict, interactive: bool) -> dict:
    """Handle bloc-based choices for alternatives.

    Blocs are groups of packages that must be installed together (e.g., all php8.4-*
    or all php8.5-* packages). Instead of asking about each capability separately,
    we ask about the bloc once and apply the choice to all capabilities.

    Args:
        bloc_info: Dict from resolver.detect_blocs()
        preferences: PreferencesMatcher instance
        choices_made: Dict to update with choices (modified in place)
        interactive: If True, prompt user for choices

    Returns:
        Dict of {bloc_key: {capability: chosen_provider}}
    """
    from . import colors

    blocs = bloc_info['blocs']
    bloc_defining = bloc_info['bloc_defining_caps']

    if not blocs:
        return {}

    result = {}  # {bloc_key: {cap: provider}}

    # Determine which bloc to use based on preferences
    bloc_keys = sorted(blocs.keys())
    chosen_bloc = None

    # Try to match preference to a bloc using version constraints
    for bloc_key in bloc_keys:
        if preferences.match_bloc_version(bloc_defining, bloc_key):
            chosen_bloc = bloc_key
            break

    # If no preference matched and we need to ask, present bloc choice
    if not chosen_bloc and interactive and len(bloc_keys) > 1:
        # Determine what the blocs represent
        bloc_label = _get_bloc_label(bloc_defining)

        print(f"\n{colors.warning(bloc_label)} - multiple versions available:")
        for i, bloc_key in enumerate(bloc_keys, 1):
            # Count providers in this bloc
            provider_count = sum(len(providers) for providers in blocs[bloc_key].values())
            print(f"  {i}. {bloc_key} ({provider_count} packages)")

        while True:
            try:
                choice = input(f"\nChoice? [1-{len(bloc_keys)}] ")
                idx = int(choice) - 1
                if 0 <= idx < len(bloc_keys):
                    chosen_bloc = bloc_keys[idx]
                    break
            except ValueError:
                pass
            except (EOFError, KeyboardInterrupt):
                print("\nAborted")
                return None  # Signal abort
        print()

    # If still no choice, default to first (highest version usually)
    if not chosen_bloc:
        chosen_bloc = bloc_keys[-1]  # Last = highest version

    # Now apply the bloc choice to all capabilities in that bloc
    bloc_data = blocs[chosen_bloc]

    # Track providers already chosen - when we choose a provider for one capability,
    # it may also provide other capabilities in the same bloc
    chosen_providers = set()

    for cap, providers in bloc_data.items():
        if providers:
            # First, check if a previously chosen provider can satisfy this capability
            matching_chosen = [p for p in providers if p in chosen_providers]
            if matching_chosen:
                # Reuse the already-chosen provider
                result.setdefault(chosen_bloc, {})[cap] = matching_chosen[0]
                continue

            # Filter providers based on preferences (e.g., remove apache-* if --prefer=nginx)
            filtered = preferences.filter_providers(providers)

            if len(filtered) == 1:
                # Only one provider after filtering - auto-select
                chosen = filtered[0]
            else:
                # Multiple providers - try preference match first
                chosen = None
                for prov in filtered:
                    if preferences.match_provider_name(prov):
                        chosen = prov
                        break

                # If no match and interactive, ask user
                if not chosen and interactive:
                    chosen = _ask_secondary_choice(cap, filtered)
                    if chosen is None:  # Aborted
                        return None

                # Default to first
                if not chosen:
                    chosen = filtered[0]

            result.setdefault(chosen_bloc, {})[cap] = chosen
            chosen_providers.add(chosen)

    return result


def _get_bloc_label(bloc_defining: dict) -> str:
    """Generate a label for bloc choices based on detected capabilities.

    Args:
        bloc_defining: Dict of {capability: [versions]}

    Returns:
        The name of the first bloc-defining capability
    """
    caps = sorted(bloc_defining.keys())
    if caps:
        return caps[0]
    return "version"


def _ask_secondary_choice(capability: str, providers: list) -> str:
    """Ask user to choose between providers within the same bloc.

    This handles cases like php-webinterface where multiple providers
    exist in the same bloc.

    Args:
        capability: The capability name
        providers: List of provider names

    Returns:
        Chosen provider name, or None if aborted
    """
    from . import colors

    print(f"  {colors.info(capability)} provided by:")
    for i, prov in enumerate(providers[:8], 1):
        print(f"    {i}. {prov}")
    if len(providers) > 8:
        print(f"    ... and {len(providers) - 8} more")

    while True:
        try:
            choice = input(f"  Choice? [1-{min(len(providers), 8)}] ")
            idx = int(choice) - 1
            if 0 <= idx < len(providers):
                return providers[idx]
        except ValueError:
            pass
        except (EOFError, KeyboardInterrupt):
            print("\nAborted")
            return None  # Signal abort

    return providers[0]


def _resolve_for_tree(resolver, pkg_name: str, choices: dict,
                      preferences: 'PreferencesMatcher'):
    """Run resolution for tree display.

    Returns:
        Tuple of (result, graph, aborted)
    """
    # Run actual resolution with libsolv
    result, aborted = _resolve_with_alternatives(
        resolver, [pkg_name], choices, auto_mode=False, preferences=preferences
    )

    if aborted:
        return None, None, True

    if not result.success or not result.actions:
        return result, None, False

    # Build dependency graph from resolution
    graph = resolver.build_dependency_graph(result, [pkg_name])

    return result, graph, False


def _print_dep_tree_from_resolution(resolver, pkg_name: str, choices: dict,
                                     preferences):
    """Print dependency tree using real libsolv resolution.

    Args:
        resolver: Resolver instance
        pkg_name: Package name to analyze
        choices: Dict of choices made for alternatives
        preferences: PreferencesMatcher instance
    """
    from . import colors

    result, graph, aborted = _resolve_for_tree(resolver, pkg_name, choices, preferences)

    if aborted:
        print("Aborted")
        return

    if result is None:
        print(f"{colors.error('Error:')} Failed to resolve {pkg_name}")
        return

    if not result.success:
        print(f"{colors.error('Error:')} Resolution failed:")
        for prob in result.problems:
            print(f"  {prob}")
        return

    if not graph:
        print(f"{pkg_name}: no dependencies to install")
        return

    _print_dep_tree_from_graph(pkg_name, graph, choices)


def _print_dep_tree_from_graph(pkg_name: str, graph: dict, choices: dict,
                                max_depth: int = 10):
    """Print dependency tree from a pre-computed graph.

    Args:
        pkg_name: Package name being analyzed
        graph: Dependency graph from build_dependency_graph()
        choices: Dict of choices made for alternatives
        max_depth: Maximum recursion depth
    """
    from . import colors

    if not graph:
        print(f"{pkg_name}: no dependencies to install")
        return

    # Find which packages were alternatives (for coloring)
    alternative_pkgs = set(choices.values()) if choices else set()

    # Print tree starting from root package
    print(f"\n{pkg_name}")

    def print_tree(pkg: str, visited: set, prefix: str, depth: int):
        if depth > max_depth:
            print(f"{prefix}└── {colors.dim('... (max depth)')}")
            return

        deps = graph.get(pkg, [])
        if not deps:
            return

        # Sort deps and filter already visited
        deps_to_show = [(d, d in alternative_pkgs) for d in sorted(deps) if d not in visited]

        for i, (dep, is_alt) in enumerate(deps_to_show):
            is_last = (i == len(deps_to_show) - 1)
            connector = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")

            # Color based on whether it's an alternative
            if is_alt:
                dep_display = colors.info(dep)  # Cyan for alternatives
            else:
                dep_display = dep

            print(f"{prefix}{connector}{dep_display}")

            # Recurse
            new_visited = visited | {dep}
            print_tree(dep, new_visited, child_prefix, depth + 1)

    # Start tree
    visited = {pkg_name}
    print_tree(pkg_name, visited, "", 0)

    # Legend
    print(f"\n{colors.dim('Legend:')} {colors.info('cyan')} = chosen alternative")


def _print_dep_tree_packages(db: PackageDatabase, providers: list, find_provider, visited: set, prefix: str, max_depth: int, depth: int = 0):
    """Recursively print dependency tree (packages only)."""
    if depth > max_depth:
        if providers:
            print(f"{prefix}└── ... ({len(providers)} packages, max depth reached)")
        return

    for i, provider in enumerate(providers):
        is_last = (i == len(providers) - 1)
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

        if provider in visited:
            print(f"{prefix}{connector}{provider} (circular)")
            continue

        visited.add(provider)

        # Get sub-dependencies of this provider
        sub_providers = []
        sub_pkg = db.get_package(provider)
        if sub_pkg and sub_pkg.get('requires'):
            sub_deps = [d for d in sub_pkg['requires']
                       if not d.startswith('/') and not d.startswith('rpmlib(')]

            # Group sub-deps by provider
            seen = set()
            for dep in sub_deps:
                sub_prov = find_provider(dep)
                if sub_prov and sub_prov not in visited and sub_prov not in seen:
                    sub_providers.append(sub_prov)
                    seen.add(sub_prov)
            sub_providers.sort()

        if sub_providers:
            print(f"{prefix}{connector}{provider} ({len(sub_providers)})")
            _print_dep_tree_packages(db, sub_providers, find_provider, visited, child_prefix, max_depth, depth + 1)
        else:
            print(f"{prefix}{connector}{provider}")


def _print_dep_tree_legacy(db: PackageDatabase, by_provider: dict, find_provider, visited: set, prefix: str, max_depth: int, depth: int = 0):
    """Recursively print dependency tree with capabilities detail."""
    if depth > max_depth:
        if by_provider:
            print(f"{prefix}└── ... ({len(by_provider)} packages, max depth reached)")
        return

    providers = sorted(by_provider.keys())
    for i, provider in enumerate(providers):
        is_last = (i == len(providers) - 1)
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")
        caps = by_provider[provider]

        if provider in visited:
            print(f"{prefix}{connector}{provider} (circular)")
            continue

        visited.add(provider)

        # Get sub-dependencies first to know if we have children
        sub_by_provider = {}
        sub_pkg = db.get_package(provider)
        if sub_pkg and sub_pkg.get('requires'):
            sub_deps = [d for d in sub_pkg['requires']
                       if not d.startswith('/') and not d.startswith('rpmlib(')]

            for dep in sub_deps:
                sub_prov = find_provider(dep)
                if sub_prov and sub_prov not in visited:
                    if sub_prov not in sub_by_provider:
                        sub_by_provider[sub_prov] = []
                    sub_by_provider[sub_prov].append(dep)

        has_children = bool(sub_by_provider)

        # Print provider with its capabilities
        if len(caps) == 1:
            print(f"{prefix}{connector}{provider}: {caps[0]}")
        else:
            print(f"{prefix}{connector}{provider}:")
            # Use child_prefix for capabilities to maintain vertical lines
            caps_prefix = child_prefix
            sorted_caps = sorted(caps)[:5]
            for j, cap in enumerate(sorted_caps):
                # Last cap only if no children AND it's the last cap
                cap_last = (j == len(sorted_caps) - 1) and not has_children and len(caps) <= 5
                cap_connector = "└── " if cap_last else "├── "
                print(f"{caps_prefix}{cap_connector}{cap}")
            if len(caps) > 5:
                more_last = not has_children
                more_connector = "└── " if more_last else "├── "
                print(f"{caps_prefix}{more_connector}... (+{len(caps) - 5} more)")

        # Print sub-dependencies
        if sub_by_provider:
            _print_dep_tree_legacy(db, sub_by_provider, find_provider, visited, child_prefix, max_depth, depth + 1)


def _is_virtual_provide(provide: str) -> bool:
    """Check if a provide is a virtual/generic capability that shouldn't be used for rdeps.

    Only filter truly generic provides that many unrelated packages share.
    Be careful NOT to filter specific provides like pkgconfig(xxx), cmake(xxx), etc.
    """
    prov = provide.strip()

    # rpmlib(...) - internal RPM capabilities, always ignore
    if prov.startswith('rpmlib('):
        return True

    # font(:lang=XX) - generic language support, many packages provide same
    # But font(SpecificFontName) is specific, keep it
    if prov.startswith('font(:lang='):
        return True

    # Empty provides like "application()" with no content
    if prov.endswith('()'):
        return True

    # config(pkgname) = version - RPM config file tracking, not a real dep
    if prov.startswith('config('):
        return True

    return False


def _get_rdeps(pkg_name: str, db: PackageDatabase, dep_types: str = 'R',
               installed_only: bool = True, cache: dict = None,
               installed_pkgs: set = None) -> dict:
    """Get packages that depend on pkg_name.

    Args:
        pkg_name: Package name to find reverse deps for
        db: Package database
        dep_types: Which dependency types to check: 'R', 'r', 's' or combination
        installed_only: If True, only return installed packages
        cache: Optional cache dict to store results
        installed_pkgs: Set of installed package names (for filtering)

    Returns:
        dict: {rdep_name: dep_type} where dep_type is 'R', 'r', or 's'
    """
    cache_key = (pkg_name, dep_types, installed_only)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    import rpm

    # Get what this package provides - from both RPM and database
    provides = [pkg_name]

    # From RPM database (installed)
    try:
        ts = rpm.TransactionSet()
        mi = ts.dbMatch('name', pkg_name)
        for hdr in mi:
            rpm_provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
            for prov in rpm_provides:
                if prov not in provides and not _is_virtual_provide(prov):
                    provides.append(prov)
            break
    except:
        pass

    # Also from urpmi database
    pkg = db.get_package(pkg_name)
    if pkg and pkg.get('provides'):
        for prov in pkg['provides']:
            cap = prov.split('[')[0].strip()
            if cap not in provides and not _is_virtual_provide(cap):
                provides.append(cap)

    rdeps = {}  # {name: dep_type}
    priority = {'R': 3, 'r': 2, 's': 1}

    def add_rdep(name: str, dep_type: str):
        if name == pkg_name:
            return
        # Filter by installed if requested
        if installed_only and installed_pkgs is not None:
            if name not in installed_pkgs:
                return
        current = rdeps.get(name)
        if current is None or priority[dep_type] > priority[current]:
            rdeps[name] = dep_type

    def matches_provides(req: str) -> bool:
        """Check if a requirement matches any of our provides."""
        req_base = req.split('(')[0]
        return req_base in provides or req in provides

    # Check installed packages (like cmd_rdepends does)
    try:
        ts = rpm.TransactionSet()
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == pkg_name or name == 'gpg-pubkey':
                continue
            # Filter by installed_pkgs if provided
            if installed_only and installed_pkgs is not None:
                if name not in installed_pkgs:
                    continue

            # Check Requires
            if 'R' in dep_types:
                requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
                for req in requires:
                    if matches_provides(req):
                        add_rdep(name, 'R')
                        break

            # Check Recommends
            if 'r' in dep_types:
                recommends = hdr[rpm.RPMTAG_RECOMMENDNAME] or []
                for rec in recommends:
                    if matches_provides(rec):
                        add_rdep(name, 'r')
                        break

            # Check Suggests
            if 's' in dep_types:
                suggests = hdr[rpm.RPMTAG_SUGGESTNAME] or []
                for sug in suggests:
                    if matches_provides(sug):
                        add_rdep(name, 's')
                        break
    except:
        pass

    # Also query urpmi database for non-installed packages
    if not installed_only:
        for cap in provides:
            if 'R' in dep_types:
                for r in db.whatrequires(cap, limit=500):
                    add_rdep(r['name'], 'R')
            if 'r' in dep_types:
                for r in db.whatrecommends(cap, limit=500):
                    add_rdep(r['name'], 'r')
            if 's' in dep_types:
                for r in db.whatsuggests(cap, limit=500):
                    add_rdep(r['name'], 's')

    if cache is not None:
        cache[cache_key] = rdeps

    return rdeps


def cmd_rdepends(args, db: PackageDatabase) -> int:
    """Handle rdepends command - show reverse dependencies."""
    from . import colors
    from ..core.resolver import Resolver

    package = args.package
    pkg_name = _extract_pkg_name(package)
    show_tree = getattr(args, 'tree', False)

    # Get set of installed packages for coloring
    installed_pkgs = set()
    try:
        import rpm
        ts = rpm.TransactionSet()
        for hdr in ts.dbMatch():
            installed_pkgs.add(hdr[rpm.RPMTAG_NAME])
    except ImportError:
        pass

    # Get unrequested packages (auto-installed as deps)
    resolver = Resolver(db)
    unrequested_pkgs = resolver._get_unrequested_packages()

    # Cache for reverse deps lookup
    rdeps_cache = {}

    # For initial call, try to get specific version if NEVRA provided
    initial_pkg = db.get_package_smart(package)

    def get_rdeps(pkg_name: str, pkg_override: dict = None) -> list:
        """Get packages that depend on pkg_name."""
        if pkg_name in rdeps_cache:
            return rdeps_cache[pkg_name]

        # Get what this package provides
        pkg = pkg_override or db.get_package(pkg_name)
        provides = [pkg_name]

        if pkg and pkg.get('provides'):
            for prov in pkg['provides']:
                cap = prov.split('[')[0].strip()
                # Skip virtual provides that don't represent real deps
                if cap not in provides and not _is_virtual_provide(cap):
                    provides.append(cap)

        rdeps = set()

        # Check installed packages
        try:
            import rpm
            ts = rpm.TransactionSet()
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                if name == pkg_name or name == 'gpg-pubkey':
                    continue
                requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
                for req in requires:
                    req_base = req.split('(')[0]
                    if req_base in provides or req in provides:
                        rdeps.add(name)
                        break
        except ImportError:
            pass

        # Check database
        for cap in provides:
            results = db.whatrequires(cap, limit=200)
            for r in results:
                if r['name'] != pkg_name:
                    rdeps.add(r['name'])

        rdeps_cache[pkg_name] = sorted(rdeps)
        return rdeps_cache[pkg_name]

    # Get first level (use initial_pkg if available for NEVRA support)
    direct_rdeps = get_rdeps(pkg_name, initial_pkg)

    if not direct_rdeps:
        print(f"No package depends on '{package}'")
        return 0

    show_all = getattr(args, 'all', False)

    def format_pkg(name: str) -> str:
        """Format package name: green if explicit, blue if auto-installed, dim if not installed."""
        if name in installed_pkgs:
            if name.lower() in unrequested_pkgs:
                return colors.info(name)  # blue: auto-installed
            return colors.success(name)   # green: explicit
        return colors.dim(name)  # grey: not installed

    if show_tree:
        # Recursive tree with reverse arrows
        max_depth = getattr(args, 'depth', 3)
        hide_uninstalled = getattr(args, 'hide_uninstalled', False)

        # Pre-compute which packages lead to installed packages (for filtering)
        reachable_cache = None
        if hide_uninstalled:
            # Build rdeps graph once (fast single pass over RPM db)
            rdeps_graph = _build_rdeps_graph(db)
            reachable_cache = _build_installed_reachable_set(
                direct_rdeps, rdeps_graph, installed_pkgs, max_depth, db)

        print(f"{format_pkg(package)}")
        _print_rdep_tree(direct_rdeps, get_rdeps, installed_pkgs, unrequested_pkgs,
                         visited={package}, prefix="", max_depth=max_depth,
                         hide_uninstalled=hide_uninstalled, reachable_cache=reachable_cache)
    elif show_all:
        # Flat list of all recursive reverse dependencies
        all_rdeps = set(direct_rdeps)
        visited = {package}
        to_process = list(direct_rdeps)

        while to_process:
            pkg = to_process.pop(0)
            if pkg in visited:
                continue
            visited.add(pkg)

            sub_rdeps = get_rdeps(pkg)
            for rdep in sub_rdeps:
                if rdep not in visited:
                    all_rdeps.add(rdep)
                    to_process.append(rdep)

        print(f"All packages that depend on {package}: {len(all_rdeps)}\n")
        for rdep in sorted(all_rdeps):
            print(f"  {format_pkg(rdep)}")
    else:
        # Flat list of direct reverse dependencies
        print(f"Packages that depend on {package}: {len(direct_rdeps)}\n")
        for rdep in direct_rdeps:
            print(f"  {format_pkg(rdep)}")

    return 0


def _build_rdeps_graph(db: PackageDatabase) -> dict:
    """Build complete reverse dependency graph in one pass.

    Returns:
        dict: {pkg_name: set of packages that depend on it}
    """
    import rpm

    # Build provides map: capability -> package name
    provides_map = {}  # {capability: pkg_name}

    ts = rpm.TransactionSet()
    all_headers = list(ts.dbMatch())  # Cache headers

    for hdr in all_headers:
        name = hdr[rpm.RPMTAG_NAME]
        if name == 'gpg-pubkey':
            continue
        # This package provides itself
        provides_map[name] = name
        # And its explicit provides
        rpm_provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
        for prov in rpm_provides:
            if not _is_virtual_provide(prov):
                provides_map[prov] = name

    # Build reverse deps: who depends on whom
    rdeps_graph = {}  # {pkg_name: set of rdeps}

    for hdr in all_headers:
        name = hdr[rpm.RPMTAG_NAME]
        if name == 'gpg-pubkey':
            continue
        requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
        for req in requires:
            req_base = req.split('(')[0]
            # Check both the full req and base name
            provider = provides_map.get(req) or provides_map.get(req_base)
            if provider and provider != name:
                if provider not in rdeps_graph:
                    rdeps_graph[provider] = set()
                rdeps_graph[provider].add(name)

    return rdeps_graph


def _build_installed_reachable_set(rdeps: list, rdeps_graph: dict, installed_pkgs: set,
                                    max_depth: int, db: PackageDatabase) -> set:
    """Build set of packages that lead to at least one installed package.

    Uses pre-built rdeps_graph for installed packages, extends with urpmi data for others.
    """
    reachable = set()
    visited = set()
    # Extended graph with urpmi data (lazy loaded)
    extended_cache = {}

    def get_rdeps_for_pkg(pkg_name: str) -> set:
        """Get rdeps, using pre-built graph for installed, urpmi for others."""
        # First check pre-built graph (installed packages)
        if pkg_name in rdeps_graph:
            return rdeps_graph[pkg_name]
        # Check extended cache
        if pkg_name in extended_cache:
            return extended_cache[pkg_name]
        # Not in RPM graph - query urpmi database
        result = set()
        pkg = db.get_package(pkg_name)
        provides = [pkg_name]
        if pkg and pkg.get('provides'):
            for prov in pkg['provides']:
                cap = prov.split('[')[0].strip()
                if cap not in provides and not _is_virtual_provide(cap):
                    provides.append(cap)
        for cap in provides:
            for r in db.whatrequires(cap, limit=200):
                if r['name'] != pkg_name:
                    result.add(r['name'])
        extended_cache[pkg_name] = result
        return result

    def dfs(pkg_name: str, depth: int) -> bool:
        """Returns True if pkg is installed or leads to an installed package."""
        if pkg_name in reachable:
            return True
        if pkg_name in visited or depth > max_depth:
            return pkg_name in reachable
        visited.add(pkg_name)

        is_installed = pkg_name in installed_pkgs
        if is_installed:
            reachable.add(pkg_name)

        # Get rdeps (fast for installed, lazy for others)
        has_installed_descendant = False
        for rdep in get_rdeps_for_pkg(pkg_name):
            if dfs(rdep, depth + 1):
                has_installed_descendant = True

        if has_installed_descendant:
            reachable.add(pkg_name)

        return is_installed or has_installed_descendant

    for r in rdeps:
        dfs(r, 0)

    return reachable


def _print_rdep_tree(rdeps: list, get_rdeps, installed_pkgs: set, unrequested_pkgs: set,
                     visited: set, prefix: str, max_depth: int, depth: int = 0,
                     hide_uninstalled: bool = False, reachable_cache: set = None):
    """Print reverse dependency tree with reverse arrows to show direction."""
    from . import colors

    def format_pkg(name: str) -> str:
        """Format package name: green if explicit, blue if auto-installed, dim if not installed."""
        if name in installed_pkgs:
            if name.lower() in unrequested_pkgs:
                return colors.info(name)  # blue: auto-installed
            return colors.success(name)   # green: explicit
        return colors.dim(name)  # grey: not installed

    # Filter out packages that don't lead to any installed package
    if hide_uninstalled and reachable_cache is not None:
        rdeps = [r for r in rdeps if r in reachable_cache]

    if depth > max_depth:
        if rdeps:
            print(f"{prefix}╰◄─ ... ({len(rdeps)} packages, max depth reached)")
        return

    for i, pkg_name in enumerate(rdeps):
        is_last = (i == len(rdeps) - 1)
        # Use reverse arrows: ◄ to show "depends on" direction
        connector = "╰◄─ " if is_last else "├◄─ "
        child_prefix = prefix + ("    " if is_last else "│   ")

        if pkg_name in visited:
            print(f"{prefix}{connector}{format_pkg(pkg_name)} (circular)")
            continue

        sub_rdeps = get_rdeps(pkg_name)
        # Filter sub_rdeps to only those leading to installed packages
        if hide_uninstalled and reachable_cache is not None:
            sub_rdeps = [r for r in sub_rdeps if r in reachable_cache]

        if sub_rdeps:
            print(f"{prefix}{connector}{format_pkg(pkg_name)} ({len(sub_rdeps)})")
            visited.add(pkg_name)
            _print_rdep_tree(sub_rdeps, get_rdeps, installed_pkgs, unrequested_pkgs,
                             visited, child_prefix, max_depth, depth + 1,
                             hide_uninstalled=hide_uninstalled, reachable_cache=reachable_cache)
        else:
            print(f"{prefix}{connector}{format_pkg(pkg_name)}")


def cmd_recommends(args, db: PackageDatabase) -> int:
    """Handle recommends command - show packages recommended by a package."""
    from ..core.resolver import Resolver

    package = args.package
    pkg_name = _extract_pkg_name(package)

    resolver = Resolver(db)
    recommends = resolver.get_package_recommends(pkg_name)

    if not recommends:
        print(f"{package}: no recommends")
        return 0

    print(f"Packages recommended by {package}: {len(recommends)}\n")
    for rec in sorted(recommends):
        # Get providers for this capability
        providers = resolver.get_providers(rec.split()[0], include_installed=True)
        if providers:
            print(f"  {rec} -> {', '.join(providers[:3])}")
        else:
            print(f"  {rec}")

    return 0


def cmd_whatrecommends(args, db: PackageDatabase) -> int:
    """Handle whatrecommends command - show packages that recommend a package."""
    package = args.package
    pkg_name = _extract_pkg_name(package)

    # Get what this package provides
    pkg = db.get_package(pkg_name)
    provides = [pkg_name]
    if pkg and pkg.get('provides'):
        for prov in pkg['provides']:
            cap = prov.split('[')[0].strip()
            if cap not in provides:
                provides.append(cap)

    results = set()

    # Check database for each provide
    for cap in provides:
        for r in db.whatrecommends(cap, limit=200):
            results.add(r['name'])

    # Also check installed packages via rpm
    try:
        import rpm
        ts = rpm.TransactionSet()
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == pkg_name or name == 'gpg-pubkey':
                continue
            recs = hdr[rpm.RPMTAG_RECOMMENDNAME] or []
            for rec in recs:
                rec_base = rec.split('(')[0].split()[0]
                if rec_base in provides or rec in provides:
                    results.add(name)
                    break
    except ImportError:
        pass

    if not results:
        print(f"No package recommends '{package}'")
        return 0

    print(f"Packages that recommend {package}: {len(results)}\n")
    for name in sorted(results):
        print(f"  {name}")

    return 0


def cmd_suggests(args, db: PackageDatabase) -> int:
    """Handle suggests command - show packages suggested by a package."""
    from ..core.resolver import Resolver

    package = args.package
    pkg_name = _extract_pkg_name(package)

    resolver = Resolver(db)
    suggests = resolver.get_package_suggests(pkg_name)

    if not suggests:
        print(f"{package}: no suggests")
        return 0

    print(f"Packages suggested by {package}: {len(suggests)}\n")
    for sug in sorted(suggests):
        # Get providers for this capability
        providers = resolver.get_providers(sug.split()[0], include_installed=True)
        if providers:
            print(f"  {sug} -> {', '.join(providers[:3])}")
        else:
            print(f"  {sug}")

    return 0


def cmd_whatsuggests(args, db: PackageDatabase) -> int:
    """Handle whatsuggests command - show packages that suggest a package."""
    package = args.package
    pkg_name = _extract_pkg_name(package)

    # Get what this package provides
    pkg = db.get_package(pkg_name)
    provides = [pkg_name]
    if pkg and pkg.get('provides'):
        for prov in pkg['provides']:
            cap = prov.split('[')[0].strip()
            if cap not in provides:
                provides.append(cap)

    results = set()

    # Check database for each provide
    for cap in provides:
        for r in db.whatsuggests(cap, limit=200):
            results.add(r['name'])

    # Also check installed packages via rpm
    try:
        import rpm
        ts = rpm.TransactionSet()
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == pkg_name or name == 'gpg-pubkey':
                continue
            sugs = hdr[rpm.RPMTAG_SUGGESTNAME] or []
            for sug in sugs:
                sug_base = sug.split('(')[0].split()[0]
                if sug_base in provides or sug in provides:
                    results.add(name)
                    break
    except ImportError:
        pass

    if not results:
        print(f"No package suggests '{package}'")
        return 0

    print(f"Packages that suggest {package}: {len(results)}\n")
    for name in sorted(results):
        print(f"  {name}")

    return 0


def cmd_why(args, db: PackageDatabase) -> int:
    """Handle why command - explain why a package is installed."""
    from ..core.resolver import Resolver
    from . import colors
    from collections import deque

    package = args.package
    pkg_name = _extract_pkg_name(package)

    # Get set of installed packages
    installed_pkgs = set()
    try:
        import rpm
        ts = rpm.TransactionSet()
        for hdr in ts.dbMatch():
            installed_pkgs.add(hdr[rpm.RPMTAG_NAME])
    except ImportError:
        print("rpm module not available")
        return 1

    if pkg_name not in installed_pkgs:
        print(f"Package '{pkg_name}' is not installed")
        return 1

    # Get the list of auto-installed packages
    resolver = Resolver(db)
    unrequested = resolver._get_unrequested_packages()

    # Check if manually installed
    if pkg_name.lower() not in unrequested:
        print(f"{colors.bold(pkg_name)}: {colors.success('explicitly installed')}")
        return 0

    DEP_PRIORITY = {'R': 3, 'r': 2, 's': 1}
    rdeps_cache = {}  # Cache for _get_rdeps calls

    # Helper to format dependency type
    def format_dep_type(dep_type: str, short: bool = False) -> str:
        if dep_type == 'R':
            return colors.success('R') if short else colors.success('required')
        elif dep_type == 'r':
            return colors.info('r') if short else colors.info('recommended')
        else:
            return colors.dim('s') if short else colors.dim('suggested')

    # Get direct rdeps with their dependency types (R/r/s)
    direct_rdeps = _get_rdeps(pkg_name, db, 'Rrs', installed_only=True,
                              cache=rdeps_cache, installed_pkgs=installed_pkgs)

    if not direct_rdeps:
        print(f"{colors.bold(pkg_name)}: {colors.warning('orphan')} (nothing requires it)")
        print(f"\nThis package can be removed with: urpm autoremove --orphans")
        return 0

    # For each direct rdep, find ALL paths to explicit packages using ONLY requires
    # We want to find ALL explicit packages, not just the first one
    results = {}  # direct_rdep -> list of (explicit_pkg, path, initial_dep_type)

    for direct, initial_dep_type in direct_rdeps.items():
        # BFS using only requires to find ALL explicit packages
        queue = deque([(direct, [direct])])
        visited = {direct}
        found_explicits = []

        while queue:
            current, path = queue.popleft()

            # Is current explicit?
            if current.lower() not in unrequested:
                found_explicits.append((current, path, initial_dep_type))
                # Continue exploring - there may be more explicit packages above
                # (e.g., task-pulseaudio is explicit, but task-lxde also depends on it)

            # Continue searching through packages using REQUIRES only
            rdeps_of_current = _get_rdeps(current, db, 'R', installed_only=True,
                                          cache=rdeps_cache, installed_pkgs=installed_pkgs)
            for requirer in rdeps_of_current:
                if requirer in visited:
                    continue
                visited.add(requirer)
                queue.append((requirer, path + [requirer]))

        results[direct] = found_explicits if found_explicits else None

    # Separate into branches that lead to explicit vs orphan branches
    explicit_branches = {k: v for k, v in results.items() if v is not None}
    orphan_branches = [k for k, v in results.items() if v is None]

    if not explicit_branches:
        print(f"{colors.bold(pkg_name)}: {colors.warning('orphan')} (no explicit package requires it)")
        print(f"\nThis package can be removed with: urpm autoremove --orphans")
        return 0

    # Group by explicit package
    # Format: explicit -> list of (direct_rdep, path, dep_type)
    by_explicit = {}
    for direct, explicits_list in explicit_branches.items():
        for explicit, path, dep_type in explicits_list:
            if explicit not in by_explicit:
                by_explicit[explicit] = []
            by_explicit[explicit].append((direct, path, dep_type))

    # Count by dependency type for summary
    # The dep_type is the initial link (pkg_name -> direct_rdep), rest is all Requires
    dep_type_counts = {'R': 0, 'r': 0, 's': 0}
    for entries in by_explicit.values():
        # Use shortest path and its dep_type
        entries.sort(key=lambda x: len(x[1]))
        _, _, dep_type = entries[0]
        dep_type_counts[dep_type] += 1

    print(f"{colors.bold(pkg_name)}: installed as dependency")

    # Summary line
    summary_parts = []
    if dep_type_counts['R']:
        summary_parts.append(f"{colors.success(str(dep_type_counts['R']))} required")
    if dep_type_counts['r']:
        summary_parts.append(f"{colors.info(str(dep_type_counts['r']))} recommended")
    if dep_type_counts['s']:
        summary_parts.append(f"{colors.dim(str(dep_type_counts['s']))} suggested")
    print(f"\nBy {', '.join(summary_parts)} explicit package(s):\n")

    # Sort explicit packages by: requires first, then recommends, then suggests
    def sort_key(pkg):
        entries = by_explicit[pkg]
        entries.sort(key=lambda x: len(x[1]))
        _, _, dep_type = entries[0]
        return (-DEP_PRIORITY[dep_type], pkg)

    for explicit_pkg in sorted(by_explicit.keys(), key=sort_key):
        entries = by_explicit[explicit_pkg]
        # Use shortest path
        entries.sort(key=lambda x: len(x[1]))
        direct, path, dep_type = entries[0]

        dep_marker = format_dep_type(dep_type, short=True)

        if len(path) == 1:
            # Direct dependency from explicit
            print(f"  [{dep_marker}] {colors.success(explicit_pkg)}")
        else:
            # Indirect - show chain (path goes from direct_rdep to explicit)
            # Reverse to show from explicit perspective: explicit <- ... <- direct
            chain = " ← ".join(reversed(path[:-1]))
            print(f"  [{dep_marker}] {colors.success(explicit_pkg)} (via {colors.dim(chain)})")

    # Show disconnected chains (rdeps that don't lead to any explicit package)
    if orphan_branches:
        print(f"\n{colors.dim('Also required by (no explicit package in chain):')}")
        for branch in sorted(orphan_branches)[:5]:
            dep_type = direct_rdeps.get(branch, 'R')
            print(f"  [{format_dep_type(dep_type, short=True)}] {colors.dim(branch)}")
        if len(orphan_branches) > 5:
            print(f"  {colors.dim(f'... and {len(orphan_branches) - 5} more')}")

    return 0


def cmd_not_implemented(args, db: PackageDatabase) -> int:
    """Placeholder for not yet implemented commands."""
    print(f"Command '{args.command}' not yet implemented")
    return 1


# =============================================================================
# Main entry point
# =============================================================================

def main(argv=None) -> int:
    """Main CLI entry point."""
    # Check required dependencies first
    missing = check_dependencies()
    if missing:
        print_missing_dependencies(missing)
        return 1

    parser = create_parser()
    args = parser.parse_args(argv)

    # Configure logging based on verbose flag
    if getattr(args, 'verbose', False):
        import logging
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(name)s - %(levelname)s - %(message)s',
            stream=sys.stderr
        )

    # Initialize color support
    from . import colors
    colors.init(nocolor=getattr(args, 'nocolor', False))

    # Initialize display mode
    from . import display
    if getattr(args, 'json', False):
        display.init(mode='json', show_all=True)  # JSON always shows all
    elif getattr(args, 'flat', False):
        display.init(mode='flat', show_all=True)  # Flat always shows all
    else:
        display.init(mode='columns', show_all=getattr(args, 'show_all', False))

    # Get database path based on --urpm-root
    from ..core.config import get_db_path
    urpm_root = getattr(args, 'urpm_root', None)
    db_path = get_db_path(urpm_root=urpm_root)

    if not args.command:
        # Check if any media is configured
        try:
            db = PackageDatabase(db_path=db_path)
            media_list = db.list_media()
        except Exception:
            # Can't open database or no media - show quick start guide
            media_list = []

        if not media_list:
            # No media configured - show quick start guide
            print_quickstart_guide()
            return 0
        else:
            # Media exists - show normal help
            parser.print_help()
            return 1

    # Open database for command execution
    db = PackageDatabase(db_path=db_path)

    try:
        # Route to command handler
        if args.command == 'init':
            return cmd_init(args, db)

        elif args.command == 'cleanup':
            return cmd_cleanup(args, db)

        elif args.command in ('install', 'i'):
            return cmd_install(args, db)

        elif args.command in ('download', 'dl'):
            return cmd_download(args, db)

        elif args.command == 'mkimage':
            return cmd_mkimage(args, db)

        elif args.command == 'build':
            return cmd_build(args, db)

        elif args.command in ('erase', 'e'):
            return cmd_erase(args, db)

        elif args.command == 'update':
            # apt-style: update = metadata only
            return cmd_media_update(args, db)

        elif args.command in ('upgrade', 'u'):
            return cmd_upgrade(args, db)

        elif args.command in ('list', 'l'):
            return cmd_list(args, db)

        elif args.command in ('search', 's', 'query', 'q'):
            return cmd_search(args, db)

        elif args.command in ('show', 'sh', 'info'):
            return cmd_show(args, db)

        elif args.command in ('media', 'm'):
            if args.media_command in ('list', 'l', 'ls', None):
                return cmd_media_list(args, db)
            elif args.media_command in ('add', 'a'):
                return cmd_media_add(args, db)
            elif args.media_command in ('remove', 'r'):
                return cmd_media_remove(args, db)
            elif args.media_command in ('enable', 'e'):
                return cmd_media_enable(args, db)
            elif args.media_command in ('disable', 'd'):
                return cmd_media_disable(args, db)
            elif args.media_command in ('update', 'u'):
                return cmd_media_update(args, db)
            elif args.media_command == 'import':
                return cmd_media_import(args, db)
            elif args.media_command in ('set', 's'):
                return cmd_media_set(args, db)
            elif args.media_command == 'seed-info':
                return cmd_media_seed_info(args, db)
            elif args.media_command == 'link':
                return cmd_media_link(args, db)
            elif args.media_command in ('autoconfig', 'auto', 'ac'):
                return cmd_media_autoconfig(args, db)
            else:
                return cmd_not_implemented(args, db)

        elif args.command in ('server', 'srv'):
            if args.server_command in ('list', 'l', 'ls', None):
                return cmd_server_list(args, db)
            elif args.server_command in ('add', 'a'):
                return cmd_server_add(args, db)
            elif args.server_command in ('remove', 'r', 'rm'):
                return cmd_server_remove(args, db)
            elif args.server_command in ('enable', 'e'):
                return cmd_server_enable(args, db)
            elif args.server_command in ('disable', 'd'):
                return cmd_server_disable(args, db)
            elif args.server_command == 'priority':
                return cmd_server_priority(args, db)
            elif args.server_command in ('test', 't'):
                return cmd_server_test(args, db)
            elif args.server_command == 'ip-mode':
                return cmd_server_ipmode(args, db)
            elif args.server_command in ('autoconfig', 'auto'):
                return cmd_server_autoconfig(args, db)
            else:
                return cmd_not_implemented(args, db)

        elif args.command in ('mirror', 'proxy'):
            if args.mirror_command == 'status' or args.mirror_command is None:
                return cmd_mirror_status(args, db)
            elif args.mirror_command == 'enable':
                return cmd_mirror_enable(args, db)
            elif args.mirror_command == 'disable':
                return cmd_mirror_disable(args, db)
            elif args.mirror_command == 'quota':
                return cmd_mirror_quota(args, db)
            elif args.mirror_command == 'disable-version':
                return cmd_mirror_disable_version(args, db)
            elif args.mirror_command == 'enable-version':
                return cmd_mirror_enable_version(args, db)
            elif args.mirror_command == 'clean':
                return cmd_mirror_clean(args, db)
            elif args.mirror_command == 'sync':
                return cmd_mirror_sync(args, db)
            elif args.mirror_command == 'rate-limit':
                return cmd_mirror_ratelimit(args, db)
            else:
                return cmd_not_implemented(args, db)

        elif args.command in ('cache', 'c'):
            if args.cache_command == 'info' or args.cache_command is None:
                return cmd_cache_info(args, db)
            elif args.cache_command == 'clean':
                return cmd_cache_clean(args, db)
            elif args.cache_command == 'rebuild':
                return cmd_cache_rebuild(args, db)
            elif args.cache_command == 'rebuild-fts':
                return cmd_cache_rebuild_fts(args, db)
            elif args.cache_command == 'stats':
                return cmd_cache_stats(args, db)
            else:
                return cmd_not_implemented(args, db)

        elif args.command in ('history', 'h'):
            return cmd_history(args, db)

        elif args.command in ('undo',):
            return cmd_undo(args, db)

        elif args.command in ('rollback', 'r'):
            return cmd_rollback(args, db)

        elif args.command in ('cleandeps', 'cd'):
            # Alias for autoremove --faildeps
            args.faildeps = True
            args.orphans = False
            args.kernels = False
            args.all = False
            return cmd_autoremove(args, db)

        elif args.command in ('autoremove', 'ar'):
            return cmd_autoremove(args, db)

        elif args.command == 'mark':
            return cmd_mark(args, db)

        elif args.command == 'hold':
            return cmd_hold(args, db)

        elif args.command == 'unhold':
            return cmd_unhold(args, db)

        elif args.command in ('provides', 'p'):
            return cmd_provides(args, db)

        elif args.command in ('whatprovides', 'wp'):
            return cmd_whatprovides(args, db)

        elif args.command in ('find', 'f'):
            return cmd_find(args, db)

        elif args.command in ('depends', 'd', 'requires', 'req'):
            return cmd_depends(args, db)

        elif args.command in ('rdepends', 'rd', 'whatrequires', 'wr'):
            return cmd_rdepends(args, db)

        elif args.command == 'recommends':
            return cmd_recommends(args, db)

        elif args.command == 'whatrecommends':
            return cmd_whatrecommends(args, db)

        elif args.command == 'suggests':
            return cmd_suggests(args, db)

        elif args.command == 'whatsuggests':
            return cmd_whatsuggests(args, db)

        elif args.command == 'why':
            return cmd_why(args, db)

        elif args.command in ('config', 'cfg'):
            return cmd_config(args)

        elif args.command in ('key', 'k'):
            return cmd_key(args)

        elif args.command == 'peer':
            return cmd_peer(args, db)

        elif args.command == 'appstream':
            return cmd_appstream(args, db)

        else:
            return cmd_not_implemented(args, db)

    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130

    except Exception as e:
        if args.verbose:
            import traceback
            traceback.print_exc()
        else:
            print(f"Error: {e}")
        return 1

    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
