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


# Debug file paths (in current working directory)
DEBUG_LAST_INSTALLED_DEPS = Path('.last-installed-through-deps.list')
DEBUG_LAST_REMOVED_DEPS = Path('.last-removed-as-deps.list')
DEBUG_INSTALLED_DEPS_COPY = Path('.installed-through-deps.list')
DEBUG_PREV_INSTALLED_DEPS = Path('.prev-installed-through-deps.list')


def _write_debug_file(path: Path, packages: list, append: bool = False):
    """Write package names to a debug file."""
    mode = 'a' if append else 'w'
    try:
        with open(path, mode) as f:
            for pkg in sorted(packages):
                f.write(f"{pkg}\n")
    except (IOError, OSError):
        pass  # Ignore errors for debug files


def _clear_debug_file(path: Path):
    """Clear a debug file."""
    try:
        path.write_text('')
    except (IOError, OSError):
        pass


def _copy_installed_deps_list(root: str = '/', dest: Path = None):
    """Copy installed-through-deps.list to working directory for debug."""
    src = Path(root) / 'var/lib/rpm/installed-through-deps.list'
    if dest is None:
        dest = DEBUG_INSTALLED_DEPS_COPY
    try:
        if src.exists():
            dest.write_text(src.read_text())
        else:
            dest.write_text('')
    except (IOError, OSError):
        pass


def _notify_urpmd_cache_invalidate():
    """Notify local urpmd to invalidate its RPM cache index.

    This allows newly downloaded packages to be visible to peer queries.
    Tries both dev and prod ports silently.
    """
    import json
    import urllib.request
    import urllib.error
    from ..core.config import DEV_PORT, PROD_PORT

    ports = [DEV_PORT, PROD_PORT]

    for port in ports:
        try:
            url = f"http://127.0.0.1:{port}/api/invalidate-cache"
            req = urllib.request.Request(url, method='POST')
            req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.status == 200:
                    return  # Success, no need to try other port
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue  # Try next port or give up silently


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
    # update / u
    # =========================================================================
    update_parser = subparsers.add_parser(
        'update', aliases=['up'],
        help='Update packages or metadata',
        parents=[display_parent, debug_parent]
    )
    update_parser.add_argument(
        'packages', nargs='*',
        help='Packages to update (empty = all)'
    )
    update_parser.add_argument(
        '--lists', '-l',
        action='store_true',
        help='Update media metadata only'
    )
    update_parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Update all packages'
    )
    update_parser.add_argument(
        '--security',
        action='store_true',
        help='Security updates only'
    )
    update_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )
    update_parser.add_argument(
        '--noerase-orphans',
        action='store_true',
        help='Keep orphaned dependencies (do not remove them)'
    )
    update_parser.add_argument(
        '--test',
        action='store_true',
        help='Dry run - show what would be done'
    )
    update_parser.add_argument(
        '--nosignature',
        action='store_true',
        help='Skip GPG signature verification (not recommended)'
    )
    update_parser.add_argument(
        '--no-peers',
        action='store_true',
        help='Disable P2P download from LAN peers'
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

def _extract_pkg_name(package: str) -> str:
    """Extract package name from a NEVRA string.

    Args:
        package: Either a simple name like 'firefox' or NEVRA like 'firefox-120.0-1.mga10.x86_64'

    Returns:
        The package name
    """
    import re
    # If it looks like a NEVRA (has version pattern), extract name
    # Pattern: name-version-release.arch where version starts with digit
    match = re.match(r'^(.+?)-\d+[.:]', package)
    if match:
        return match.group(1)
    return package


def _extract_family(pkg_name: str) -> str:
    """Extract the family prefix from a versioned package name.

    Examples:
        php8.4-opcache → php8.4
        php8.5-fpm → php8.5
        perl5.38-DBI → perl5.38
        python3.11-requests → python3.11
        firefox → firefox (no family)

    Args:
        pkg_name: Package name

    Returns:
        Family prefix, or the full name if no version pattern detected
    """
    import re
    # Pattern: name + version number + dash + rest
    # e.g., php8.4-something, perl5.38-something, python3.11-something
    match = re.match(r'^([a-zA-Z]+\d+\.?\d*)-', pkg_name)
    if match:
        return match.group(1)
    return pkg_name


def _get_installed_families(prefix: str) -> set:
    """Get installed package families matching a prefix.

    Args:
        prefix: Base name like 'php', 'perl', 'python'

    Returns:
        Set of family names like {'php8.4', 'php8.5'}
    """
    import re
    import subprocess

    families = set()
    try:
        result = subprocess.run(
            ['rpm', '-qa', '--qf', '%{NAME}\\n'],
            capture_output=True, text=True, timeout=30
        )
        for line in result.stdout.splitlines():
            # Match packages like php8.4-*, php8.5-*, etc.
            match = re.match(rf'^({re.escape(prefix)}\d+\.?\d*)-', line)
            if match:
                families.add(match.group(1))
    except Exception:
        pass
    return families


def _resolve_virtual_package(db: PackageDatabase, pkg_name: str, auto: bool, install_all: bool) -> list:
    """Resolve a virtual package to concrete package(s).

    When multiple providers exist from different families (php8.4-opcache, php8.5-opcache),
    this function decides which one(s) to install based on:
    - What's already installed
    - User preference (interactive) or flags (--auto, --all)

    Args:
        db: Database instance
        pkg_name: Virtual package name (e.g., 'php-opcache')
        auto: If True, don't ask user
        install_all: If True, install for all installed families

    Returns:
        List of concrete package names to install, or empty list to abort
    """
    import re

    # Find all providers
    providers = db.whatprovides(pkg_name)
    if not providers:
        # Not a virtual package, return as-is
        return [pkg_name]

    # Group providers by family
    families = {}
    for prov in providers:
        family = _extract_family(prov['name'])
        if family not in families:
            families[family] = []
        families[family].append(prov)

    # Extract base prefix (php from php8.4, perl from perl5.38)
    first_family = list(families.keys())[0]
    match = re.match(r'^([a-zA-Z]+)', first_family)
    base_prefix = match.group(1) if match else first_family

    # Check which families are installed
    installed_families = _get_installed_families(base_prefix)

    # Filter providers to only families that are installed
    matching_families = {f: p for f, p in families.items() if f in installed_families}

    # Case 1: Only one family provides this
    if len(families) == 1:
        family_name = list(families.keys())[0]
        provider_name = families[family_name][0]['name']

        # Check if this family conflicts with installed families
        if installed_families and family_name not in installed_families:
            installed_str = ', '.join(sorted(installed_families))
            print(f"\nWarning: '{pkg_name}' is only provided by {provider_name}")
            print(f"         but you have {installed_str} installed.")
            print(f"         This will likely cause conflicts!")
            if auto:
                print("Aborting (use explicit package name to force)")
                return []
            try:
                answer = input(f"\nInstall anyway? [y/N] ").strip()
                if answer.lower() not in ('y', 'yes'):
                    return []
            except (EOFError, KeyboardInterrupt):
                return []

        return [provider_name]

    # Case 2: Multiple families but none installed
    if not matching_families:
        sorted_families = sorted(families.keys(), reverse=True)
        if auto:
            # Use newest version
            return [families[sorted_families[0]][0]['name']]
        # Interactive: ask user
        print(f"\nMultiple providers for '{pkg_name}':")
        for i, fam in enumerate(sorted_families, 1):
            print(f"  {i}) {families[fam][0]['name']}")
        print(f"  {len(sorted_families) + 1}) All")

        try:
            choice = input(f"\nChoice [1]: ").strip() or "1"
            if choice == str(len(sorted_families) + 1):
                return [families[f][0]['name'] for f in sorted_families]
            idx = int(choice) - 1
            if 0 <= idx < len(sorted_families):
                return [families[sorted_families[idx]][0]['name']]
        except (ValueError, EOFError):
            pass
        return [families[sorted_families[0]][0]['name']]

    # Case 3: One installed family matches
    if len(matching_families) == 1:
        family_name = list(matching_families.keys())[0]
        return [matching_families[family_name][0]['name']]

    # Case 4: Multiple installed families match
    if install_all:
        return [matching_families[f][0]['name'] for f in matching_families]

    if auto:
        # Strict mode: use newest installed family
        sorted_installed = sorted(matching_families.keys(), reverse=True)
        return [matching_families[sorted_installed[0]][0]['name']]

    # Interactive: ask user
    sorted_families = sorted(matching_families.keys(), reverse=True)
    print(f"\nMultiple installed families provide '{pkg_name}':")
    for i, fam in enumerate(sorted_families, 1):
        print(f"  {i}) {matching_families[fam][0]['name']}")
    print(f"  {len(sorted_families) + 1}) All")

    try:
        choice = input(f"\nChoice [1]: ").strip() or "1"
        if choice == str(len(sorted_families) + 1):
            return [matching_families[f][0]['name'] for f in sorted_families]
        idx = int(choice) - 1
        if 0 <= idx < len(sorted_families):
            return [matching_families[sorted_families[idx]][0]['name']]
    except (ValueError, EOFError):
        pass
    return [matching_families[sorted_families[0]][0]['name']]


def _cmd_search_unavailable(args, db: PackageDatabase) -> int:
    """List installed packages not available in any media (urpmq --unavailable)."""
    import rpm
    from . import colors

    # Build set of available package names from all medias
    available_names = set()
    for media in db.list_media():
        if not media['enabled']:
            continue
        # Get all packages from this media
        cursor = db.conn.execute(
            "SELECT DISTINCT name_lower FROM packages WHERE media_id = ?",
            (media['id'],)
        )
        for row in cursor:
            available_names.add(row[0])

    # Get all installed packages
    ts = rpm.TransactionSet()
    unavailable = []

    for hdr in ts.dbMatch():
        name = hdr[rpm.RPMTAG_NAME]
        # Skip gpg-pubkey pseudo-packages
        if name == 'gpg-pubkey':
            continue

        if name.lower() not in available_names:
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH]
            unavailable.append({
                'name': name,
                'version': version,
                'release': release,
                'arch': arch,
                'nevra': f"{name}-{version}-{release}.{arch}"
            })

    if not unavailable:
        print(colors.success("All installed packages are available in configured media"))
        return 0

    # Sort by name
    unavailable.sort(key=lambda p: p['name'].lower())

    # Filter by pattern if provided
    if args.pattern:
        import re
        try:
            regex = re.compile(args.pattern, re.IGNORECASE)
            unavailable = [p for p in unavailable if regex.search(p['name'])]
        except re.error:
            unavailable = [p for p in unavailable if args.pattern.lower() in p['name'].lower()]

        if not unavailable:
            print(colors.warning(f"No unavailable packages match '{args.pattern}'"))
            return 1

    # Display results
    for pkg in unavailable:
        name = colors.bold(pkg['name'])
        version = pkg['version']
        release_arch = colors.dim(f"{pkg['release']}.{pkg['arch']}")
        print(f"{name}-{version}-{release_arch}")

    print(colors.dim(f"\n{len(unavailable)} unavailable package(s)"))
    return 0


def cmd_search(args, db: PackageDatabase) -> int:
    """Handle search command."""
    import re
    from . import colors
    from ..core.operations import PackageOperations

    # Handle --unavailable: list installed packages not in any media
    if getattr(args, 'unavailable', False):
        return _cmd_search_unavailable(args, db)

    # Regular search requires a pattern
    if not args.pattern:
        print(colors.error("Error: search pattern required"))
        print(colors.dim("  Use --unavailable to list packages not in any media"))
        return 1

    ops = PackageOperations(db)
    results = ops.search_packages(args.pattern, search_provides=True)

    if not results:
        print(colors.warning(f"No packages found for '{args.pattern}'"))
        return 1

    # ANSI codes without reset for proper nesting
    GREEN = '\033[92m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'

    def highlight_with_base(text, pattern, base_code):
        """Highlight pattern in green, rest in base color (using raw ANSI codes)."""
        if not colors.enabled():
            return text
        try:
            regex = re.compile(f'({re.escape(pattern)})', re.IGNORECASE)
            parts = regex.split(text)
            result = [base_code]  # Start with base color
            for i, part in enumerate(parts):
                if not part:
                    continue
                if i % 2 == 1:  # Match - switch to green then back to base
                    result.append(f"{GREEN}{part}{RESET}{base_code}")
                else:  # Non-match - already in base color
                    result.append(part)
            result.append(RESET)  # End with reset
            return ''.join(result)
        except re.error:
            return f"{base_code}{text}{RESET}"

    def split_kernel_name(name: str) -> tuple:
        """Split Mageia kernel package name into base name and kernel version.

        Mageia kernel packages have names like:
        - kernel-desktop-6.12.63-1.mga10 (name=kernel-desktop, kver=6.12.63-1.mga10)
        - kernel-stable-testing-server-6.18.3-2.stabletesting.mga10

        Returns:
            (base_name, kernel_version) or (name, None) if not a kernel package
        """
        if not name.startswith('kernel-'):
            return name, None

        # Find the first segment that starts with a digit (kernel version)
        parts = name.split('-')
        for i, part in enumerate(parts):
            if i > 0 and part and part[0].isdigit():
                base_name = '-'.join(parts[:i])
                kernel_version = '-'.join(parts[i:])
                return base_name, kernel_version

        return name, None

    pattern = args.pattern

    for pkg in results:
        pkg_name = pkg['name']
        pkg_version = pkg['version']

        # For kernel packages, extract the kernel version from the name for display
        base_name, kernel_ver = split_kernel_name(pkg_name)
        if kernel_ver:
            # Kernel package: show base name in bold, kernel version as version
            display_name = base_name
            display_version = kernel_ver
        else:
            # Normal package
            display_name = pkg_name
            display_version = pkg_version

        # Name in bold, matches in green, then back to bold
        name = highlight_with_base(display_name, pattern, BOLD)
        # Version: normal (no base code), matches in green
        version = highlight_with_base(display_version, pattern, '')
        # Release.arch: all dim
        release_arch = f"{DIM}{pkg['release']}.{pkg['arch']}{RESET}"
        nevra_display = f"{name}-{version}-{release_arch}"

        summary = pkg.get('summary', '')[:60]
        summary = highlight_with_base(summary, pattern, '')

        # Show which provide matched if found via provides
        if pkg.get('matched_provide'):
            # Entire "(provides: xxx)" in dim, with matches in green
            provide_text = f"(provides: {pkg['matched_provide']})"
            provide_display = highlight_with_base(provide_text, pattern, DIM)
            print(f"{nevra_display}  {provide_display}")
        else:
            print(f"{nevra_display}  {summary}")

    print(colors.dim(f"\n{len(results)} package(s) found"))
    return 0


def cmd_show(args, db: PackageDatabase) -> int:
    """Handle show/info command."""
    from . import colors
    from ..core.operations import PackageOperations

    ops = PackageOperations(db)
    pkg = ops.get_package_info(args.package)

    if not pkg:
        print(colors.error(f"Package '{args.package}' not found"))
        return 1

    print(f"\n{colors.bold('Name:')}         {colors.info(pkg['name'])}")
    print(f"{colors.bold('Version:')}      {pkg['version']}-{pkg['release']}")
    print(f"{colors.bold('Architecture:')} {pkg['arch']}")
    print(f"{colors.bold('Size:')}         {pkg['size'] / 1024 / 1024:.1f} MB")

    if pkg.get('group_name'):
        print(f"{colors.bold('Group:')}        {pkg['group_name']}")
    if pkg.get('summary'):
        print(f"{colors.bold('Summary:')}      {pkg['summary']}")

    if pkg.get('requires'):
        req_count = len(pkg['requires'])
        print(f"\n{colors.bold(f'Requires ({req_count}):')} ")
        from . import display
        display.print_package_list(pkg['requires'], max_lines=10, color_func=colors.dim)

    if pkg.get('recommends'):
        rec_count = len(pkg['recommends'])
        print(f"\n{colors.bold(f'Recommends ({rec_count}):')} ")
        from . import display
        display.print_package_list(pkg['recommends'], max_lines=10, color_func=colors.dim)

    if pkg.get('suggests'):
        sug_count = len(pkg['suggests'])
        print(f"\n{colors.bold(f'Suggests ({sug_count}):')} ")
        from . import display
        display.print_package_list(pkg['suggests'], max_lines=10, color_func=colors.dim)

    if pkg.get('provides'):
        prov_count = len(pkg['provides'])
        print(f"\n{colors.bold(f'Provides ({prov_count}):')} ")
        from . import display
        display.print_package_list(pkg['provides'], max_lines=5, color_func=colors.dim)

    if pkg.get('conflicts'):
        conf_count = len(pkg['conflicts'])
        print(f"\n{colors.bold(f'Conflicts ({conf_count}):')} ")
        from . import display
        display.print_package_list(pkg['conflicts'], max_lines=5, color_func=colors.dim)

    if pkg.get('obsoletes'):
        obs_count = len(pkg['obsoletes'])
        print(f"\n{colors.bold(f'Obsoletes ({obs_count}):')} ")
        from . import display
        display.print_package_list(pkg['obsoletes'], max_lines=5, color_func=colors.dim)

    print()
    return 0


def cmd_media_list(args, db: PackageDatabase) -> int:
    """Handle media list command."""
    from . import colors

    show_all = getattr(args, 'all', False)
    media_list = db.list_media()

    if not media_list:
        print("No media configured")
        return 0

    # Filter to enabled only unless --all
    if not show_all:
        media_list = [m for m in media_list if m['enabled']]
        if not media_list:
            print("No enabled media (use --all to see disabled)")
            return 0

    # Find max lengths for alignment (on raw text, before coloring)
    max_name = max(len(m['name']) for m in media_list)
    max_path = max(len(m.get('relative_path') or '') for m in media_list)

    for m in media_list:
        # Get servers for this media
        servers = db.get_servers_for_media(m['id'], enabled_only=False)

        # Status: [x] or [ ]
        status = colors.success("[x]") if m['enabled'] else colors.dim("[ ]")

        # Update flag: U or space
        update_flag = colors.info("U") if m['update_media'] else " "

        # Files sync flag: F or space
        files_flag = colors.info("F") if m.get('sync_files') else " "

        # Name - pad first, then apply color
        name_raw = m['name']
        name_padded = f"{name_raw:{max_name}}"
        name = colors.dim(name_padded) if not m['enabled'] else name_padded

        # Relative path - pad first, then apply color if needed
        rel_path_raw = m.get('relative_path') or ''
        rel_path_padded = f"{rel_path_raw:{max_path}}"
        rel_path = colors.dim(rel_path_padded) if not m['enabled'] else rel_path_padded

        # Server hosts (green if enabled, dim if disabled)
        if servers:
            server_strs = []
            for s in servers:
                if s['protocol'] == 'file':
                    # Local filesystem - show [local] or path
                    display = f"[local:{s['base_path'][:20]}]" if s['base_path'] else "[local]"
                else:
                    display = s['host']
                if s['enabled']:
                    server_strs.append(colors.success(display))
                else:
                    server_strs.append(colors.dim(display))
            servers_display = " ".join(server_strs)
        else:
            servers_display = colors.warning("(no server)")

        print(f"  {status} {update_flag}{files_flag} {name}  {rel_path}  {servers_display}")

    return 0


# =============================================================================
# URL Parsing for new media schema (v8)
# =============================================================================

# Known Mageia versions (for detection)
KNOWN_VERSIONS = {'7', '8', '9', '10', 'cauldron'}

# Known architectures
KNOWN_ARCHES = {'x86_64', 'aarch64', 'armv7hl', 'i586', 'i686'}

# Known media classes
KNOWN_CLASSES = {'core', 'nonfree', 'tainted', 'debug'}

# Known media types
KNOWN_TYPES = {'release', 'updates', 'backports', 'backports_testing', 'updates_testing', 'testing'}


def _generate_media_name(class_name: str, type_name: str) -> str:
    """Generate display name from class and type.

    Examples:
        core, release -> Core Release
        nonfree, updates -> Nonfree Updates
        tainted, backports_testing -> Tainted Backports Testing
    """
    class_title = class_name.capitalize()
    type_title = type_name.replace('_', ' ').title()
    return f"{class_title} {type_title}"


def _generate_short_name(class_name: str, type_name: str) -> str:
    """Generate short_name from class and type.

    Examples:
        core, release -> core_release
        nonfree, updates -> nonfree_updates
    """
    return f"{class_name}_{type_name}"


def parse_mageia_media_url(url: str) -> dict | None:
    """Parse an official Mageia media URL.

    Detects pattern: .../version/arch/media/class/type/
    Also handles file:// URLs for local mirrors.

    Args:
        url: Full URL to a media

    Returns:
        Dict with parsed components, or None if not a recognized Mageia URL.
        Keys: protocol, host, base_path, relative_path, version, arch,
              class_name, type_name, name, short_name, is_official
    """
    from urllib.parse import urlparse

    # Parse URL
    parsed = urlparse(url.rstrip('/'))

    if parsed.scheme == 'file':
        protocol = 'file'
        host = ''  # No host for file:// URLs
        path = parsed.path
    elif parsed.scheme in ('http', 'https'):
        protocol = parsed.scheme
        host = parsed.netloc
        path = parsed.path
    else:
        return None  # Unknown protocol

    # Split path into components
    parts = [p for p in path.split('/') if p]

    # Look for the pattern: version/arch/media/class/type
    # Or for debug: version/arch/media/debug/class/type
    # Search for 'media' keyword
    try:
        media_idx = parts.index('media')
    except ValueError:
        return None  # No 'media' in path

    # Need at least: something before media, and class/type after
    if media_idx < 2 or len(parts) < media_idx + 3:
        return None

    # Check for debug media: .../media/debug/{class}/{type}
    is_debug = False
    if parts[media_idx + 1] == 'debug':
        is_debug = True
        if len(parts) < media_idx + 4:
            return None
        class_name = parts[media_idx + 2]
        type_name = parts[media_idx + 3]
    else:
        class_name = parts[media_idx + 1]
        type_name = parts[media_idx + 2]

    # Validate class and type
    if class_name not in KNOWN_CLASSES:
        return None
    if type_name not in KNOWN_TYPES:
        return None

    # Look backwards from 'media' for version and arch
    # Pattern should be: version/arch/media
    arch = parts[media_idx - 1]
    version = parts[media_idx - 2]

    # Validate version and arch
    if arch not in KNOWN_ARCHES:
        return None
    if version not in KNOWN_VERSIONS:
        return None

    # Calculate base_path (everything before version)
    # e.g., /mageia or /pub/linux/Mageia
    version_idx = media_idx - 2
    base_path_parts = parts[:version_idx]
    if base_path_parts:
        base_path = '/' + '/'.join(base_path_parts)
    else:
        base_path = ''

    # Calculate relative_path (version onwards)
    # e.g., 9/x86_64/media/core/release
    relative_path = '/'.join(parts[version_idx:])

    # Generate names
    if is_debug:
        name = _generate_media_name(class_name, type_name) + " Debug"
        short_name = "debug_" + _generate_short_name(class_name, type_name)
    else:
        name = _generate_media_name(class_name, type_name)
        short_name = _generate_short_name(class_name, type_name)

    return {
        'protocol': protocol,
        'host': host,
        'base_path': base_path,
        'relative_path': relative_path,
        'version': version,
        'arch': arch,
        'class_name': class_name,
        'is_debug': is_debug,
        'type_name': type_name,
        'name': name,
        'short_name': short_name,
        'is_official': True,
    }


def parse_custom_media_url(url: str) -> dict | None:
    """Parse a custom (non-Mageia) media URL.

    For custom URLs, we can't auto-detect version/arch.
    We use the hostname as base and the path as relative_path.

    Args:
        url: Full URL to a custom media

    Returns:
        Dict with parsed components, or None if invalid.
        Keys: protocol, host, base_path, relative_path
    """
    from urllib.parse import urlparse

    parsed = urlparse(url.rstrip('/'))

    if parsed.scheme == 'file':
        protocol = 'file'
        host = ''
        # For file://, everything is the path
        relative_path = parsed.path.lstrip('/')
        base_path = ''
    elif parsed.scheme in ('http', 'https'):
        protocol = parsed.scheme
        host = parsed.netloc
        # For http(s), base_path is empty, relative_path is the full path
        relative_path = parsed.path.lstrip('/')
        base_path = ''
    else:
        return None

    return {
        'protocol': protocol,
        'host': host,
        'base_path': base_path,
        'relative_path': relative_path,
        'is_official': False,
    }


def _generate_server_name(protocol: str, host: str) -> str:
    """Generate a server name from protocol and host.

    Examples:
        https, mirrors.mageia.org -> mageia-official
        https, distrib-coffee.ipsl.jussieu.fr -> distrib-coffee
        file, '' -> local-mirror
    """
    if protocol == 'file':
        return 'local-mirror'

    # Use first part of hostname
    if '.' in host:
        first_part = host.split('.')[0]
        # Special case for common mirror names
        if first_part in ('mirrors', 'mirror', 'ftp', 'www'):
            # Use second part instead
            parts = host.split('.')
            if len(parts) > 1:
                first_part = parts[1]
        return first_part
    return host


def _fetch_media_pubkey(url: str) -> bytes | None:
    """Fetch pubkey from media_info/pubkey.

    Args:
        url: Media base URL

    Returns:
        Key data as bytes, or None if not found
    """
    import urllib.request
    import urllib.error

    pubkey_url = url.rstrip('/') + '/media_info/pubkey'
    try:
        with urllib.request.urlopen(pubkey_url, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # No pubkey, that's OK
        raise
    except urllib.error.URLError:
        return None


def _get_gpg_key_info(key_data: bytes) -> dict | None:
    """Parse GPG key info using gpg command.

    Args:
        key_data: Raw GPG key data

    Returns:
        Dict with 'keyid', 'fingerprint', 'uid', 'created' or None on error
    """
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode='wb', suffix='.gpg', delete=False) as tmp:
        tmp.write(key_data)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ['gpg', '--show-keys', '--keyid-format', 'long', '--with-colons', tmp_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None

        info = {}
        for line in result.stdout.split('\n'):
            fields = line.split(':')
            if fields[0] == 'pub':
                # pub:...:keyid:created:...
                info['keyid'] = fields[4][-8:].lower()  # Last 8 chars
                info['keyid_long'] = fields[4].lower()
                if fields[5]:
                    info['created'] = fields[5]
            elif fields[0] == 'fpr':
                info['fingerprint'] = fields[9]
            elif fields[0] == 'uid' and 'uid' not in info:
                info['uid'] = fields[9]

        return info if info.get('keyid') else None
    finally:
        os.unlink(tmp_path)


def _is_key_in_rpm_keyring(keyid: str) -> bool:
    """Check if a GPG key is already in the RPM keyring.

    Args:
        keyid: Key ID (8 hex chars, lowercase)

    Returns:
        True if key is installed
    """
    import rpm

    ts = rpm.TransactionSet()
    for hdr in ts.dbMatch('name', 'gpg-pubkey'):
        version = hdr[rpm.RPMTAG_VERSION].lower()
        if version == keyid:
            return True
    return False


def _import_gpg_key(key_data: bytes) -> bool:
    """Import GPG key into RPM keyring.

    Args:
        key_data: Raw GPG key data

    Returns:
        True on success
    """
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode='wb', suffix='.gpg', delete=False) as tmp:
        tmp.write(key_data)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ['rpm', '--import', tmp_path],
            capture_output=True, text=True
        )
        return result.returncode == 0
    finally:
        os.unlink(tmp_path)


