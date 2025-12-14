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
from pathlib import Path

from .. import __version__
from ..core.database import PackageDatabase


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
        '--json',
        action='store_true',
        help='JSON output for scripting'
    )

    parser.add_argument(
        '--nocolor',
        action='store_true',
        help='Disable colored output'
    )

    # Register custom action for aliases
    parser.register('action', 'parsers', AliasedSubParsersAction)
    
    subparsers = parser.add_subparsers(
        dest='command',
        title='commands',
        metavar='<command>'
    )
    
    # =========================================================================
    # install / i
    # =========================================================================
    install_parser = subparsers.add_parser(
        'install', aliases=['i'],
        help='Install packages'
    )
    install_parser.add_argument(
        'packages', nargs='+',
        help='Package names to install'
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
        '--no-recommends',
        action='store_true',
        help='Skip recommended packages'
    )
    install_parser.add_argument(
        '--all',
        action='store_true',
        help='Install for all matching families (e.g., both php8.4 and php8.5)'
    )
    install_parser.add_argument(
        '--nosignature',
        action='store_true',
        help='Skip GPG signature verification (not recommended)'
    )

    # =========================================================================
    # erase / e (like rpm -e, urpme)
    # =========================================================================
    erase_parser = subparsers.add_parser(
        'erase', aliases=['e'],
        help='Erase (remove) packages'
    )
    erase_parser.add_argument(
        'packages', nargs='+',
        help='Package names to erase'
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
        help='Also remove orphan dependencies'
    )

    # =========================================================================
    # search / s / query / q
    # =========================================================================
    search_parser = subparsers.add_parser(
        'search', aliases=['s', 'query', 'q'],
        help='Search packages'
    )
    search_parser.add_argument(
        'pattern',
        help='Search pattern'
    )
    search_parser.add_argument(
        '--installed',
        action='store_true',
        help='Search only installed packages'
    )

    # =========================================================================
    # show / sh / info
    # =========================================================================
    show_parser = subparsers.add_parser(
        'show', aliases=['sh', 'info'],
        help='Show package details'
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
        help='List packages'
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
        help='Show what a package provides'
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
        help='Find packages providing a capability'
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
        help='Find which package contains a file'
    )
    find_parser.add_argument(
        'pattern',
        help='File pattern'
    )
    
    # =========================================================================
    # depends / d
    # =========================================================================
    depends_parser = subparsers.add_parser(
        'depends', aliases=['d'],
        help='Show package dependencies'
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
    
    # =========================================================================
    # rdepends / rd
    # =========================================================================
    rdepends_parser = subparsers.add_parser(
        'rdepends', aliases=['rd'],
        help='Show reverse dependencies'
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
    
    # =========================================================================
    # update / u
    # =========================================================================
    update_parser = subparsers.add_parser(
        'update', aliases=['u'],
        help='Update packages or metadata'
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

    # =========================================================================
    # upgrade (alias for update --all)
    # =========================================================================
    upgrade_parser = subparsers.add_parser(
        'upgrade',
        help='Upgrade all packages (alias for update --all)'
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
    
    # =========================================================================
    # autoremove / ar
    # =========================================================================
    autoremove_parser = subparsers.add_parser(
        'autoremove', aliases=['ar'],
        help='Remove orphaned packages, old kernels, or failed deps'
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
    # media / m
    # =========================================================================
    media_parser = subparsers.add_parser(
        'media', aliases=['m'],
        help='Manage media sources'
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
    
    # media add / a
    media_add = media_subparsers.add_parser(
        'add', aliases=['a'],
        help='Add media source'
    )
    media_add.add_argument('name', help='Media name')
    media_add.add_argument('url', help='Media URL')
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
    
    # =========================================================================
    # cache / c
    # =========================================================================
    cache_parser = subparsers.add_parser(
        'cache', aliases=['c'],
        help='Manage cache'
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

    # =========================================================================
    # history / h
    # =========================================================================
    history_parser = subparsers.add_parser(
        'history', aliases=['h'],
        help='Show transaction history'
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
        help='Rollback transactions: "rollback 5" (last 5), "rollback to 42" (to #42), "rollback to 26/11/2025"'
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
        help='Undo last transaction, or a specific one'
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
        help='Remove orphan deps from interrupted transactions (alias: autoremove --faildeps)'
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
        help='Manage urpm configuration'
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

    # =========================================================================
    # key - GPG key management
    # =========================================================================
    key_parser = subparsers.add_parser(
        'key', aliases=['k'],
        help='Manage GPG keys for package verification'
    )
    key_subparsers = key_parser.add_subparsers(dest='key_cmd', metavar='COMMAND')

    key_subparsers.add_parser('list', aliases=['ls', 'l'], help='List installed GPG keys')

    key_import = key_subparsers.add_parser('import', aliases=['i', 'add'], help='Import GPG key')
    key_import.add_argument('keyfile', help='Path to key file or HTTPS URL')

    key_remove = key_subparsers.add_parser('remove', aliases=['rm', 'del'], help='Remove GPG key')
    key_remove.add_argument('keyid', help='Key ID to remove (e.g., 80420f66)')

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
                if answer.lower() not in ('y', 'yes', 'o', 'oui'):
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


def cmd_search(args, db: PackageDatabase) -> int:
    """Handle search command."""
    import re
    from . import colors

    results = db.search(args.pattern, search_provides=True)

    if not results:
        print(colors.warning(f"No packages found for '{args.pattern}'"))
        return 1

    def highlight(text, pattern):
        """Highlight pattern occurrences in text with green."""
        if not colors.enabled():
            return text
        try:
            regex = re.compile(f'({re.escape(pattern)})', re.IGNORECASE)
            return regex.sub(colors.success(r'\1'), text)
        except re.error:
            return text

    pattern = args.pattern

    for pkg in results:
        # Name in bold, version normal, release.arch in dim
        name = colors.bold(highlight(pkg['name'], pattern))
        version = highlight(pkg['version'], pattern)
        release_arch = colors.dim(f"{pkg['release']}.{pkg['arch']}")
        nevra_display = f"{name}-{version}-{release_arch}"

        summary = pkg.get('summary', '')[:60]
        summary = highlight(summary, pattern)

        # Show which provide matched if found via provides
        if pkg.get('matched_provide'):
            matched = highlight(pkg['matched_provide'], pattern)
            print(f"{nevra_display}  {colors.dim(f'(provides: {matched})')}")
        else:
            print(f"{nevra_display}  {summary}")

    print(colors.dim(f"\n{len(results)} package(s) found"))
    return 0


def cmd_show(args, db: PackageDatabase) -> int:
    """Handle show/info command."""
    from . import colors

    pkg = db.get_package_smart(args.package)

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
        for dep in pkg['requires'][:10]:
            print(f"  {colors.dim('-')} {dep}")
        if req_count > 10:
            print(colors.dim(f"  ... and {req_count - 10} more"))

    if pkg.get('provides'):
        prov_count = len(pkg['provides'])
        print(f"\n{colors.bold(f'Provides ({prov_count}):')} ")
        for prov in pkg['provides'][:5]:
            print(f"  {colors.dim('-')} {prov}")
        if prov_count > 5:
            print(colors.dim(f"  ... and {prov_count - 5} more"))

    print()
    return 0


def cmd_media_list(args, db: PackageDatabase) -> int:
    """Handle media list command."""
    media_list = db.list_media()

    if not media_list:
        print("No media configured")
        return 0

    for m in media_list:
        status = "[x]" if m['enabled'] else "[ ]"
        update_tag = " [update]" if m['update_media'] else ""
        print(f"  {status} {m['name']:20} {m['url'] or m['mirrorlist'] or ''}{update_tag}")

    return 0


def cmd_media_add(args, db: PackageDatabase) -> int:
    """Handle media add command."""
    name = args.name
    url = args.url

    # Check if already exists
    if db.get_media(name):
        print(f"Media '{name}' already exists")
        return 1

    media_id = db.add_media(
        name=name,
        url=url,
        enabled=not args.disabled,
        update=args.update
    )

    print(f"Added media '{name}' (id={media_id})")
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
    from ..core.sync import sync_media, sync_all_media

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

        result = sync_media(db, args.name, single_progress, force=True)
        print()  # newline after progress

        if result.success:
            print(colors.success(f"  {result.packages_count} packages"))
            return 0
        else:
            print(f"  {colors.error('Error')}: {result.error}")
            return 1
    else:
        # Update all media
        print("Updating all media...")
        results = sync_all_media(db, progress, force=True)
        print()  # newline after progress

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
            print(f"\n{colors.info('Total')}: {colors.success(str(total_packages))} packages from {len(results)} media ({colors.error(str(errors))} errors)")
        else:
            print(f"\n{colors.info('Total')}: {colors.success(str(total_packages))} packages from {len(results)} media")
        return 1 if errors else 0


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

    if args.verbose or len(orphans) <= 10:
        for rpm_file in orphans:
            print(f"  {rpm_file.name}")
    else:
        for rpm_file in orphans[:5]:
            print(f"  {rpm_file.name}")
        print(f"  ... and {len(orphans) - 5} more")

    if args.dry_run:
        print(f"\nDry run: would remove {len(orphans)} files ({size_str})")
        return 0

    if not args.auto:
        try:
            answer = input(f"\nRemove {len(orphans)} files ({size_str})? [y/N] ")
            if answer.lower() not in ('y', 'yes', 'o', 'oui'):
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

    for media in enabled_media:
        print(f"\n  {media['name']}...", end='', flush=True)
        try:
            result = sync_media(db, media['name'], force=True)
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

        # Count by hostname/media
        for hostname_dir in medias_dir.iterdir():
            if not hostname_dir.is_dir():
                continue
            for media_dir in hostname_dir.iterdir():
                if not media_dir.is_dir():
                    continue
                rpms = list(media_dir.glob("*.rpm"))
                if rpms:
                    rpm_count = len(rpms)
                    rpm_size = sum(f.stat().st_size for f in rpms)
                    total_rpms += rpm_count
                    total_size += rpm_size

                    if rpm_size > 1024 * 1024 * 1024:
                        size_str = f"{rpm_size / 1024 / 1024 / 1024:.1f} GB"
                    elif rpm_size > 1024 * 1024:
                        size_str = f"{rpm_size / 1024 / 1024:.1f} MB"
                    else:
                        size_str = f"{rpm_size / 1024:.1f} KB"

                    print(f"  {hostname_dir.name}/{media_dir.name}: {rpm_count} RPMs ({size_str})")

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


def cmd_install(args, db: PackageDatabase) -> int:
    """Handle install command."""
    import signal
    from ..core.resolver import Resolver, format_size
    from ..core.download import Downloader, DownloadItem

    # Resolve virtual packages to concrete packages
    # This handles cases like php-opcache → php8.5-opcache based on what's installed
    auto_mode = getattr(args, 'auto', False)
    install_all = getattr(args, 'all', False)

    resolved_packages = []
    for pkg in args.packages:
        pkg_name = _extract_pkg_name(pkg)
        concrete = _resolve_virtual_package(db, pkg_name, auto_mode, install_all)
        resolved_packages.extend(concrete)

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

    resolver = Resolver(db)
    result = resolver.resolve_install(resolved_packages)

    if not result.success:
        print("Resolution failed:")
        for p in result.problems:
            print(f"  {p}")
        return 1

    if not result.actions:
        print("Nothing to do")
        return 0

    # Determine which packages are explicit vs dependencies
    # Include both the requested names AND the resolved concrete packages
    explicit_names = set(_extract_pkg_name(p).lower() for p in args.packages)
    explicit_names.update(p.lower() for p in resolved_packages)

    # Also include packages that provide what the user requested
    # (e.g., user asked for 'nvim', resolver found 'neovim' which provides 'nvim')
    requested_names = set(_extract_pkg_name(p).lower() for p in args.packages)
    for action in result.actions:
        # Check if this package provides any of the requested names
        pkg_info = db.get_package(action.name)
        if pkg_info and pkg_info.get('provides'):
            for prov in pkg_info['provides']:
                # Extract provide name (remove version constraints)
                prov_name = prov.split('[')[0].split('=')[0].split('<')[0].split('>')[0].strip().lower()
                if prov_name in requested_names:
                    explicit_names.add(action.name.lower())
                    break

    # Show transaction summary
    from . import colors
    print(f"\n{colors.bold(f'{len(result.actions)} packages to install')} ({format_size(result.install_size)})\n")

    for action in result.actions:
        marker = "*" if action.name.lower() in explicit_names else " "
        action_str = action.action.value
        if action_str == 'install':
            action_colored = colors.info(f"{action_str:10}")
        elif action_str == 'upgrade':
            action_colored = colors.info(f"{action_str:10}")
        elif action_str == 'remove':
            action_colored = colors.error(f"{action_str:10}")
        else:
            action_colored = f"{action_str:10}"
        print(f"  {marker} {action_colored} {action.nevra}")

    if not args.auto:
        print()
        try:
            answer = input("Proceed? [y/N] ")
            if answer.lower() not in ('y', 'yes', 'o', 'oui'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    if args.test:
        print("\n(dry run - no changes made)")
        return 0

    # Build download items
    print(colors.info("\nDownloading packages..."))
    download_items = []
    media_cache = {}

    for action in result.actions:
        media_name = action.media_name
        if media_name not in media_cache:
            media = db.get_media(media_name)
            media_cache[media_name] = media['url'] if media else ''

        media_url = media_cache[media_name]
        if not media_url:
            print(f"  Warning: no URL for media '{media_name}'")
            continue

        # Parse EVR - remove epoch for filename
        evr = action.evr
        if ':' in evr:
            evr = evr.split(':', 1)[1]
        version, release = evr.rsplit('-', 1) if '-' in evr else (evr, '1')

        download_items.append(DownloadItem(
            name=action.name,
            version=version,
            release=release,
            arch=action.arch,
            media_url=media_url,
            media_name=media_name,
            size=action.size
        ))

    # Download with progress
    downloader = Downloader()

    def progress(name, pkg_num, pkg_total, bytes_done, bytes_total):
        pct = (bytes_done * 100 // bytes_total) if bytes_total > 0 else 0
        print(f"\r\033[K  [{pkg_num}/{pkg_total}] {pct}% - {name}", end='', flush=True)

    dl_results, downloaded, cached = downloader.download_all(download_items, progress)
    print()  # newline after progress

    # Check for failures
    failed = [r for r in dl_results if not r.success]
    if failed:
        print(colors.error(f"\n{len(failed)} download(s) failed:"))
        for r in failed[:5]:
            print(f"  {colors.error(r.item.name)}: {r.error}")
        return 1

    cache_str = colors.warning(str(cached)) if cached > 0 else colors.dim(str(cached))
    print(f"  {colors.success(f'{downloaded} downloaded')}, {cache_str} from cache")

    # Collect RPM paths for installation
    rpm_paths = [r.path for r in dl_results if r.success and r.path]

    if not rpm_paths:
        print("No packages to install")
        return 0

    # Install packages
    from ..core.install import Installer, check_root

    if not check_root():
        print(colors.error("\nError: root privileges required for installation"))
        print("Try: sudo urpm install", ' '.join(args.packages))
        return 1

    # Begin transaction for history
    cmd_line = "urpm install " + " ".join(args.packages)
    transaction_id = db.begin_transaction('install', cmd_line)

    # Record all packages in transaction
    for action in result.actions:
        reason = 'explicit' if action.name.lower() in explicit_names else 'dependency'
        db.record_package(
            transaction_id,
            action.nevra,
            action.name,
            action.action.value,
            reason
        )

    # Setup Ctrl+C handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            # Second Ctrl+C - force abort
            print("\n\nForce abort!")
            db.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")
            print("Press Ctrl+C again to force abort (may leave system inconsistent)")

    signal.signal(signal.SIGINT, sigint_handler)

    print(colors.info(f"\nInstalling {len(rpm_paths)} packages..."))
    installer = Installer()

    def install_progress(name, current, total):
        print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

    try:
        verify_sigs = not getattr(args, 'nosignature', False)
        install_result = installer.install(rpm_paths, install_progress, verify_signatures=verify_sigs)
        print()  # newline after progress

        if not install_result.success:
            print(colors.error(f"\nInstallation failed:"))
            for err in install_result.errors[:5]:
                print(f"  {colors.error(err)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Installation interrupted after {install_result.installed} packages"))
            db.abort_transaction(transaction_id)
            return 130

        print(colors.success(f"  {install_result.installed} packages installed"))
        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        dep_packages = [a.name for a in result.actions
                        if a.name.lower() not in explicit_names]
        explicit_packages = [a.name for a in result.actions
                            if a.name.lower() in explicit_names]
        if dep_packages:
            resolver.mark_as_dependency(dep_packages)
        if explicit_packages:
            resolver.mark_as_explicit(explicit_packages)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_erase(args, db: PackageDatabase) -> int:
    """Handle erase (remove) command."""
    import platform
    import signal

    from ..core.resolver import Resolver, format_size
    from ..core.install import Installer, EraseResult, check_root

    # Check root
    from . import colors

    if not check_root():
        print(colors.error("Error: erase requires root privileges"))
        return 1

    # Resolve what to remove
    arch = platform.machine()
    resolver = Resolver(db, arch=arch)
    clean_deps = getattr(args, 'auto_orphans', False)
    result = resolver.resolve_remove(args.packages, clean_deps=clean_deps)

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

    # Find orphaned dependencies that will be left behind
    erase_names = [a.name for a in result.actions]
    orphans = resolver.find_erase_orphans(erase_names)

    all_actions = result.actions
    total_size = result.remove_size

    # Add orphans to the removal
    if orphans:
        all_actions = list(result.actions) + orphans
        for o in orphans:
            total_size += o.size

    # Show what will be erased
    print(f"\n{colors.bold(f'The following {len(all_actions)} package(s) will be erased:')}")

    if explicit:
        print(f"\n  {colors.info(f'Requested ({len(explicit)}):')}")
        for action in explicit:
            print(f"    {action.nevra}")

    if deps:
        print(f"\n  {colors.warning(f'Reverse dependencies ({len(deps)}):')}")
        for action in deps:
            print(f"    {action.nevra}")

    if orphans:
        print(f"\n  {colors.warning(f'Orphaned dependencies ({len(orphans)}):')}")
        for action in orphans:
            print(f"    {action.nevra}")

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

    # Record transaction
    cmd_line = ' '.join(['urpm', 'erase'] + args.packages)
    transaction_id = db.begin_transaction('erase', cmd_line)

    # Setup Ctrl+C handler
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
            print("Press Ctrl+C again to force abort (may leave system inconsistent)")

    signal.signal(signal.SIGINT, sigint_handler)

    # Erase packages (all from resolution, including reverse deps and orphans)
    print(colors.info(f"\nErasing {len(all_actions)} packages..."))
    packages_to_erase = [action.name for action in all_actions]

    installer = Installer()

    def erase_progress(name, current, total):
        print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

    try:
        # Record packages being erased (with correct reason)
        for action in all_actions:
            reason = 'explicit' if action.name.lower() in explicit_names else 'dependency'
            db.record_package(
                transaction_id,
                action.nevra,
                action.name,
                'remove',
                reason
            )

        erase_result = installer.erase(packages_to_erase, erase_progress)
        print()  # newline after progress

        if not erase_result.success:
            print(colors.error(f"\nErase failed:"))
            for err in erase_result.errors[:5]:
                print(f"  {colors.error(err)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Erase interrupted after {erase_result.erased} packages"))
            db.abort_transaction(transaction_id)
            return 130

        print(colors.success(f"  {erase_result.erased} packages erased"))
        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        erased_packages = [action.name for action in all_actions]
        resolver.unmark_packages(erased_packages)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_update(args, db: PackageDatabase) -> int:
    """Handle update/upgrade command."""
    import platform
    import signal

    from . import colors

    # If --lists, just update media metadata
    if getattr(args, 'lists', False):
        # Reuse media update logic
        args.name = None  # Update all
        return cmd_media_update(args, db)

    from ..core.resolver import Resolver, format_size
    from ..core.download import Downloader, DownloadItem
    from ..core.install import Installer, check_root

    # Determine what to upgrade
    packages = getattr(args, 'packages', []) or []
    upgrade_all = getattr(args, 'all', False) or args.command == 'upgrade'

    if not packages and not upgrade_all:
        print("Specify packages to update, or use --all/-a for full system upgrade")
        print("Use --lists/-l to update media metadata only")
        return 1

    # Check root
    if not check_root():
        print(colors.error("Error: upgrade requires root privileges"))
        return 1

    # Resolve upgrades
    arch = platform.machine()
    resolver = Resolver(db, arch=arch)

    if upgrade_all:
        print("Resolving system upgrade...")
        result = resolver.resolve_upgrade()
    else:
        print(f"Resolving upgrade for: {', '.join(packages)}")
        result = resolver.resolve_upgrade(packages)

    if not result.success:
        print(colors.error("Resolution failed:"))
        for prob in result.problems:
            print(f"  {colors.error(prob)}")
        return 1

    if not result.actions:
        print(colors.success("All packages are up to date."))
        return 0

    # Categorize actions
    upgrades = [a for a in result.actions if a.action.value == 'upgrade']
    installs = [a for a in result.actions if a.action.value == 'install']
    removes = [a for a in result.actions if a.action.value == 'remove']
    downgrades = [a for a in result.actions if a.action.value == 'downgrade']

    # Find orphaned dependencies (unless --noerase-orphans)
    orphans = []
    if upgrades and not getattr(args, 'noerase_orphans', False):
        orphans = resolver.find_upgrade_orphans(upgrades)

    # Show packages by category
    from . import colors
    print(f"\n{colors.bold('Transaction summary:')}")
    if upgrades:
        print(f"\n  {colors.info(f'Upgrade ({len(upgrades)}):')}")
        for a in sorted(upgrades, key=lambda x: x.name.lower()):
            print(f"    {colors.info(a.nevra)}")
    if installs:
        print(f"\n  {colors.success(f'Install ({len(installs)}) - new dependencies:')}")
        for a in sorted(installs, key=lambda x: x.name.lower()):
            print(f"    {colors.success(a.nevra)}")
    if removes:
        print(f"\n  {colors.error(f'Remove ({len(removes)}) - obsoleted:')}")
        for a in sorted(removes, key=lambda x: x.name.lower()):
            print(f"    {colors.error(a.nevra)}")
    if downgrades:
        print(f"\n  {colors.warning(f'Downgrade ({len(downgrades)}):')}")
        for a in sorted(downgrades, key=lambda x: x.name.lower()):
            print(f"    {colors.warning(a.nevra)}")
    if orphans:
        print(f"\n  {colors.error(f'Remove ({len(orphans)}) - orphaned dependencies:')}")
        for a in sorted(orphans, key=lambda x: x.name.lower()):
            print(f"    {colors.error(a.nevra)}")

    if result.install_size > 0:
        print(f"\nDownload size: {format_size(result.install_size)}")

    # Confirmation
    if not getattr(args, 'auto', False):
        try:
            response = input("\nProceed with upgrade? [y/N] ")
            if response.lower() not in ('y', 'yes', 'o', 'oui'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    if getattr(args, 'test', False):
        print("\n(dry run - no changes made)")
        return 0

    # Build download items (only for upgrades and installs)
    to_download = [a for a in result.actions if a.action.value in ('upgrade', 'install')]

    if to_download:
        print(f"\nDownloading {len(to_download)} packages...")
        download_items = []
        media_cache = {}

        for action in to_download:
            media_name = action.media_name
            if media_name not in media_cache:
                media = db.get_media(media_name)
                media_cache[media_name] = media['url'] if media else ''

            media_url = media_cache[media_name]
            if not media_url:
                continue

            # Parse EVR
            evr = action.evr
            if ':' in evr:
                evr = evr.split(':', 1)[1]
            version, release = evr.rsplit('-', 1) if '-' in evr else (evr, '1')

            download_items.append(DownloadItem(
                name=action.name,
                version=version,
                release=release,
                arch=action.arch,
                media_url=media_url,
                media_name=media_name,
                size=action.size
            ))

        # Download
        downloader = Downloader()

        def progress(name, pkg_num, pkg_total, bytes_done, bytes_total):
            pct = (bytes_done * 100 // bytes_total) if bytes_total > 0 else 0
            print(f"\r\033[K  [{pkg_num}/{pkg_total}] {pct}% - {name}", end='', flush=True)

        dl_results, downloaded, cached = downloader.download_all(download_items, progress)
        print()

        # Check failures
        failed = [r for r in dl_results if not r.success]
        if failed:
            print(colors.error(f"\n{len(failed)} download(s) failed:"))
            for r in failed[:5]:
                print(f"  {colors.error(r.item.name)}: {r.error}")
            return 1

        cache_str = colors.warning(str(cached)) if cached > 0 else colors.dim(str(cached))
        print(f"  {colors.success(f'{downloaded} downloaded')}, {cache_str} from cache")

        rpm_paths = [r.path for r in dl_results if r.success and r.path]
    else:
        rpm_paths = []

    # Record transaction
    if upgrade_all:
        cmd_line = "urpm upgrade"
    else:
        cmd_line = "urpm update " + " ".join(packages)
    transaction_id = db.begin_transaction('upgrade', cmd_line)

    # Record packages
    explicit_names = set(p.lower() for p in packages) if packages else set()
    for action in result.actions:
        reason = 'explicit' if action.name.lower() in explicit_names or upgrade_all else 'dependency'
        db.record_package(
            transaction_id,
            action.nevra,
            action.name,
            action.action.value,
            reason
        )

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
        if rpm_paths:
            print(f"\nUpgrading {len(rpm_paths)} packages...")
            installer = Installer()

            def install_progress(name, current, total):
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

            verify_sigs = not getattr(args, 'nosignature', False)
            install_result = installer.install(rpm_paths, install_progress, verify_signatures=verify_sigs)
            print()

            if not install_result.success:
                print(colors.error(f"\nUpgrade failed:"))
                for err in install_result.errors[:5]:
                    print(f"  {colors.error(err)}")
                db.abort_transaction(transaction_id)
                return 1

            if interrupted[0]:
                print(colors.warning(f"\n  Upgrade interrupted after {install_result.installed} packages"))
                db.abort_transaction(transaction_id)
                return 130

            print(colors.success(f"  {install_result.installed} packages upgraded"))

        # Remove orphaned dependencies
        if orphans and not interrupted[0]:
            print(f"\nRemoving {colors.warning(str(len(orphans)))} orphaned dependencies...")
            orphan_names = [a.name for a in orphans]

            orphan_installer = Installer()
            erase_result = orphan_installer.erase(orphan_names)
            if erase_result.success:
                print(colors.success(f"  {erase_result.removed} packages removed"))
                # Record orphan removals in transaction
                for a in orphans:
                    db.record_package(
                        transaction_id,
                        a.nevra,
                        a.name,
                        'remove',
                        'orphan'
                    )
                # Unmark from installed-through-deps.list
                resolver.unmark_packages(orphan_names)
            else:
                print(colors.warning(f"  Warning: failed to remove some orphans"))
                for err in erase_result.errors[:3]:
                    print(f"    {colors.warning(err)}")

        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        # New installs during upgrade are dependencies
        new_deps = [a.name for a in result.actions if a.action.value == 'install']
        if new_deps:
            resolver.mark_as_dependency(new_deps)
        # Removed packages (obsoleted) should be unmarked
        removed = [a.name for a in result.actions if a.action.value == 'remove']
        if removed:
            resolver.unmark_packages(removed)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
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
        from ..core.resolver import Resolver

        arch = platform.machine()
        resolver = Resolver(db, arch=arch)

        print("Checking for updates...")
        result = resolver.resolve_upgrade()

        if not result.success:
            print("Error checking updates:")
            for p in result.problems:
                print(f"  {p}")
            return 1

        upgrades = [a for a in result.actions if a.action.value == 'upgrade']

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
    from ..core.install import Installer, check_root

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
            if response.lower() in ('y', 'yes', 'o', 'oui'):
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
        for nevra in sorted(nevras):
            print(f"    {colors.error(nevra)}")

    print(f"\nDisk space to free: {format_size(total_size)}")

    # Confirmation
    if not getattr(args, 'auto', False):
        try:
            response = input("\nRemove these packages? [y/N] ")
            if response.lower() not in ('y', 'yes', 'o', 'oui'):
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

        installer = Installer()

        def erase_progress(name, current, total):
            print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

        result = installer.erase(package_names, erase_progress)
        print()

        if not result.success:
            print(colors.error(f"\nRemoval failed:"))
            for err in result.errors[:5]:
                print(f"  {colors.error(err)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Interrupted after {result.erased} packages"))
            db.abort_transaction(transaction_id)
            return 130

        print(colors.success(f"  {result.erased} packages removed"))

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
            for p in trans['dependencies'][:20]:
                action = p['action']
                print(f"    {_color_action(f'{action:10}')} {colors.dim(p['pkg_nevra'])}")
            if dep_count > 20:
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

    print(f"\n{colors.bold('ID'):>4} | {colors.bold('Date'):10} | {colors.bold('Action'):8} | {colors.bold('Status'):11} | {colors.bold('Packages')}")
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
        print("Usage: urpm config <blacklist|redlist|kernel-keep> ...")
        print("\nSubcommands:")
        print("  blacklist  Manage blacklist (critical packages)")
        print("  redlist    Manage redlist (packages requiring confirmation)")
        print("  kernel-keep  Number of kernels to keep")
        return 1

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
            if response.lower() not in ('y', 'yes', 'o', 'oui'):
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


def cmd_undo(args, db: PackageDatabase) -> int:
    """Handle undo command - undo last or specific transaction."""
    import signal
    import platform
    from ..core.install import Installer, check_root
    from ..core.resolver import Resolver
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
            if answer.lower() not in ('y', 'yes', 'o', 'oui'):
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
        installer = Installer()

        # First remove packages that were installed (all at once for dependency handling)
        if to_remove and not interrupted:
            print(colors.info(f"\nRemoving {len(to_remove)} package(s)..."))

            def erase_progress(name, current, total):
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

            result = installer.erase(to_remove, erase_progress)
            print()  # newline after progress

            if not result.success:
                print(colors.error("\nErase failed:"))
                for err in result.errors[:10]:
                    print(f"  {colors.error(err)}")
                db.abort_transaction(undo_trans_id)
                return 1

            if interrupted:
                db.abort_transaction(undo_trans_id)
                print(colors.warning(f"\nUndo interrupted after {result.erased} packages"))
                return 130

            # Record removed packages
            for name in to_remove:
                db.record_package(undo_trans_id, name, name, 'remove', 'explicit')

            print(colors.success(f"  {result.erased} packages removed"))

        # Then reinstall packages that were removed
        if to_install and not interrupted:
            print(colors.info(f"\nReinstalling {len(to_install)} package(s)..."))
            for nevra in to_install:
                if interrupted:
                    break
                print(f"  {colors.warning('Note:')} {nevra} needs to be downloaded/installed")
                # TODO: integrate with resolver/downloader for proper reinstall

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
    from ..core.install import Installer, check_root
    from ..core.resolver import Resolver
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
            if answer.lower() not in ('y', 'yes', 'o', 'oui'):
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
        installer = Installer()

        # First remove packages that were installed (all at once for dependency handling)
        if to_remove and not interrupted:
            print(colors.info(f"\nRemoving {len(to_remove)} package(s)..."))

            def erase_progress(name, current, total):
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

            result = installer.erase(to_remove, erase_progress)
            print()  # newline after progress

            if not result.success:
                print(colors.error("\nErase failed:"))
                for err in result.errors[:10]:
                    print(f"  {colors.error(err)}")
                db.abort_transaction(trans_id)
                return 1

            if interrupted:
                db.abort_transaction(trans_id)
                print(colors.warning(f"\nRollback interrupted after {result.erased} packages"))
                return 130

            # Record removed packages
            for name in to_remove:
                db.record_package(trans_id, name, name, 'remove', 'explicit')

            print(colors.success(f"  {result.erased} packages removed"))

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
    from ..core.install import Installer, check_root
    from ..core.resolver import Resolver

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
    for nevra in all_orphans[:10]:
        print(f"  {nevra}")
    if len(all_orphans) > 10:
        print(f"  ... and {len(all_orphans) - 10} more")

    if not args.auto:
        try:
            answer = input("\nRemove these packages? [y/N] ")
            if answer.lower() not in ('y', 'yes', 'o', 'oui'):
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

        # Erase packages
        print(f"\nErasing {len(packages_to_erase)} orphan dependencies...")
        installer = Installer()

        def erase_progress(name, current, total):
            print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)

        erase_result = installer.erase(packages_to_erase, erase_progress)
        print()

        if not erase_result.success:
            print(f"\nErase failed:")
            for err in erase_result.errors[:5]:
                print(f"  {err}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted_flag[0]:
            print(f"\n  Erase interrupted after {erase_result.erased} packages")
            db.abort_transaction(transaction_id)
            return 130

        print(f"  {erase_result.erased} packages erased")

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
    pattern = args.pattern

    found = []

    # Search in installed packages via rpm
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
                found.append({
                    'nevra': f"{name}-{version}-{release}.{arch}",
                    'file': pattern,
                    'installed': True
                })
        else:
            # Pattern search - need to iterate all packages
            import fnmatch
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                if name == 'gpg-pubkey':
                    continue
                files = hdr[rpm.RPMTAG_FILENAMES] or []
                for f in files:
                    if fnmatch.fnmatch(f, f'*{pattern}*') or pattern in f:
                        version = hdr[rpm.RPMTAG_VERSION]
                        release = hdr[rpm.RPMTAG_RELEASE]
                        arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                        found.append({
                            'nevra': f"{name}-{version}-{release}.{arch}",
                            'file': f,
                            'installed': True
                        })
                        break  # Only show package once
    except ImportError:
        pass

    if not found:
        print(f"No installed package contains '{pattern}'")
        print("Note: searching non-installed packages requires hdlist (not yet implemented)")
        return 1

    print(f"Packages containing '{pattern}':")
    for match in found[:50]:
        print(f"  {match['nevra']}: {match['file']}")

    if len(found) > 50:
        print(f"  ... and {len(found) - 50} more")

    return 0


def cmd_depends(args, db: PackageDatabase) -> int:
    """Handle depends command - show package dependencies."""
    package = args.package
    pkg_name = _extract_pkg_name(package)
    show_tree = getattr(args, 'tree', False)
    legacy = getattr(args, 'legacy', False)

    # Build provider cache for resolving capabilities
    provider_cache = {}

    def find_provider(capability: str) -> str:
        """Find which package provides a capability."""
        # Normalize: remove version info and (64bit) suffix
        cap_base = capability.split()[0]
        # Remove (64bit), ()(64bit), (GLIBC_xxx)(64bit), etc.
        if '(' in cap_base:
            cap_base = cap_base.split('(')[0]

        if cap_base in provider_cache:
            return provider_cache[cap_base]

        # Check installed packages first
        try:
            import rpm
            ts = rpm.TransactionSet()
            # Try the full capability first (for soname matching)
            for hdr in ts.dbMatch('providename', capability.split()[0]):
                name = hdr[rpm.RPMTAG_NAME]
                provider_cache[cap_base] = name
                return name
            # Fallback to base name
            for hdr in ts.dbMatch('providename', cap_base):
                name = hdr[rpm.RPMTAG_NAME]
                provider_cache[cap_base] = name
                return name
        except:
            pass

        # Check database
        results = db.whatprovides(cap_base)
        if results:
            name = results[0]['name']
            provider_cache[cap_base] = name
            return name

        provider_cache[cap_base] = None
        return None

    # Try to find package in database
    pkg = db.get_package_smart(package)

    # Also check installed via rpm
    installed_deps = None
    try:
        import rpm
        ts = rpm.TransactionSet()
        mi = ts.dbMatch('name', pkg_name)
        for hdr in mi:
            requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
            installed_deps = list(requires)
            break
    except ImportError:
        pass

    if not pkg and not installed_deps:
        print(f"Package '{package}' not found")
        return 1

    # Use installed deps if available, otherwise database
    deps = installed_deps if installed_deps else pkg.get('requires', [])

    if not deps:
        print(f"{package}: no dependencies")
        return 0

    # Filter out file deps and rpmlib
    deps = [d for d in deps if not d.startswith('/') and not d.startswith('rpmlib(')]

    # Group by provider package
    by_provider = {}
    unresolved = []
    for dep in deps:
        provider = find_provider(dep)
        if provider:
            if provider not in by_provider:
                by_provider[provider] = []
            by_provider[provider].append(dep)
        else:
            # Unresolved capability - keep for --legacy mode only
            unresolved.append(dep)

    show_all = getattr(args, 'all', False)

    if legacy and not show_tree:
        # --legacy: raw capabilities like urpmq/dnf
        print(f"Dependencies of {package} ({len(deps)}):")
        for dep in sorted(deps):
            print(f"  {dep}")
    elif legacy and show_tree:
        # --tree --legacy: tree with capabilities detail per package
        print(f"{package}")
        _print_dep_tree_legacy(db, by_provider, find_provider, visited={package}, prefix="", max_depth=4)
    elif show_tree:
        # --tree: recursive tree of packages only
        print(f"{package}")
        providers = sorted(by_provider.keys())
        _print_dep_tree_packages(db, providers, find_provider, visited={package}, prefix="", max_depth=4)
    elif show_all:
        # --all: flat list of all recursive dependencies
        all_deps = set(by_provider.keys())
        visited = {package}
        to_process = list(by_provider.keys())

        while to_process:
            prov = to_process.pop(0)
            if prov in visited:
                continue
            visited.add(prov)

            sub_pkg = db.get_package(prov)
            if sub_pkg and sub_pkg.get('requires'):
                sub_deps = [d for d in sub_pkg['requires']
                           if not d.startswith('/') and not d.startswith('rpmlib(')]
                for dep in sub_deps:
                    sub_prov = find_provider(dep)
                    if sub_prov and sub_prov not in visited:
                        all_deps.add(sub_prov)
                        to_process.append(sub_prov)

        print(f"All dependencies of {package}: {len(all_deps)} packages\n")
        for prov in sorted(all_deps):
            print(f"  {prov}")
    else:
        # Default: flat list of direct dependencies
        print(f"Dependencies of {package}: {len(by_provider)} packages\n")
        for provider in sorted(by_provider.keys()):
            print(f"  {provider}")

    return 0


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


def cmd_rdepends(args, db: PackageDatabase) -> int:
    """Handle rdepends command - show reverse dependencies."""
    package = args.package
    pkg_name = _extract_pkg_name(package)
    show_tree = getattr(args, 'tree', False)

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
                if cap not in provides:
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

    if show_tree:
        # Recursive tree with reverse arrows
        print(f"{package}")
        _print_rdep_tree(direct_rdeps, get_rdeps, visited={package}, prefix="", max_depth=3)
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
            print(f"  {rdep}")
    else:
        # Flat list of direct reverse dependencies
        print(f"Packages that depend on {package}: {len(direct_rdeps)}\n")
        for rdep in direct_rdeps:
            print(f"  {rdep}")

    return 0


def _print_rdep_tree(rdeps: list, get_rdeps, visited: set, prefix: str, max_depth: int, depth: int = 0):
    """Print reverse dependency tree with reverse arrows to show direction."""
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
            print(f"{prefix}{connector}{pkg_name} (circular)")
            continue

        sub_rdeps = get_rdeps(pkg_name)
        if sub_rdeps:
            print(f"{prefix}{connector}{pkg_name} ({len(sub_rdeps)})")
            visited.add(pkg_name)
            _print_rdep_tree(sub_rdeps, get_rdeps, visited, child_prefix, max_depth, depth + 1)
        else:
            print(f"{prefix}{connector}{pkg_name}")


def cmd_not_implemented(args, db: PackageDatabase) -> int:
    """Placeholder for not yet implemented commands."""
    print(f"Command '{args.command}' not yet implemented")
    return 1


# =============================================================================
# Main entry point
# =============================================================================

def main(argv=None) -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    # Initialize color support
    from . import colors
    colors.init(nocolor=getattr(args, 'nocolor', False))

    if not args.command:
        parser.print_help()
        return 1

    # Open database
    db = PackageDatabase()
    
    try:
        # Route to command handler
        if args.command in ('install', 'i'):
            return cmd_install(args, db)

        elif args.command in ('erase', 'e'):
            return cmd_erase(args, db)

        elif args.command in ('update', 'u', 'upgrade'):
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
            else:
                return cmd_not_implemented(args, db)
        
        elif args.command in ('cache', 'c'):
            if args.cache_command == 'info':
                return cmd_cache_info(args, db)
            elif args.cache_command == 'clean':
                return cmd_cache_clean(args, db)
            elif args.cache_command == 'rebuild':
                return cmd_cache_rebuild(args, db)
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

        elif args.command in ('provides', 'p'):
            return cmd_provides(args, db)

        elif args.command in ('whatprovides', 'wp'):
            return cmd_whatprovides(args, db)

        elif args.command in ('find', 'f'):
            return cmd_find(args, db)

        elif args.command in ('depends', 'd'):
            return cmd_depends(args, db)

        elif args.command in ('rdepends', 'rd'):
            return cmd_rdepends(args, db)

        elif args.command in ('config', 'cfg'):
            return cmd_config(args)

        elif args.command in ('key', 'k'):
            return cmd_key(args)

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
