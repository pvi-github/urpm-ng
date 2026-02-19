"""Package query commands (search, show, list, find, provides)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def _cmd_search_unavailable(args, db: 'PackageDatabase') -> int:
    """List installed packages not available in any media (urpmq --unavailable)."""
    import rpm
    from .. import colors

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


def cmd_search(args, db: 'PackageDatabase') -> int:
    """Handle search command."""
    import re
    from .. import colors
    from ...core.operations import PackageOperations

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


def cmd_show(args, db: 'PackageDatabase') -> int:
    """Handle show/info command."""
    from .. import colors
    from ...core.operations import PackageOperations

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
        from .. import display
        display.print_package_list(pkg['requires'], max_lines=10, color_func=colors.dim)

    if pkg.get('recommends'):
        rec_count = len(pkg['recommends'])
        print(f"\n{colors.bold(f'Recommends ({rec_count}):')} ")
        from .. import display
        display.print_package_list(pkg['recommends'], max_lines=10, color_func=colors.dim)

    if pkg.get('suggests'):
        sug_count = len(pkg['suggests'])
        print(f"\n{colors.bold(f'Suggests ({sug_count}):')} ")
        from .. import display
        display.print_package_list(pkg['suggests'], max_lines=10, color_func=colors.dim)

    if pkg.get('provides'):
        prov_count = len(pkg['provides'])
        print(f"\n{colors.bold(f'Provides ({prov_count}):')} ")
        from .. import display
        display.print_package_list(pkg['provides'], max_lines=5, color_func=colors.dim)

    if pkg.get('conflicts'):
        conf_count = len(pkg['conflicts'])
        print(f"\n{colors.bold(f'Conflicts ({conf_count}):')} ")
        from .. import display
        display.print_package_list(pkg['conflicts'], max_lines=5, color_func=colors.dim)

    if pkg.get('obsoletes'):
        obs_count = len(pkg['obsoletes'])
        print(f"\n{colors.bold(f'Obsoletes ({obs_count}):')} ")
        from .. import display
        display.print_package_list(pkg['obsoletes'], max_lines=5, color_func=colors.dim)

    print()
    return 0


# Media commands moved to urpm/cli/commands/media.py
# Server commands moved to urpm/cli/commands/server.py
# Mirror commands moved to urpm/cli/commands/mirror.py
# Cache commands moved to urpm/cli/commands/cache.py



def cmd_list(args, db: 'PackageDatabase') -> int:
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
        from ...core.operations import PackageOperations

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



def cmd_provides(args, db: 'PackageDatabase') -> int:
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


def cmd_whatprovides(args, db: 'PackageDatabase') -> int:
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


def cmd_find(args, db: 'PackageDatabase') -> int:
    """Handle find command - find packages containing a file (like urpmf)."""
    from .. import colors
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
                    from ...core.install import check_root
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