# Standard Mageia media types (class/type combinations)
STANDARD_MEDIA_TYPES = [
    ('core', 'release'),
    ('core', 'updates'),
    ('nonfree', 'release'),
    ('nonfree', 'updates'),
    ('tainted', 'release'),
    ('tainted', 'updates'),
]


def cmd_init(args, db: PackageDatabase) -> int:
    """Initialize urpm setup with standard Mageia media from mirrorlist.

    Creates database and adds all standard media (core, nonfree, tainted × release, updates)
    using mirrors from the provided mirrorlist URL.
    """
    from . import colors
    from urllib.request import urlopen, Request
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import re
    import time
    import platform

    mirrorlist_url = args.mirrorlist
    version = getattr(args, 'release', None)
    arch = getattr(args, 'arch', None) or platform.machine()

    # If no mirrorlist but --release provided, auto-construct URL
    if not mirrorlist_url:
        if version:
            mirrorlist_url = f"https://mirrors.mageia.org/api/mageia.{version}.{arch}.list"
            print(f"Using mirrorlist: {mirrorlist_url}")
        else:
            print(colors.error("Either --mirrorlist or --release is required"))
            print(colors.dim("Examples:"))
            print(colors.dim("  urpm init --release 10"))
            print(colors.dim("  urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'"))
            return 1
    elif not version or not arch:
        # Try to extract version and arch from mirrorlist URL if not provided
        # URL format: https://mirrors.mageia.org/api/mageia.10.x86_64.list
        match = re.search(r'mageia\.([^.]+)\.([^.]+)\.list', mirrorlist_url)
        if match:
            if not version:
                version = match.group(1)
            if not arch:
                arch = match.group(2)

    # Fallback to system version if still not determined
    if not version:
        try:
            with open('/etc/os-release') as f:
                for line in f:
                    if line.startswith('VERSION_ID='):
                        version = line.strip().split('=')[1].strip('"')
                        break
        except (IOError, OSError):
            pass

    if not version:
        print(colors.error("Cannot determine Mageia version"))
        print(colors.dim("Use --release to specify (e.g., --release 10 or --release cauldron)"))
        return 1

    urpm_root = getattr(args, 'urpm_root', None)
    if urpm_root:
        print(f"Initializing urpm in {urpm_root}/var/lib/urpm/")
        import subprocess
        import os
        import stat

        # Prepare chroot filesystem structure
        print("Preparing chroot filesystem...")
        root_path = Path(urpm_root)

        # Create essential directories
        essential_dirs = [
            'dev', 'dev/pts', 'dev/shm',
            'proc', 'sys',
            'etc', 'var/tmp', 'var/lib/rpm',
            'run', 'tmp',
            # UsrMerge target directories
            'usr/bin', 'usr/sbin', 'usr/lib', 'usr/lib64'
        ]
        for d in essential_dirs:
            (root_path / d).mkdir(parents=True, exist_ok=True)

        # Note: UsrMerge symlinks (/bin -> usr/bin, etc.) are created by
        # the filesystem package. Don't create them here or it will conflict.
        # We only create the target directories (usr/bin, etc.) above.

        # Set proper permissions for /tmp and /var/tmp
        (root_path / 'tmp').chmod(0o1777)
        (root_path / 'var/tmp').chmod(0o1777)

        # Skip mount operations if no_mount flag is set (used by mkimage)
        # Container runtimes handle /dev and /proc mounting internally
        no_mount = getattr(args, 'no_mount', False)

        # Check if filesystem supports device nodes (nodev mount option)
        def is_nodev_filesystem(path: Path) -> bool:
            """Check if path is on a filesystem mounted with nodev."""
            try:
                with open('/proc/mounts', 'r') as f:
                    # Find the mount point for this path
                    best_match = None
                    best_len = 0
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 4:
                            mount_point = parts[1]
                            options = parts[3]
                            # Check if this mount point is a prefix of our path
                            try:
                                if str(path.resolve()).startswith(mount_point):
                                    if len(mount_point) > best_len:
                                        best_len = len(mount_point)
                                        best_match = options
                            except (OSError, ValueError):
                                pass
                    if best_match and 'nodev' in best_match.split(','):
                        return True
            except (OSError, IOError):
                pass
            return False

        # Bind mount /dev from host (works on any filesystem including nodev)
        chroot_dev = root_path / 'dev'
        dev_mounted = False

        # Check if already mounted
        def is_dev_mounted(chroot_dev: Path) -> bool:
            try:
                with open('/proc/mounts', 'r') as f:
                    chroot_dev_str = str(chroot_dev.resolve())
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2 and parts[1] == chroot_dev_str:
                            return True
            except (OSError, IOError):
                pass
            return False

        if no_mount:
            print("  Skipping mount operations (container mode)")
        elif not is_dev_mounted(chroot_dev):
            if is_nodev_filesystem(root_path):
                print("  Filesystem has nodev - bind mounting /dev from host...")
            else:
                print("  Bind mounting /dev from host...")

            result = subprocess.run(
                ['mount', '--bind', '/dev', str(chroot_dev)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                dev_mounted = True
                print(colors.dim(f"  (unmount with: umount {chroot_dev})"))
            else:
                print(colors.warning(f"  Failed to mount /dev: {result.stderr.strip()}"))
                # Fall back to creating device nodes if mount failed
                print("  Falling back to creating device nodes...")
                old_umask = os.umask(0)
                try:
                    dev_nodes = [
                        ('null', stat.S_IFCHR | 0o666, 1, 3),
                        ('zero', stat.S_IFCHR | 0o666, 1, 5),
                        ('random', stat.S_IFCHR | 0o666, 1, 8),
                        ('urandom', stat.S_IFCHR | 0o666, 1, 9),
                        ('console', stat.S_IFCHR | 0o600, 5, 1),
                        ('tty', stat.S_IFCHR | 0o666, 5, 0),
                    ]
                    for name, mode, major, minor in dev_nodes:
                        dev_path = root_path / 'dev' / name
                        if not dev_path.exists():
                            try:
                                os.mknod(str(dev_path), mode, os.makedev(major, minor))
                            except (PermissionError, OSError):
                                pass
                finally:
                    os.umask(old_umask)
        else:
            print("  /dev already mounted")
            dev_mounted = True

        # Create /dev/fd symlink (only if not using bind mount and not container mode)
        if not no_mount:
            fd_link = root_path / 'dev/fd'
            if not dev_mounted and not fd_link.exists():
                try:
                    fd_link.symlink_to('/proc/self/fd')
                except OSError:
                    pass

            # Create /dev/stdin, stdout, stderr symlinks (only if not using bind mount)
            if not dev_mounted:
                for i, name in enumerate(['stdin', 'stdout', 'stderr']):
                    link_path = root_path / 'dev' / name
                    if not link_path.exists():
                        try:
                            link_path.symlink_to(f'/proc/self/fd/{i}')
                        except OSError:
                            pass

            # Mount /proc (needed by many scriptlets)
            chroot_proc = root_path / 'proc'
            def is_proc_mounted(chroot_proc: Path) -> bool:
                try:
                    with open('/proc/mounts', 'r') as f:
                        chroot_proc_str = str(chroot_proc.resolve())
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 2 and parts[1] == chroot_proc_str:
                                return True
                except (OSError, IOError):
                    pass
                return False

            if not is_proc_mounted(chroot_proc):
                print("  Mounting /proc...")
                result = subprocess.run(
                    ['mount', '-t', 'proc', 'proc', str(chroot_proc)],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    print(colors.dim(f"  (unmount with: umount {chroot_proc})"))
                else:
                    print(colors.warning(f"  Failed to mount /proc: {result.stderr.strip()}"))
            else:
                print("  /proc already mounted")

            # Create /etc/mtab symlink to /proc/mounts
            mtab_link = root_path / 'etc/mtab'
            if not mtab_link.exists():
                try:
                    mtab_link.symlink_to('/proc/mounts')
                except OSError:
                    pass

        # Copy /etc/resolv.conf for DNS resolution
        resolv_src = Path('/etc/resolv.conf')
        resolv_dst = root_path / 'etc/resolv.conf'
        if resolv_src.exists() and not resolv_dst.exists():
            try:
                import shutil
                shutil.copy2(str(resolv_src), str(resolv_dst))
            except (OSError, IOError):
                pass

        # Create minimal /etc/passwd and /etc/group for RPM
        # These are needed before the first package installation
        passwd_file = root_path / 'etc/passwd'
        if not passwd_file.exists():
            try:
                passwd_file.write_text("root:x:0:0:root:/root:/bin/bash\n")
            except (OSError, IOError):
                pass

        group_file = root_path / 'etc/group'
        if not group_file.exists():
            try:
                # Minimal groups needed by common packages
                group_file.write_text(
                    "root:x:0:\n"
                    "bin:x:1:\n"
                    "daemon:x:2:\n"
                    "sys:x:3:\n"
                    "tty:x:5:\n"
                    "disk:x:6:\n"
                    "wheel:x:10:\n"
                    "mail:x:12:\n"
                    "man:x:15:\n"
                    "utmp:x:22:\n"
                    "audio:x:63:\n"
                    "video:x:39:\n"
                    "users:x:100:\n"
                    "nobody:x:65534:\n"
                )
            except (OSError, IOError):
                pass

        # Initialize empty rpmdb in the chroot
        rpmdb_dir = root_path / "var/lib/rpm"
        print(f"Initializing rpmdb...")
        result = subprocess.run(
            ['rpm', '--root', urpm_root, '--initdb'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(colors.error(f"Failed to initialize rpmdb: {result.stderr}"))
            return 1

        # Import Mageia GPG key into the chroot
        print(f"Importing Mageia GPG key...")
        # Try to copy host's Mageia key to chroot
        key_paths = [
            '/etc/pki/rpm-gpg/RPM-GPG-KEY-Mageia',
            '/usr/share/distribution-gpg-keys/mageia/RPM-GPG-KEY-Mageia'
        ]
        key_imported = False
        for key_path in key_paths:
            if Path(key_path).exists():
                result = subprocess.run(
                    ['rpm', '--root', urpm_root, '--import', key_path],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    print(f"  Imported key from {key_path}")
                    key_imported = True
                    break
        if not key_imported:
            print(colors.warning("  Could not import GPG key (use --nosignature if needed)"))
    else:
        print(f"Initializing urpm for Mageia {version} ({arch})")

    # Check if media already exist
    existing_media = db.list_media()
    if existing_media:
        print(colors.warning(f"Warning: {len(existing_media)} media already configured"))
        auto = getattr(args, 'auto', False)
        if not auto:
            try:
                response = input("Continue and add more? [y/N] ")
                if response.lower() not in ('y', 'yes'):
                    print("Aborted")
                    return 1
            except (KeyboardInterrupt, EOFError):
                print("\nAborted")
                return 1

    # Fetch mirrorlist
    print(f"Fetching mirrorlist...", end=' ', flush=True)

    try:
        req = Request(mirrorlist_url, headers={'User-Agent': 'urpm/0.1'})
        with urlopen(req, timeout=60) as response:
            content = response.read().decode('utf-8').strip()
            lines = [line.strip() for line in content.split('\n') if line.strip()]
    except (URLError, HTTPError) as e:
        print(colors.error(f"failed: {e}"))
        return 1

    if not lines:
        print(colors.warning("empty"))
        print(colors.dim("The mirrorlist may not be available yet for this version."))
        return 1

    # Parse mirrorlist format: key=value,key=value,...,url=https://...
    # Example: continent=EU,zone=FR,...,url=https://ftp.belnet.be/mageia/distrib/10/x86_64
    mirror_urls = []
    for line in lines:
        # Extract url= field from CSV-like format
        for field in line.split(','):
            if field.startswith('url='):
                mirror_urls.append(field[4:])  # Remove 'url=' prefix
                break

    print(f"{len(mirror_urls)} mirrors")

    if not mirror_urls:
        print(colors.warning("No URLs found in mirrorlist"))
        return 1

    # Parse mirror URLs to extract base paths
    # Mirror URLs look like: https://ftp.belnet.be/mageia/distrib/10/x86_64
    # We need to extract the base: https://ftp.belnet.be/mageia/distrib/
    # The suffix to strip is: {version}/{arch}
    suffix_pattern = re.compile(rf'{re.escape(version)}/{re.escape(arch)}/?$')

    candidates = []
    for url in mirror_urls:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            continue

        # Extract base path by stripping the suffix
        base_path = suffix_pattern.sub('', parsed.path).rstrip('/')

        candidates.append({
            'scheme': parsed.scheme,
            'host': parsed.hostname,
            'base_path': base_path,
            'full_url': url,
        })

    if not candidates:
        print(colors.error("No valid HTTP/HTTPS mirrors found"))
        return 1

    # Test latency to find best mirrors
    print(f"Testing latency to {len(candidates)} mirrors...", end=' ', flush=True)

    def test_latency(candidate):
        test_url = candidate['full_url']
        try:
            start = time.time()
            req = Request(test_url, method='HEAD')
            with urlopen(req, timeout=5) as resp:
                latency = (time.time() - start) * 1000
                return (candidate, latency)
        except Exception:
            return (candidate, None)

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(test_latency, c): c for c in candidates}
        for future in as_completed(futures):
            candidate, latency = future.result()
            if latency is not None:
                results.append((candidate, latency))

    print(f"{len(results)} reachable")

    if not results:
        print(colors.error("No reachable mirrors found"))
        return 1

    # Sort by latency and take best 3
    results.sort(key=lambda x: x[1])
    best_mirrors = results[:3]

    print(f"\nBest mirrors:")
    for candidate, latency in best_mirrors:
        print(f"  {candidate['host']} ({latency:.0f}ms)")

    # Add servers
    print(f"\nAdding servers...")
    servers_added = []

    for candidate, latency in best_mirrors:
        # Check if server already exists
        existing = db.get_server_by_location(
            candidate['scheme'],
            candidate['host'],
            candidate['base_path']
        )
        if existing:
            print(f"  {candidate['host']}: already exists")
            servers_added.append(existing)
            continue

        # Generate server name from hostname
        server_name = _generate_server_name(candidate['scheme'], candidate['host'])

        # Make name unique if needed
        base_name = server_name
        counter = 1
        while True:
            try:
                server_id = db.add_server(
                    name=server_name,
                    protocol=candidate['scheme'],
                    host=candidate['host'],
                    base_path=candidate['base_path'],
                    is_official=True,
                    enabled=True,
                    priority=50
                )
                print(f"  {server_name} (id={server_id})")
                servers_added.append({'id': server_id, 'name': server_name})
                break
            except Exception as e:
                if 'UNIQUE constraint' in str(e) and 'name' in str(e):
                    counter += 1
                    server_name = f"{base_name}-{counter}"
                else:
                    print(colors.error(f"  Failed to add {candidate['host']}: {e}"))
                    break

    if not servers_added:
        print(colors.error("No servers could be added"))
        return 1

    # Add standard media
    print(f"\nAdding standard media for Mageia {version} ({arch})...")
    media_added = []

    for media_class, media_type in STANDARD_MEDIA_TYPES:
        name = f"{media_class.capitalize()} {media_type.capitalize()}"
        short_name = f"{media_class}_{media_type}"
        relative_path = f"{version}/{arch}/media/{media_class}/{media_type}"
        is_update = (media_type == 'updates')

        # Check if media already exists
        existing = db.get_media_by_version_arch_shortname(version, arch, short_name)
        if existing:
            print(f"  {name}: already exists")
            media_added.append(existing)
            continue

        try:
            media_id = db.add_media(
                name=name,
                short_name=short_name,
                mageia_version=version,
                architecture=arch,
                relative_path=relative_path,
                is_official=True,
                allow_unsigned=False,
                enabled=True,
                update_media=is_update,
                priority=50,
                url=None
            )
            print(f"  {name} (id={media_id})")
            media_added.append({'id': media_id, 'name': name, 'short_name': short_name})
        except Exception as e:
            print(colors.error(f"  Failed to add {name}: {e}"))

    if not media_added:
        print(colors.error("No media could be added"))
        return 1

    # Link servers to media
    print(f"\nLinking servers to media...")
    for server in servers_added:
        for media in media_added:
            if not db.server_media_link_exists(server['id'], media['id']):
                db.link_server_media(server['id'], media['id'])

    print(colors.success(f"\nInitialized with {len(servers_added)} server(s) and {len(media_added)} media"))

    # Sync media unless --no-sync
    if not getattr(args, 'no_sync', False):
        print(f"\nSyncing media metadata...")
        # Trigger sync for all media
        for media in media_added:
            media_name = media.get('name', '')
            short_name = media.get('short_name', media_name)
            print(f"  Syncing {short_name}...", end=' ', flush=True)
            try:
                from ..core.sync import sync_media
                result = sync_media(db, media_name, urpm_root=urpm_root)
                if result.success:
                    print(f"{result.packages_count} packages")
                else:
                    print(colors.warning(f"failed: {result.error or 'unknown'}"))
            except Exception as e:
                print(colors.warning(f"failed: {e}"))

    print(colors.success("\nDone! You can now install packages."))
    if urpm_root:
        print(colors.dim(f"Example: urpm --urpm-root {urpm_root} --root {urpm_root} install basesystem-minimal"))

    return 0


def cmd_media_add(args, db: PackageDatabase) -> int:
    """Handle media add command.

    Supports two modes:
    1. Official Mageia media: urpm media add <url>
       Auto-parses URL to extract version, arch, class, type
    2. Custom media: urpm media add --custom <name> <short_name> <url>
       User provides name and short_name explicitly

    Uses v8 schema with server/media/server_media tables.
    Falls back to legacy mode if URL parsing fails.
    """
    from . import colors
    from ..core.install import check_root

    url = args.url
    custom_args = getattr(args, 'custom', None)
    is_custom = custom_args is not None

    # Parse URL based on mode
    if is_custom:
        # Custom mode: user provides name and short_name via --custom "Name" short_name
        name = custom_args[0]
        short_name = custom_args[1]

        parsed = parse_custom_media_url(url)
        if not parsed:
            print(colors.error(f"Error: could not parse URL: {url}"))
            return 1

        parsed['name'] = name
        parsed['short_name'] = short_name
        # For custom, we need version/arch from system or args
        # Default to current system
        import platform
        machine = platform.machine()
        parsed['version'] = getattr(args, 'version', 'custom')
        parsed['arch'] = machine if machine in KNOWN_ARCHES else 'x86_64'

    else:
        # Official mode: auto-parse URL
        parsed = parse_mageia_media_url(url)

        if not parsed:
            # Fallback: try legacy mode if --name is provided
            if hasattr(args, 'name') and args.name:
                print(colors.dim("URL not recognized as official Mageia, using legacy mode"))
                media_id = db.add_media_legacy(
                    name=args.name,
                    url=url,
                    enabled=not getattr(args, 'disabled', False),
                    update=getattr(args, 'update', False)
                )
                print(f"Added media '{args.name}' (id={media_id}) [legacy mode]")
                return 0
            else:
                print(colors.error("Error: URL not recognized as official Mageia media"))
                print("For official media, URL must contain: .../version/arch/media/class/type/")
                print("For custom media, use: urpm media add --custom <name> <short_name> <url>")
                return 1

    # Extract parsed values
    protocol = parsed['protocol']
    host = parsed['host']
    base_path = parsed['base_path']
    relative_path = parsed['relative_path']
    name = parsed['name']
    short_name = parsed['short_name']
    version = parsed['version']
    arch = parsed['arch']
    is_official = parsed['is_official']

    # Check --allow-unsigned is only used with custom media
    allow_unsigned = getattr(args, 'allow_unsigned', False)
    if allow_unsigned and is_official:
        print(colors.error("Error: --allow-unsigned can only be used with custom media"))
        return 1

    # GPG key import (optional, only with --import-key)
    # Signature verification happens at package install time, not here
    import_key = getattr(args, 'import_key', False)

    if import_key and protocol != 'file':
        print(f"Fetching GPG key from {url}/media_info/pubkey...")
        try:
            key_data = _fetch_media_pubkey(url)
        except Exception as e:
            print(colors.error(f"Error: could not fetch pubkey: {e}"))
            return 1

        if not key_data:
            print(colors.error("Error: no pubkey found at media"))
            return 1

        key_info = _get_gpg_key_info(key_data)
        if not key_info:
            print(colors.error("Error: could not parse pubkey"))
            return 1

        keyid = key_info['keyid']
        print(f"  Key ID:      {key_info.get('keyid_long', keyid)}")
        if key_info.get('fingerprint'):
            fp = key_info['fingerprint']
            fp_formatted = ' '.join([fp[i:i+4] for i in range(0, len(fp), 4)])
            print(f"  Fingerprint: {fp_formatted}")
        if key_info.get('uid'):
            print(f"  User ID:     {key_info['uid']}")

        if _is_key_in_rpm_keyring(keyid):
            print(colors.success(f"  Key {keyid} already in keyring"))
        else:
            # Import the key
            auto = getattr(args, 'auto', False)
            if not auto:
                try:
                    response = input("\nImport this key? [y/N] ")
                    if response.lower() not in ('y', 'yes'):
                        print("Aborted")
                        return 1
                except (KeyboardInterrupt, EOFError):
                    print("\nAborted")
                    return 1

            if not check_root():
                print(colors.error("Error: importing keys requires root privileges"))
                return 1

            if _import_gpg_key(key_data):
                print(colors.success(f"  Key {keyid} imported"))
            else:
                print(colors.error("  Failed to import key"))
                return 1

    # --- Server upsert ---
    # Check if server already exists by protocol+host+base_path
    server = db.get_server_by_location(protocol, host, base_path)
    server_created = False

    if not server:
        # Create new server
        server_name = _generate_server_name(protocol, host)
        # Make server name unique if needed
        base_server_name = server_name
        counter = 1
        while True:
            try:
                server_id = db.add_server(
                    name=server_name,
                    protocol=protocol,
                    host=host,
                    base_path=base_path,
                    is_official=is_official,
                    enabled=True,
                    priority=50
                )
                server_created = True
                print(f"  Created server '{server_name}' (id={server_id})")
                server = {'id': server_id, 'name': server_name}
                break
            except Exception as e:
                if 'UNIQUE constraint' in str(e) and 'name' in str(e):
                    counter += 1
                    server_name = f"{base_server_name}-{counter}"
                else:
                    raise
    else:
        print(f"  Using existing server '{server['name']}' (id={server['id']})")

    # --- Media upsert ---
    # Check if media already exists by version+arch+short_name
    media = db.get_media_by_version_arch_shortname(version, arch, short_name)
    media_created = False

    if not media:
        # Create new media
        media_id = db.add_media(
            name=name,
            short_name=short_name,
            mageia_version=version,
            architecture=arch,
            relative_path=relative_path,
            is_official=is_official,
            allow_unsigned=allow_unsigned,
            enabled=not getattr(args, 'disabled', False),
            update_media=getattr(args, 'update', False),
            priority=50,
            url=None  # No legacy URL needed with server/media model
        )
        media_created = True
        print(f"  Created media '{name}' (id={media_id})")
        media = {'id': media_id, 'name': name}
    else:
        print(f"  Using existing media '{media['name']}' (id={media['id']})")
        media_id = media['id']

    # --- Link server to media ---
    if not db.server_media_link_exists(server['id'], media['id']):
        db.link_server_media(server['id'], media['id'])
        print(f"  Linked server '{server['name']}' -> media '{media['name']}'")
    else:
        print(f"  Link already exists: server '{server['name']}' -> media '{media['name']}'")

    # Summary
    print()
    if server_created and media_created:
        print(colors.success(f"Added media '{name}' with new server"))
    elif media_created:
        print(colors.success(f"Added media '{name}' to existing server"))
    elif server_created:
        print(colors.success(f"Added new server for existing media '{name}'"))
    else:
        print(colors.success(f"Linked existing server to existing media '{name}'"))

    return 0


def cmd_media_remove(args, db: PackageDatabase) -> int:
    """Handle media remove command."""
    name = args.name

    if not db.get_media(name):
        print(f"Media '{name}' not found")
        return 1

    db.remove_media(name)
    print(f"Removed media '{name}'")
    return 0


def cmd_media_enable(args, db: PackageDatabase) -> int:
    """Handle media enable command."""
    name = args.name

    if not db.get_media(name):
        print(f"Media '{name}' not found")
        return 1

    db.enable_media(name, enabled=True)
    print(f"Enabled media '{name}'")
    return 0


def cmd_media_disable(args, db: PackageDatabase) -> int:
    """Handle media disable command."""
    name = args.name

    if not db.get_media(name):
        print(f"Media '{name}' not found")
        return 1

    db.enable_media(name, enabled=False)
    print(f"Disabled media '{name}'")
    return 0


def cmd_media_update(args, db: PackageDatabase) -> int:
    """Handle media update command."""
    from . import colors
    from ..core.sync import sync_media, sync_all_media, sync_files_xml, sync_all_files_xml
    from ..core.install import check_root
    import threading

    # Check root privileges (media update writes to database)
    if not check_root():
        print(colors.error("Error: root privileges required for media update"))
        print("Try: sudo urpm media update")
        return 1

    sync_files = getattr(args, 'files', False)

    def progress(media_name, stage, current, total):
        # Clear line with ANSI escape code, then print
        if total > 0:
            msg = f"  {media_name}: {stage} ({current}/{total})"
        else:
            msg = f"  {media_name}: {stage}"
        print(f"\r\033[K{msg}", end='', flush=True)

    if args.name:
        # Update specific media
        media = db.get_media(args.name)
        if not media:
            print(colors.error(f"Media '{args.name}' not found"))
            return 1

        print(f"Updating {args.name}...")

        def single_progress(stage, current, total):
            progress(args.name, stage, current, total)

        urpm_root = getattr(args, 'urpm_root', None)
        result = sync_media(db, args.name, single_progress, force=True, urpm_root=urpm_root)
        print()  # newline after progress

        if result.success:
            print(colors.success(f"  {result.packages_count} packages"))

            # Sync files.xml if requested
            if sync_files:
                print(f"  Downloading files.xml for {args.name}...")
                files_result = sync_files_xml(db, args.name, single_progress, force=True)
                print()  # newline after progress
                if files_result.success:
                    if files_result.skipped:
                        print(colors.info(f"  files.xml: up-to-date ({files_result.file_count} files)"))
                    else:
                        print(colors.success(f"  files.xml: {files_result.file_count} files from {files_result.pkg_count} packages"))
                else:
                    print(f"  {colors.warning('Warning')}: files.xml: {files_result.error}")

            return 0
        else:
            print(f"  {colors.error('Error')}: {result.error}")
            return 1
    else:
        # Update all media in parallel
        import time
        print("Updating all media (parallel)...")

        # Helper to format elapsed time
        def format_elapsed(seconds):
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            if mins > 0:
                return f"{mins}m{secs}s"
            else:
                return f"{secs}s"

        # Track status for each media
        media_status = {}
        status_lock = threading.Lock()
        media_list = [m['name'] for m in db.list_media() if m['enabled']]
        num_lines = 0

        def parallel_progress(media_name, stage, current, total):
            nonlocal num_lines
            with status_lock:
                # Update status
                if total > 0:
                    media_status[media_name] = f"{stage} ({current}/{total})"
                else:
                    media_status[media_name] = stage

                # Redraw all status lines
                if num_lines > 0:
                    print(f"\033[{num_lines}F", end='', flush=True)

                for name in media_list:
                    status = media_status.get(name, "waiting...")
                    print(f"\033[K  {name}: {status}")

                num_lines = len(media_list)

        sync_start = time.time()
        results = sync_all_media(db, parallel_progress, force=True)
        sync_elapsed = time.time() - sync_start

        # Clear progress lines
        if num_lines > 0:
            print(f"\033[{num_lines}F", end='', flush=True)
            for _ in range(num_lines):
                print("\033[K", end='')
                print("\033[1B", end='')
            print(f"\033[{num_lines}F", end='', flush=True)

        total_packages = 0
        errors = 0

        for name, result in results:
            if result.success:
                count = result.packages_count
                count_str = colors.success(str(count)) if count > 0 else str(count)
                print(f"  {colors.info(name)}: {count_str} packages")
                total_packages += count
            else:
                print(f"  {colors.error(name)}: ERROR - {result.error}")
                errors += 1

        if errors:
            print(f"\n{colors.info('Total')}: {colors.success(str(total_packages))} packages from {len(results)} media in {format_elapsed(sync_elapsed)} ({colors.error(str(errors))} errors)")
        else:
            print(f"\n{colors.info('Total')}: {colors.success(str(total_packages))} packages from {len(results)} media in {format_elapsed(sync_elapsed)}")

        # Sync files.xml if requested
        if sync_files:
            print(f"\nSyncing files.xml...")

            # Track status for each media (same pattern as synthesis sync)
            # Filter by version/arch like sync_all_files_xml does
            from ..core.config import get_accepted_versions
            import platform

            accepted_versions, _, _ = get_accepted_versions(db)
            arch = platform.machine()

            files_status = {}
            files_lock = threading.Lock()
            files_media_list = []
            for m in db.list_media():
                if not m['enabled'] or not m.get('sync_files'):
                    continue
                # Same filter as sync_all_files_xml
                media_version = m.get('mageia_version', '')
                media_arch = m.get('architecture', '')
                if accepted_versions:
                    version_ok = not media_version or media_version in accepted_versions
                else:
                    version_ok = True
                arch_ok = not media_arch or not arch or media_arch == arch
                if version_ok and arch_ok:
                    files_media_list.append(m['name'])
            files_num_lines = 0

            def files_progress(media_name, stage, dl_current, dl_total, import_current, import_total):
                nonlocal files_num_lines
                with files_lock:
                    # Build status string
                    if stage == 'checking':
                        status = "checking..."
                    elif stage == 'skipped':
                        status = "up-to-date"
                    elif stage == 'downloading':
                        if dl_total > 0:
                            pct = int(100 * dl_current / dl_total)
                            status = f"downloading {pct}%"
                        else:
                            status = "downloading..."
                    elif stage == 'downloaded':
                        status = "downloaded"
                    elif stage in ('syncing', 'analyzing', 'diff'):
                        status = "analyzing..."
                    elif stage == 'importing':
                        if import_total > 0:
                            pct = min(99, int(100 * import_current / import_total))
                            status = f"importing {pct}%"
                        else:
                            status = "importing..."
                    elif stage == 'indexing':
                        status = "creating indexes..."
                    elif stage == 'done':
                        status = colors.success("done")
                    elif stage == 'error':
                        status = colors.error("error")
                    else:
                        status = stage

                    files_status[media_name] = status

                    # Redraw all status lines
                    if files_num_lines > 0:
                        print(f"\033[{files_num_lines}F", end='', flush=True)

                    for name in files_media_list:
                        st = files_status.get(name, "waiting...")
                        print(f"\033[K  {name}: {st}")

                    files_num_lines = len(files_media_list)

            # Run parallel sync (force=False to respect MD5 checks)
            files_start = time.time()
            files_results = sync_all_files_xml(
                db,
                progress_callback=files_progress,
                force=False,
                max_workers=4,
                filter_version=True
            )
            files_elapsed = time.time() - files_start

            # Clear progress lines
            if files_num_lines > 0:
                print(f"\033[{files_num_lines}F", end='', flush=True)
                for _ in range(files_num_lines):
                    print("\033[K", end='')
                    print("\033[1B", end='')
                print(f"\033[{files_num_lines}F", end='', flush=True)

            # Print final results
            for name, result in files_results:
                if result.success:
                    if result.skipped:
                        print(f"  {name}: up-to-date")
                    else:
                        count_str = colors.success(f"{result.file_count:,}") if result.file_count > 0 else "0"
                        print(f"  {name}: {count_str} files")
                else:
                    print(f"  {colors.error(name)}: ERROR - {result.error}")

            # Final summary
            total_files = sum(r.file_count for _, r in files_results if r.success)
            files_errors = sum(1 for _, r in files_results if not r.success)

            if files_errors > 0:
                print(f"\n{colors.info('Total files')}: {colors.success(f'{total_files:,}')} in {format_elapsed(files_elapsed)} ({colors.error(str(files_errors))} errors)")
            else:
                print(f"\n{colors.info('Total files')}: {colors.success(f'{total_files:,}')} in {format_elapsed(files_elapsed)}")

        return 1 if errors else 0


def parse_urpmi_cfg(filepath: str) -> list:
    """Parse urpmi.cfg file and return list of media configurations.

    Returns:
        List of dicts with keys: name, url, enabled, update
    """
    import re

    media_list = []

    with open(filepath, 'r') as f:
        content = f.read()

    # Pattern to match media blocks:
    # Name\ With\ Spaces URL {
    #   options...
    # }
    # The name can have escaped spaces (\ ) and the URL follows
    # URL can be: https://..., http://..., file://..., or /local/path
    pattern = r'([^\s{]+(?:\\ [^\s{]+)*)\s+((?:https?|file)://[^\s{]+|/[^\s{]+)\s*\{([^}]*)\}'

    for match in re.finditer(pattern, content):
        raw_name = match.group(1)
        url_or_path = match.group(2)
        options_block = match.group(3)

        # Normalize local paths to file:// URLs
        if url_or_path.startswith('/') and not url_or_path.startswith('//'):
            url = f'file://{url_or_path}'
        else:
            url = url_or_path

        # Unescape the name (replace '\ ' with ' ')
        name = raw_name.replace('\\ ', ' ')

        # Parse options
        enabled = True
        update = False

        for line in options_block.split('\n'):
            line = line.strip()
            if line == 'ignore':
                enabled = False
            elif line == 'update':
                update = True
            # key-ids is informational, we don't use it currently

        media_list.append({
            'name': name,
            'url': url,
            'enabled': enabled,
            'update': update,
        })

    return media_list


def _import_single_media(db: PackageDatabase, media: dict, colors) -> bool:
    """Import a single media from urpmi.cfg into v8 schema.

    Args:
        db: Database instance
        media: Dict with 'name', 'url', 'enabled', 'update' from parse_urpmi_cfg
        colors: Colors module

    Returns:
        True if successful, False otherwise
    """
    url = media['url']
    name = media['name']
    enabled = media['enabled']
    update = media['update']

    # Parse URL to extract server and media info
    parsed = parse_mageia_media_url(url)

    if not parsed:
        # Fallback to legacy mode for non-Mageia URLs
        db.add_media_legacy(
            name=name,
            url=url,
            enabled=enabled,
            update=update
        )
        return True

    # Extract parsed values
    protocol = parsed['protocol']
    host = parsed['host']
    base_path = parsed['base_path']
    relative_path = parsed['relative_path']
    version = parsed['version']
    arch = parsed['arch']
    short_name = parsed['short_name']
    is_official = parsed['is_official']

    # --- Server upsert ---
    server = db.get_server_by_location(protocol, host, base_path)

    if not server:
        # Create new server
        server_name = _generate_server_name(protocol, host)
        # Make server name unique if needed
        base_server_name = server_name
        counter = 1
        while True:
            try:
                server_id = db.add_server(
                    name=server_name,
                    protocol=protocol,
                    host=host,
                    base_path=base_path,
                    is_official=is_official,
                    enabled=True,
                    priority=50
                )
                server = {'id': server_id, 'name': server_name}
                break
            except Exception as e:
                if 'UNIQUE constraint' in str(e) and 'name' in str(e):
                    counter += 1
                    server_name = f"{base_server_name}-{counter}"
                else:
                    raise

    # --- Media upsert ---
    existing_media = db.get_media_by_version_arch_shortname(version, arch, short_name)

    if not existing_media:
        # Create new media with the name from urpmi.cfg (preserves user's naming)
        media_id = db.add_media(
            name=name,  # Use original name from urpmi.cfg
            short_name=short_name,
            mageia_version=version,
            architecture=arch,
            relative_path=relative_path,
            is_official=is_official,
            allow_unsigned=False,
            enabled=enabled,
            update_media=update,
            priority=50,
            url=None
        )
        existing_media = {'id': media_id, 'name': name}
    else:
        media_id = existing_media['id']

    # --- Link server to media ---
    if not db.server_media_link_exists(server['id'], existing_media['id']):
        db.link_server_media(server['id'], existing_media['id'])

    return True


def cmd_media_import(args, db: PackageDatabase) -> int:
    """Handle media import command - import from urpmi.cfg."""
    from . import colors
    import os

    filepath = args.file

    if not os.path.exists(filepath):
        print(colors.error(f"File not found: {filepath}"))
        return 1

    try:
        media_list = parse_urpmi_cfg(filepath)
    except Exception as e:
        print(colors.error(f"Failed to parse {filepath}: {e}"))
        return 1

    if not media_list:
        print(colors.warning("No media found in file"))
        return 0

    # Get existing media names
    existing = {m['name'].lower(): m['name'] for m in db.list_media()}

    # Categorize media
    to_add = []
    to_skip = []
    to_replace = []

    for media in media_list:
        if media['name'].lower() in existing:
            if args.replace:
                to_replace.append(media)
            else:
                to_skip.append(media)
        else:
            to_add.append(media)

    # Show summary
    print(f"\n{colors.bold('Import from:')} {filepath}")
    print(f"  Found: {len(media_list)} media")

    if to_add:
        print(f"\n  {colors.success('To add:')} {len(to_add)}")
        for m in to_add:
            status = ""
            if not m['enabled']:
                status = " (disabled)"
            if m['update']:
                status += " [update]"
            print(f"    {m['name']}{status}")

    if to_replace:
        print(f"\n  {colors.warning('To replace:')} {len(to_replace)}")
        for m in to_replace:
            print(f"    {m['name']}")

    if to_skip:
        print(f"\n  {colors.info('Skipped (already exist):')} {len(to_skip)}")
        for m in to_skip:
            print(f"    {m['name']}")

    if not to_add and not to_replace:
        print(colors.info("\nNothing to import"))
        return 0

    # Confirmation
    if not args.auto:
        try:
            response = input(f"\nImport {len(to_add) + len(to_replace)} media? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    # Import media
    added = 0
    replaced = 0
    errors = 0

    for media in to_replace:
        try:
            # Remove existing first
            orig_name = existing[media['name'].lower()]
            db.remove_media(orig_name)
            _import_single_media(db, media, colors)
            replaced += 1
            print(f"  {colors.warning('Replaced:')} {media['name']}")
        except Exception as e:
            print(f"  {colors.error('Error:')} {media['name']}: {e}")
            errors += 1

    for media in to_add:
        try:
            _import_single_media(db, media, colors)
            added += 1
            print(f"  {colors.success('Added:')} {media['name']}")
        except Exception as e:
            print(f"  {colors.error('Error:')} {media['name']}: {e}")
            errors += 1

    print(f"\n{colors.bold('Summary:')} {added} added, {replaced} replaced, {errors} errors")

    if added + replaced > 0:
        print(colors.info("\nRun 'urpm media update' to fetch package lists"))

    return 1 if errors else 0


def cmd_media_set(args, db: PackageDatabase) -> int:
    """Handle media set command - modify media settings."""
    from . import colors
    from datetime import datetime

    # Handle --all option for sync_files
    use_all = getattr(args, 'all', False)
    sync_files = getattr(args, 'sync_files', None)

    if use_all:
        # --all only works with --sync-files / --no-sync-files for now
        if sync_files is None:
            print(colors.error("--all requires --sync-files or --no-sync-files"))
            return 1

        count = db.set_all_media_sync_files(sync_files, enabled_only=True)
        status = "enabled" if sync_files else "disabled"
        print(colors.success(f"sync_files {status} on {count} media"))
        return 0

    # Normal mode: require media name
    if not args.name:
        print(colors.error("Media name required (or use --all with --sync-files)"))
        return 1

    media = db.get_media(args.name)
    if not media:
        print(colors.error(f"Media '{args.name}' not found"))
        return 1

    changes = []

    # Parse and apply changes
    shared = None
    if args.shared:
        shared = args.shared == 'yes'
        changes.append(f"shared: {'yes' if shared else 'no'}")

    replication_policy = None
    replication_seeds = None
    if args.replication:
        if args.replication in ('none', 'on_demand', 'seed'):
            replication_policy = args.replication
            changes.append(f"replication: {replication_policy}")
        else:
            print(colors.error(f"Invalid replication policy: {args.replication}"))
            print("Valid values: none, on_demand, seed")
            return 1

    if hasattr(args, 'seeds') and args.seeds:
        # Parse comma-separated sections
        replication_seeds = [s.strip() for s in args.seeds.split(',')]
        changes.append(f"seeds: {', '.join(replication_seeds)}")

    quota_mb = None
    if args.quota:
        # Parse size like 5G, 500M
        size_str = args.quota.upper()
        try:
            if size_str.endswith('G'):
                quota_mb = int(float(size_str[:-1]) * 1024)
            elif size_str.endswith('M'):
                quota_mb = int(float(size_str[:-1]))
            elif size_str.endswith('K'):
                quota_mb = max(1, int(float(size_str[:-1]) / 1024))
            else:
                quota_mb = int(size_str)
            changes.append(f"quota: {quota_mb} MB")
        except ValueError:
            print(colors.error(f"Invalid size format: {args.quota}"))
            return 1

    retention_days = args.retention
    if retention_days is not None:
        changes.append(f"retention: {retention_days} days")

    priority = args.priority
    if priority is not None:
        changes.append(f"priority: {priority}")

    # Handle sync_files option
    sync_files = None
    if getattr(args, 'sync_files', None) is not None:
        sync_files = args.sync_files
        changes.append(f"sync_files: {'yes' if sync_files else 'no'}")

    if not changes:
        print(colors.warning("No changes specified"))
        print("Use --shared, --replication, --seeds, --quota, --retention, --priority, --sync-files, or --no-sync-files")
        return 1

    # Apply mirror settings
    if any([shared is not None, replication_policy, replication_seeds is not None,
            quota_mb is not None, retention_days is not None]):
        db.update_media_mirror_settings(
            media['id'],
            shared=shared,
            replication_policy=replication_policy,
            replication_seeds=replication_seeds,
            quota_mb=quota_mb,
            retention_days=retention_days
        )

    # Apply priority separately (it's in the base media table)
    if priority is not None:
        db.conn.execute(
            "UPDATE media SET priority = ? WHERE id = ?",
            (priority, media['id'])
        )
        db.conn.commit()

    # Apply sync_files
    if sync_files is not None:
        db.set_media_sync_files(args.name, sync_files)

    print(colors.success(f"Updated '{args.name}':"))
    for change in changes:
        print(f"  - {change}")

    return 0


def cmd_media_seed_info(args, db: PackageDatabase) -> int:
    """Show seed set info for a media."""
    from . import colors
    import json
    from pathlib import Path
    from ..core.rpmsrate import RpmsrateParser, DEFAULT_RPMSRATE_PATH

    media = db.get_media(args.name)
    if not media:
        print(colors.error(f"Media '{args.name}' not found"))
        return 1

    policy = media.get('replication_policy', 'on_demand')
    if policy != 'seed':
        print(colors.warning(f"Media '{args.name}' has replication_policy='{policy}', not 'seed'"))
        print("Use: urpm media set <name> --replication=seed --seeds=INSTALL,CAT_PLASMA5,...")
        return 1

    # Default sections (same as DVD content)
    DEFAULT_SEED_SECTIONS = [
        'INSTALL',
        # Desktop environments
        'CAT_PLASMA5', 'CAT_GNOME', 'CAT_XFCE', 'CAT_MATE', 'CAT_LXDE', 'CAT_LXQT',
        'CAT_X', 'CAT_GRAPHICAL_DESKTOP',
        # Core system
        'CAT_SYSTEM', 'CAT_ARCHIVING', 'CAT_FILE_TOOLS', 'CAT_TERMINALS',
        'CAT_EDITORS', 'CAT_MINIMAL_DOCS', 'CAT_CONFIG',
        # Multimedia
        'CAT_AUDIO', 'CAT_VIDEO', 'SOUND', 'BURNER', 'SCANNER', 'PHOTO',
        # Applications
        'CAT_OFFICE', 'CAT_GRAPHICS', 'CAT_GAMES',
        # Network
        'CAT_NETWORKING_WWW', 'CAT_NETWORKING_WWW_SERVER',
        'CAT_NETWORKING_FILE', 'CAT_NETWORKING_REMOTE_ACCESS',
        'CAT_NETWORKING_MAIL', 'CAT_NETWORKING_IRC',
        # Development
        'CAT_DEVELOPMENT',
        # Other
        'CAT_PRINTER', 'CAT_ACCESSIBILITY', 'CAT_SPELLCHECK', 'CAT_MONITORING',
    ]

    # Parse seeds
    seeds_json = media.get('replication_seeds')
    if seeds_json:
        try:
            sections = json.loads(seeds_json)
        except json.JSONDecodeError:
            print(colors.error("Invalid replication_seeds JSON in database"))
            return 1
    else:
        sections = DEFAULT_SEED_SECTIONS

    print(f"Media: {colors.bold(args.name)}")
    print(f"Sections: {', '.join(sections)}")

    # Check rpmsrate-raw
    if not DEFAULT_RPMSRATE_PATH.exists():
        print(colors.warning(f"\nrpmsrate-raw not found at {DEFAULT_RPMSRATE_PATH}"))
        print("Install the meta-task package to enable seed-based replication")
        return 1

    # Parse rpmsrate
    try:
        parser = RpmsrateParser(DEFAULT_RPMSRATE_PATH)
        parser.parse()
    except Exception as e:
        print(colors.error(f"Error parsing rpmsrate-raw: {e}"))
        return 1

    # Get active categories
    active_categories = [s for s in sections if s.startswith('CAT_')]

    # Get seed packages
    seed_packages = parser.get_packages(
        sections=sections,
        active_categories=active_categories,
        ignore_conditions=['DRIVER', 'HW', 'HW_CAT'],
        min_priority=4
    )

    print(f"\nPackages from rpmsrate: {colors.count(len(seed_packages))}")

    # Count how many are in this media
    all_packages = db.get_packages_for_media(media['id'])
    media_pkg_names = {p['name'] for p in all_packages}
    matching = seed_packages & media_pkg_names

    print(f"Matching in this media: {colors.count(len(matching))}")

    # Note: 'size' is installed size, not RPM download size (typically ~3x smaller)
    seed_size = sum(p.get('size', 0) or 0 for p in all_packages if p['name'] in seed_packages)
    print(f"Installed size (seeds only): {colors.bold(f'{seed_size / 1024 / 1024 / 1024:.1f}')} GB")

    # Collect dependencies (not resolve - we want all packages for replication, conflicts OK)
    missing_seeds = seed_packages - media_pkg_names
    if missing_seeds:
        print(colors.dim(f"  ({len(missing_seeds)} seeds not in media: {', '.join(sorted(missing_seeds)[:5])}...)"))

    print(colors.dim("\nCollecting dependencies..."))
    try:
        # Use collect_dependencies which ignores conflicts (for DVD/mirror replication)
        result = db.collect_dependencies(seed_packages)

        full_set = result['packages']
        not_found = result['not_found']
        total_size = result['total_size']

        print(f"With dependencies: {colors.count(len(full_set))} packages")
        est_download = total_size / 3
        print(f"Estimated download: ~{colors.bold(f'{est_download / 1024 / 1024 / 1024:.1f}')} GB (installed: {total_size / 1024 / 1024 / 1024:.1f} GB)")

        # Show breakdown
        deps_only = full_set - seed_packages
        print(f"  - Seeds: {len(seed_packages & full_set)}, Dependencies: {len(deps_only)}")

        if not_found:
            print(colors.dim(f"  - Not found: {len(not_found)} ({', '.join(sorted(not_found)[:5])}...)"))

    except Exception as e:
        print(colors.warning(f"Dependency collection failed: {e}"))
        import traceback
        traceback.print_exc()

    # Show some examples
    if matching:
        print(f"\nExample seed packages: {', '.join(sorted(matching)[:10])}...")

    return 0


def cmd_media_link(args, db: PackageDatabase) -> int:
    """Handle media link command - link/unlink servers to a media."""
    from . import colors
    from ..core.config import build_server_url
    import urllib.request
    from pathlib import Path

    # Find media
    media = db.get_media(args.name)
    if not media:
        print(colors.error(f"Media '{args.name}' not found"))
        return 1

    media_id = media['id']
    relative_path = media.get('relative_path', '')
    added = []
    removed = []
    skipped = []
    errors = []

    # Get all servers for +all/-all
    all_servers = db.list_servers()

    def check_server_has_media(server: dict) -> bool:
        """Check if server has this media available."""
        if not relative_path:
            return True  # Can't check without relative_path

        if server['protocol'] == 'file':
            # Local filesystem check
            md5_path = Path(server['base_path']) / relative_path / "media_info" / "MD5SUM"
            return md5_path.exists()
        else:
            # Remote check via HEAD request
            base_url = build_server_url(server)
            url = f"{base_url}/{relative_path}/media_info/MD5SUM"
            try:
                req = urllib.request.Request(url, method='HEAD')
                urllib.request.urlopen(req, timeout=5)
                return True
            except:
                return False

    def try_add_server(server: dict) -> bool:
        """Try to add a server, returns True if added."""
        if db.server_media_link_exists(server['id'], media_id):
            return False  # Already linked

        if not check_server_has_media(server):
            skipped.append(server['name'])
            return False

        db.link_server_media(server['id'], media_id)
        added.append(server['name'])
        return True

    for change in args.changes:
        if change == '+all':
            # Link all servers that have the media
            print(f"Checking {len(all_servers)} servers...", flush=True)
            for server in all_servers:
                try_add_server(server)

        elif change == '-all':
            # Unlink all servers
            for server in all_servers:
                if db.server_media_link_exists(server['id'], media_id):
                    db.unlink_server_media(server['id'], media_id)
                    removed.append(server['name'])

        elif change.startswith('+'):
            server_name = change[1:]
            server = db.get_server(server_name)
            if not server:
                errors.append(f"Server '{server_name}' not found")
                continue
            if db.server_media_link_exists(server['id'], media_id):
                errors.append(f"Server '{server_name}' already linked")
                continue
            if not check_server_has_media(server):
                skipped.append(server_name)
                continue
            db.link_server_media(server['id'], media_id)
            added.append(server_name)

        elif change.startswith('-'):
            server_name = change[1:]
            server = db.get_server(server_name)
            if not server:
                errors.append(f"Server '{server_name}' not found")
                continue
            if not db.server_media_link_exists(server['id'], media_id):
                errors.append(f"Server '{server_name}' not linked")
                continue
            db.unlink_server_media(server['id'], media_id)
            removed.append(server_name)

        else:
            errors.append(f"Invalid change '{change}' - use +server or -server")

    # Report results
    if added:
        print(colors.success(f"Added: {', '.join(added)}"))
    if removed:
        print(f"Removed: {', '.join(removed)}")
    if skipped:
        print(colors.warning(f"Skipped (media not available): {', '.join(skipped)}"))
    if errors:
        for err in errors:
            print(colors.error(err))
        return 1

    # Show current servers
    servers = db.get_servers_for_media(media_id, enabled_only=False)
    if servers:
        print(f"\nServers for '{args.name}':")
        for s in servers:
            status = colors.success("[x]") if s['enabled'] else colors.dim("[ ]")
            print(f"  {status} {s['name']} (priority: {s['priority']})")
    else:
        print(colors.dim(f"\nNo servers linked to '{args.name}'"))

    return 0


def cmd_media_autoconfig(args, db: PackageDatabase) -> int:
    """Handle media autoconfig command - auto-add official Mageia media for a release."""
    from . import colors
    from urllib.request import urlopen, Request
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import platform
    import time
    import re

    # Get release and arch
    release = args.release
    arch = getattr(args, 'arch', None) or platform.machine()
    dry_run = getattr(args, 'dry_run', False)
    no_nonfree = getattr(args, 'no_nonfree', False)
    no_tainted = getattr(args, 'no_tainted', False)

    print(f"Auto-configuring media for Mageia {release} ({arch})")

    # Define media types to add
    # Format: (type, repo, name_suffix)
    media_types = [
        ('core', 'release', 'Core Release'),
        ('core', 'updates', 'Core Updates'),
    ]
    if not no_nonfree:
        media_types.extend([
            ('nonfree', 'release', 'Nonfree Release'),
            ('nonfree', 'updates', 'Nonfree Updates'),
        ])
    if not no_tainted:
        media_types.extend([
            ('tainted', 'release', 'Tainted Release'),
            ('tainted', 'updates', 'Tainted Updates'),
        ])

    # Fetch mirrorlist to get a good server
    # Format: key=value,key=value,...,url=<url>
    mirrorlist_url = f"https://mirrors.mageia.org/api/mageia.{release}.{arch}.list"
    print(f"Fetching mirrorlist from {mirrorlist_url}...", end=' ', flush=True)

    try:
        req = Request(mirrorlist_url)
        req.add_header('User-Agent', 'urpm-ng')
        with urlopen(req, timeout=30) as response:
            content = response.read().decode('utf-8').strip()
            lines = [line.strip() for line in content.split('\n') if line.strip()]
    except (URLError, HTTPError) as e:
        print(colors.error(f"failed: {e}"))
        return 1

    if not lines:
        print(colors.warning("empty mirrorlist"))
        return 1

    # Parse mirrorlist format: continent=XX,zone=XX,...,url=<url>
    mirror_urls = []
    for line in lines:
        # Extract url= field
        url_match = re.search(r'url=(.+)$', line)
        if url_match:
            url = url_match.group(1)
            # Only keep http/https
            if url.startswith('http://') or url.startswith('https://'):
                mirror_urls.append(url)

    if not mirror_urls:
        print(colors.warning("no http/https mirrors found"))
        return 1

    print(f"{len(mirror_urls)} http(s) mirrors")

    # Test a few mirrors to find a fast one
    print("Testing mirror latency...", end=' ', flush=True)

    def test_mirror(url):
        """Test mirror latency by fetching a small file."""
        try:
            # URL is like: https://host/path/distrib/<release>/<arch>
            # Append /media/core/release/ and test with HEAD
            test_url = url.rstrip('/') + '/media/core/release/'
            req = Request(test_url, method='HEAD')
            req.add_header('User-Agent', 'urpm-ng')
            start = time.time()
            with urlopen(req, timeout=5) as response:
                latency = time.time() - start
                return (latency, url)
        except Exception:
            return (float('inf'), url)

    # Test first 15 mirrors (prefer https)
    https_mirrors = [u for u in mirror_urls if u.startswith('https://')]
    http_mirrors = [u for u in mirror_urls if u.startswith('http://') and not u.startswith('https://')]
    test_urls = (https_mirrors[:10] + http_mirrors[:5])[:15]

    latencies = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(test_mirror, url): url for url in test_urls}
        for future in as_completed(futures):
            result = future.result()
            if result[0] < float('inf'):
                latencies.append(result)

    if not latencies:
        print(colors.warning("all mirrors unreachable"))
        return 1

    # Sort by latency and pick top 3
    latencies.sort(key=lambda x: x[0])
    best_mirrors = latencies[:3]
    print(f"best: {best_mirrors[0][0]*1000:.0f}ms")

    # Extract base URL from mirror URL
    # Mirror URL format: https://mirror.example.com/path/distrib/<release>/<arch>
    # We need: https://mirror.example.com/path/distrib/
    def extract_base_url(mirror_url, release, arch):
        """Extract base URL from distrib URL."""
        # Pattern to match and remove: /<release>/<arch> at the end
        pattern = rf'/{re.escape(str(release))}/{re.escape(arch)}/?$'
        base = re.sub(pattern, '', mirror_url).rstrip('/')
        return base

    # Check existing media to avoid duplicates
    existing_media = db.list_media()
    existing_names = {m['name'] for m in existing_media}

    # Add media
    added = 0
    skipped = 0

    # First, add servers from best mirrors
    server_to_use = None
    for latency, mirror_url in best_mirrors[:1]:  # Just use the best one
        base_url = extract_base_url(mirror_url, release, arch)
        parsed = urlparse(base_url)
        server_name = parsed.hostname

        # Check if server already exists
        existing_server = db.get_server(server_name)
        if existing_server:
            server_to_use = existing_server
        elif not dry_run:
            db.add_server(
                name=server_name,
                protocol=parsed.scheme,
                host=parsed.hostname,
                base_path=parsed.path
            )
            print(f"  Added server: {server_name}")
            server_to_use = db.get_server(server_name)
        else:
            print(f"  Would add server: {server_name} ({base_url})")

    # Add each media type
    for media_type, repo, name_suffix in media_types:
        # Media name: e.g., "mga10-core-release" or "mga10-x86_64-core-release" for non-host arch
        if arch == platform.machine():
            media_name = f"mga{release}-{media_type}-{repo}"
        else:
            media_name = f"mga{release}-{arch}-{media_type}-{repo}"

        if media_name in existing_names:
            print(f"  Skipping {media_name} (already exists)")
            skipped += 1
            continue

        # Relative path for this media: <release>/<arch>/media/<type>/<repo>/
        relative_path = f"{release}/{arch}/media/{media_type}/{repo}"

        if dry_run:
            print(f"  Would add media: {media_name} -> {relative_path}")
        else:
            # Add the media
            is_update = (repo == 'updates')
            db.add_media(
                name=media_name,
                short_name=media_name,  # Already filesystem-safe
                mageia_version=str(release),
                architecture=arch,
                relative_path=relative_path,
                is_official=True,
                update_media=is_update
            )
            print(f"  Added media: {media_name}")

            # Link media to all enabled servers
            media = db.get_media(media_name)
            if media:
                for server in db.list_servers(enabled_only=True):
                    db.link_server_media(server['id'], media['id'])

        added += 1

    # Summary
    print()
    if dry_run:
        print(colors.warning(f"Dry run: would add {added} media, {skipped} already exist"))
    else:
        print(colors.success(f"Added {added} media, {skipped} already existed"))
        if added > 0:
            print(colors.dim("Run 'urpm media update' to sync metadata"))

    return 0


# =============================================================================
# Server commands
# =============================================================================

def cmd_server_list(args, db: PackageDatabase) -> int:
    """Handle server list command."""
    from . import colors

    show_all = getattr(args, 'all', False)
    servers = db.list_servers(enabled_only=not show_all)

    if not servers:
        print(colors.info("No servers configured"))
        return 0

    # Calculate column widths dynamically (no truncation)
    name_width = max(4, max(len(srv['name']) for srv in servers))
    host_width = max(4, max(len(srv['host']) for srv in servers))

    # Header
    print(f"\n{'Name':<{name_width}} {'Protocol':<8} {'Host':<{host_width}} {'Pri':>4} {'IP':>6} {'Status':<8}")
    print("-" * (name_width + host_width + 35))

    for srv in servers:
        status = colors.success("enabled") if srv['enabled'] else colors.dim("disabled")
        ip_mode = srv.get('ip_mode', 'auto')
        # Pad first, then colorize (ANSI codes break alignment)
        ip_padded = f"{ip_mode:>6}"
        if ip_mode == 'dual':
            ip_str = colors.success(ip_padded)
        elif ip_mode == 'ipv6':
            ip_str = colors.info(ip_padded)
        elif ip_mode == 'auto':
            ip_str = colors.dim(ip_padded)
        else:
            ip_str = ip_padded

        print(f"{srv['name']:<{name_width}} {srv['protocol']:<8} {srv['host']:<{host_width}} {srv['priority']:>4} {ip_str} {status}")

    print()
    return 0


def cmd_server_add(args, db: PackageDatabase) -> int:
    """Handle server add command."""
    from . import colors
    from urllib.parse import urlparse
    from ..core.config import test_server_ip_connectivity, build_server_url
    import urllib.request
    import socket

    url = args.url.rstrip('/')
    parsed = urlparse(url)

    if parsed.scheme not in ('http', 'https', 'file'):
        print(colors.error(f"Invalid protocol: {parsed.scheme}"))
        print("Supported protocols: http, https, file")
        return 1

    protocol = parsed.scheme
    host = parsed.netloc or 'localhost'
    base_path = parsed.path

    # Check if server already exists
    existing = db.get_server_by_location(protocol, host, base_path)
    if existing:
        print(colors.warning(f"Server already exists: {existing['name']}"))
        return 1

    # Check if name is taken
    if db.get_server(args.name):
        print(colors.error(f"Server name already exists: {args.name}"))
        return 1

    # Test IP connectivity for remote servers
    ip_mode = 'auto'
    if protocol in ('http', 'https'):
        port = 443 if protocol == 'https' else 80
        print(f"Testing connectivity to {host}...")
        ip_mode = test_server_ip_connectivity(host, port, timeout=5.0)
        print(f"  IP mode: {ip_mode}")

    # Add server
    is_official = not args.custom
    enabled = not args.disabled
    priority = args.priority

    try:
        server_id = db.add_server(
            name=args.name,
            protocol=protocol,
            host=host,
            base_path=base_path,
            is_official=is_official,
            enabled=enabled,
            priority=priority
        )
        # Set detected ip_mode
        db.set_server_ip_mode_by_id(server_id, ip_mode)

        print(colors.success(f"Added server: {args.name}"))
        print(f"  URL: {url}")
        print(f"  Priority: {priority}")
        print(f"  IP mode: {ip_mode}")
        if not enabled:
            print(colors.dim("  Status: disabled"))
    except Exception as e:
        print(colors.error(f"Failed to add server: {e}"))
        return 1

    # Scan existing media to see which ones this server provides
    media_list = db.list_media()
    if not media_list:
        return 0

    # Filter media with relative_path
    media_to_scan = [(m['id'], m['name'], m.get('relative_path', ''))
                     for m in media_list if m.get('relative_path')]

    if not media_to_scan:
        return 0

    print(f"\nScanning {len(media_to_scan)} media...", end=' ', flush=True)

    # Build base URL
    server = {'protocol': protocol, 'host': host, 'base_path': base_path}
    base_url = build_server_url(server)

    if protocol == 'file':
        # Local filesystem - fast sequential check
        from pathlib import Path
        found = []
        for media_id, media_name, relative_path in media_to_scan:
            md5_path = Path(base_path) / relative_path / "media_info" / "MD5SUM"
            if md5_path.exists():
                found.append((media_id, media_name))
    else:
        # Remote - parallel HEAD requests with ip_mode
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..core.config import get_socket_family_for_ip_mode

        family = get_socket_family_for_ip_mode(ip_mode)

        def check_media(media_id, media_name, relative_path):
            test_url = f"{base_url}/{relative_path}/media_info/MD5SUM"
            try:
                # Patch getaddrinfo for this thread if needed
                original_getaddrinfo = None
                if family != 0:
                    original_getaddrinfo = socket.getaddrinfo
                    def patched(host, port, fam=0, type=0, proto=0, flags=0):
                        if fam == 0:
                            fam = family
                        return original_getaddrinfo(host, port, fam, type, proto, flags)
                    socket.getaddrinfo = patched

                try:
                    req = urllib.request.Request(test_url, method='HEAD')
                    urllib.request.urlopen(req, timeout=3)
                    return (media_id, media_name)
                finally:
                    if original_getaddrinfo:
                        socket.getaddrinfo = original_getaddrinfo
            except Exception:
                return None

        found = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_media, mid, mname, rpath): mname
                      for mid, mname, rpath in media_to_scan}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    found.append(result)

    # Link found media
    for media_id, media_name in found:
        db.link_server_media(server_id, media_id)

    print(f"{len(found)} found")
    if found:
        for _, media_name in sorted(found, key=lambda x: x[1]):
            print(f"  {colors.success('+')} {media_name}")
    else:
        print(colors.warning(f"No existing media found on this server"))

    return 0


def cmd_server_remove(args, db: PackageDatabase) -> int:
    """Handle server remove command."""
    from . import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(f"Server not found: {args.name}"))
        return 1

    db.remove_server(args.name)
    print(colors.success(f"Removed server: {args.name}"))
    return 0


def cmd_server_enable(args, db: PackageDatabase) -> int:
    """Handle server enable command."""
    from . import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(f"Server not found: {args.name}"))
        return 1

    if server['enabled']:
        print(colors.info(f"Server already enabled: {args.name}"))
        return 0

    db.enable_server(args.name, True)
    print(colors.success(f"Enabled server: {args.name}"))
    return 0


def cmd_server_disable(args, db: PackageDatabase) -> int:
    """Handle server disable command."""
    from . import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(f"Server not found: {args.name}"))
        return 1

    if not server['enabled']:
        print(colors.info(f"Server already disabled: {args.name}"))
        return 0

    db.enable_server(args.name, False)
    print(colors.success(f"Disabled server: {args.name}"))
    return 0


