"""Package dependency commands: depends, rdepends, why, recommends, suggests."""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.package import extract_pkg_name as _extract_pkg_name
from ..helpers.alternatives import (
    PreferencesMatcher,
    _resolve_with_alternatives,
)


def cmd_depends(args, db: 'PackageDatabase') -> int:
    """Handle depends command - show package dependencies."""
    from ...core.resolver import Resolver
    from .. import colors

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
    from .. import colors

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
    from .. import colors

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


def _print_dep_tree_packages(db: 'PackageDatabase', providers: list, find_provider, visited: set, prefix: str, max_depth: int, depth: int = 0):
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


def _print_dep_tree_legacy(db: 'PackageDatabase', by_provider: dict, find_provider, visited: set, prefix: str, max_depth: int, depth: int = 0):
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


def _get_rdeps(pkg_name: str, db: 'PackageDatabase', dep_types: str = 'R',
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


def cmd_rdepends(args, db: 'PackageDatabase') -> int:
    """Handle rdepends command - show reverse dependencies."""
    from .. import colors
    from ...core.resolver import Resolver

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


def _build_rdeps_graph(db: 'PackageDatabase') -> dict:
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
                                    max_depth: int, db: 'PackageDatabase') -> set:
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
    from .. import colors

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


def cmd_recommends(args, db: 'PackageDatabase') -> int:
    """Handle recommends command - show packages recommended by a package."""
    from ...core.resolver import Resolver

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


def cmd_whatrecommends(args, db: 'PackageDatabase') -> int:
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


def cmd_suggests(args, db: 'PackageDatabase') -> int:
    """Handle suggests command - show packages suggested by a package."""
    from ...core.resolver import Resolver

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


def cmd_whatsuggests(args, db: 'PackageDatabase') -> int:
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


def cmd_why(args, db: 'PackageDatabase') -> int:
    """Handle why command - explain why a package is installed."""
    from ...core.resolver import Resolver
    from .. import colors
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
