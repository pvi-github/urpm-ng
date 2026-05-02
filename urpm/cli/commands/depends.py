"""Package dependency commands: depends, rdepends, why, recommends, suggests."""

import sys
from typing import TYPE_CHECKING

from ...i18n import _, ngettext
if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.package import (
    extract_pkg_name as _extract_pkg_name,
    pick_arch_for_lookup,
    resolve_target_arch,
    system_arch,
)
from ..helpers.alternatives import (
    PreferencesMatcher,
    _resolve_with_alternatives,
)


def cmd_depends(args, db: 'PackageDatabase') -> int:
    """Handle depends command - show package dependencies.

    Uses libsolv SAT solver for accurate dependency resolution.
    Modes:
        - default: direct dependencies + unresolved alternatives
        - --all: complete transitive dependency closure
        - --tree: dependency tree with graph from solver
        - --legacy: raw capability strings (unchanged)
    """
    from ...core.resolver import Resolver, TransactionType
    from .. import colors

    package = args.package
    pkg_name = _extract_pkg_name(package)
    show_tree = getattr(args, 'tree', False)
    legacy = getattr(args, 'legacy', False)
    show_all = getattr(args, 'all', False)
    prefer_str = getattr(args, 'prefer', None)

    preferences = PreferencesMatcher(prefer_str)

    if legacy:
        # --legacy: raw capabilities from libsolv (no resolution)
        resolver = Resolver(db)
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
            except Exception:
                pass

        if not requires:
            pkg = db.get_package_smart(package)
            if not pkg:
                print(_("Package '{package}' not found").format(package=package))
                return 1
            print(_("{package}: no dependencies").format(package=package))
            return 0

        print(_("Dependencies of {package} ({count}):").format(
            package=package, count=len(requires)))
        for cap in sorted(requires):
            print(f"  {cap}")
        return 0

    # --- Solver-based resolution ---
    # ignore_installed=True: resolve without @System for complete dependency closure
    # _preserve_pool=True: avoid costly pool recreation in alternatives loop
    resolver = Resolver(db, ignore_installed=True)
    resolver._preserve_pool = True

    if show_tree or show_all:
        # --tree / --all: need complete resolution (interactive alternatives if needed)
        choices = {}
        result, aborted = _resolve_with_alternatives(
            resolver, [pkg_name], choices,
            auto_mode=False, preferences=preferences
        )

        if aborted:
            print(_("Aborted"))
            return 1

        if not result or not result.success:
            if result and result.problems:
                for p in result.problems:
                    print(colors.error(p))
            return 1

        dep_names = sorted({a.name for a in result.actions
                           if a.action == TransactionType.INSTALL
                           and a.name != pkg_name})

        if show_all:
            print(ngettext(
                "All dependencies of {package}: {count} package",
                "All dependencies of {package}: {count} packages",
                len(dep_names)
            ).format(package=package, count=len(dep_names)) + "\n")
            for name in dep_names:
                print(f"  {name}")
        else:
            # --tree: build graph and display as tree
            graph = resolver.build_dependency_graph(result, [pkg_name])
            no_libs = getattr(args, 'no_libs', False)
            max_depth = getattr(args, 'depth', 5)
            use_pager = getattr(args, 'pager', False)

            _print_depends_tree(
                pkg_name, graph, colors,
                no_libs=no_libs, max_depth=max_depth, use_pager=use_pager
            )
            if getattr(args, 'legend', False):
                _print_depends_legend(colors)
    else:
        # Default mode: direct deps from solver + unresolved alternatives shown separately
        result = resolver.resolve_install([pkg_name])

        if result.problems and not result.actions:
            for p in result.problems:
                print(colors.error(p))
            return 1

        # Extract direct deps from solver graph
        direct_deps = []
        if result.actions:
            graph = resolver.build_dependency_graph(result, [pkg_name])
            direct_deps = sorted(graph.get(pkg_name, []))

        if direct_deps:
            print(ngettext(
                "Dependencies of {package}: {count} package",
                "Dependencies of {package}: {count} packages",
                len(direct_deps)
            ).format(package=package, count=len(direct_deps)) + "\n")
            for dep in direct_deps:
                print(f"  {dep}")
        elif not result.alternatives:
            pkg = db.get_package_smart(package)
            if not pkg:
                print(_("Package '{package}' not found").format(package=package))
                return 1
            print(_("{package}: no dependencies").format(package=package))

        # Show unresolved alternatives
        if result.alternatives:
            if direct_deps:
                print()
            print(_("Alternatives ({count} capabilities with choices):").format(
                count=len(result.alternatives)) + "\n")
            for alt in result.alternatives:
                providers_str = ' | '.join(alt.providers[:5])
                if len(alt.providers) > 5:
                    providers_str += f" (+{len(alt.providers) - 5})"
                print(f"  {colors.warning(alt.capability)}")
                if alt.required_by:
                    print(f"    ({_('required by')} {alt.required_by})")
                print(f"    → {colors.dim(providers_str)}")

    return 0