def cmd_server_priority(args, db: PackageDatabase) -> int:
    """Handle server priority command."""
    from . import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(f"Server not found: {args.name}"))
        return 1

    db.set_server_priority(args.name, args.priority)
    print(colors.success(f"Set priority for {args.name}: {args.priority}"))
    return 0


def cmd_server_test(args, db: PackageDatabase) -> int:
    """Handle server test command - test connectivity and detect IP mode."""
    from . import colors
    from ..core.config import test_server_ip_connectivity

    if args.name:
        # Test specific server
        server = db.get_server(args.name)
        if not server:
            print(colors.error(f"Server not found: {args.name}"))
            return 1
        servers = [server]
    else:
        # Test all enabled servers
        servers = db.list_servers(enabled_only=True)

    if not servers:
        print(colors.info("No servers to test"))
        return 0

    errors = 0
    for srv in servers:
        if srv['protocol'] == 'file':
            print(f"{srv['name']}: local filesystem (skipped)")
            continue

        host = srv['host']
        port = 443 if srv['protocol'] == 'https' else 80
        print(f"Testing {srv['name']} ({host})...", end=' ', flush=True)

        old_mode = srv.get('ip_mode', 'auto')
        new_mode = test_server_ip_connectivity(host, port, timeout=5.0)

        if new_mode == 'auto':
            # Could not test
            print(colors.warning(f"unreachable (keeping {old_mode})"))
            errors += 1
        elif new_mode != old_mode:
            db.set_server_ip_mode(srv['name'], new_mode)
            print(colors.success(f"{new_mode} (was {old_mode})"))
        else:
            print(f"{new_mode}")

    return 1 if errors else 0


