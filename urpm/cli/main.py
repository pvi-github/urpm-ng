"""
Main CLI entry point for urpm

Provides a modern CLI with short aliases:
- urpm install / urpm i
- urpm remove / urpm r
- urpm search / urpm s
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
    
    # =========================================================================
    # remove / r
    # =========================================================================
    remove_parser = subparsers.add_parser(
        'remove', aliases=['r'],
        help='Remove packages'
    )
    remove_parser.add_argument(
        'packages', nargs='+',
        help='Package names to remove'
    )
    remove_parser.add_argument(
        '--auto', '-y',
        action='store_true',
        help='No confirmation'
    )
    remove_parser.add_argument(
        '--test',
        action='store_true',
        help='Dry run (simulation)'
    )
    
    # =========================================================================
    # search / s
    # =========================================================================
    search_parser = subparsers.add_parser(
        'search', aliases=['s'],
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
    # query / q (alias for search, compat with urpmq)
    # =========================================================================
    query_parser = subparsers.add_parser(
        'query', aliases=['q'],
        help='Query packages (alias for search)'
    )
    query_parser.add_argument(
        'pattern',
        help='Search pattern'
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
        help='Find package providing a file/capability'
    )
    provides_parser.add_argument(
        'capability',
        help='File path or capability name'
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
        help='Show as tree'
    )
    depends_parser.add_argument(
        '--recursive',
        action='store_true',
        help='Show recursive dependencies'
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
    
    # =========================================================================
    # autoremove / ar
    # =========================================================================
    autoremove_parser = subparsers.add_parser(
        'autoremove', aliases=['ar'],
        help='Remove orphaned packages'
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
    cache_subparsers.add_parser('clean', help='Clean cache')
    cache_subparsers.add_parser('rebuild', help='Rebuild cache')
    cache_subparsers.add_parser('stats', help='Cache statistics')
    
    return parser


# =============================================================================
# Command handlers
# =============================================================================

def cmd_search(args, db: PackageDatabase) -> int:
    """Handle search command."""
    results = db.search(args.pattern)
    
    if not results:
        print(f"No packages found for '{args.pattern}'")
        return 1
    
    for pkg in results:
        nevra = f"{pkg['name']}-{pkg['version']}-{pkg['release']}.{pkg['arch']}"
        summary = pkg.get('summary', '')[:60]
        print(f"{nevra:50} {summary}")
    
    return 0


def cmd_show(args, db: PackageDatabase) -> int:
    """Handle show/info command."""
    pkg = db.get_package(args.package)
    
    if not pkg:
        print(f"Package '{args.package}' not found")
        return 1
    
    print(f"\nName:         {pkg['name']}")
    print(f"Version:      {pkg['version']}-{pkg['release']}")
    print(f"Architecture: {pkg['arch']}")
    print(f"Size:         {pkg['size'] / 1024 / 1024:.1f} MB")
    
    if pkg.get('group_name'):
        print(f"Group:        {pkg['group_name']}")
    if pkg.get('summary'):
        print(f"Summary:      {pkg['summary']}")
    
    if pkg.get('requires'):
        print(f"\nRequires ({len(pkg['requires'])}):")
        for dep in pkg['requires'][:10]:
            print(f"  - {dep}")
        if len(pkg['requires']) > 10:
            print(f"  ... and {len(pkg['requires']) - 10} more")
    
    if pkg.get('provides'):
        print(f"\nProvides ({len(pkg['provides'])}):")
        for prov in pkg['provides'][:5]:
            print(f"  - {prov}")
        if len(pkg['provides']) > 5:
            print(f"  ... and {len(pkg['provides']) - 5} more")
    
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
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Open database
    db = PackageDatabase()
    
    try:
        # Route to command handler
        if args.command in ('search', 's', 'query', 'q'):
            return cmd_search(args, db)
        
        elif args.command in ('show', 'sh', 'info'):
            return cmd_show(args, db)
        
        elif args.command in ('media', 'm'):
            if args.media_command in ('list', 'l', 'ls', None):
                return cmd_media_list(args, db)
            else:
                return cmd_not_implemented(args, db)
        
        elif args.command in ('cache', 'c'):
            if args.cache_command == 'info':
                return cmd_cache_info(args, db)
            else:
                return cmd_not_implemented(args, db)
        
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