def _print_depends_tree(pkg_name: str, graph: dict, colors,
                        no_libs: bool = False, max_depth: int = 5,
                        use_pager: bool = False):
    """Print dependency tree from solver graph.

    Uses the adjacency list from build_dependency_graph() for accurate
    tree display. Packages are colored green if installed.

    Args:
        pkg_name: Root package name
        graph: Adjacency list {pkg: [deps]} from build_dependency_graph
        colors: Colors module for terminal output
        no_libs: If True, hide library packages (lib*, glibc, etc.)
        max_depth: Maximum recursion depth
        use_pager: If True, pipe output through less -R
    """
    # Build installed set for coloring
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

    def print_tree(pkg: str, visited: set, prefix: str, depth: int):
        """Recursively print package dependencies as a tree."""
        deps = sorted(graph.get(pkg, []))
        if no_libs:
            deps = [d for d in deps if not is_lib_package(d)]

        for i, dep in enumerate(deps):
            is_last = (i == len(deps) - 1)
            connector = "└⧐─ " if is_last else "├⧐─ "
            child_prefix = prefix + ("    " if is_last else "│   ")

            # Color: green if installed, normal if not
            if dep in installed_pkgs:
                dep_display = colors.success(dep)
            else:
                dep_display = dep

            if dep in visited:
                # Already seen: show with cycle marker
                print(f"{prefix}{connector}{colors.dim(dep)} 🔄")
            elif depth >= max_depth:
                print(f"{prefix}{connector}{dep_display} {colors.dim('▷▷')}")
            else:
                print(f"{prefix}{connector}{dep_display}")
                visited.add(dep)
                print_tree(dep, visited, child_prefix, depth + 1)

    def do_print_tree():
        print(f"\n{pkg_name}")
        print_tree(pkg_name, {pkg_name}, "", 0)

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


def _print_depends_legend(colors):
    """Print symbol legend for depends --tree output."""
    print()
    print(_("Legend:"))
    print(f"  ├⧐─  {_('depends on (dependency arrow)')}")
    print(f"  🔄   {_('already listed above (cycle)')}")
    print(f"  ▷▷   {_('deeper dependencies exist (max depth)')}")


def _print_rdepends_legend(colors):
    """Print symbol legend for rdepends --tree output."""
    print()
    print(_("Legend:"))
    print(f"  ├⧏─  {_('is required by (reverse dependency)')}")
    print(f"  ★    {_('already expanded above (truncated)')}")


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

    # Architecture honoured by the urpmi DB lookup below. When the package
    # is installed, the rpmdb header tells us the truth; otherwise we fall
    # back to the host arch so SQLite does not return a foreign-arch row
    # (e.g. ``i686`` for ``lib64fuse2`` on an ``x86_64`` host with 32-bit
    # media enabled), which would carry suffix-less sonames and break the
    # downstream ``whatrequires`` / ``whatrecommends`` / ``whatsuggests``
    # queries.
    inst_arch = None

    # From RPM database (installed)
    try:
        ts = rpm.TransactionSet()
        mi = ts.dbMatch('name', pkg_name)
        for hdr in mi:
            inst_arch = hdr[rpm.RPMTAG_ARCH]
            rpm_provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
            for prov in rpm_provides:
                if prov not in provides and not _is_virtual_provide(prov):
                    provides.append(prov)
            break
    except:
        pass

    # Also from urpmi database
    pkg = db.get_package(pkg_name, arch=inst_arch or system_arch())
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