def cmd_server_ipmode(args, db: PackageDatabase) -> int:
    """Handle server ip-mode command - manually set IP mode."""
    from . import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(f"Server not found: {args.name}"))
        return 1

    old_mode = server.get('ip_mode', 'auto')
    db.set_server_ip_mode(args.name, args.mode)
    print(colors.success(f"Set IP mode for {args.name}: {args.mode} (was {old_mode})"))
    return 0


def cmd_server_autoconfig(args, db: PackageDatabase) -> int:
    """Handle server autoconfig command - auto-discover servers from Mageia mirrorlist."""
    from . import colors
    from urllib.request import urlopen, Request
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import re
    import time

    TARGET_SERVERS = 5  # Target number of enabled servers

    # Get system version and arch
    version = getattr(args, 'release', None)
    if not version:
        try:
            with open('/etc/os-release') as f:
                for line in f:
                    if line.startswith('VERSION_ID='):
                        version = line.strip().split('=')[1].strip('"')
                        break
        except (IOError, OSError):
            pass

    if not version:
        print(colors.error("Cannot detect Mageia version from /etc/os-release"))
        print(colors.dim("Use --release to specify manually (e.g., --release 9)"))
        return 1

    import platform
    arch = platform.machine()

    # Count existing enabled servers
    existing_servers = db.list_servers(enabled_only=True)
    existing_count = len(existing_servers)

    if existing_count >= TARGET_SERVERS:
        print(f"Already have {existing_count} enabled servers (target: {TARGET_SERVERS})")
        print(colors.dim("Use 'urpm server remove' to remove some first if needed."))
        return 0

    needed = TARGET_SERVERS - existing_count
    print(f"Have {existing_count} enabled servers, need {needed} more to reach {TARGET_SERVERS}")

    # Get all servers for duplicate check
    all_servers = db.list_servers()
    existing_urls = set()
    existing_names = set()
    for s in all_servers:
        url = f"{s['protocol']}://{s['host']}{s.get('base_path', '')}".rstrip('/')
        existing_urls.add(url)
        existing_names.add(s['name'])

    # Fetch mirrorlist
    mirrorlist_url = f"https://www.mageia.org/mirrorlist/?release={version}&arch={arch}&section=core&repo=release"

    print(f"Fetching mirrorlist for Mageia {version} ({arch})...", end=' ', flush=True)

    try:
        with urlopen(mirrorlist_url, timeout=60) as response:
            content = response.read().decode('utf-8').strip()
            mirror_urls = content.split('\n') if content else []
    except (URLError, HTTPError) as e:
        print(colors.error(f"failed: {e}"))
        return 1

    if not mirror_urls or not any(u.strip() for u in mirror_urls):
        print(colors.warning("empty"))
        print(colors.dim("The mirrorlist may not be available yet for this version."))
        return 0

    print(f"{len(mirror_urls)} mirrors")

    # Pattern to strip from URLs: {version}/{arch}/media/core/release/
    suffix_pattern = re.compile(rf'{re.escape(version)}/{re.escape(arch)}/media/core/release/?$')

    # Parse and filter candidates
    candidates = []
    skipped_protocol = 0
    skipped_duplicate = 0

    for url in mirror_urls:
        url = url.strip()
        if not url:
            continue

        parsed = urlparse(url)

        # Filter: only http/https
        if parsed.scheme not in ('http', 'https'):
            skipped_protocol += 1
            continue

        # Extract base path by stripping the suffix
        base_path = suffix_pattern.sub('', parsed.path).rstrip('/')
        full_base = f"{parsed.scheme}://{parsed.hostname}{base_path}"

        # Check for duplicate
        if full_base in existing_urls:
            skipped_duplicate += 1
            continue

        candidates.append({
            'scheme': parsed.scheme,
            'host': parsed.hostname,
            'base_path': base_path,
            'full_url': url,  # Original URL for latency test
        })

    if not candidates:
        print("No new servers to add")
        if skipped_duplicate:
            print(colors.dim(f"  ({skipped_duplicate} already configured)"))
        return 0

    print(f"Testing latency to {len(candidates)} candidates...", end=' ', flush=True)

    # Test latency to each candidate in parallel
    def test_latency(candidate):
        """Test latency with HEAD request, return (candidate, latency_ms) or (candidate, None)."""
        test_url = candidate['full_url']
        try:
            start = time.time()
            req = Request(test_url, method='HEAD')
            with urlopen(req, timeout=5) as resp:
                latency = (time.time() - start) * 1000
                return (candidate, latency)
        except Exception:
            return (candidate, None)

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(test_latency, c): c for c in candidates}
        for future in as_completed(futures):
            candidate, latency = future.result()
            if latency is not None:
                results.append((candidate, latency))

    print(f"{len(results)} reachable")

    if not results:
        print(colors.warning("No reachable mirrors found"))
        return 0

    # Sort by latency and take the best N
    results.sort(key=lambda x: x[1])
    best = results[:needed]

    if args.dry_run:
        print(f"\nWould add {len(best)} server(s):")
        for candidate, latency in best:
            print(f"  {candidate['host']} ({latency:.0f}ms)")
        return 0

    # Add best servers
    added_servers = []
    for candidate, latency in best:
        shortname = candidate['host']
        # Ensure unique name
        original = shortname
        counter = 1
        while shortname in existing_names:
            shortname = f"{original}-{counter}"
            counter += 1

        try:
            server_id = db.add_server(
                shortname, candidate['scheme'], candidate['host'], candidate['base_path']
            )
            print(colors.success(f"  Added: {shortname} ({latency:.0f}ms)"))
            existing_names.add(shortname)
            added_servers.append((server_id, shortname))
        except Exception as e:
            print(colors.warning(f"  Failed to add {shortname}: {e}"))

    if not added_servers:
        return 0

    # Scan enabled media to link with new servers
    all_media = db.list_media()
    enabled_media = [m for m in all_media if m.get('enabled', 1)]
    if not enabled_media:
        print("\nNo enabled media to scan")
        return 0

    media_to_scan = [(m['id'], m['name'], m.get('relative_path', ''))
                     for m in enabled_media if m.get('relative_path')]

    if not media_to_scan:
        return 0

    print(f"\nScanning {len(media_to_scan)} enabled media...", end=' ', flush=True)

    # For each new server, check which media it provides
    from ..core.config import build_server_url

    total_links = 0
    for server_id, server_name in added_servers:
        server = db.get_server(server_name)
        base_url = build_server_url(server)

        def check_media(media_id, media_name, relative_path):
            test_url = f"{base_url}/{relative_path}/media_info/MD5SUM"
            try:
                req = Request(test_url, method='HEAD')
                urlopen(req, timeout=3)
                return media_id
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_media, mid, mname, rpath): mid
                      for mid, mname, rpath in media_to_scan}
            for future in as_completed(futures):
                media_id = future.result()
                if media_id:
                    db.link_server_media(server_id, media_id)
                    total_links += 1

    print(f"{total_links} links created")

    # Summary
    print(f"\nAdded {len(added_servers)} server(s), now have {existing_count + len(added_servers)} enabled")
    if skipped_protocol:
        print(colors.dim(f"Skipped {skipped_protocol} (ftp/other protocol)"))

    return 0


# =============================================================================
# Mirror commands
# =============================================================================

def cmd_mirror_status(args, db: PackageDatabase) -> int:
    """Handle mirror status command."""
    from . import colors
    from ..core.cache import CacheManager, format_size

    # Global proxy status
    enabled = db.is_mirror_enabled()
    disabled_versions = db.get_disabled_mirror_versions()
    global_quota = db.get_mirror_config('global_quota_mb')
    rate_limit = db.get_mirror_config('rate_limit_enabled', '1')

    print(colors.bold("\nMirror Status"))
    print("-" * 40)
    print(f"Mirror mode:      {colors.success('enabled') if enabled else colors.dim('disabled')}")
    if disabled_versions:
        print(f"Disabled versions: {', '.join(disabled_versions)}")
    if global_quota:
        print(f"Global quota:     {global_quota} MB")
    print(f"Rate limiting:    {'on' if rate_limit == '1' else colors.warning('off')}")

    # Cache statistics
    cache_mgr = CacheManager(db)
    stats = cache_mgr.get_usage()

    print(colors.bold("\nCache Statistics"))
    print("-" * 40)
    print(f"Total files:      {stats.get('total_files', 0)}")
    print(f"Total size:       {format_size(stats.get('total_size', 0))}")
    print(f"Referenced:       {stats.get('referenced_files', 0)} files ({format_size(stats.get('referenced_size', 0))})")
    print(f"Unreferenced:     {stats.get('unreferenced_files', 0)} files ({format_size(stats.get('unreferenced_size', 0))})")

    if stats.get('quota_bytes'):
        pct = stats.get('quota_used_pct', 0)
        pct_str = f"{pct:.1f}%"
        if pct > 90:
            pct_str = colors.error(pct_str)
        elif pct > 75:
            pct_str = colors.warning(pct_str)
        print(f"Quota used:       {pct_str}")

    # Per-media summary
    print(colors.bold("\nMedia with mirror settings"))
    print("-" * 40)
    media_list = db.list_media()
    has_settings = False
    for m in media_list:
        if m.get('quota_mb') or m.get('replication_policy') != 'on_demand' or not m.get('shared', 1):
            has_settings = True
            shared_str = colors.success('Y') if m.get('shared', 1) else colors.dim('N')
            policy = m.get('replication_policy', 'on_demand')
            quota = f"{m['quota_mb']}M" if m.get('quota_mb') else '-'
            print(f"  {m['name'][:30]:<30} shared={shared_str} repl={policy:<10} quota={quota}")

    if not has_settings:
        print(colors.dim("  (all media using defaults)"))

    return 0


def cmd_mirror_enable(args, db: PackageDatabase) -> int:
    """Handle mirror enable command."""
    from . import colors

    db.set_mirror_config('enabled', '1')
    print(colors.success("Mirror mode enabled"))
    print("This urpmd will now serve packages to peers on the network.")
    return 0


def cmd_mirror_disable(args, db: PackageDatabase) -> int:
    """Handle mirror disable command."""
    from . import colors

    db.set_mirror_config('enabled', '0')
    print(colors.success("Mirror mode disabled"))
    print("This urpmd will no longer serve packages to peers.")
    return 0


def cmd_mirror_quota(args, db: PackageDatabase) -> int:
    """Handle mirror quota command."""
    from . import colors
    from ..core.cache import format_size

    if not args.size:
        # Show current quota
        current = db.get_mirror_config('global_quota_mb')
        if current:
            print(f"Global quota: {current} MB ({format_size(int(current) * 1024 * 1024)})")
        else:
            print("No global quota set")
        return 0

    # Parse and set quota
    size_str = args.size.upper()
    try:
        if size_str.endswith('G'):
            quota_mb = int(float(size_str[:-1]) * 1024)
        elif size_str.endswith('M'):
            quota_mb = int(float(size_str[:-1]))
        elif size_str.endswith('K'):
            quota_mb = max(1, int(float(size_str[:-1]) / 1024))
        else:
            quota_mb = int(size_str)
    except ValueError:
        print(colors.error(f"Invalid size format: {args.size}"))
        return 1

    db.set_mirror_config('global_quota_mb', str(quota_mb))
    print(colors.success(f"Global quota set to {quota_mb} MB ({format_size(quota_mb * 1024 * 1024)})"))
    return 0


def cmd_mirror_disable_version(args, db: PackageDatabase) -> int:
    """Handle mirror disable-version command."""
    from . import colors

    current = db.get_disabled_mirror_versions()
    new_versions = [v.strip() for v in args.versions.split(',') if v.strip()]

    # Merge with existing
    all_disabled = set(current) | set(new_versions)
    db.set_mirror_config('disabled_versions', ','.join(sorted(all_disabled)))

    print(colors.success(f"Disabled mirroring for Mageia version(s): {', '.join(new_versions)}"))
    if current:
        print(f"Previously disabled: {', '.join(current)}")
    print(f"Now disabled: {', '.join(sorted(all_disabled))}")
    return 0


