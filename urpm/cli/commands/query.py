"""Package query commands (search, show, list, find, provides)."""

from typing import TYPE_CHECKING

from ...i18n import _, ngettext, confirm_yes
from ..helpers.package import extract_pkg_name as _extract_pkg_name
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
        print(colors.success(_("All installed packages are available in configured media")))
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
            print(colors.warning(_("No unavailable packages match '{pattern}'").format(pattern=args.pattern)))
            return 1

    # Display results
    for pkg in unavailable:
        name = colors.bold(pkg['name'])
        version = pkg['version']
        release_arch = colors.dim(f"{pkg['release']}.{pkg['arch']}")
        print(f"{name}-{version}-{release_arch}")

    print(colors.dim("\n" + ngettext(
        "{count} unavailable package",
        "{count} unavailable packages",
        len(unavailable)).format(count=len(unavailable))))
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
        print(colors.error(_("Error: search pattern required")))
        print(colors.dim(_("  Use --unavailable to list packages not in any media")))
        return 1

    ops = PackageOperations(db)
    results = ops.search_packages(args.pattern, search_provides=True)

    if not results:
        print(colors.warning(_("No packages found for '{pattern}'").format(pattern=args.pattern)))
        return 1

    # ANSI codes without reset for proper nesting
    GREEN = '\033[92m'
    BOLD = '\033[1m'
    DIM = '\033[90m'
    RESET = '\033[0m'

    import shutil
    term_width = shutil.get_terminal_size().columns

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

        summary = pkg.get('summary', '')
        summary_display = highlight_with_base(summary, pattern, '')

        # Show which provide matched if found via provides
        if pkg.get('matched_provide'):
            # Strip version info like [== 0.29.0] from provides
            provide_name = re.sub(r'\[.*?\]', '', pkg['matched_provide']).strip()
            provide_text = f"(provides: {provide_name})"
            provide_display = f"{DIM}{highlight_with_base(provide_text, pattern, DIM)}{RESET}"
            # Right-align provides on terminal width
            nevra_plain = f"{display_name}-{display_version}-{pkg['release']}.{pkg['arch']}"
            provide_plain = f"(provides: {provide_name})"
            padding = max(2, term_width - len(nevra_plain) - len(provide_plain))
            print(f"{nevra_display}{' ' * padding}{provide_display}")
        else:
            print(nevra_display)

        # Summary below, indented
        if summary:
            print(f"  {summary_display}")
        print()

    print(colors.dim("\n" + ngettext(
        "{count} package found",
        "{count} packages found",
        len(results)).format(count=len(results))))
    return 0