def _get_rdeps_from_pool(pool, pkg_name: str, installed_only: bool = True,
                         cache: dict = None) -> list:
    """Get reverse dependencies of a package using the libsolv pool.

    Finds all packages whose REQUIRES match any capability provided
    by the target package. Uses the pool as single data source instead
    of scanning RPM DB and urpmi DB separately.

    Args:
        pool: libsolv Pool instance (must have createwhatprovides() called)
        pkg_name: Package name to find reverse deps for
        installed_only: If True, only scan installed packages (@System)
        cache: Optional cache dict for memoization

    Returns:
        Sorted list of package names that depend on pkg_name
    """
    if cache is not None and pkg_name in cache:
        return cache[pkg_name]

    import solv
    from ...core.resolution.pool import lookup_all_requires

    # Get all capabilities provided by the target package
    provides = {pkg_name}
    sel = pool.select(pkg_name, solv.Selection.SELECTION_NAME)
    for s in sel.solvables():
        for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
            cap = str(dep).split()[0]
            if not _is_virtual_provide(cap):
                provides.add(cap)

    # Choose which solvables to scan
    if installed_only and pool.installed:
        solvables = pool.installed.solvables
    elif installed_only:
        # No installed repo available
        result = []
        if cache is not None:
            cache[pkg_name] = result
        return result
    else:
        solvables = pool.solvables

    # Find packages whose requires match our provides
    rdeps = set()
    for s in solvables:
        if not s.repo or s.name == pkg_name or s.name == 'gpg-pubkey':
            continue
        for dep in lookup_all_requires(s):
            req_cap = str(dep).split()[0]
            if req_cap in provides:
                rdeps.add(s.name)
                break

    result = sorted(rdeps)
    if cache is not None:
        cache[pkg_name] = result
    return result


def cmd_rdepends(args, db: 'PackageDatabase') -> int:
    """Handle rdepends command - show reverse dependencies.

    Uses the libsolv pool to find packages whose REQUIRES match
    capabilities provided by the target package.
    """
    from .. import colors
    from ...core.resolver import Resolver

    package = args.package
    pkg_name = _extract_pkg_name(package)
    show_tree = getattr(args, 'tree', False)
    show_all = getattr(args, 'all', False)

    # Create resolver with @System for installed package awareness
    resolver = Resolver(db)
    resolver._preserve_pool = True
    resolver.pool = resolver._create_pool()

    pool = resolver.pool

    # Build installed set from pool for coloring
    installed_pkgs = set()
    if pool.installed:
        for s in pool.installed.solvables:
            installed_pkgs.add(s.name)

    # Get unrequested packages (auto-installed as deps)
    unrequested_pkgs = resolver._get_unrequested_packages()

    # Cache for rdeps lookup
    rdeps_cache = {}

    def get_rdeps(name: str) -> list:
        """Get reverse deps via pool, with caching."""
        return _get_rdeps_from_pool(pool, name, installed_only=False, cache=rdeps_cache)

    # Get direct reverse deps
    direct_rdeps = get_rdeps(pkg_name)

    if not direct_rdeps:
        print(_("No package depends on '{package}'").format(package=package))
        return 0

    def format_pkg(name: str) -> str:
        """Format package name with color based on install status."""
        if name in installed_pkgs:
            if name.lower() in unrequested_pkgs:
                return colors.info(name)    # blue: auto-installed
            return colors.success(name)     # green: explicit
        return colors.dim(name)             # grey: not installed

    if show_tree:
        # Recursive tree with reverse arrows
        max_depth = getattr(args, 'depth', 3)
        hide_uninstalled = getattr(args, 'hide_uninstalled', False)

        # Pre-compute reachable set for --hide-uninstalled optimization
        reachable_cache = None
        if hide_uninstalled:
            rdeps_graph = _build_rdeps_graph(db)
            reachable_cache = _build_installed_reachable_set(
                direct_rdeps, rdeps_graph, installed_pkgs, max_depth, db)

        print(f"{format_pkg(package)}")
        _print_rdep_tree(direct_rdeps, get_rdeps, installed_pkgs, unrequested_pkgs,
                         visited={package}, prefix="", max_depth=max_depth,
                         hide_uninstalled=hide_uninstalled, reachable_cache=reachable_cache)
        if getattr(args, 'legend', False):
            _print_rdepends_legend(colors)
    elif show_all:
        # Flat list of all recursive reverse dependencies (BFS)
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

        print(_("All packages that depend on {package}: {count}").format(
            package=package, count=len(all_rdeps)) + "\n")
        for rdep in sorted(all_rdeps):
            print(f"  {format_pkg(rdep)}")
    else:
        # Flat list of direct reverse dependencies
        print(_("Packages that depend on {package}: {count}").format(
            package=package, count=len(direct_rdeps)) + "\n")
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
        # Not in RPM graph - query urpmi database. Pin to the host arch so
        # SQLite does not pick a foreign-arch row whose suffix-less sonames
        # would silently miss every consumer using the ``(64bit)`` form.
        result = set()
        pkg = db.get_package(pkg_name, arch=system_arch())
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
            print(f"{prefix}└⧏─ ... " + _("({count} packages, max depth reached)").format(count=len(rdeps)))
        return

    for i, pkg_name in enumerate(rdeps):
        is_last = (i == len(rdeps) - 1)
        connector = "└⧏─ " if is_last else "├⧏─ "
        child_prefix = prefix + ("    " if is_last else "│   ")

        if pkg_name in visited:
            print(f"{prefix}{connector}{format_pkg(pkg_name)} ★")
        else:
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
        print(_("{package}: no recommends").format(package=package))
        return 0

    print(_("Packages recommended by {package}: {count}").format(package=package, count=len(recommends)) + "\n")
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
    # Pin the lookup to the target arch so multi-arch systems don't pick
    # up the foreign-arch row whose sonames lack the ``(64bit)`` suffix.
    # Without this, ``whatrecommends lib64fuse2`` on x86_64 may return
    # the i686 row whose ``Provides`` list is missing the 64-bit
    # capabilities, producing a false-negative empty result for the
    # 5 x86_64 packages that recommend ``libfuse.so.2()(64bit)``.
    arch = pick_arch_for_lookup(package, resolve_target_arch(args))

    # Get what this package provides
    pkg = db.get_package(pkg_name, arch=arch)
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
        print(_("No package recommends '{package}'").format(package=package))
        return 0

    print(_("Packages that recommend {package}: {count}").format(package=package, count=len(results)) + "\n")
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
        print(_("{package}: no suggests").format(package=package))
        return 0

    print(_("Packages suggested by {package}: {count}").format(package=package, count=len(suggests)) + "\n")
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
    # See ``cmd_whatrecommends`` — same multi-arch pitfall: the foreign
    # i686 row of a Mageia ``lib64*`` package carries plain sonames
    # without the ``(64bit)`` suffix, so a naive ``db.get_package`` on a
    # multi-arch host drops the 64-bit suggesters.
    arch = pick_arch_for_lookup(package, resolve_target_arch(args))

    # Get what this package provides
    pkg = db.get_package(pkg_name, arch=arch)
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
        print(_("No package suggests '{package}'").format(package=package))
        return 0

    print(_("Packages that suggest {package}: {count}").format(package=package, count=len(results)) + "\n")
    for name in sorted(results):
        print(f"  {name}")

    return 0