def cmd_mirror_enable_version(args, db: PackageDatabase) -> int:
    """Handle mirror enable-version command."""
    from . import colors

    current = db.get_disabled_mirror_versions()
    to_enable = [v.strip() for v in args.versions.split(',') if v.strip()]

    # Remove from disabled list
    still_disabled = [v for v in current if v not in to_enable]
    db.set_mirror_config('disabled_versions', ','.join(sorted(still_disabled)))

    enabled = [v for v in to_enable if v in current]
    if enabled:
        print(colors.success(f"Re-enabled mirroring for Mageia version(s): {', '.join(enabled)}"))
    else:
        print(colors.warning(f"Version(s) {', '.join(to_enable)} were not disabled"))

    if still_disabled:
        print(f"Still disabled: {', '.join(still_disabled)}")
    return 0


def cmd_mirror_clean(args, db: PackageDatabase) -> int:
    """Handle mirror clean command - enforce quotas and retention."""
    from . import colors
    from ..core.cache import CacheManager, format_size

    cache_mgr = CacheManager(db)
    dry_run = getattr(args, 'dry_run', False)

    if dry_run:
        print(colors.info("Dry run mode - no files will be deleted\n"))

    result = cache_mgr.enforce_quotas(dry_run=dry_run)

    # Report results
    print(colors.bold("Cleanup results:"))
    print(f"  Unreferenced files: {result['unreferenced_deleted']} ({format_size(result['unreferenced_bytes'])})")
    print(f"  Retention policy:   {result['retention_deleted']} ({format_size(result['retention_bytes'])})")
    print(f"  Quota enforcement:  {result['quota_deleted']} ({format_size(result['quota_bytes'])})")
    print(f"  {colors.bold('Total:')}            {result['total_deleted']} ({format_size(result['total_bytes'])})")

    if result['errors']:
        print(colors.warning(f"\n{len(result['errors'])} errors occurred"))

    if dry_run and result['total_deleted'] > 0:
        print(colors.info("\nRun without --dry-run to actually delete files"))

    return 0


def cmd_mirror_sync(args, db: PackageDatabase) -> int:
    """Handle mirror sync command - force sync according to replication policies.

    Unlike the background daemon which waits for idle, this downloads immediately.
    """
    from . import colors
    from ..core.rpmsrate import RpmsrateParser, DEFAULT_RPMSRATE_PATH
    from ..core.download import Downloader, DownloadItem
    from ..core.config import get_media_local_path, build_media_url
    import json

    # Default sections (same as DVD content)
    DEFAULT_SEED_SECTIONS = [
        'INSTALL',
        # Desktop environments
        'CAT_PLASMA5', 'CAT_GNOME', 'CAT_XFCE', 'CAT_MATE', 'CAT_LXDE', 'CAT_LXQT',
        'CAT_X', 'CAT_GRAPHICAL_DESKTOP',
        # Core system
        'CAT_SYSTEM', 'CAT_ARCHIVING', 'CAT_FILE_TOOLS', 'CAT_TERMINALS',
        'CAT_EDITORS', 'CAT_MINIMAL_DOCS', 'CAT_CONFIG',
        # Multimedia
        'CAT_AUDIO', 'CAT_VIDEO', 'SOUND', 'BURNER', 'SCANNER', 'PHOTO',
        # Applications
        'CAT_OFFICE', 'CAT_GRAPHICS', 'CAT_GAMES',
        # Network
        'CAT_NETWORKING_WWW', 'CAT_NETWORKING_WWW_SERVER',
        'CAT_NETWORKING_FILE', 'CAT_NETWORKING_REMOTE_ACCESS',
        'CAT_NETWORKING_MAIL', 'CAT_NETWORKING_IRC',
        # Development
        'CAT_DEVELOPMENT',
        # Other
        'CAT_PRINTER', 'CAT_ACCESSIBILITY', 'CAT_SPELLCHECK', 'CAT_MONITORING',
    ]

    # Find media with replication_policy='seed'
    media_to_replicate = []
    for media in db.list_media():
        if media.get('replication_policy') == 'seed' and media.get('enabled'):
            if args.media and media['name'] != args.media:
                continue
            media_to_replicate.append(media)

    if not media_to_replicate:
        if args.media:
            print(colors.error(f"Media '{args.media}' not found or doesn't have replication_policy='seed'"))
        else:
            print(colors.warning("No media with replication_policy='seed'"))
            print("Use: urpm media set <name> --replication=seed")
        return 1

    print(f"Media to sync: {len(media_to_replicate)}")
    for m in media_to_replicate:
        print(f"  - {m['name']}")

    # Compute seed set
    print(colors.dim("\nComputing seed set..."))

    # Collect all sections from all media
    all_sections = set()
    for media in media_to_replicate:
        seeds_json = media.get('replication_seeds')
        if seeds_json:
            try:
                sections = json.loads(seeds_json)
                all_sections.update(sections)
            except json.JSONDecodeError:
                print(colors.warning(f"Invalid replication_seeds JSON for {media['name']}"))
        else:
            all_sections.update(DEFAULT_SEED_SECTIONS)

    # Parse rpmsrate
    if not DEFAULT_RPMSRATE_PATH.exists():
        print(colors.error(f"rpmsrate-raw not found at {DEFAULT_RPMSRATE_PATH}"))
        print("Install the meta-task package to enable seed-based replication")
        return 1

    try:
        parser = RpmsrateParser(DEFAULT_RPMSRATE_PATH)
        parser.parse()
    except Exception as e:
        print(colors.error(f"Error parsing rpmsrate-raw: {e}"))
        return 1

    active_categories = [s for s in all_sections if s.startswith('CAT_')]
    seed_packages, locale_patterns = parser.get_packages_and_patterns(
        sections=list(all_sections),
        active_categories=active_categories,
        ignore_conditions=['DRIVER', 'HW', 'HW_CAT'],
        min_priority=4
    )

    print(f"Seed packages from rpmsrate: {len(seed_packages)}")

    # Expand locale patterns using database
    if locale_patterns:
        print(f"Locale patterns to expand: {len(locale_patterns)}")
        expanded = 0
        for pattern in locale_patterns:
            # Find all packages in DB matching this prefix
            cursor = db.conn.execute(
                "SELECT DISTINCT name FROM packages WHERE name LIKE ?",
                (pattern + '%',)
            )
            for (name,) in cursor:
                if name not in seed_packages:
                    seed_packages.add(name)
                    expanded += 1
        print(f"Expanded locale packages: +{expanded}")

    # Expand with dependencies
    result = db.collect_dependencies(seed_packages)
    seed_names = result['packages']
    print(f"With dependencies: {colors.count(len(seed_names))} packages")

    # Import RPM version comparison utilities
    from .core.rpm import evr_key

    # Collect packages to mirror
    # For each media, keep only the latest version of each package name
    all_missing = []
    by_media = {}  # media_name -> (total, cached, missing)

    # First pass: collect latest version per package name per media
    packages_per_media = {}  # media_id -> {pkg_name -> pkg}
    for media in media_to_replicate:
        all_packages = db.get_packages_for_media(media['id'])
        if not all_packages:
            continue

        latest_by_name = {}
        for pkg in all_packages:
            if pkg['name'] in seed_names:
                name = pkg['name']
                if name not in latest_by_name or evr_key(pkg) > evr_key(latest_by_name[name]):
                    latest_by_name[name] = pkg

        packages_per_media[media['id']] = (media, latest_by_name)

    if getattr(args, 'latest_only', False):
        # --latest-only: deduplicate across media too, prefer Updates
        packages_by_name = {}  # name -> (media, pkg)
        for media_id, (media, latest_by_name) in packages_per_media.items():
            for pkg_name, pkg in latest_by_name.items():
                packages_by_name[pkg_name] = (media, pkg)  # Later media wins

        print(f"Unique packages to mirror: {len(packages_by_name)} (--latest-only)")

        for pkg_name, (media, pkg) in packages_by_name.items():
            media_name = media['name']
            if media_name not in by_media:
                by_media[media_name] = [0, 0, 0]
            by_media[media_name][0] += 1

            filename = pkg.get('filename')
            if not filename:
                continue

            cache_dir = get_media_local_path(media)
            pkg_path = cache_dir / filename
            if pkg_path.exists():
                by_media[media_name][1] += 1
            else:
                all_missing.append((media, pkg))
                by_media[media_name][2] += 1
    else:
        # Default: include latest version from each media (release + updates)
        total_packages = 0
        for media_id, (media, latest_by_name) in packages_per_media.items():
            media_name = media['name']
            by_media[media_name] = [0, 0, 0]

            for pkg_name, pkg in latest_by_name.items():
                by_media[media_name][0] += 1
                total_packages += 1

                filename = pkg.get('filename')
                if not filename:
                    continue

                cache_dir = get_media_local_path(media)
                pkg_path = cache_dir / filename
                if pkg_path.exists():
                    by_media[media_name][1] += 1
                else:
                    all_missing.append((media, pkg))
                    by_media[media_name][2] += 1

        print(f"Total packages to mirror: {total_packages} (release + updates, latest versions)")

    # Show per-media breakdown
    for media_name in sorted(by_media.keys()):
        total, cached, missing = by_media[media_name]
        print(f"  {media_name}: {total} packages ({cached} cached, {missing} to download)")

    if not all_missing:
        print(colors.success("\nAll seed packages are already cached!"))
        return 0

    # Note: 'size' in database is installed size, not RPM file size
    # RPM files are typically ~3x smaller than installed size
    installed_size = sum(p.get('size', 0) or 0 for _, p in all_missing)
    estimated_download = installed_size / 3  # Rough estimate
    print(f"\n{colors.bold('To download')}: {len(all_missing)} packages")
    print(f"  Estimated download: ~{estimated_download / 1024 / 1024 / 1024:.1f} GB (installed: {installed_size / 1024 / 1024 / 1024:.1f} GB)")

    # Build download items
    print(colors.dim("\nPreparing downloads..."))

    # Pre-compute servers per media
    media_info = {}  # media_id -> (servers, relative_path, is_official)
    for media in media_to_replicate:
        servers = db.get_servers_for_media(media['id'])
        if servers:
            media_info[media['id']] = (servers, media['relative_path'], media.get('is_official', 1))

    download_items = []
    skipped = 0
    for media, pkg in all_missing:
        info = media_info.get(media['id'])
        if not info:
            skipped += 1
            continue

        servers, relative_path, is_official = info
        item = DownloadItem(
            name=pkg['name'],
            version=pkg['version'],
            release=pkg['release'],
            arch=pkg['arch'],
            media_id=media['id'],
            relative_path=relative_path,
            is_official=bool(is_official),
            servers=servers,
            size=pkg.get('filesize', 0) or 0
        )
        download_items.append(item)
        print(f"Insert 1 {pkg['name']} {pkg.get('filesize',0)}")

    if not download_items:
        print(colors.warning("No items to download (no servers configured?)"))
        return 1

    print(f"Downloading {len(download_items)} packages...")

    # Use parallel downloader (same as urpm i/u)
    from ..core.config import get_base_dir
    cache_dir = get_base_dir(urpm_root=getattr(args, 'urpm_root', None))
    downloader = Downloader(cache_dir=cache_dir, use_peers=False, db=db)

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

    # Suppress logging during download to avoid polluting progress display
    import logging
    logging.getLogger('urpm.core.download').setLevel(logging.ERROR)

    dl_results, downloaded, cached, peer_stats = downloader.download_all(download_items, progress)

    # Restore logging
    logging.getLogger('urpm.core.download').setLevel(logging.WARNING)

    progress_display.finish()

    # Summary
    failed = [r for r in dl_results if not r.success]
    print(f"\n{colors.bold('Done')}: {downloaded} downloaded, {cached} cached, {len(failed)} failed")

    # Notify urpmd to invalidate cache index (so new downloads are visible to peers)
    if downloaded > 0:
        _notify_urpmd_cache_invalidate()

    if failed:
        print(colors.warning(f"\nFailed downloads:"))
        for r in failed[:10]:
            print(f"  {r.item.name}: {r.error}")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more")

    return 0 if not failed else 1


def cmd_mirror_ratelimit(args, db: PackageDatabase) -> int:
    """Handle mirror rate-limit command."""
    from . import colors

    if not args.setting:
        # Show current setting
        enabled = db.get_mirror_config('rate_limit_enabled', '1')
        rate = db.get_mirror_config('rate_limit_requests_per_min', '60')
        if enabled == '0':
            print(f"Rate limiting: {colors.warning('OFF')} (install party mode)")
        else:
            print(f"Rate limiting: {colors.success('ON')} ({rate} requests/min)")
        return 0

    setting = args.setting.lower()
    if setting == 'off':
        db.set_mirror_config('rate_limit_enabled', '0')
        print(colors.warning("Rate limiting disabled (install party mode)"))
    elif setting == 'on':
        db.set_mirror_config('rate_limit_enabled', '1')
        rate = db.get_mirror_config('rate_limit_requests_per_min', '60')
        print(colors.success(f"Rate limiting enabled ({rate} requests/min)"))
    elif '/min' in setting:
        # Parse N/min
        try:
            rate = int(setting.replace('/min', ''))
            db.set_mirror_config('rate_limit_enabled', '1')
            db.set_mirror_config('rate_limit_requests_per_min', str(rate))
            print(colors.success(f"Rate limiting set to {rate} requests/min"))
        except ValueError:
            print(colors.error(f"Invalid rate format: {args.setting}"))
            print("Use: on, off, or N/min (e.g., 60/min)")
            return 1
    else:
        print(colors.error(f"Invalid setting: {args.setting}"))
        print("Use: on, off, or N/min (e.g., 60/min)")
        return 1

    return 0


def cmd_cache_info(args, db: PackageDatabase) -> int:
    """Handle cache info command."""
    stats = db.get_stats()

    print(f"\nCache: {stats['db_path']}")
    print(f"Size:  {stats['db_size_mb']:.1f} MB")
    print(f"Packages: {stats['packages']:,}")
    print(f"Provides: {stats['provides']:,}")
    print(f"Requires: {stats['requires']:,}")
    print(f"Media:    {stats['media']}")
    print()

    return 0


def cmd_cache_clean(args, db: PackageDatabase) -> int:
    """Handle cache clean command - remove orphan RPMs from cache."""
    from pathlib import Path

    cache_dir = Path.home() / ".cache" / "urpm"
    medias_dir = cache_dir / "medias"

    if not medias_dir.exists():
        print("No RPM cache found")
        return 0

    # Get all NEVRAs from database, organized by media
    db_nevras = set()
    cursor = db.conn.execute("""
        SELECT p.nevra, m.name, m.url
        FROM packages p
        JOIN media m ON p.media_id = m.id
    """)
    for row in cursor:
        db_nevras.add(row[0])

    # Scan cache for RPM files
    orphans = []
    total_size = 0

    for rpm_file in medias_dir.rglob("*.rpm"):
        # Extract NEVRA from filename (e.g., firefox-120.0-1.mga9.x86_64.rpm)
        filename = rpm_file.stem  # Remove .rpm
        if filename not in db_nevras:
            orphans.append(rpm_file)
            total_size += rpm_file.stat().st_size

    if not orphans:
        print("No orphan RPMs found in cache")
        return 0

    # Format size
    if total_size > 1024 * 1024 * 1024:
        size_str = f"{total_size / 1024 / 1024 / 1024:.1f} GB"
    elif total_size > 1024 * 1024:
        size_str = f"{total_size / 1024 / 1024:.1f} MB"
    else:
        size_str = f"{total_size / 1024:.1f} KB"

    print(f"\nFound {len(orphans)} orphan RPMs ({size_str}):")

    from . import display
    rpm_names = [rpm_file.name for rpm_file in orphans]
    display.print_package_list(rpm_names, max_lines=10)

    if args.dry_run:
        print(f"\nDry run: would remove {len(orphans)} files ({size_str})")
        return 0

    if not args.auto:
        try:
            answer = input(f"\nRemove {len(orphans)} files ({size_str})? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Remove the files
    removed = 0
    freed = 0
    for rpm_file in orphans:
        try:
            size = rpm_file.stat().st_size
            rpm_file.unlink()
            removed += 1
            freed += size
        except OSError as e:
            print(f"  Warning: could not remove {rpm_file.name}: {e}")

    if freed > 1024 * 1024 * 1024:
        freed_str = f"{freed / 1024 / 1024 / 1024:.1f} GB"
    elif freed > 1024 * 1024:
        freed_str = f"{freed / 1024 / 1024:.1f} MB"
    else:
        freed_str = f"{freed / 1024:.1f} KB"

    print(f"Removed {removed} files, freed {freed_str}")
    return 0


def cmd_cache_rebuild(args, db: PackageDatabase) -> int:
    """Handle cache rebuild command - rebuild database from synthesis files."""
    from pathlib import Path
    from ..core.sync import sync_media

    print("Rebuilding cache database...")

    # Get list of media
    media_list = db.list_media()

    if not media_list:
        print("No media configured")
        return 1

    # Clear all packages first
    print(f"Clearing {db.get_stats()['packages']:,} packages...")
    db.conn.execute("DELETE FROM packages")
    db.conn.execute("DELETE FROM provides")
    db.conn.execute("DELETE FROM requires")
    db.conn.execute("DELETE FROM conflicts")
    db.conn.execute("DELETE FROM obsoletes")
    db.conn.commit()

    # Re-sync each enabled media
    enabled_media = [m for m in media_list if m['enabled']]
    print(f"Re-importing {len(enabled_media)} enabled media...")

    urpm_root = getattr(args, 'urpm_root', None)
    for media in enabled_media:
        print(f"\n  {media['name']}...", end='', flush=True)
        try:
            result = sync_media(db, media['name'], force=True, urpm_root=urpm_root)
            if result.success:
                print(f" {result.packages_count:,} packages")
            else:
                print(f" ERROR: {result.error}")
        except Exception as e:
            print(f" ERROR: {e}")

    stats = db.get_stats()
    print(f"\nDone: {stats['packages']:,} packages, {stats['provides']:,} provides")
    return 0


def cmd_cache_stats(args, db: PackageDatabase) -> int:
    """Handle cache stats command - detailed cache statistics."""
    from pathlib import Path

    cache_dir = Path.home() / ".cache" / "urpm"

    # Database stats
    stats = db.get_stats()
    print(f"\n{'='*60}")
    print("DATABASE")
    print(f"{'='*60}")
    print(f"  Path:      {stats['db_path']}")
    print(f"  Size:      {stats['db_size_mb']:.1f} MB")
    print(f"  Packages:  {stats['packages']:,}")
    print(f"  Provides:  {stats['provides']:,}")
    print(f"  Requires:  {stats['requires']:,}")

    # Media stats
    media_list = db.list_media()
    print(f"\n{'='*60}")
    print("MEDIA")
    print(f"{'='*60}")

    for media in media_list:
        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM packages WHERE media_id = ?",
            (media['id'],)
        )
        pkg_count = cursor.fetchone()[0]
        status = "enabled" if media['enabled'] else "disabled"
        print(f"  {media['name']}: {pkg_count:,} packages ({status})")

    # RPM cache stats
    medias_dir = cache_dir / "medias"
    print(f"\n{'='*60}")
    print("RPM CACHE")
    print(f"{'='*60}")

    if not medias_dir.exists():
        print("  No RPM cache found")
    else:
        total_rpms = 0
        total_size = 0

        # Find all RPMs recursively, group by parent directory
        from collections import defaultdict
        dir_stats = defaultdict(lambda: {'count': 0, 'size': 0})

        for rpm_path in medias_dir.rglob("*.rpm"):
            if rpm_path.is_file():
                try:
                    size = rpm_path.stat().st_size
                    # Get relative path from medias_dir
                    rel_path = rpm_path.relative_to(medias_dir)
                    # Use parent dir as key (e.g., official/10/x86_64/media/core/release)
                    parent_key = str(rel_path.parent)
                    dir_stats[parent_key]['count'] += 1
                    dir_stats[parent_key]['size'] += size
                    total_rpms += 1
                    total_size += size
                except OSError:
                    continue

        # Display sorted by path
        for path_key in sorted(dir_stats.keys()):
            stats = dir_stats[path_key]
            rpm_size = stats['size']
            rpm_count = stats['count']

            if rpm_size > 1024 * 1024 * 1024:
                size_str = f"{rpm_size / 1024 / 1024 / 1024:.1f} GB"
            elif rpm_size > 1024 * 1024:
                size_str = f"{rpm_size / 1024 / 1024:.1f} MB"
            else:
                size_str = f"{rpm_size / 1024:.1f} KB"

            print(f"  {path_key}: {rpm_count} RPMs ({size_str})")

        if total_size > 1024 * 1024 * 1024:
            total_str = f"{total_size / 1024 / 1024 / 1024:.1f} GB"
        elif total_size > 1024 * 1024:
            total_str = f"{total_size / 1024 / 1024:.1f} MB"
        else:
            total_str = f"{total_size / 1024:.1f} KB"

        print(f"\n  Total: {total_rpms} RPMs ({total_str})")

    # History stats
    cursor = db.conn.execute("SELECT COUNT(*) FROM history")
    history_count = cursor.fetchone()[0]
    cursor = db.conn.execute("SELECT COUNT(*) FROM history_packages")
    history_pkgs = cursor.fetchone()[0]

    print(f"\n{'='*60}")
    print("HISTORY")
    print(f"{'='*60}")
    print(f"  Transactions: {history_count}")
    print(f"  Package records: {history_pkgs}")

    print()
    return 0


