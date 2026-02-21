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
import sys

from .. import __version__
from ..core.database import PackageDatabase
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
    cmd_media_link, cmd_media_autoconfig,
)
from .commands.query import (
    cmd_search, cmd_show, cmd_list, cmd_provides, cmd_whatprovides, cmd_find,
)
from .commands.install import (
    cmd_install, cmd_download,
)
from .commands.remove import (
    cmd_erase,
)
from .commands.upgrade import (
    cmd_upgrade,
)
from .commands.cleanup import (
    cmd_autoremove, cmd_mark, cmd_hold, cmd_unhold,
)
from .commands.depends import (
    cmd_depends, cmd_rdepends, cmd_recommends, cmd_whatrecommends,
    cmd_suggests, cmd_whatsuggests, cmd_why,
)
from .commands.build import (
    cmd_cleanup, cmd_mkimage, cmd_build,
)
from .commands.appstream import (
    cmd_appstream,
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
    print("\nInstall with:", file=sys.stderr)
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
    # TODO: cleanup_parser isn't used yet
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
    # TODO: peer_list isn't used yet
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
    # TODO: appstream_status isn't used yet
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