def cmd_why(args, db: 'PackageDatabase') -> int:
    """Handle why command - explain why a package is installed."""
    from ...core.resolver import Resolver
    from .. import colors
    from collections import deque

    package = args.package

    # Get set of installed packages
    installed_pkgs = set()
    try:
        import rpm
        ts = rpm.TransactionSet()
        for hdr in ts.dbMatch():
            installed_pkgs.add(hdr[rpm.RPMTAG_NAME])
    except ImportError:
        print(_("rpm module not available"))
        return 1

    # Resolve the user-typed string to an installed Name.  The literal
    # input wins whenever it matches the rpmdb directly: Mageia ABI-
    # versioned packages (``lib64polkit1-devel-127``,
    # ``lua-rpm-macros-1``, ``xmltex-20020625``,
    # ``kernel-desktop-devel-6.18.22-1.mga10``) embed numbers / dates /
    # kernel versions in their Name and must not be split as if those
    # suffixes were a NEVRA's Version-Release.  Only fall back to
    # ``extract_pkg_name`` if the literal does not match the rpmdb —
    # that handles the case where the user pastes a full NEVRA copied
    # from another command's output.
    if package in installed_pkgs:
        pkg_name = package
    else:
        pkg_name = _extract_pkg_name(package)
        if pkg_name not in installed_pkgs:
            print(_("Package '{package}' is not installed").format(package=package))
            return 1

    # Get the list of auto-installed packages
    resolver = Resolver(db)
    unrequested = resolver._get_unrequested_packages()

    # Check if it's a build dependency
    builddeps = resolver._get_builddep_packages()
    if pkg_name.lower() in builddeps:
        source = builddeps[pkg_name.lower()]
        msg = _("build dependency of {source}").format(source=source)
        print(f"{colors.bold(pkg_name)}: {colors.info(msg)}")
        print("\n" + _("This package can be removed with: urpm autoremove --buildrequires"))
        return 0

    # Check if manually installed
    if pkg_name.lower() not in unrequested:
        print(f"{colors.bold(pkg_name)}: {colors.success(_('explicitly installed'))}")
        return 0

    # Single source of truth for orphan classification: delegate to
    # ``Resolver.is_orphan`` so this verb cannot diverge from
    # ``urpm autoremove`` / ``urpme --auto-orphans`` on the same
    # system state.  The BFS below is preserved purely for the
    # explanatory listing of which paths lead to which explicit
    # ancestors when the package is NOT an orphan.
    if resolver.is_orphan(pkg_name):
        print(f"{colors.bold(pkg_name)}: {colors.warning(_('orphan'))} "
              + _("(no explicit package requires it)"))
        print("\n" + _("This package can be removed with: urpm autoremove --orphans"))
        return 0

    DEP_PRIORITY = {'R': 3, 'r': 2, 's': 1}
    rdeps_cache = {}  # Cache for _get_rdeps calls

    # Helper to format dependency type
    def format_dep_type(dep_type: str, short: bool = False) -> str:
        if dep_type == 'R':
            return colors.success(_('R')) if short else colors.success(_('required'))
        elif dep_type == 'r':
            return colors.info(_('r')) if short else colors.info(_('recommended'))
        else:
            return colors.dim(_('s')) if short else colors.dim(_('suggested'))

    # Get direct rdeps with their dependency types (R/r/s)
    direct_rdeps = _get_rdeps(pkg_name, db, 'Rrs', installed_only=True,
                              cache=rdeps_cache, installed_pkgs=installed_pkgs)

    if not direct_rdeps:
        # ``is_orphan`` returned False (we passed the early exit above)
        # yet no Requires/Recommends/Suggests chain matches.  The
        # protective edge can only come from Supplements.  Surface
        # this explicitly rather than mislabel the package as orphan.
        print(f"{colors.bold(pkg_name)}: {colors.success(_('kept by Supplements'))}")
        print("\n" + _("This package is triggered by a Supplements "
                       "relationship from another installed package."))
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
        # ``is_orphan`` returned False (we passed the early exit above)
        # yet no Requires-only chain reaches an explicit ancestor.  The
        # package is kept alive by a weak-dep-only chain (Recommends or
        # Suggests transitive) or by a Supplements trigger.  Surface
        # the rdeps we did find without claiming the package is orphan.
        print(f"{colors.bold(pkg_name)}: "
              + _("kept by weak dependency chain"))
        for direct in sorted(orphan_branches):
            print(f"  ← {direct}")
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
        _pkg, _path, dep_type = entries[0]
        dep_type_counts[dep_type] += 1

    print(f"{colors.bold(pkg_name)}: " + _("installed as dependency"))

    # Summary line
    summary_parts = []
    if dep_type_counts['R']:
        summary_parts.append(f"{colors.success(str(dep_type_counts['R']))} required")
    if dep_type_counts['r']:
        summary_parts.append(f"{colors.info(str(dep_type_counts['r']))} recommended")
    if dep_type_counts['s']:
        summary_parts.append(f"{colors.dim(str(dep_type_counts['s']))} suggested")
    print("\n" + _("By {packages} explicit package(s):").format(packages=', '.join(summary_parts)) + "\n")

    # Sort explicit packages by: requires first, then recommends, then suggests
    def sort_key(pkg):
        entries = by_explicit[pkg]
        entries.sort(key=lambda x: len(x[1]))
        _pkg, _path, dep_type = entries[0]
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
            print(f"  [{dep_marker}] {colors.success(explicit_pkg)} " + _("(via {chain})").format(chain=colors.dim(chain)))

    # Show disconnected chains (rdeps that don't lead to any explicit package)
    if orphan_branches:
        print("\n" + colors.dim(_("Also required by (no explicit package in chain):")))
        for branch in sorted(orphan_branches)[:5]:
            dep_type = direct_rdeps.get(branch, 'R')
            print(f"  [{format_dep_type(dep_type, short=True)}] {colors.dim(branch)}")
        if len(orphan_branches) > 5:
            print("  " + colors.dim("... " + _("and {count} more").format(count=len(orphan_branches) - 5)))

    return 0