def cmd_cache_rebuild_fts(args, db: PackageDatabase) -> int:
    """Handle cache rebuild-fts command - rebuild FTS index for file search."""
    import time
    import urllib.request
    import json

    # Check current FTS state
    stats = db.get_fts_stats()

    print(f"\nFTS Index Status:")
    print(f"  Available: {'yes' if stats['available'] else 'no'}")
    print(f"  Current:   {'yes' if stats['current'] else 'no'}")
    print(f"  Files in package_files: {stats['pf_count']:,}")
    print(f"  Files in FTS index:     {stats['fts_count']:,}")

    if stats['last_rebuild']:
        from datetime import datetime
        rebuild_time = datetime.fromtimestamp(stats['last_rebuild'])
        print(f"  Last rebuild: {rebuild_time.strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"\nRebuilding FTS index...", flush=True)

    # Try to use urpmd API if running (avoids database lock issues)
    from ..core.config import DEV_PORT, PROD_PORT, is_dev_mode
    port = DEV_PORT if is_dev_mode() else PROD_PORT

    try:
        req = urllib.request.Request(
            f'http://localhost:{port}/api/rebuild-fts',
            data=b'{}',
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode())

        if result.get('success'):
            print(f"\nDone: {result.get('indexed', 0):,} files indexed in {result.get('elapsed', 0)}s")
            print("  (rebuilt via urpmd)")
            return 0
        elif result.get('error'):
            print(f"Error from urpmd: {result['error']}")
            return 1
    except urllib.error.URLError:
        # urpmd not running, do it directly
        pass
    except Exception as e:
        # urpmd error, try direct rebuild
        print(f"  urpmd unavailable ({e}), rebuilding directly...")

    # Direct rebuild (urpmd not running)
    start_time = time.time()
    last_progress = [0]

    def progress_callback(current: int, total: int):
        pct = int(current * 100 / total) if total > 0 else 0
        # Show progress every 10%
        if pct >= last_progress[0] + 10 or current == total:
            print(f"  {pct}% ({current:,} / {total:,} files)", flush=True)
            last_progress[0] = (pct // 10) * 10

    indexed = db.rebuild_fts_index(progress_callback=progress_callback)

    elapsed = time.time() - start_time
    print(f"\nDone: {indexed:,} files indexed in {elapsed:.1f}s")

    return 0


def _extract_version(pkg_name: str) -> str:
    """Extract version from package name (e.g., php8.4-fpm -> 8.4)."""
    import re
    match = re.search(r'(\d+\.\d+)', pkg_name)
    return match.group(1) if match else None


def _group_by_version(packages: set) -> dict:
    """Group packages by their version.

    Returns dict: {version: set of packages}
    Packages without version go under None key.
    """
    groups = {}
    for pkg in packages:
        ver = _extract_version(pkg)
        if ver not in groups:
            groups[ver] = set()
        groups[ver].add(pkg)
    return groups


def _check_preferences_compatibility(resolver, packages: list, preferences) -> list:
    """Check if preferences are compatible with each other.

    Does a test resolution with strict constraints to detect conflicts.

    Returns:
        List of warning messages if incompatibilities detected, empty list otherwise.
    """
    import solv

    if not preferences.resolved_packages:
        return []

    pool = resolver.pool
    if pool is None:
        return []

    warnings = []

    # Internal capabilities that should not trigger alternative locking
    INTERNAL_CAPABILITIES = {
        'should-restart', 'postshell', 'config', 'bundled', 'debuginfo',
        'application', 'application()',
    }

    # Build test jobs: INSTALL preferred packages, LOCK alternatives
    jobs = []
    favored = set()

    # Collect all capabilities provided by preferred packages
    preferred_caps = {}  # cap -> pkg_name
    for pkg_name in preferences.resolved_packages:
        sel = pool.select(pkg_name, solv.Selection.SELECTION_NAME)
        for s in sel.solvables():
            if s.repo and s.repo.name != '@System':
                favored.add(pkg_name)
                jobs += sel.jobs(solv.Job.SOLVER_INSTALL)
                for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                    cap = str(dep).split()[0]
                    # Skip noise and internal capabilities
                    if cap.startswith(('rpmlib(', '/', 'lib', 'pkgconfig(')):
                        continue
                    if cap in INTERNAL_CAPABILITIES:
                        continue
                    # Skip self-provide (package name = capability)
                    if cap == s.name:
                        continue
                    preferred_caps[cap] = pkg_name
                break

    # Lock alternatives for key capabilities
    locked = set()
    for cap, pkg_name in preferred_caps.items():
        cap_dep = pool.Dep(cap)
        providers = [p for p in pool.whatprovides(cap_dep)
                    if p.repo and p.repo.name != '@System']
        for p in providers:
            if p.name != pkg_name and p.name not in locked and p.name not in favored:
                locked.add(p.name)
                lock_sel = pool.select(p.name, solv.Selection.SELECTION_NAME)
                if not lock_sel.isempty():
                    jobs += lock_sel.jobs(solv.Job.SOLVER_LOCK)

    # Add requested packages
    for name in packages:
        sel = pool.select(name, solv.Selection.SELECTION_NAME |
                         solv.Selection.SELECTION_PROVIDES)
        if not sel.isempty():
            jobs += sel.jobs(solv.Job.SOLVER_INSTALL)

    # Test solve
    solver = pool.Solver()
    problems = solver.solve(jobs)

    if problems:
        # Analyze problems to find which preferences conflict
        for problem in problems:
            prob_str = str(problem)
            # Extract meaningful info from problem
            if 'php-webinterface' in prob_str or 'webinterface' in prob_str.lower():
                # Find what's missing
                missing_combo = []
                for pkg in preferences.resolved_packages:
                    missing_combo.append(pkg)
                warnings.append(
                    f"Preference conflict: no provider for 'php-webinterface' "
                    f"compatible with {', '.join(sorted(preferences.resolved_packages))}"
                )
            else:
                warnings.append(f"Preference conflict: {prob_str[:100]}")

    return warnings


def _add_preferences_to_choices(pool, resolved_packages: set, choices: dict) -> dict:
    """Add resolved packages to choices via their provides.

    Only adds a capability to choices if exactly ONE package from resolved_packages
    provides it. If multiple preferred packages provide the same capability,
    returns them for user to choose.

    Returns:
        Dict of {capability: [providers]} for capabilities that need user choice
    """
    import solv
    from collections import defaultdict

    # Internal RPM/systemd triggers - not user-facing capabilities
    # These are provided by many unrelated packages and should not be used
    # for alternative selection
    INTERNAL_CAPABILITIES = {
        'should-restart',       # systemd restart trigger (glibc, dbus, systemd...)
        'postshell',            # post-install shell requirement
        'config',               # generic config capability
        'bundled',              # bundled library marker
        'debuginfo',            # debug info marker
        'application',          # generic "is an application" marker
        'application()',        # same with parentheses
    }

    # First pass: collect all capabilities and their providers from resolved_packages
    cap_to_providers = defaultdict(set)  # {capability: {provider1, provider2, ...}}

    for pkg_name in resolved_packages:
        sel = pool.select(pkg_name, solv.Selection.SELECTION_NAME)
        for s in sel.solvables():
            if s.repo and s.repo.name != '@System':
                for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                    cap = str(dep).split()[0]
                    # Skip noise
                    if cap.startswith(('rpmlib(', '/', 'lib', 'pkgconfig(')):
                        continue
                    # Skip internal triggers
                    if cap in INTERNAL_CAPABILITIES:
                        continue
                    # Skip self-provide
                    if cap == s.name:
                        continue
                    cap_to_providers[cap].add(s.name)
                break  # Only need first matching solvable

    # Collect capabilities needing user choice
    needs_choice = {}

    # Second pass: add to choices if exactly one provider, track conflicts
    for cap, providers in cap_to_providers.items():
        if cap not in choices:
            if len(providers) == 1:
                choices[cap] = next(iter(providers))
            elif len(providers) > 1:
                # Multiple favored packages provide this - user must choose
                needs_choice[cap] = sorted(providers)

    return needs_choice


def _resolve_with_alternatives(resolver, packages: list, choices: dict,
                               auto_mode: bool, preferences: 'PreferencesMatcher' = None,
                               local_packages: set = None) -> tuple:
    """Resolve packages, handling alternatives interactively with bloc detection.

    Args:
        resolver: Resolver instance
        packages: List of package names to resolve
        choices: Dict mapping capability -> chosen package (modified in place)
        auto_mode: If True, use first choice automatically; if False, ask user
        preferences: PreferencesMatcher instance
        local_packages: Set of package names from local RPM files

    Returns:
        Tuple of (result, aborted) where result is the Resolution and aborted
        is True if user cancelled during alternative selection.
    """
    if local_packages is None:
        local_packages = set()
    from . import colors
    import solv

    if preferences is None:
        preferences = PreferencesMatcher()

    def match_preference(name: str) -> bool:
        """Check if a name matches any preference."""
        return preferences.match_provider_name(name)

    def expand_choice(pkg_name: str, choices: dict):
        """Expand a choice to capabilities provided AND required by the package.

        When user chooses php8.4-fpm-nginx:
        - Record it for capabilities it PROVIDES (php-webinterface, etc.)
        - Also resolve its REQUIRES (nginx) and add them to choices
        """
        if resolver.pool is None:
            return

        sel = resolver.pool.select(pkg_name, solv.Selection.SELECTION_NAME)
        for s in sel.solvables():
            if s.repo and s.repo.name != '@System':
                # Propagate to provided capabilities
                for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                    prov_cap = str(dep).split()[0]
                    if prov_cap.startswith(('lib', '/', 'pkgconfig(', 'rpmlib(')):
                        continue
                    if prov_cap == pkg_name or '(' in prov_cap:
                        continue
                    if prov_cap not in choices:
                        choices[prov_cap] = pkg_name

                # Propagate required packages to choices
                new_choices = []
                for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                    req_cap = str(dep).split()[0]
                    if req_cap.startswith(('lib', '/', 'pkgconfig(', 'rpmlib(')):
                        continue
                    if '(' in req_cap or req_cap in choices:
                        continue
                    # Find the provider for this require
                    req_dep = resolver.pool.Dep(req_cap)
                    providers = [p for p in resolver.pool.whatprovides(req_dep)
                                if p.repo and p.repo.name != '@System']
                    if len(providers) == 1:
                        # Only one provider - auto-select it
                        provider_name = providers[0].name
                        choices[req_cap] = provider_name
                        new_choices.append(provider_name)
                break

        # Recursively expand new choices (but avoid infinite loops)
        for new_pkg in new_choices:
            if new_pkg != pkg_name:
                expand_choice(new_pkg, choices)

    patterns_resolved = False
    preferences_applied = False

    while True:
        # First pass: create pool and resolve preferences
        if not patterns_resolved:
            # Create pool without solving to resolve preferences
            # Preserve pool only if it has @LocalRPMs repo (for local RPM installation)
            has_local_rpms = resolver.pool is not None and any(
                r.name == '@LocalRPMs' for r in resolver.pool.repos
            )
            if not has_local_rpms:
                resolver.pool = resolver._create_pool()
            if resolver.pool:
                preferences.resolve_patterns(resolver.pool)
                patterns_resolved = True

                # Check for version conflicts in resolved_packages (e.g., php8.4-fpm vs php8.5-fpm)
                if not preferences_applied and len(preferences.resolved_packages) > 1:
                    version_groups = _group_by_version(preferences.resolved_packages)
                    # Remove None key (packages without version like nginx, lighttpd)
                    versionless = version_groups.pop(None, set())
                    if len(version_groups) > 1 and not auto_mode:
                        # Multiple versions detected, ask user
                        print(f"\nMultiple versions in preferences:")
                        sorted_versions = sorted(version_groups.keys())
                        for i, ver in enumerate(sorted_versions, 1):
                            pkgs = version_groups[ver]
                            print(f"  {i}. {ver} ({', '.join(sorted(pkgs)[:3])}{'...' if len(pkgs) > 3 else ''})")

                        while True:
                            try:
                                choice = input(f"Choice? [1-{len(sorted_versions)}] ")
                                idx = int(choice) - 1
                                if 0 <= idx < len(sorted_versions):
                                    chosen_version = sorted_versions[idx]
                                    # Keep packages of chosen version + versionless packages
                                    preferences.resolved_packages = version_groups[chosen_version] | versionless
                                    # Re-compute compatible providers
                                    preferences._compatible_providers.clear()
                                    preferences._find_compatible_providers(resolver.pool)
                                    break
                            except (ValueError, EOFError, KeyboardInterrupt):
                                print("\nAborted")
                                return None, True

                    preferences_applied = True

                # NOTE: We no longer pre-validate preferences here.
                # Instead, preferences are applied during the iterative resolution
                # when alternatives are encountered (see match_preference() below).
                # This avoids false conflicts from aggressive LOCKing.

        # Pass favored/disfavored to help solver make consistent choices
        # but don't pre-validate - let the iterative process handle conflicts
        favored = preferences.resolved_packages | preferences._compatible_providers
        result = resolver.resolve_install(
            packages,
            choices=choices,
            favored_packages=favored,
            explicit_disfavor=preferences.disfavored_packages,
            preference_patterns=preferences.name_patterns,
            local_packages=local_packages
        )

        # Handle alternatives (multiple providers for same capability)
        if result.alternatives:
            # Collect all alternative capabilities
            alt_caps = [alt.capability for alt in result.alternatives]

            # Detect blocs among alternatives
            bloc_info = resolver.detect_blocs(alt_caps)

            if bloc_info['blocs'] and not auto_mode:
                bloc_choices = _handle_bloc_choices(
                    bloc_info, preferences, choices, interactive=True
                )

                if bloc_choices:
                    for bloc_key, cap_providers in bloc_choices.items():
                        for cap, provider in cap_providers.items():
                            choices[cap] = provider
                            expand_choice(provider, choices)
                continue

            elif bloc_info['blocs'] and auto_mode:
                bloc_choices = _handle_bloc_choices(
                    bloc_info, preferences, choices, interactive=False
                )
                if bloc_choices:
                    for bloc_key, cap_providers in bloc_choices.items():
                        for cap, provider in cap_providers.items():
                            choices[cap] = provider
                            expand_choice(provider, choices)
                continue

            # No blocs detected - handle alternatives individually
            if not auto_mode:
                for alt in result.alternatives:
                    # Skip if already chosen
                    if alt.capability in choices:
                        continue

                    # Filter providers based on preferences
                    filtered = preferences.filter_providers(alt.providers)

                    # If only one after filtering, auto-select
                    if len(filtered) == 1:
                        choices[alt.capability] = filtered[0]
                        expand_choice(filtered[0], choices)
                        continue

                    # Try to match preference
                    matched = None
                    for prov in filtered:
                        if match_preference(prov):
                            matched = prov
                            break

                    if matched:
                        choices[alt.capability] = matched
                        expand_choice(matched, choices)
                        continue

                    # No preference matched, ask user
                    if alt.required_by:
                        print(f"\n{alt.capability} (required by {alt.required_by}):")
                    else:
                        print(f"\n{alt.capability}:")
                    for i, provider in enumerate(filtered[:8], 1):
                        print(f"  {i}. {provider}")
                    if len(filtered) > 8:
                        print(f"  ... and {len(filtered) - 8} more")

                    while True:
                        try:
                            choice = input(f"Choice? [1-{min(len(filtered), 8)}] ")
                            idx = int(choice) - 1
                            if 0 <= idx < len(filtered):
                                chosen_pkg = filtered[idx]
                                choices[alt.capability] = chosen_pkg
                                expand_choice(chosen_pkg, choices)
                                break
                        except ValueError:
                            pass
                        except (EOFError, KeyboardInterrupt):
                            print("\nAborted")
                            return result, True
                # Re-resolve with new choices
                continue

            else:
                # Auto mode without blocs: use preferences or first choice
                for alt in result.alternatives:
                    filtered = preferences.filter_providers(alt.providers)
                    matched = None
                    for prov in filtered:
                        if match_preference(prov):
                            matched = prov
                            break
                    chosen_pkg = matched if matched else filtered[0]
                    choices[alt.capability] = chosen_pkg
                    expand_choice(chosen_pkg, choices)
                continue

        break  # No more alternatives, exit loop

    return result, False


def _create_resolver(db: 'PackageDatabase', args, **kwargs) -> 'Resolver':
    """Create a Resolver with root options from args.

    Args:
        db: Package database
        args: Parsed arguments (may contain root, urpm_root)
        **kwargs: Additional arguments to pass to Resolver

    Returns:
        Configured Resolver instance
    """
    from ..core.resolver import Resolver
    import platform

    # Get root options from args
    root = getattr(args, 'root', None)
    urpm_root = getattr(args, 'urpm_root', None)

    # Default arch if not provided
    if 'arch' not in kwargs:
        kwargs['arch'] = platform.machine()

    return Resolver(db, root=root, urpm_root=urpm_root, **kwargs)


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


def cmd_update(args, db: PackageDatabase) -> int:
    """Handle update/upgrade command."""
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

    # If --lists, just update media metadata
    if getattr(args, 'lists', False):
        # Reuse media update logic
        args.name = None  # Update all
        return cmd_media_update(args, db)

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
    # --all flag OR (upgrade/u command with no packages) = full system upgrade
    upgrade_all = getattr(args, 'all', False) or (args.command in ('upgrade', 'u') and not packages)

    if not packages and not upgrade_all:
        print("Specify packages to update, or use --all/-a for full system upgrade")
        print("Use --lists/-l to update media metadata only")
        return 1

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


def cmd_list(args, db: PackageDatabase) -> int:
    """Handle list command."""
    import platform

    filter_type = getattr(args, 'filter', 'installed')

    if filter_type == 'installed':
        # List installed packages from rpmdb
        try:
            import rpm
            ts = rpm.TransactionSet()
            packages = []
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                if name == 'gpg-pubkey':
                    continue
                version = hdr[rpm.RPMTAG_VERSION]
                release = hdr[rpm.RPMTAG_RELEASE]
                arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                packages.append((name, version, release, arch))

            packages.sort(key=lambda x: x[0].lower())
            for name, version, release, arch in packages:
                print(f"{name}-{version}-{release}.{arch}")

            print(f"\n{len(packages)} packages installed")
        except ImportError:
            print("Error: rpm module not available")
            return 1

    elif filter_type == 'available':
        # List available packages from our database
        cursor = db.conn.execute("""
            SELECT DISTINCT name, version, release, arch
            FROM packages
            ORDER BY name
        """)
        count = 0
        for row in cursor:
            print(f"{row[0]}-{row[1]}-{row[2]}.{row[3]}")
            count += 1
        print(f"\n{count} packages available")

    elif filter_type in ('updates', 'upgradable'):
        # List packages with available updates
        from ..core.operations import PackageOperations

        ops = PackageOperations(db)
        print("Checking for updates...")
        success, upgrades, problems = ops.get_updates()

        if not success:
            print("Error checking updates:")
            for p in problems:
                print(f"  {p}")
            return 1

        if not upgrades:
            print("All packages are up to date.")
            return 0

        for u in sorted(upgrades, key=lambda x: x.name.lower()):
            print(f"{u.nevra}")

        print(f"\n{len(upgrades)} packages can be upgraded")

    elif filter_type == 'all':
        # List all packages (installed + available)
        try:
            import rpm
            installed = set()
            ts = rpm.TransactionSet()
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                if name != 'gpg-pubkey':
                    installed.add(name)
        except ImportError:
            installed = set()

        cursor = db.conn.execute("""
            SELECT DISTINCT name, version, release, arch
            FROM packages
            ORDER BY name
        """)

        count = 0
        for row in cursor:
            marker = "[i]" if row[0] in installed else "   "
            print(f"{marker} {row[0]}-{row[1]}-{row[2]}.{row[3]}")
            count += 1

        print(f"\n{count} packages ({len(installed)} installed)")

    return 0


def _get_running_kernel() -> str:
    """Get the running kernel package name."""
    import os
    release = os.uname().release  # e.g., "6.6.58-1.mga9-desktop"
    # Extract version-release part to match against kernel packages
    return release


def _get_root_fstype() -> str:
    """Get the filesystem type of the root partition."""
    import subprocess
    try:
        result = subprocess.run(
            ['findmnt', '-n', '-o', 'FSTYPE', '/'],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().lower()
    except Exception:
        return 'ext4'  # Safe default


def _get_blacklist() -> set:
    """Get the blacklist of critical packages that must never be removed.

    These packages, if removed, would make the system unbootable or unusable.
    """
    import os

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
    running = _get_running_kernel()
    # Add kernel packages matching running version
    # The kernel name pattern is kernel-<variant>-<version>-<release>.<arch>
    # Running kernel is like "6.6.58-1.mga9-desktop"
    # We protect packages where version-release matches

    # Dynamic: root filesystem tools
    fstype = _get_root_fstype()
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
    blacklist.update(_get_user_blacklist())

    return blacklist


def _get_redlist() -> set:
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
    redlist.update(_get_user_redlist())

    return redlist


# =============================================================================
# Configuration file management
# =============================================================================

CONFIG_FILE = Path('/etc/urpm/autoremove.conf')

def _read_config() -> dict:
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


def _write_config(config: dict) -> bool:
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


def _get_user_blacklist() -> set:
    """Get user-configured blacklist packages."""
    return _read_config().get('blacklist', set())


def _get_user_redlist() -> set:
    """Get user-configured redlist packages."""
    return _read_config().get('redlist', set())


def _get_kernel_keep() -> int:
    """Get the number of kernels to keep."""
    return _read_config().get('kernel_keep', 2)


def _is_running_kernel(pkg_name: str, pkg_version: str, pkg_release: str) -> bool:
    """Check if a package is the running kernel."""
    import os
    running = os.uname().release
    # Running kernel looks like "6.6.58-1.mga9-desktop"
    # Package version-release looks like "6.6.58-1.mga9"
    return running.startswith(f"{pkg_version}-{pkg_release}")


def _find_old_kernels(keep_count: int = None) -> list:
    """Find old kernels that can be removed.

    Args:
        keep_count: Number of recent kernels to keep (in addition to running).
                    If None, uses the configured value from kernel-keep.

    Returns:
        List of (name, nevra, size) tuples for kernels to remove
    """
    import os
    import rpm

    if keep_count is None:
        keep_count = _get_kernel_keep()

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
    from collections import defaultdict
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


def _find_faildeps(db: 'PackageDatabase') -> tuple:
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
                name = _extract_pkg_name(nevra)
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


def cmd_history(args, db: PackageDatabase) -> int:
    """Handle history command."""
    from datetime import datetime
    from . import colors

    def _color_action(action):
        """Color an action string."""
        action_stripped = action.strip()
        if action_stripped in ('install', 'reinstall'):
            return colors.success(action)
        elif action_stripped in ('remove', 'erase', 'autoremove'):
            return colors.error(action)
        elif action_stripped in ('upgrade', 'update'):
            return colors.info(action)
        elif action_stripped == 'downgrade':
            return colors.warning(action)
        elif action_stripped == 'undo':
            return colors.warning(action)
        elif action_stripped == 'rollback':
            return colors.warning(action)
        return action

    def _color_status(status):
        """Color a status string."""
        status_stripped = status.strip()
        if status_stripped == 'completed':
            return colors.success(status)
        elif status_stripped == 'aborted':
            return colors.error(status)
        elif status_stripped.startswith('undone'):
            return colors.dim(status)
        return status

    # Delete specific transactions
    if getattr(args, 'delete', None):
        deleted = 0
        for tid in args.delete:
            # Check if transaction exists
            trans = db.get_transaction(tid)
            if not trans:
                print(colors.error(f"Transaction #{tid} not found"))
                continue

            # Clear undone_by references to this transaction FIRST
            db.conn.execute("UPDATE history SET undone_by = NULL WHERE undone_by = ?", (tid,))
            # Delete from history_packages (foreign key)
            db.conn.execute("DELETE FROM history_packages WHERE history_id = ?", (tid,))
            # Delete from history
            db.conn.execute("DELETE FROM history WHERE id = ?", (tid,))
            deleted += 1
            print(f"Deleted transaction #{tid}")

        db.conn.commit()
        print(colors.success(f"\n{deleted} transaction(s) deleted"))
        return 0

    # Show details of specific transaction
    if args.detail:
        trans = db.get_transaction(args.detail)
        if not trans:
            print(colors.error(f"Transaction #{args.detail} not found"))
            return 1

        dt = datetime.fromtimestamp(trans['timestamp'])
        trans_id = trans['id']
        print(f"\n{colors.bold(f'Transaction #{trans_id}')} - {dt.strftime('%Y-%m-%d %H:%M')}")
        print(f"  {colors.bold('Action:')} {_color_action(trans['action'])}")
        print(f"  {colors.bold('Status:')} {_color_status(trans['status'])}")
        if trans['command']:
            print(f"  {colors.bold('Command:')} {trans['command']}")
        if trans['undone_by']:
            print(f"  {colors.bold('Undone by:')} #{trans['undone_by']}")

        if trans['explicit']:
            exp_count = len(trans['explicit'])
            print(f"\n  {colors.bold(f'Explicit ({exp_count}):')} ")
            for p in trans['explicit']:
                action = p['action']
                print(f"    {_color_action(f'{action:10}')} {p['pkg_nevra']}")

        if trans['dependencies']:
            dep_count = len(trans['dependencies'])
            print(f"\n  {colors.bold(f'Dependencies ({dep_count}):')} ")
            show_all = getattr(args, 'show_all', False)
            deps_to_show = trans['dependencies'] if show_all else trans['dependencies'][:20]
            for p in deps_to_show:
                action = p['action']
                print(f"    {_color_action(f'{action:10}')} {colors.dim(p['pkg_nevra'])}")
            if dep_count > 20 and not show_all:
                print(colors.dim(f"    ... and {dep_count - 20} more"))

        print()
        return 0

    # List history
    action_filter = None
    if args.install:
        action_filter = 'install'
    elif args.remove:
        action_filter = 'remove'

    history = db.list_history(limit=args.count, action_filter=action_filter)

    if not history:
        print(colors.info("No transaction history"))
        return 0

    print(f"\n{colors.bold('  ID')} | {colors.bold('Date      ')} | {colors.bold('Action  ')} | {colors.bold('Status     ')} | {colors.bold('Packages')}")
    print("-" * 70)

    for h in history:
        dt = datetime.fromtimestamp(h['timestamp'])
        date_str = dt.strftime('%Y-%m-%d')
        explicit = h['explicit_pkgs'] or ''
        if len(explicit) > 30:
            explicit = explicit[:27] + '...'

        status = h['status']
        if h['undone_by']:
            status = f"undone(#{h['undone_by']})"

        pkg_info = explicit
        if h['pkg_count'] > 1 and explicit:
            dep_count = h['pkg_count'] - len(explicit.split(','))
            if dep_count > 0:
                pkg_info += colors.dim(f" (+{dep_count} deps)")

        action = h['action']
        print(f"{h['id']:>4} | {date_str:10} | {_color_action(f'{action:8}')} | {_color_status(f'{status:11}')} | {pkg_info}")

    print()
    return 0


def cmd_config(args) -> int:
    """Handle config command - manage urpm configuration."""

    if not hasattr(args, 'config_cmd') or not args.config_cmd:
        print("Usage: urpm config <blacklist|redlist|kernel-keep|version-mode> ...")
        print("\nSubcommands:")
        print("  blacklist     Manage blacklist (critical packages)")
        print("  redlist       Manage redlist (packages requiring confirmation)")
        print("  kernel-keep   Number of kernels to keep")
        print("  version-mode  Choose between system version and cauldron")
        return 1

    # Handle version-mode (uses database, not config file)
    if args.config_cmd in ('version-mode', 'vm'):
        from ..core.database import PackageDatabase
        from ..core.config import get_db_path, get_system_version, get_accepted_versions

        db = PackageDatabase(get_db_path())

        if hasattr(args, 'mode') and args.mode is not None:
            if args.mode == 'auto':
                # Remove preference
                db.set_config('version-mode', None)
                print("version-mode preference removed (auto-detection)")
            else:
                db.set_config('version-mode', args.mode)
                print(f"version-mode set to '{args.mode}'")
            return 0
        else:
            # Show current state
            current = db.get_config('version-mode')
            system_version = get_system_version()
            accepted, needs_choice, info = get_accepted_versions(db, system_version)

            print(f"\nSystem version: {system_version or 'unknown'}")
            print(f"Configured preference: {current or 'auto (none set)'}")

            if info['cauldron_media']:
                print(f"Cauldron media: {', '.join(info['cauldron_media'][:3])}" +
                      (f" (+{len(info['cauldron_media'])-3} more)" if len(info['cauldron_media']) > 3 else ""))
            if info['system_version_media']:
                print(f"System version media: {', '.join(info['system_version_media'][:3])}" +
                      (f" (+{len(info['system_version_media'])-3} more)" if len(info['system_version_media']) > 3 else ""))

            if needs_choice:
                print(f"\nConflict: Both {system_version} and cauldron media are enabled.")
                print("Use 'urpm config version-mode <system|cauldron>' to choose.")
            elif accepted:
                print(f"\nActive version filter: {', '.join(sorted(accepted))}")
            print()
            return 0

    config = _read_config()

    # Handle kernel-keep
    if args.config_cmd in ('kernel-keep', 'kk'):
        if hasattr(args, 'count') and args.count is not None:
            if args.count < 0:
                print("Error: kernel-keep must be >= 0")
                return 1
            config['kernel_keep'] = args.count
            if _write_config(config):
                print(f"kernel-keep set to {args.count}")
                return 0
            return 1
        else:
            print(f"kernel-keep = {config['kernel_keep']}")
            return 0

    # Handle blacklist
    if args.config_cmd in ('blacklist', 'bl'):
        list_name = 'blacklist'
        builtin = _get_blacklist()
    elif args.config_cmd in ('redlist', 'rl'):
        list_name = 'redlist'
        builtin = _get_redlist()
    else:
        print(f"Unknown config command: {args.config_cmd}")
        return 1

    action = getattr(args, f'{list_name}_cmd', None)

    if not action or action in ('list', 'ls'):
        # Show list
        user_list = config.get(list_name, set())
        print(f"\n{list_name.title()} (built-in):")
        for pkg in sorted(builtin):
            print(f"  {pkg}")

        if user_list:
            print(f"\n{list_name.title()} (user-configured):")
            for pkg in sorted(user_list):
                print(f"  {pkg}")
        else:
            print(f"\nNo user-configured {list_name} entries")

        print()
        return 0

    elif action in ('add', 'a'):
        pkg = args.package
        if pkg in builtin:
            print(f"{pkg} is already in the built-in {list_name}")
            return 0
        if pkg in config[list_name]:
            print(f"{pkg} is already in the user {list_name}")
            return 0
        config[list_name].add(pkg)
        if _write_config(config):
            print(f"Added {pkg} to {list_name}")
            return 0
        return 1

    elif action in ('remove', 'rm'):
        pkg = args.package
        if pkg in builtin:
            print(f"Error: {pkg} is in the built-in {list_name} and cannot be removed")
            return 1
        if pkg not in config[list_name]:
            print(f"{pkg} is not in the user {list_name}")
            return 1
        config[list_name].remove(pkg)
        if _write_config(config):
            print(f"Removed {pkg} from {list_name}")
            return 0
        return 1

    else:
        print(f"Usage: urpm config {list_name} <list|add|remove> [package]")
        return 1


def cmd_key(args) -> int:
    """Handle key command - manage GPG keys for package verification."""
    import os
    import rpm
    import subprocess
    from ..core.install import check_root

    if not hasattr(args, 'key_cmd') or not args.key_cmd:
        print("Usage: urpm key <list|import|remove> ...")
        print("\nCommands:")
        print("  list            List installed GPG keys")
        print("  import <file>   Import GPG key from file or HTTPS URL")
        print("  remove <keyid>  Remove GPG key")
        return 1

    # List keys
    if args.key_cmd in ('list', 'ls', 'l'):
        ts = rpm.TransactionSet()
        keys = []

        for hdr in ts.dbMatch('name', 'gpg-pubkey'):
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            summary = hdr[rpm.RPMTAG_SUMMARY]
            keys.append((version, release, summary))

        if not keys:
            print("No GPG keys installed")
            return 0

        print(f"\nInstalled GPG keys ({len(keys)}):\n")
        for version, release, summary in sorted(keys):
            print(f"  {version}-{release}")
            print(f"    {summary}")
        print()
        return 0

    # Import key
    elif args.key_cmd in ('import', 'i', 'add'):
        if not check_root():
            print("Error: importing keys requires root privileges")
            return 1

        if not hasattr(args, 'keyfile') or not args.keyfile:
            print("Usage: urpm key import <keyfile|url>")
            return 1

        key_source = args.keyfile

        # Check if it's an HTTPS URL
        if key_source.startswith('https://'):
            import tempfile
            import urllib.request
            import urllib.error

            print(f"Downloading key from {key_source}...")
            try:
                with urllib.request.urlopen(key_source, timeout=30) as response:
                    key_data = response.read()

                # Write to temporary file and import
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.gpg', delete=False) as tmp:
                    tmp.write(key_data)
                    tmp_path = tmp.name

                try:
                    result = subprocess.run(
                        ['rpm', '--import', tmp_path],
                        capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        print(f"Key imported from {key_source}")
                        return 0
                    else:
                        print(f"Failed to import key: {result.stderr}")
                        return 1
                finally:
                    os.unlink(tmp_path)

            except urllib.error.URLError as e:
                print(f"Error: failed to download key: {e.reason}")
                return 1
            except Exception as e:
                print(f"Error: {e}")
                return 1

        elif key_source.startswith('http://'):
            print("Error: HTTP URLs are not allowed for security reasons. Use HTTPS.")
            return 1

        else:
            # Import from local file
            if not os.path.exists(key_source):
                print(f"Error: file not found: {key_source}")
                return 1

            result = subprocess.run(
                ['rpm', '--import', key_source],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"Key imported from {key_source}")
                return 0
            else:
                print(f"Failed to import key: {result.stderr}")
                return 1

    # Remove key
    elif args.key_cmd in ('remove', 'rm', 'del'):
        if not check_root():
            print("Error: removing keys requires root privileges")
            return 1

        keyid = args.keyid.lower()

        # Find the key
        ts = rpm.TransactionSet()
        found = None
        for hdr in ts.dbMatch('name', 'gpg-pubkey'):
            version = hdr[rpm.RPMTAG_VERSION]
            if version.lower() == keyid:
                found = f"gpg-pubkey-{version}-{hdr[rpm.RPMTAG_RELEASE]}"
                break

        if not found:
            print(f"Key not found: {keyid}")
            print("Use 'urpm key list' to see installed keys")
            return 1

        # Confirm
        print(f"Removing key: {found}")
        try:
            response = input("Are you sure? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted")
            return 0

        result = subprocess.run(
            ['rpm', '-e', found],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("Key removed")
            return 0
        else:
            print(f"Failed to remove key: {result.stderr}")
            return 1

    else:
        print(f"Unknown key command: {args.key_cmd}")
        return 1


def _query_daemon_peers() -> list:
    """Query local urpmd for discovered peers."""
    import json
    import urllib.request
    import urllib.error
    from ..core.config import PROD_PORT, DEV_PORT

    # Try dev port first, then prod
    for port in [DEV_PORT, PROD_PORT]:
        try:
            url = f"http://127.0.0.1:{port}/api/peers"
            req = urllib.request.Request(url)
            req.add_header('Accept', 'application/json')
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data.get('peers', [])
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
            continue
    return []


def cmd_peer(args, db: PackageDatabase) -> int:
    """Handle peer command - manage P2P peers and provenance."""
    from datetime import datetime
    from . import colors
    from pathlib import Path

    # peer list - show peer stats (default when no subcommand)
    if args.peer_command in ('list', 'ls', None):
        # Query daemon for discovered peers
        discovered_peers = _query_daemon_peers()
        stats = db.get_peer_stats()
        blacklisted = db.list_blacklisted_peers()
        blacklisted_hosts = {(b['peer_host'], b['peer_port']) for b in blacklisted}

        has_content = discovered_peers or stats or blacklisted

        if not has_content:
            print("No peers discovered and no download history.")
            print("Make sure urpmd is running for peer discovery.")
            return 0

        # Show discovered peers from daemon
        if discovered_peers:
            print(colors.bold("Discovered peers on LAN:\n"))
            print(f"{'Peer':<30} {'Media':>8} {'Last seen':<20} {'Status'}")
            print("-" * 70)
            for p in discovered_peers:
                peer_id = f"{p['host']}:{p['port']}"
                media_count = len(p.get('media', []))
                last_seen = p.get('last_seen', '')[:19].replace('T', ' ')  # ISO format to readable

                # Check status
                if (p['host'], p['port']) in blacklisted_hosts or \
                   (p['host'], None) in blacklisted_hosts:
                    status = colors.error("BLACKLISTED")
                elif p.get('alive', True):
                    status = colors.ok("online")
                else:
                    status = colors.warning("offline")

                print(f"{peer_id:<30} {media_count:>8} {last_seen:<20} {status}")
            print()
        else:
            print(colors.warning("No peers discovered on LAN (is urpmd running?)\n"))

        # Show download statistics
        if stats:
            print(colors.bold("Download history:\n"))
            print(f"{'Peer':<30} {'Downloads':>10} {'Size':>12} {'Last download':<20}")
            print("-" * 75)
            for s in stats:
                peer_id = f"{s['peer_host']}:{s['peer_port']}"
                size_mb = (s['total_bytes'] or 0) / (1024 * 1024)
                last_dl = datetime.fromtimestamp(s['last_download']).strftime('%Y-%m-%d %H:%M')
                print(f"{peer_id:<30} {s['download_count']:>10} {size_mb:>10.1f}MB {last_dl:<20}")
            print()

        if blacklisted:
            print(colors.bold("Blacklisted peers:\n"))
            for b in blacklisted:
                port_str = f":{b['peer_port']}" if b['peer_port'] else " (all ports)"
                bl_time = datetime.fromtimestamp(b['blacklist_time']).strftime('%Y-%m-%d %H:%M')
                reason = f" - {b['reason']}" if b['reason'] else ""
                print(f"  {b['peer_host']}{port_str} (since {bl_time}){reason}")

        return 0

    # peer downloads - list packages downloaded from peers
    elif args.peer_command in ('downloads', 'dl'):
        downloads = db.get_peer_downloads(peer_host=args.host, limit=args.limit)

        if not downloads:
            if args.host:
                print(f"No downloads recorded from peer: {args.host}")
            else:
                print("No peer downloads recorded yet.")
            return 0

        print(colors.bold(f"Packages downloaded from peers (last {args.limit}):\n"))
        print(f"{'Filename':<50} {'Peer':<25} {'Date':<20}")
        print("-" * 95)
        for d in downloads:
            peer_id = f"{d['peer_host']}:{d['peer_port']}"
            dl_time = datetime.fromtimestamp(d['download_time']).strftime('%Y-%m-%d %H:%M')
            # Truncate filename if too long
            filename = d['filename']
            if len(filename) > 48:
                filename = filename[:45] + "..."
            print(f"{filename:<50} {peer_id:<25} {dl_time:<20}")

        return 0

    # peer blacklist - add to blacklist
    elif args.peer_command in ('blacklist', 'bl', 'block'):
        host = args.host
        port = getattr(args, 'port', None)
        reason = getattr(args, 'reason', None)

        # Check if already blacklisted
        if db.is_peer_blacklisted(host, port):
            print(f"Peer {host} is already blacklisted.")
            return 0

        db.blacklist_peer(host, port, reason)
        port_str = f":{port}" if port else " (all ports)"
        print(f"Blacklisted peer: {host}{port_str}")
        print("Note: use 'urpm peer clean <host>' to remove RPMs downloaded from this peer.")
        return 0

    # peer unblacklist - remove from blacklist
    elif args.peer_command in ('unblacklist', 'unbl', 'unblock'):
        host = args.host
        port = getattr(args, 'port', None)

        if not db.is_peer_blacklisted(host, port):
            print(f"Peer {host} is not blacklisted.")
            return 0

        db.unblacklist_peer(host, port)
        port_str = f":{port}" if port else ""
        print(f"Removed {host}{port_str} from blacklist.")
        return 0

    # peer clean - delete files from a peer
    elif args.peer_command == 'clean':
        host = args.host

        # Get files from this peer
        files = db.get_files_from_peer(host)
        if not files:
            print(f"No files recorded from peer: {host}")
            return 0

        # Count existing files
        existing = []
        for f in files:
            p = Path(f)
            if p.exists():
                existing.append(p)

        print(f"Found {len(files)} records from peer {host}")
        print(f"  {len(existing)} files still exist on disk")

        if not existing:
            # Just clean up records
            count = db.delete_peer_downloads(host)
            print(f"Removed {count} download records.")
            return 0

        # Confirm deletion
        if not args.yes:
            print(f"\nFiles to delete:")
            from . import display
            show_all = getattr(args, 'show_all', False)
            display.print_package_list([str(p) for p in existing], max_lines=10, show_all=show_all)

            try:
                response = input(f"\nDelete {len(existing)} files? [y/N] ")
                if response.lower() not in ('y', 'yes'):
                    print("Aborted")
                    return 0
            except (KeyboardInterrupt, EOFError):
                print("\nAborted")
                return 0

        # Delete files
        deleted = 0
        errors = 0
        for p in existing:
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                print(f"  Error deleting {p}: {e}")
                errors += 1

        # Clean up records
        count = db.delete_peer_downloads(host)

        print(f"Deleted {deleted} files ({errors} errors)")
        print(f"Removed {count} download records.")
        return 0 if errors == 0 else 1

    else:
        print(f"Unknown peer command: {args.peer_command}")
        return 1


def cmd_appstream(args, db: PackageDatabase) -> int:
    """Handle appstream command - generate AppStream catalog."""
    import gzip
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from ..core.config import get_system_version
    from . import colors

    if args.appstream_command in ('generate', 'gen', None):
        # Get system version for catalog naming
        version = get_system_version() or 'unknown'

        # Determine output path (handle missing args when called without subcommand)
        output_arg = getattr(args, 'output', None)
        no_compress = getattr(args, 'no_compress', False)
        if output_arg:
            output_path = Path(output_arg)
        else:
            catalog_dir = Path('/var/cache/swcatalog/xml')
            catalog_dir.mkdir(parents=True, exist_ok=True)
            if no_compress:
                output_path = catalog_dir / f'mageia-{version}.xml'
            else:
                output_path = catalog_dir / f'mageia-{version}.xml.gz'

        print(f"Generating AppStream catalog for Mageia {version}...")
        print(f"Output: {output_path}")

        # RPM groups that indicate desktop applications
        DESKTOP_GROUPS = {
            # Games
            'games', 'games/arcade', 'games/boards', 'games/cards', 'games/puzzles',
            'games/sports', 'games/strategy', 'games/adventure', 'games/rpg',
            # Graphical desktop applications
            'graphical desktop/gnome', 'graphical desktop/kde', 'graphical desktop/xfce',
            'graphical desktop/other',
            # Office & productivity
            'office', 'office/suite', 'office/wordprocessor', 'office/spreadsheet',
            'office/presentation', 'office/database', 'office/finance',
            # Graphics
            'graphics', 'graphics/viewer', 'graphics/editor', 'graphics/3d',
            'graphics/photography', 'graphics/scanning',
            # Multimedia
            'video', 'video/players', 'video/editors',
            'sound', 'sound/players', 'sound/editors', 'sound/mixers',
            # Networking / Internet
            'networking/www', 'networking/mail', 'networking/chat',
            'networking/instant messaging', 'networking/news', 'networking/ftp',
            'networking/file transfer', 'networking/remote access',
            # Education & Science
            'education', 'sciences', 'sciences/astronomy', 'sciences/chemistry',
            'sciences/mathematics', 'sciences/physics',
            # Development (IDEs only)
            'development/ide',
            # Accessibility
            'accessibility',
            # Archiving
            'archiving/compression',
            # Editors
            'editors',
            # Emulators
            'emulators',
            # File tools
            'file tools',
            # Terminals
            'terminals',
        }

        # Map RPM groups to freedesktop categories
        GROUP_TO_CATEGORY = {
            'games': 'Game', 'games/arcade': 'Game', 'games/boards': 'Game',
            'games/cards': 'Game', 'games/puzzles': 'Game', 'games/sports': 'Game',
            'games/strategy': 'Game', 'games/adventure': 'Game', 'games/rpg': 'Game',
            'office': 'Office', 'office/suite': 'Office', 'office/wordprocessor': 'Office',
            'office/spreadsheet': 'Office', 'office/presentation': 'Office',
            'office/database': 'Office', 'office/finance': 'Office',
            'graphics': 'Graphics', 'graphics/viewer': 'Graphics', 'graphics/editor': 'Graphics',
            'graphics/3d': 'Graphics', 'graphics/photography': 'Graphics', 'graphics/scanning': 'Graphics',
            'video': 'AudioVideo', 'video/players': 'AudioVideo', 'video/editors': 'AudioVideo',
            'sound': 'AudioVideo', 'sound/players': 'AudioVideo', 'sound/editors': 'AudioVideo',
            'sound/mixers': 'AudioVideo',
            'networking/www': 'Network', 'networking/mail': 'Network', 'networking/chat': 'Network',
            'networking/instant messaging': 'Network', 'networking/news': 'Network',
            'networking/ftp': 'Network', 'networking/file transfer': 'Network',
            'networking/remote access': 'Network',
            'education': 'Education', 'sciences': 'Science', 'sciences/astronomy': 'Science',
            'sciences/chemistry': 'Science', 'sciences/mathematics': 'Science',
            'sciences/physics': 'Science',
            'development/ide': 'Development',
            'accessibility': 'Accessibility',
            'archiving/compression': 'Utility',
            'editors': 'TextEditor',
            'emulators': 'Game',
            'file tools': 'Utility',
            'terminals': 'TerminalEmulator',
            'graphical desktop/gnome': 'GNOME', 'graphical desktop/kde': 'KDE',
            'graphical desktop/xfce': 'XFCE', 'graphical desktop/other': 'Utility',
        }

        # Create root element
        root = ET.Element('components')
        root.set('version', '0.14')
        root.set('origin', f'mageia-{version}')

        conn = db._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT DISTINCT
                p.name, p.version, p.release, p.arch,
                p.summary, p.description, p.url, p.license,
                p.size, p.group_name
            FROM packages p
            JOIN media m ON p.media_id = m.id
            WHERE m.enabled = 1
            ORDER BY p.name
        ''')

        pkg_count = 0
        skipped = 0
        for row in cursor.fetchall():
            name, ver, release, arch, summary, description, url, license_, size, group_name = row

            # Skip non-application packages
            if name.endswith(('-debug', '-debuginfo', '-devel', '-static', '-doc', '-docs')):
                skipped += 1
                continue
            if name.startswith(('lib', 'perl-', 'python-', 'python3-', 'ruby-', 'golang-', 'rust-')):
                skipped += 1
                continue
            if name.endswith(('-libs', '-common', '-data', '-lang', '-l10n', '-i18n')):
                skipped += 1
                continue

            # Filter by group - only desktop applications
            group_lower = (group_name or '').lower()
            if not any(group_lower.startswith(g) or group_lower == g for g in DESKTOP_GROUPS):
                skipped += 1
                continue

            # Create component as desktop-application
            component = ET.SubElement(root, 'component')
            component.set('type', 'desktop-application')

            # Desktop ID (AppStream spec requires .desktop suffix)
            desktop_id = f'{name}.desktop'
            ET.SubElement(component, 'id').text = desktop_id
            ET.SubElement(component, 'pkgname').text = name
            ET.SubElement(component, 'name').text = name
            ET.SubElement(component, 'summary').text = summary or f'{name} application'

            # Launchable (desktop file reference)
            launchable = ET.SubElement(component, 'launchable')
            launchable.set('type', 'desktop-id')
            launchable.text = desktop_id

            if description:
                desc_elem = ET.SubElement(component, 'description')
                p_elem = ET.SubElement(desc_elem, 'p')
                p_elem.text = description[:500]

            if url:
                url_elem = ET.SubElement(component, 'url')
                url_elem.set('type', 'homepage')
                url_elem.text = url

            if license_:
                ET.SubElement(component, 'project_license').text = license_

            # Category from group mapping
            categories = ET.SubElement(component, 'categories')
            category = GROUP_TO_CATEGORY.get(group_lower, 'Utility')
            ET.SubElement(categories, 'category').text = category

            # Icon - try package name, fallback to stock
            icon = ET.SubElement(component, 'icon')
            icon.set('type', 'stock')
            icon.text = name  # Many apps have icon named after package

            pkg_count += 1

        print(f"Generated {pkg_count} desktop application components")
        print(f"Skipped {skipped} non-application packages")

        # Write output
        tree = ET.ElementTree(root)

        # Add XML declaration
        xml_str = ET.tostring(root, encoding='unicode')
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        if no_compress or not str(output_path).endswith('.gz'):
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(xml_str)
        else:
            with gzip.open(output_path, 'wt', encoding='utf-8') as f:
                f.write(xml_str)

        print(colors.ok(f"AppStream catalog generated: {output_path}"))
        print("\nTo refresh the AppStream cache, run:")
        print("  sudo appstreamcli refresh-cache --force")

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


def cmd_undo(args, db: PackageDatabase) -> int:
    """Handle undo command - undo last or specific transaction."""
    import signal
    import platform
    from ..core.install import check_root
    from ..core.resolver import Resolver
    from ..core.transaction_queue import TransactionQueue
    from ..core.background_install import InstallLock
    from . import colors

    # Check root
    if not check_root():
        print(colors.error("Error: undo requires root privileges"))
        return 1

    # Determine which transaction to undo
    if args.transaction_id is None:
        # No ID: find last complete, non-undone transaction
        history = db.list_history(limit=20)
        target_id = None
        for h in history:
            trans = db.get_transaction(h['id'])
            if trans and trans['status'] == 'complete' and not trans['undone_by']:
                target_id = h['id']
                break
        if target_id is None:
            print(colors.warning("No undoable transaction found in history"))
            return 1
    else:
        target_id = args.transaction_id

    trans = db.get_transaction(target_id)
    if not trans:
        print(colors.error(f"Transaction #{target_id} not found"))
        return 1

    if trans['status'] != 'complete':
        print(colors.error(f"Transaction #{target_id} is not complete (status: {trans['status']})"))
        return 1

    if trans['undone_by']:
        print(colors.warning(f"Transaction #{target_id} was already undone by #{trans['undone_by']}"))
        return 1

    # Build reverse actions for THIS transaction only
    to_remove = []   # Names of packages to remove
    to_install = []  # NEVRAs to reinstall

    for pkg in trans['packages']:
        action = pkg['action']
        nevra = pkg['pkg_nevra']
        name = pkg['pkg_name']
        previous = pkg.get('previous_nevra')

        if action == 'install':
            to_remove.append(name)
        elif action == 'remove':
            to_install.append(nevra)
        elif action == 'upgrade':
            to_remove.append(name)
            if previous:
                to_install.append(previous)
        elif action == 'downgrade':
            to_remove.append(name)
            if previous:
                to_install.append(previous)

    # Show summary
    print(f"\n{colors.bold(f'Undo transaction #{target_id}')} ({colors.warning(trans['action'])})")

    if to_remove:
        print(f"\n{colors.warning(f'Packages to remove ({len(to_remove)}):')}")
        for name in sorted(to_remove):
            print(f"  {colors.pkg_remove('-')} {name}")

    if to_install:
        print(f"\n{colors.success(f'Packages to reinstall ({len(to_install)}):')}")
        for nevra in sorted(to_install):
            print(f"  {colors.pkg_install('+')} {nevra}")

    if not to_remove and not to_install:
        print(colors.info("Nothing to undo"))
        return 0

    if not args.auto:
        try:
            answer = input("\nProceed? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Handle Ctrl+C
    interrupted = False
    def handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
        print("\nInterrupted! Finishing current operation...")

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    # Start undo transaction
    undo_trans_id = db.begin_transaction('undo', f'urpm undo {target_id}')

    try:
        # First remove packages that were installed (all at once for dependency handling)
        if to_remove and not interrupted:
            # Check if another operation is in progress
            lock = InstallLock()
            if not lock.acquire(blocking=False):
                print(colors.warning("  RPM database is locked by another process."))
                print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
                lock.acquire(blocking=True)
            lock.release()  # Release - child will acquire its own lock

            print(colors.info(f"\nRemoving {len(to_remove)} package(s)..."))

            last_erase_shown = [None]

            # Build transaction queue
            from ..core.config import get_rpm_root
            rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
            queue = TransactionQueue(root=rpm_root or "/")
            queue.add_erase(to_remove, operation_id="undo_remove")

            # Progress callback
            def queue_progress(op_id: str, name: str, current: int, total: int):
                if last_erase_shown[0] != name:
                    print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                    last_erase_shown[0] = name

            # Execute the queue
            queue_result = queue.execute(progress_callback=queue_progress)

            # Print done
            print(f"\r\033[K  [{len(to_remove)}/{len(to_remove)}] done")

            if not queue_result.success:
                print(colors.error("\nErase failed:"))
                if queue_result.operations:
                    for err in queue_result.operations[0].errors[:10]:
                        print(f"  {colors.error(err)}")
                elif queue_result.overall_error:
                    print(f"  {colors.error(queue_result.overall_error)}")
                db.abort_transaction(undo_trans_id)
                return 1

            if interrupted:
                erased_count = queue_result.operations[0].count if queue_result.operations else 0
                db.abort_transaction(undo_trans_id)
                print(colors.warning(f"\nUndo interrupted after {erased_count} packages"))
                return 130

            # Record removed packages
            for name in to_remove:
                db.record_package(undo_trans_id, name, name, 'remove', 'explicit')

            erased_count = queue_result.operations[0].count if queue_result.operations else len(to_remove)
            print(colors.success(f"  {erased_count} packages removed"))

        # Then reinstall packages that were removed
        if to_install and not interrupted:
            print(colors.info(f"\nReinstalling {len(to_install)} package(s)..."))

            # Parse NEVRAs and find packages in repositories
            from ..core.download import Downloader, DownloadItem
            from ..core.config import get_base_dir

            download_items = []
            not_found = []

            # Cache media and servers lookups
            media_cache = {}
            servers_cache = {}

            for nevra in to_install:
                # Parse NEVRA: name-[epoch:]version-release.arch
                # Example: dhcp-client-3:4.4.3P1-4.mga10.x86_64
                if '.' in nevra:
                    name_evr, arch = nevra.rsplit('.', 1)
                else:
                    name_evr = nevra
                    arch = platform.machine()

                # Split name from evr at last hyphen before version
                # This is tricky because package names can contain hyphens
                # We look for the pattern -[epoch:]version-release
                import re
                match = re.match(r'^(.+)-(\d+:)?([^-]+-[^-]+)$', name_evr)
                if match:
                    name = match.group(1)
                    epoch = match.group(2).rstrip(':') if match.group(2) else None
                    ver_rel = match.group(3)
                    version, release = ver_rel.rsplit('-', 1)
                    evr = f"{epoch}:{version}-{release}" if epoch else f"{version}-{release}"
                else:
                    # Fallback: try simpler parsing
                    parts = name_evr.rsplit('-', 2)
                    if len(parts) >= 3:
                        name = parts[0]
                        version = parts[1]
                        release = parts[2]
                        evr = f"{version}-{release}"
                    else:
                        print(f"  {colors.warning('Warning:')} cannot parse NEVRA: {nevra}")
                        not_found.append(nevra)
                        continue

                # Find package in database
                pkg = db.find_package_by_nevra(name, evr, arch)
                if not pkg:
                    # Try without epoch in evr
                    if ':' in evr:
                        evr_no_epoch = evr.split(':', 1)[1]
                        pkg = db.find_package_by_nevra(name, evr_no_epoch, arch)

                if not pkg:
                    print(f"  {colors.warning('Warning:')} {nevra} not found in repositories")
                    not_found.append(nevra)
                    continue

                # Get media info
                media_id = pkg['media_id']
                if media_id not in media_cache:
                    media_cache[media_id] = db.get_media_by_id(media_id)
                media = media_cache[media_id]

                if not media:
                    print(f"  {colors.warning('Warning:')} media not found for {nevra}")
                    not_found.append(nevra)
                    continue

                # Get servers for this media
                if media_id not in servers_cache:
                    servers_cache[media_id] = [dict(s) for s in db.get_servers_for_media(media_id, enabled_only=True)]

                servers = servers_cache[media_id]

                # Build download item
                # Parse version/release from evr for DownloadItem
                dl_evr = evr
                if ':' in dl_evr:
                    dl_evr = dl_evr.split(':', 1)[1]  # Remove epoch for filename
                dl_version, dl_release = dl_evr.rsplit('-', 1) if '-' in dl_evr else (dl_evr, '1')

                if media.get('relative_path'):
                    download_items.append(DownloadItem(
                        name=name,
                        version=dl_version,
                        release=dl_release,
                        arch=arch,
                        media_id=media_id,
                        relative_path=media['relative_path'],
                        is_official=bool(media.get('is_official', 1)),
                        servers=servers,
                        media_name=media.get('name', ''),
                        size=pkg.get('size', 0)
                    ))
                elif media.get('url'):
                    download_items.append(DownloadItem(
                        name=name,
                        version=dl_version,
                        release=dl_release,
                        arch=arch,
                        media_url=media['url'],
                        media_name=media.get('name', ''),
                        size=pkg.get('filesize', 0)
                    ))
                else:
                    print(f"  {colors.warning('Warning:')} no URL or servers for {nevra}")
                    not_found.append(nevra)

            # Download packages
            rpm_paths = []
            if download_items:
                urpm_root = getattr(args, 'urpm_root', None)
                cache_dir = get_base_dir(urpm_root=urpm_root)
                downloader = Downloader(cache_dir=cache_dir, use_peers=True, db=db)

                # Simple progress for undo
                def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                             item_bytes=None, item_total=None, slots_status=None):
                    pct = int(bytes_done * 100 / bytes_total) if bytes_total else 0
                    print(f"\r\033[K  Downloading [{pkg_num}/{pkg_total}] {name} {pct}%", end='', flush=True)

                dl_results, downloaded, cached, _ = downloader.download_all(download_items, progress)
                print(f"\r\033[K  {downloaded} downloaded, {cached} from cache")

                # Collect successful downloads
                for result in dl_results:
                    if result.success and result.path:
                        rpm_paths.append(result.path)
                    else:
                        print(f"  {colors.error('Failed:')} {result.name}: {result.error}")

            # Install downloaded packages
            if rpm_paths and not interrupted:
                print(colors.info(f"  Installing {len(rpm_paths)} package(s)..."))

                # Check lock
                lock = InstallLock()
                if not lock.acquire(blocking=False):
                    print(colors.dim("  Waiting for RPM lock..."))
                    lock.acquire(blocking=True)
                lock.release()

                # Build transaction queue for install
                install_queue = TransactionQueue(root=rpm_root or "/")
                install_queue.add_install(rpm_paths, operation_id="undo_reinstall")

                last_install_shown = [None]

                def install_progress(op_id: str, name: str, current: int, total: int):
                    if last_install_shown[0] != name:
                        print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                        last_install_shown[0] = name

                install_result = install_queue.execute(progress_callback=install_progress)
                print(f"\r\033[K  [{len(rpm_paths)}/{len(rpm_paths)}] done")

                if not install_result.success:
                    print(colors.error("  Reinstall failed:"))
                    if install_result.operations:
                        for err in install_result.operations[0].errors[:5]:
                            print(f"    {colors.error(err)}")
                    # Don't fail the whole undo - removal was successful
                else:
                    # Record reinstalled packages
                    for nevra in to_install:
                        if nevra not in not_found:
                            name = nevra.rsplit('.', 1)[0].rsplit('-', 2)[0] if '.' in nevra else nevra.rsplit('-', 2)[0]
                            db.record_package(undo_trans_id, name, nevra, 'install', 'explicit')
                    installed_count = install_result.operations[0].count if install_result.operations else len(rpm_paths)
                    print(colors.success(f"  {installed_count} packages reinstalled"))

            elif not_found and not rpm_paths:
                print(colors.warning(f"  Could not reinstall: packages not found in repositories"))

        if interrupted:
            db.abort_transaction(undo_trans_id)
            print(colors.warning("\nUndo interrupted"))
            return 130

        # Mark original transaction as undone
        db.mark_undone(target_id, undo_trans_id)

        db.complete_transaction(undo_trans_id)

        # Update installed-through-deps.list for urpmi compatibility
        if to_remove:
            arch = platform.machine()
            resolver = Resolver(db, arch=arch)
            resolver.unmark_packages(to_remove)

        print(colors.success(f"\nUndo complete (transaction #{undo_trans_id})"))
        return 0

    except Exception as e:
        db.abort_transaction(undo_trans_id)
        print(colors.error(f"\nUndo failed: {e}"))
        return 1

    finally:
        signal.signal(signal.SIGINT, old_handler)


def _parse_date(date_str: str) -> int:
    """Parse a date string and return timestamp.

    Supports formats:
    - DD/MM/YYYY
    - DD/MM/YYYY HH:MM
    - YYYY-MM-DD
    - YYYY-MM-DD HH:MM
    """
    import time
    from datetime import datetime

    formats = [
        '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {date_str}")


def cmd_rollback(args, db: PackageDatabase) -> int:
    """Handle rollback command.

    Usage:
    - rollback         : rollback last transaction
    - rollback N       : rollback last N transactions
    - rollback to N    : rollback to state after transaction #N
    - rollback to DATE : rollback to state at DATE
    """
    import signal
    import platform
    from ..core.install import check_root
    from ..core.resolver import Resolver
    from ..core.transaction_queue import TransactionQueue
    from ..core.background_install import InstallLock
    from . import colors

    # Check root
    if not check_root():
        print(colors.error("Error: rollback requires root privileges"))
        return 1

    rollback_args = args.args if hasattr(args, 'args') else []

    # Parse arguments to determine mode and target
    mode = 'count'  # 'count' or 'to'
    count = 1
    target_id = None
    target_timestamp = None

    if not rollback_args:
        # No args: rollback 1
        count = 1
    elif rollback_args[0].lower() == 'to':
        # "rollback to ..."
        mode = 'to'
        if len(rollback_args) < 2:
            print("Usage: rollback to <transaction_id|date>")
            return 1
        target_str = ' '.join(rollback_args[1:])
        # Try as transaction ID first
        try:
            target_id = int(target_str)
        except ValueError:
            # Try as date
            try:
                target_timestamp = _parse_date(target_str)
            except ValueError as e:
                print(colors.error(f"Error: {e}"))
                print("Usage: rollback to <transaction_id|date>")
                print("Date formats: DD/MM/YYYY, DD/MM/YYYY HH:MM, YYYY-MM-DD")
                return 1
    else:
        # "rollback N"
        try:
            count = int(rollback_args[0])
            if count < 1:
                print(colors.error("Count must be at least 1"))
                return 1
        except ValueError:
            print(colors.error(f"Invalid argument: {rollback_args[0]}"))
            print("Usage: rollback [N] | rollback to <id|date>")
            return 1

    # Get history
    history = db.list_history(limit=200)
    if not history:
        print(colors.warning("No transactions in history"))
        return 1

    # Determine which transactions to undo
    if mode == 'count':
        # Undo the last N transactions
        to_undo = [h for h in history if h['status'] == 'complete'][:count]
        if not to_undo:
            print(colors.info("No completed transactions to rollback"))
            return 0
        target_desc = f"last {count} transaction(s)"
    else:
        # mode == 'to'
        if target_id is not None:
            # Rollback to state after transaction #target_id
            to_undo = [h for h in history
                       if h['id'] > target_id and h['status'] == 'complete']
            if not to_undo:
                print(colors.info(f"Already at or before transaction #{target_id}"))
                return 0
            target_desc = f"state after transaction #{target_id}"
        else:
            # Rollback to state at target_timestamp
            to_undo = [h for h in history
                       if h['timestamp'] > target_timestamp and h['status'] == 'complete']
            if not to_undo:
                from datetime import datetime
                date_str = datetime.fromtimestamp(target_timestamp).strftime('%d/%m/%Y %H:%M')
                print(colors.info(f"No transactions after {date_str}"))
                return 0
            target_desc = f"state at {datetime.fromtimestamp(target_timestamp).strftime('%d/%m/%Y %H:%M')}"

    # Collect all actions to reverse
    to_install = []  # NEVRAs to reinstall
    to_remove = []   # Package names to remove

    for h in to_undo:
        trans_detail = db.get_transaction(h['id'])
        if not trans_detail:
            continue

        for pkg in trans_detail['packages']:
            action = pkg['action']
            nevra = pkg['pkg_nevra']
            name = pkg['pkg_name']
            previous = pkg.get('previous_nevra')

            if action == 'install':
                if name not in to_remove:
                    to_remove.append(name)
                to_install = [n for n in to_install if not n.startswith(name + '-')]

            elif action == 'remove':
                if nevra not in to_install:
                    to_install.append(nevra)
                if name in to_remove:
                    to_remove.remove(name)

            elif action == 'upgrade':
                if previous:
                    to_install.append(previous)
                    if name not in to_remove:
                        to_remove.append(name)

            elif action == 'downgrade':
                if previous:
                    to_install.append(previous)
                    if name not in to_remove:
                        to_remove.append(name)

    # Show summary
    print(f"\n{colors.bold(f'Rollback to {target_desc}')}")
    print(f"  {colors.info(f'Undoing {len(to_undo)} transaction(s):')}\n")

    for h in to_undo:
        from datetime import datetime
        date_str = datetime.fromtimestamp(h['timestamp']).strftime('%d/%m/%Y %H:%M')
        action = h['action']
        action_color = colors.warning(action)
        print(f"    #{h['id']} {date_str} {action_color} - {h['explicit_pkgs'] or '(deps)'}")

    if to_remove:
        print(f"\n{colors.warning(f'Packages to remove ({len(to_remove)}):')}")
        for name in sorted(to_remove):
            print(f"  {colors.pkg_remove('-')} {name}")

    if to_install:
        print(f"\n{colors.success(f'Packages to reinstall ({len(to_install)}):')}")
        for nevra in sorted(to_install):
            print(f"  {colors.pkg_install('+')} {nevra}")

    if not to_remove and not to_install:
        print(colors.info("\nNothing to do"))
        return 0

    if not args.auto:
        try:
            answer = input("\nProceed? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Handle Ctrl+C
    interrupted = False
    def handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
        print("\nInterrupted! Finishing current operation...")

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    # Start rollback transaction
    trans_id = db.begin_transaction('rollback', f'urpm rollback {" ".join(map(str, rollback_args)) or "1"}')

    try:
        # First remove packages that were installed (all at once for dependency handling)
        if to_remove and not interrupted:
            # Check if another operation is in progress
            lock = InstallLock()
            if not lock.acquire(blocking=False):
                print(colors.warning("  RPM database is locked by another process."))
                print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
                lock.acquire(blocking=True)
            lock.release()  # Release - child will acquire its own lock

            print(colors.info(f"\nRemoving {len(to_remove)} package(s)..."))

            last_erase_shown = [None]

            # Build transaction queue
            from ..core.config import get_rpm_root
            rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
            queue = TransactionQueue(root=rpm_root or "/")
            queue.add_erase(to_remove, operation_id="rollback_remove")

            # Progress callback
            def queue_progress(op_id: str, name: str, current: int, total: int):
                if last_erase_shown[0] != name:
                    print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                    last_erase_shown[0] = name

            # Execute the queue
            queue_result = queue.execute(progress_callback=queue_progress)

            # Print done
            print(f"\r\033[K  [{len(to_remove)}/{len(to_remove)}] done")

            if not queue_result.success:
                print(colors.error("\nErase failed:"))
                if queue_result.operations:
                    for err in queue_result.operations[0].errors[:10]:
                        print(f"  {colors.error(err)}")
                elif queue_result.overall_error:
                    print(f"  {colors.error(queue_result.overall_error)}")
                db.abort_transaction(trans_id)
                return 1

            if interrupted:
                erased_count = queue_result.operations[0].count if queue_result.operations else 0
                db.abort_transaction(trans_id)
                print(colors.warning(f"\nRollback interrupted after {erased_count} packages"))
                return 130

            # Record removed packages
            for name in to_remove:
                db.record_package(trans_id, name, name, 'remove', 'explicit')

            erased_count = queue_result.operations[0].count if queue_result.operations else len(to_remove)
            print(colors.success(f"  {erased_count} packages removed"))

        # Then reinstall packages that were removed
        if to_install and not interrupted:
            print(colors.info(f"\nReinstalling {len(to_install)} package(s)..."))
            for nevra in to_install:
                if interrupted:
                    break
                print(f"  {colors.warning('Note:')} {nevra} needs to be downloaded/installed")
                # TODO: integrate with resolver/downloader for proper reinstall

        if interrupted:
            db.abort_transaction(trans_id)
            print(colors.warning("\nRollback interrupted"))
            return 130

        # Mark all undone transactions
        for h in to_undo:
            db.mark_undone(h['id'], trans_id)

        db.complete_transaction(trans_id)

        # Update installed-through-deps.list for urpmi compatibility
        if to_remove:
            arch = platform.machine()
            resolver = Resolver(db, arch=arch)
            resolver.unmark_packages(to_remove)

        print(colors.success(f"\nRollback complete (transaction #{trans_id})"))
        return 0

    except Exception as e:
        db.abort_transaction(trans_id)
        print(colors.error(f"\nRollback failed: {e}"))
        return 1

    finally:
        signal.signal(signal.SIGINT, old_handler)


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


def cmd_provides(args, db: PackageDatabase) -> int:
    """Handle provides command - show what a package provides."""
    package = args.package
    pkg_name = _extract_pkg_name(package)

    provides = []
    found_name = package

    # Check installed packages first
    try:
        import rpm
        ts = rpm.TransactionSet()

        # Try exact name first
        found = False
        for hdr in ts.dbMatch('name', pkg_name):
            # If NEVRA was given, check it matches
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
            nevra = f"{pkg_name}-{version}-{release}.{arch}"

            if package != pkg_name and nevra != package:
                continue  # NEVRA doesn't match

            found_name = nevra
            prov_names = hdr[rpm.RPMTAG_PROVIDENAME] or []
            prov_versions = hdr[rpm.RPMTAG_PROVIDEVERSION] or []

            for i, prov in enumerate(prov_names):
                ver = prov_versions[i] if i < len(prov_versions) else ''
                if ver:
                    provides.append(f"{prov} = {ver}")
                else:
                    provides.append(prov)
            found = True
            break

    except ImportError:
        pass

    # If not installed, check database
    if not provides:
        pkg = db.get_package_smart(package)
        if pkg and pkg.get('provides'):
            provides = pkg['provides']
            found_name = pkg.get('nevra', pkg_name)

    if not provides:
        print(f"Package '{package}' not found")
        return 1

    print(f"Package {found_name} provides ({len(provides)}):\n")
    for prov in sorted(provides):
        print(f"  {prov}")

    return 0


def cmd_whatprovides(args, db: PackageDatabase) -> int:
    """Handle whatprovides command - find packages providing a capability."""
    capability = args.capability

    # Check if user wants glob matching (contains * or ?)
    use_glob = '*' in capability or '?' in capability

    # Search in database (available packages)
    results = []

    if use_glob:
        # Convert glob to SQL LIKE pattern
        pattern = capability.replace('*', '%').replace('?', '_')
        cursor = db.conn.execute("""
            SELECT DISTINCT p.name, p.version, p.release, p.arch, p.nevra,
                   m.name as media_name
            FROM packages p
            JOIN provides pr ON pr.pkg_id = p.id
            LEFT JOIN media m ON p.media_id = m.id
            WHERE pr.capability LIKE ?
            ORDER BY p.name
            LIMIT 100
        """, (pattern,))
        results = [dict(row) for row in cursor]
    else:
        # Exact match first
        results = db.whatprovides(capability)

        # Also try matching the base name (without version brackets)
        if not results:
            cursor = db.conn.execute("""
                SELECT DISTINCT p.name, p.version, p.release, p.arch, p.nevra,
                       m.name as media_name
                FROM packages p
                JOIN provides pr ON pr.pkg_id = p.id
                LEFT JOIN media m ON p.media_id = m.id
                WHERE pr.capability = ? OR pr.capability LIKE ?
                ORDER BY p.name
                LIMIT 100
            """, (capability, f'{capability}[%'))
            results = [dict(row) for row in cursor]

    # Also check installed packages via rpm
    installed_matches = []
    try:
        import rpm
        ts = rpm.TransactionSet()

        if use_glob:
            # For glob, iterate all packages (slower but necessary)
            import fnmatch
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                if name == 'gpg-pubkey':
                    continue
                provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
                for prov in provides:
                    if fnmatch.fnmatch(prov, capability):
                        version = hdr[rpm.RPMTAG_VERSION]
                        release = hdr[rpm.RPMTAG_RELEASE]
                        arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                        nevra = f"{name}-{version}-{release}.{arch}"
                        if not any(m['nevra'] == nevra for m in installed_matches):
                            installed_matches.append({
                                'name': name,
                                'nevra': nevra,
                                'installed': True
                            })
                        break
        else:
            # Exact match - use rpm index
            for hdr in ts.dbMatch('providename', capability):
                name = hdr[rpm.RPMTAG_NAME]
                version = hdr[rpm.RPMTAG_VERSION]
                release = hdr[rpm.RPMTAG_RELEASE]
                arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                installed_matches.append({
                    'name': name,
                    'nevra': f"{name}-{version}-{release}.{arch}",
                    'installed': True
                })

        # If capability looks like a file path, also search files
        if capability.startswith('/'):
            for hdr in ts.dbMatch('basenames', capability):
                name = hdr[rpm.RPMTAG_NAME]
                version = hdr[rpm.RPMTAG_VERSION]
                release = hdr[rpm.RPMTAG_RELEASE]
                arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                nevra = f"{name}-{version}-{release}.{arch}"
                if not any(m['nevra'] == nevra for m in installed_matches):
                    installed_matches.append({
                        'name': name,
                        'nevra': nevra,
                        'installed': True
                    })
    except ImportError:
        pass

    if not results and not installed_matches:
        print(f"No package provides '{capability}'")
        return 1

    # Show installed matches first
    if installed_matches:
        print("Installed:")
        for pkg in installed_matches:
            print(f"  {pkg['nevra']}")

    # Show available (not installed)
    installed_nevras = {m['nevra'] for m in installed_matches}
    available = [r for r in results if r['nevra'] not in installed_nevras]

    if available:
        if installed_matches:
            print("\nAvailable:")
        for pkg in available:
            media = pkg.get('media_name', '')
            media_str = f" [{media}]" if media else ""
            print(f"  {pkg['nevra']}{media_str}")

    return 0


def cmd_find(args, db: PackageDatabase) -> int:
    """Handle find command - find packages containing a file (like urpmf)."""
    from . import colors
    from collections import OrderedDict

    pattern = args.pattern
    search_available = getattr(args, 'available', False)
    search_installed = getattr(args, 'installed', False)
    show_all = getattr(args, 'show_all', False)

    # Limit files per package (--show-all shows all)
    FILES_PER_PKG = 5 if not show_all else 0  # 0 = unlimited

    # Default: search both if neither flag is specified
    if not search_available and not search_installed:
        search_both = True
    else:
        search_both = False

    installed_found = []
    available_found = []

    # Search in installed packages via rpm
    if search_installed or search_both:
        try:
            import rpm
            ts = rpm.TransactionSet()

            if pattern.startswith('/'):
                # Exact file path
                for hdr in ts.dbMatch('basenames', pattern):
                    name = hdr[rpm.RPMTAG_NAME]
                    version = hdr[rpm.RPMTAG_VERSION]
                    release = hdr[rpm.RPMTAG_RELEASE]
                    arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                    installed_found.append({
                        'nevra': f"{name}-{version}-{release}.{arch}",
                        'file': pattern
                    })
            else:
                # Pattern search - need to iterate all packages
                import fnmatch
                # Convert SQL wildcards to fnmatch wildcards
                fnmatch_pattern = pattern.replace('%', '*').replace('_', '?')
                has_wildcards = '*' in fnmatch_pattern or '?' in fnmatch_pattern

                if fnmatch_pattern.startswith('/'):
                    # Absolute path - use as-is
                    pass
                elif has_wildcards:
                    # User specified wildcards - use as-is
                    pass
                else:
                    # No wildcards, no leading / - search for exact filename
                    # nvim → */nvim (file named nvim)
                    fnmatch_pattern = '*/' + fnmatch_pattern

                for hdr in ts.dbMatch():
                    name = hdr[rpm.RPMTAG_NAME]
                    if name == 'gpg-pubkey':
                        continue
                    files = hdr[rpm.RPMTAG_FILENAMES] or []
                    version = hdr[rpm.RPMTAG_VERSION]
                    release = hdr[rpm.RPMTAG_RELEASE]
                    arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                    nevra = f"{name}-{version}-{release}.{arch}"
                    for f in files:
                        if fnmatch.fnmatch(f, fnmatch_pattern):
                            installed_found.append({
                                'nevra': nevra,
                                'file': f
                            })
        except ImportError:
            pass

    # Search in available packages via database (files.xml)
    if search_available or search_both:
        # Check if we have files.xml data
        stats = db.get_files_stats()
        if stats['total_files'] == 0:
            # No data - check if sync_files is enabled on any media
            has_sync_files = db.has_any_sync_files_media()

            if not has_sync_files:
                # Prompt user to enable files.xml sync
                print(colors.info("La recherche dans les paquets disponibles nécessite le téléchargement"))
                print(colors.info("des fichiers files.xml (~500 Mo, ~10-15 minutes la première fois)."))
                print()

                try:
                    response = input("Activer cette fonctionnalité ? [o/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 1

                if response in ('o', 'oui', 'y', 'yes'):
                    # Enable sync_files on all enabled media
                    from ..core.install import check_root
                    if not check_root():
                        print(colors.error("Erreur: droits root requis pour activer sync_files"))
                        print("Essayez: sudo urpm media set --all --sync-files")
                        return 1

                    db.set_all_media_sync_files(True, enabled_only=True)
                    enabled_count = len(db.get_media_with_sync_files())
                    print(colors.success(f"sync_files activé sur {enabled_count} media"))
                    print()
                    print("Lancez maintenant: sudo urpm media update --files")
                    print("(~10-15 minutes la première fois, puis quasi-instantané)")
                    return 0
                else:
                    print(colors.dim("Fonctionnalité non activée."))
                    print(colors.dim("Pour activer plus tard: sudo urpm media set --all --sync-files"))
                    return 0

            elif search_available:
                # sync_files is enabled but no data yet
                print(colors.warning("sync_files est activé mais les données ne sont pas encore téléchargées."))
                print("Lancez: sudo urpm media update --files")
                return 1
            # else: silently skip available search if searching both
        else:
            # Check if FTS index needs rebuild (migration case)
            if db.is_fts_available() and not db.is_fts_index_current():
                print(colors.warning("L'index de recherche rapide (FTS) doit être reconstruit."))
                print(colors.dim("Lancez: sudo urpm media update --files"))
                print(colors.dim("(La recherche sera plus lente en attendant)"))
                print()

            # Search in database (uses FTS if available, falls back to B-tree)
            results = db.search_files(
                pattern,
                limit=0  # Fetch all, display limits handled by FILES_PER_PKG
            )

            # Collect all matching files
            for r in results:
                available_found.append({
                    'nevra': r['pkg_nevra'],
                    'file': r['file_path'],
                    'media': r['media_name']
                })

    # Display results
    if not installed_found and not available_found:
        print(f"No package contains '{pattern}'")
        if search_both or search_available:
            stats = db.get_files_stats()
            if stats['total_files'] == 0:
                print(colors.info("Hint: run 'sudo urpm media update --files' to enable searching available packages"))
        return 1

    # Helper to highlight pattern in file path (green)
    def highlight_pattern(filepath: str, pat: str) -> str:
        """Highlight pattern matches in filepath with green color."""
        import re
        try:
            # Strip leading/trailing wildcards (they match everything, no point highlighting)
            regex_pat = pat.strip('%*')
            if not regex_pat:
                return filepath  # Pattern is only wildcards, nothing to highlight

            # Escape regex special chars
            regex_pat = re.sub(r'([.^$+{}\\|\[\]()])', r'\\\1', regex_pat)
            # Convert remaining internal wildcards to regex
            regex_pat = regex_pat.replace('%', '.*').replace('*', '.*')
            regex_pat = regex_pat.replace('?', '.').replace('_', '.')
            return re.sub(f'({regex_pat})', lambda m: colors.success(m.group(1)), filepath, flags=re.IGNORECASE)
        except re.error:
            return filepath

    # Helper to group results by package
    def group_by_package(results: list) -> OrderedDict:
        """Group results by nevra, preserving order of first occurrence."""
        grouped = OrderedDict()
        for r in results:
            nevra = r['nevra']
            if nevra not in grouped:
                grouped[nevra] = {'media': r.get('media'), 'files': []}
            grouped[nevra]['files'].append(r['file'])
        return grouped

    # Helper to display a group of packages
    def display_grouped(grouped: OrderedDict, max_files: int, show_media: bool = False) -> tuple:
        """Display grouped packages, return (shown_files, hidden_files)."""
        total_shown = 0
        total_hidden = 0
        for nevra, data in grouped.items():
            files = data['files']
            media_str = f" {colors.dim('[' + data['media'] + ']')}" if show_media and data.get('media') else ""
            pkg_display = colors.cyan(nevra)
            print(f"  {pkg_display}:{media_str}")

            # Show files with optional limit
            files_to_show = files if max_files == 0 else files[:max_files]
            for f in files_to_show:
                print(f"    {highlight_pattern(f, pattern)}")
                total_shown += 1

            # Show "... N more" if truncated
            hidden = len(files) - len(files_to_show)
            if hidden > 0:
                print(colors.dim(f"    ... ({hidden} more)"))
                total_hidden += hidden

        return total_shown, total_hidden

    total_shown = 0
    total_hidden = 0

    if installed_found:
        print(colors.info("Installed:"))
        grouped = group_by_package(installed_found)
        shown, hidden = display_grouped(grouped, FILES_PER_PKG, show_media=False)
        total_shown += shown
        total_hidden += hidden

    if available_found:
        # Filter out already-installed packages (by NEVRA)
        installed_nevras = {m['nevra'] for m in installed_found}
        available_not_installed = [a for a in available_found if a['nevra'] not in installed_nevras]

        if available_not_installed:
            if installed_found:
                print()
            print(colors.info("Available (not installed):"))
            grouped = group_by_package(available_not_installed)
            shown, hidden = display_grouped(grouped, FILES_PER_PKG, show_media=True)
            total_shown += shown
            total_hidden += hidden

    # Summary if some files were hidden
    if total_hidden > 0:
        print(f"\n{colors.dim(f'{total_hidden} files hidden (use --show-all to see all)')}")

    return 0


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

        elif args.command in ('update', 'up', 'upgrade', 'u'):
            return cmd_update(args, db)

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