def cmd_show(args, db: 'PackageDatabase') -> int:
    """Handle show/info command."""
    from .. import colors
    from ...core.operations import PackageOperations

    ops = PackageOperations(db)
    pkg = ops.get_package_info(args.package)

    if not pkg:
        print(colors.error(_("Package '{package}' not found").format(package=args.package)))
        return 1

    print(f"\n{colors.bold(_('Name:'))}         {colors.info(pkg['name'])}")
    print(f"{colors.bold(_('Version:'))}      {pkg['version']}-{pkg['release']}")
    print(f"{colors.bold(_('Architecture:'))} {pkg['arch']}")
    print(f"{colors.bold(_('Size:'))}         {pkg['size'] / 1024 / 1024:.1f} MB")

    if pkg.get('group_name'):
        print(f"{colors.bold(_('Group:'))}        {pkg['group_name']}")
    if pkg.get('summary'):
        print(f"{colors.bold(_('Summary:'))}      {pkg['summary']}")

    if pkg.get('requires'):
        req_count = len(pkg['requires'])
        print("\n" + colors.bold(_("Requires ({count}):").format(count=req_count)) + " ")
        from .. import display
        display.print_package_list(pkg['requires'], max_lines=10, color_func=colors.dim)

    if pkg.get('recommends'):
        rec_count = len(pkg['recommends'])
        print("\n" + colors.bold(_("Recommends ({count}):").format(count=rec_count)) + " ")
        from .. import display
        display.print_package_list(pkg['recommends'], max_lines=10, color_func=colors.dim)

    if pkg.get('suggests'):
        sug_count = len(pkg['suggests'])
        print("\n" + colors.bold(_("Suggests ({count}):").format(count=sug_count)) + " ")
        from .. import display
        display.print_package_list(pkg['suggests'], max_lines=10, color_func=colors.dim)

    if pkg.get('provides'):
        prov_count = len(pkg['provides'])
        print("\n" + colors.bold(_("Provides ({count}):").format(count=prov_count)) + " ")
        from .. import display
        display.print_package_list(pkg['provides'], max_lines=5, color_func=colors.dim)

    if pkg.get('conflicts'):
        conf_count = len(pkg['conflicts'])
        print("\n" + colors.bold(_("Conflicts ({count}):").format(count=conf_count)) + " ")
        from .. import display
        display.print_package_list(pkg['conflicts'], max_lines=5, color_func=colors.dim)

    if pkg.get('obsoletes'):
        obs_count = len(pkg['obsoletes'])
        print("\n" + colors.bold(_("Obsoletes ({count}):").format(count=obs_count)) + " ")
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

            print("\n" + ngettext(
                "{count} package installed",
                "{count} packages installed",
                len(packages)).format(count=len(packages)))
        except ImportError:
            print(_("Error: rpm module not available"))
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
        print("\n" + ngettext(
            "{count} package available",
            "{count} packages available",
            count).format(count=count))

    elif filter_type in ('updates', 'upgradable'):
        # List packages with available updates
        from ...core.operations import PackageOperations

        ops = PackageOperations(db)
        print(_("Checking for updates..."))
        success, upgrades, problems = ops.get_updates()

        if not success:
            print(_("Error checking updates:"))
            for p in problems:
                print(f"  {p}")
            return 1

        if not upgrades:
            print(_("All packages are up to date."))
            return 0

        for u in sorted(upgrades, key=lambda x: x.name.lower()):
            print(f"{u.nevra}")

        print("\n" + ngettext(
            "{count} package can be upgraded",
            "{count} packages can be upgraded",
            len(upgrades)).format(count=len(upgrades)))

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

        print("\n" + _("{count} packages ({installed} installed)").format(count=count, installed=len(installed)))

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
        print(_("Package '{package}' not found").format(package=package))
        return 1

    print(_("Package {name} provides ({count}):").format(name=found_name, count=len(provides)) + "\n")
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
        print(_("No package provides '{capability}'").format(capability=capability))
        return 1

    # Show installed matches first
    if installed_matches:
        print(_("Installed:"))
        for pkg in installed_matches:
            print(f"  {pkg['nevra']}")

    # Show available (not installed)
    installed_nevras = {m['nevra'] for m in installed_matches}
    available = [r for r in results if r['nevra'] not in installed_nevras]

    if available:
        if installed_matches:
            print(_("\nAvailable:"))
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

    # Search in installed packages via rpm.  Three regimes, each with
    # the cheapest available path:
    #
    #   * pattern is a bare basename (no ``/``, no wildcard) → use
    #     rpm's basenames index via ``mi.pattern(..., RPMMIRE_STRCMP,
    #     basename)``: ~50 ms instead of the second-long full-rpmdb
    #     scan.  Note that ``dbMatch('basenames', X)`` is a different
    #     API that wants a *full path*, not a basename.
    #   * pattern starts with ``/`` and has no wildcard → exact path
    #     lookup via ``dbMatch('basenames', pattern)``.
    #   * anything with wildcards → no usable index, fall back to
    #     iterating every installed header.
    if search_installed or search_both:
        try:
            import rpm
            import os.path
            ts = rpm.TransactionSet()

            has_wildcards = '*' in pattern or '?' in pattern

            def _hdr_to_nevra(hdr):
                name = hdr[rpm.RPMTAG_NAME]
                if name == 'gpg-pubkey':
                    return None
                version = hdr[rpm.RPMTAG_VERSION]
                release = hdr[rpm.RPMTAG_RELEASE]
                arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                return name, f"{name}-{version}-{release}.{arch}"

            if not has_wildcards and pattern.startswith('/'):
                # Exact full-path lookup — rpm's ``basenames`` index is
                # keyed by full file path despite its misleading name.
                for hdr in ts.dbMatch('basenames', pattern):
                    info = _hdr_to_nevra(hdr)
                    if info is None:
                        continue
                    installed_found.append({'nevra': info[1], 'file': pattern})

            elif not has_wildcards:
                # Bare basename — query the basenames index by
                # exact-string compare, then report which path(s) of
                # each matching package carry that basename.
                basename_lower = pattern.lower()
                mi = ts.dbMatch()
                mi.pattern('basenames', rpm.RPMMIRE_STRCMP, pattern)
                for hdr in mi:
                    info = _hdr_to_nevra(hdr)
                    if info is None:
                        continue
                    nevra = info[1]
                    for f in (hdr[rpm.RPMTAG_FILENAMES] or []):
                        if os.path.basename(f).lower() == basename_lower:
                            installed_found.append({'nevra': nevra, 'file': f})

            else:
                # Wildcard pattern — no usable index, iterate
                # everything.  Mirrors the historical semantics of the
                # SQLite fallback in ``search_files``.
                import fnmatch, re as _re
                fnmatch_pattern = pattern.replace('%', '*').replace('_', '?')
                _pat_re = _re.compile(
                    fnmatch.translate(fnmatch_pattern), _re.IGNORECASE
                )
                for hdr in ts.dbMatch():
                    info = _hdr_to_nevra(hdr)
                    if info is None:
                        continue
                    nevra = info[1]
                    for f in (hdr[rpm.RPMTAG_FILENAMES] or []):
                        if _pat_re.match(f):
                            installed_found.append({'nevra': nevra, 'file': f})
        except ImportError:
            pass

    # Search in available packages by streaming each media's
    # files.xml.lzma — no DB cache, no FTS, no opt-in.  The files are
    # already on disk after a regular ``urpm media update``.
    if search_available or search_both:
        from ...core.config import get_base_dir, get_media_local_path
        from ...core.files_xml import iter_file_matches
        from ...core.sync import FILES_XML_PATH

        base_dir = get_base_dir()
        media_files = []
        missing_count = 0
        for media in db.list_media():
            if not media.get('enabled', True):
                continue
            files_xml = get_media_local_path(media, base_dir) / FILES_XML_PATH
            # genhdlist2 produces ~65-byte stub files.xml.lzma for empty
            # media (typically the updates tree of an unreleased distro);
            # treat them as absent rather than parsing the empty payload
            # on every query.
            if files_xml.exists() and files_xml.stat().st_size > 200:
                media_files.append((files_xml, media['name']))
            else:
                missing_count += 1

        if not media_files:
            if search_available:
                print(colors.warning(_(
                    "No files.xml.lzma available on disk. "
                    "Run 'sudo urpm media update' first."
                )))
                return 1
            # else: searching both — silently skip the available side
        else:
            matches = iter_file_matches(
                media_files,
                pattern,
                all_versions=getattr(args, 'all_versions', False),
                limit=args.limit if getattr(args, 'limit', 0) > 0 else 0,
            )
            for m in matches:
                available_found.append({
                    'nevra': m.nevra,
                    'file': m.path,
                    'media': m.media_name,
                })

            # If the user explicitly asked for the available side and got
            # nothing while some media lack their files.xml.lzma, hint
            # that the missing data may explain the empty result.  We
            # stay quiet otherwise: empty stubs are normal during RCs.
            if (search_available and not matches and missing_count):
                print(colors.dim(_(
                    "Note: {count} enabled media have no files.xml.lzma "
                    "on disk (run 'urpm media update' to fetch)."
                ).format(count=missing_count)))

    # Display results
    if not installed_found and not available_found:
        print(_("No package contains '{pattern}'").format(pattern=pattern))
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
                print(colors.dim("    ... " + _("({count} more)").format(count=hidden)))
                total_hidden += hidden

        return total_shown, total_hidden

    total_shown = 0
    total_hidden = 0

    if installed_found:
        print(colors.info(_("Installed:")))
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
            print(colors.info(_("Available (not installed):")))
            grouped = group_by_package(available_not_installed)
            shown, hidden = display_grouped(grouped, FILES_PER_PKG, show_media=True)
            total_shown += shown
            total_hidden += hidden

    # Summary if some files were hidden
    if total_hidden > 0:
        print("\n" + colors.dim(_("{count} files hidden (use --show-all to see all)").format(count=total_hidden)))

    return 0



