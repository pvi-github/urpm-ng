"""Alternative handling and preferences matching for package resolution.

This module provides:
- PreferencesMatcher: Match packages against user preferences (--prefer)
- _resolve_with_alternatives: Interactive resolution with alternative handling
- _handle_bloc_choice: Handle bloc version choice for alternatives
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase
    from ...core.resolver import Resolver, Resolution

from ...i18n import _
from .resolver import _group_by_version

# Debug flag for preferences matching
DEBUG_PREFERENCES = False


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

        Removes providers that are:
        - Explicitly disfavored (e.g., rejected bloc providers)
        - Incompatible with stated name patterns (conflict rules)
        Puts preferred providers first.

        Args:
            providers: List of provider names

        Returns:
            Filtered and sorted list (never empty - returns original if all filtered)
        """
        # Always filter disfavored packages (from bloc choices or --prefer negatives)
        if self.disfavored_packages:
            filtered_by_disfavor = [p for p in providers
                                    if p.lower() not in self.disfavored_packages]
            # Never return empty - fallback to original if all filtered
            if filtered_by_disfavor:
                providers = filtered_by_disfavor

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



def _handle_bloc_choice(bloc_info: dict, preferences: 'PreferencesMatcher',
                        interactive: bool, remembered_bloc: str = None) -> tuple:
    """Handle bloc version choice for alternatives.

    Blocs are groups of packages that share versioned dependencies (e.g., all
    php8.4-* require php-common = 3:8.4 while php8.5-* require 3:8.5).
    This function handles ONLY the top-level version choice. Per-capability
    secondary choices (cgi vs fpm, apache vs nginx) are left to the normal
    individual alternative handler after preferences are updated.

    Args:
        bloc_info: Dict from resolver.detect_blocs()
        preferences: PreferencesMatcher instance
        interactive: If True, prompt user for version choice
        remembered_bloc: Bloc key from a previous iteration (auto-selects)

    Returns:
        Tuple of (chosen_bloc_key, aborted).
        chosen_bloc_key: string like "3:8.5", or None if no blocs.
        aborted: True if user cancelled.
    """
    from .. import colors

    blocs = bloc_info['blocs']
    bloc_defining = bloc_info['bloc_defining_caps']

    if not blocs:
        return (None, False)

    bloc_keys = sorted(blocs.keys())
    chosen_bloc = None

    # Re-use bloc from previous iteration
    if remembered_bloc and remembered_bloc in bloc_keys:
        chosen_bloc = remembered_bloc

    # Try to match preference via version constraints
    if not chosen_bloc:
        for bloc_key in bloc_keys:
            if preferences.match_bloc_version(bloc_defining, bloc_key):
                chosen_bloc = bloc_key
                break

    # If no match and interactive, prompt user for version choice
    if not chosen_bloc and interactive and len(bloc_keys) > 1:
        bloc_label = _get_bloc_label(bloc_defining)

        def _display_version(bk: str) -> str:
            """Strip epoch from display (e.g., '3:8.4' -> '8.4')."""
            return bk.split(':', 1)[-1] if ':' in bk else bk

        print(f"\n{colors.warning(bloc_label)} - {_('multiple versions available:')}")
        for i, bk in enumerate(bloc_keys, 1):
            count = sum(len(provs) for provs in blocs[bk].values())
            print(f"  {i}. {_display_version(bk)} ({count} packages)")

        while True:
            try:
                choice = input("\n" + _("Choice?") + f" [1-{len(bloc_keys)}] ")
                idx = int(choice) - 1
                if 0 <= idx < len(bloc_keys):
                    chosen_bloc = bloc_keys[idx]
                    break
            except ValueError:
                pass
            except (EOFError, KeyboardInterrupt):
                print("\n" + _("Aborted"))
                return (None, True)
        print()

    # Default to highest version
    if not chosen_bloc:
        chosen_bloc = bloc_keys[-1]

    return (chosen_bloc, False)


def _propagate_bloc_choice(bloc_info: dict, chosen_bloc: str,
                           preferences: 'PreferencesMatcher', pool) -> None:
    """Propagate bloc choice to preferences — equivalent to adding --prefer.

    Converts the interactive bloc choice into version constraints on the
    PreferencesMatcher, then re-resolves patterns. This uses the exact same
    mechanism as --prefer=php:8.5, ensuring consistent behavior.

    Args:
        bloc_info: Dict from resolver.detect_blocs()
        chosen_bloc: The chosen bloc key (e.g., "3:8.5")
        preferences: PreferencesMatcher instance (modified in place)
        pool: libsolv Pool instance
    """
    bloc_defining = bloc_info['bloc_defining_caps']
    bloc_keys = sorted(bloc_info['blocs'].keys())

    # Extract chosen version (strip epoch: "3:8.5" -> "8.5")
    chosen_version = chosen_bloc.split(':', 1)[-1] if ':' in chosen_bloc else chosen_bloc

    # Extract rejected versions
    rejected_versions = []
    for bk in bloc_keys:
        if bk != chosen_bloc:
            rejected_versions.append(bk.split(':', 1)[-1] if ':' in bk else bk)

    # Add version constraint for each bloc-defining capability base name.
    # e.g., bloc_defining = {'php-common': [...]} -> constraint php:8.5
    for cap in bloc_defining:
        base = cap.split('-')[0].lower()
        if base not in preferences.version_constraints:
            preferences.version_constraints[base] = chosen_version

    # Add rejected versions as negative patterns (disfavor php*8.4*)
    for cap in bloc_defining:
        base = cap.split('-')[0].lower()
        for rv in rejected_versions:
            neg = f"{base}*{rv}*"
            if neg not in preferences.negative_patterns:
                preferences.negative_patterns.append(neg)

    # Re-resolve with new constraints — same code path as --prefer
    preferences.resolved_packages.clear()
    preferences._compatible_providers.clear()
    preferences.disfavored_packages.clear()
    preferences.resolve_patterns(pool)


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


def _display_choices(items: list, indent: str = "  ") -> None:
    """Display numbered choices, using columns when the list is long.

    Short lists (≤ 8) use a single column. Longer lists are laid out in
    multiple columns (column-first order, like ``ls``), sized to fit the
    terminal width.

    Args:
        items: Choice labels (provider names, etc.)
        indent: Whitespace prepended to every line
    """
    import shutil

    if not items:
        return

    numbered = [f"{i}. {item}" for i, item in enumerate(items, 1)]

    if len(items) <= 8:
        for entry in numbered:
            print(f"{indent}{entry}")
        return

    # Multi-column layout
    max_entry = max(len(s) for s in numbered)
    col_width = max_entry + 3  # inter-column gap
    term_width = shutil.get_terminal_size().columns
    num_cols = max(1, (term_width - len(indent)) // col_width)
    num_rows = (len(numbered) + num_cols - 1) // num_cols

    for row in range(num_rows):
        line = indent
        for col in range(num_cols):
            idx = row + col * num_rows
            if idx < len(numbered):
                line += numbered[idx].ljust(col_width)
        print(line.rstrip())


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
    from .. import colors

    print(f"  {colors.info(capability)} " + _("provided by:"))
    _display_choices(providers, indent="    ")

    while True:
        try:
            choice = input("  " + _("Choice?") + f" [1-{len(providers)}] ")
            idx = int(choice) - 1
            if 0 <= idx < len(providers):
                return providers[idx]
        except ValueError:
            pass
        except (EOFError, KeyboardInterrupt):
            print("\n" + _("Aborted"))
            return None  # Signal abort

    return providers[0]



def _get_hard_dep_provides(packages: list, pool, alternative_caps: set) -> set:
    """Compute capabilities guaranteed by hard dependencies of requested packages.

    For each requested package, scans its REQUIRES in the pool. Requires that
    are NOT in the alternatives list (i.e., resolved without user input) are
    hard dependencies — their providers and all provided capabilities are
    collected into the returned set.

    Example: kanboard requires 'apache' (not an alternative).
    Apache provides {apache, webserver, apache-mod_*, ...}.
    So the returned set includes 'webserver', meaning php8.5-cgi (which
    requires 'webserver') is coherent with the transaction.

    Args:
        packages: List of requested package names
        pool: libsolv Pool instance
        alternative_caps: Set of capability names that are unresolved alternatives

    Returns:
        Set of capability names (lowercase) provided by hard dependencies
    """
    import solv

    hard_provides = set()
    alt_lower = {c.lower() for c in alternative_caps}

    for pkg_name in packages:
        sel = pool.select(pkg_name, solv.Selection.SELECTION_NAME)
        for s in sel.solvables():
            if s.repo and s.repo.name != '@System':
                for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                    req_cap = str(dep).split()[0]
                    if req_cap.startswith(('lib', 'rpmlib(', '/', 'config(')):
                        continue
                    if req_cap.lower() in alt_lower:
                        continue  # Unresolved alternative, not a hard dep
                    # Collect provides from all providers of this hard dep
                    req_dep = pool.Dep(req_cap)
                    for prov in pool.whatprovides(req_dep):
                        if prov.repo:
                            hard_provides.add(prov.name.lower())
                            for p_dep in prov.lookup_deparray(solv.SOLVABLE_PROVIDES):
                                cap = str(p_dep).split()[0]
                                if not cap.startswith(('lib', 'rpmlib(', '/')):
                                    hard_provides.add(cap.lower())
                break  # First non-system solvable is enough

    return hard_provides


def _filter_by_existing_choices(providers: list, choices: dict,
                                pool, hard_dep_provides: set = None) -> list:
    """Filter providers by sub-package relationship and hard-dep coherence.

    Two successive filters, each with a safe fallback (never returns empty):

    1. **Sub-package**: if a chosen package has sub-packages among candidates
       (e.g., php8.5-fpm → php8.5-fpm-nginx/apache), keep only those.
       If no sub-packages exist, all providers pass through unchanged.

    2. **Hard-dep coherence**: among remaining candidates, check if their
       REQUIRES reference a capability guaranteed by the requested packages'
       hard dependencies. If some match and others don't, keep only coherent
       ones. This avoids pulling nginx when apache is already a hard dep.

    Examples:
        choices['php'] = 'php8.5-fpm', kanboard hard deps provide apache/webserver:
          [cgi, fpm-nginx, fpm-apache, mod_php]
            → sub-pkg: [fpm-nginx, fpm-apache]
            → coherence: [fpm-apache]  (requires apache ✓, nginx ✗)
        choices['php'] = 'php8.5-cli', kanboard hard deps provide apache/webserver:
          [cgi, fpm-nginx, fpm-apache, mod_php]
            → sub-pkg: all (no cli sub-packages)
            → coherence: [cgi, fpm-apache, mod_php]  (webserver/apache ✓)

    Args:
        providers: List of provider names to filter
        choices: Dict mapping capability -> chosen package
        pool: libsolv Pool instance
        hard_dep_provides: Capabilities guaranteed by hard dependencies

    Returns:
        Filtered list (never empty — fallback at each step)
    """
    if not choices:
        return providers

    # --- Step 1: sub-package filter ---
    chosen_names = {v.lower() for v in choices.values()}
    extensions = [
        p for p in providers
        if any(p.lower().startswith(chosen + '-') for chosen in chosen_names)
    ]
    has_subpackages = bool(extensions)
    providers = extensions if extensions else providers

    # --- Step 2: hard-dep coherence ---
    if not hard_dep_provides or pool is None or len(providers) <= 1:
        return providers

    import solv

    coherent = []
    for prov_name in providers:
        sel = pool.select(prov_name, solv.Selection.SELECTION_NAME)
        for s in sel.solvables():
            if s.repo and s.repo.name != '@System':
                for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                    req = str(dep).split()[0].lower()
                    if req.startswith(('lib', 'rpmlib(', '/', 'config(')):
                        continue
                    if req in hard_dep_provides:
                        coherent.append(prov_name)
                        break
                break  # First non-system solvable

    # Contextual return:
    # - With sub-packages (fpm → fpm-nginx/fpm-apache): reduce to 1 is OK
    # - Without sub-packages (cli → cgi/fpm/mod_php): keep ≥ 2 for meaningful choice
    if has_subpackages:
        return coherent if coherent else providers
    else:
        return coherent if len(coherent) >= 2 else providers


def _find_root_alternative(alternatives: list, bloc_info: dict):
    """Find the alternative whose providers span the most version blocs.

    This is THE fundamental question — choosing one provider here implicitly
    selects the version AND the mode, making most other alternatives cascade.

    Among alternatives present in 2+ blocs, the one with the most providers
    wins (more providers = richer choice = more cascade).

    Args:
        alternatives: List of Alternative objects
        bloc_info: Dict from resolver.detect_blocs()

    Returns:
        The root Alternative, or None if no alternative spans multiple blocs.
    """
    blocs = bloc_info['blocs']
    if len(blocs) < 2:
        return None

    best = None
    best_providers = 0

    for alt in alternatives:
        bloc_count = sum(
            1 for caps in blocs.values()
            if alt.capability in caps and caps[alt.capability]
        )
        if bloc_count >= 2 and len(alt.providers) > best_providers:
            best = alt
            best_providers = len(alt.providers)

    return best


def _match_provider_to_bloc(provider_name: str, bloc_info: dict) -> str:
    """Find which bloc a chosen provider belongs to.

    First checks direct bloc membership (provider listed in blocs dict).
    Falls back to extracting the version from the package name if the
    provider has no versioned requires (e.g., apache-mod_php8.4).

    Args:
        provider_name: Chosen package name
        bloc_info: Dict from resolver.detect_blocs()

    Returns:
        Bloc key (e.g., "3:8.5") or None.
    """
    # Direct membership
    for bloc_key, caps in bloc_info['blocs'].items():
        for cap_providers in caps.values():
            if provider_name in cap_providers:
                return bloc_key

    # Fallback: version from package name
    import re
    match = re.search(r'(\d+\.\d+)', provider_name)
    if match:
        pkg_version = match.group(1)
        for bloc_key in bloc_info['blocs']:
            bloc_ver = bloc_key.split(':', 1)[-1] if ':' in bloc_key else bloc_key
            if pkg_version in bloc_ver or bloc_ver.endswith(pkg_version):
                return bloc_key

    return None


def _sort_alternatives_by_cascade(alternatives: list, pool) -> None:
    """Sort alternatives so the most cascading capability is asked first.

    An alternative whose providers PROVIDE or REQUIRE other alternative
    capabilities has a high cascade score — choosing it constrains or resolves
    more downstream alternatives.  Asking it first gives the user the most
    impactful choice early, and lets later alternatives auto-resolve.

    Example: php-webinterface providers provide 'php' (another alt capability),
    so webinterface gets a higher score and is asked before php.

    Args:
        alternatives: List of Alternative objects (sorted in place)
        pool: libsolv Pool instance
    """
    if not pool or len(alternatives) <= 1:
        return

    import solv

    alt_caps = {alt.capability.lower() for alt in alternatives}
    scores = {}

    for alt in alternatives:
        score = 0
        other_caps = alt_caps - {alt.capability.lower()}

        for prov_name in alt.providers:
            sel = pool.select(prov_name, solv.Selection.SELECTION_NAME)
            for s in sel.solvables():
                if s.repo and s.repo.name != '@System':
                    for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                        cap = str(dep).split()[0].lower()
                        if cap in other_caps:
                            score += 1
                    for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                        req = str(dep).split()[0].lower()
                        if req in other_caps:
                            score += 1
                    break

        scores[alt.capability] = score

    alternatives.sort(
        key=lambda a: (scores.get(a.capability, 0), len(a.providers)),
        reverse=True
    )


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
    from .. import colors
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
                    # Find the provider for this require (excluding disfavored)
                    req_dep = resolver.pool.Dep(req_cap)
                    providers = [p for p in resolver.pool.whatprovides(req_dep)
                                if p.repo and p.repo.name != '@System'
                                and p.name.lower() not in preferences.disfavored_packages]
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
    remembered_bloc = None  # Persists bloc choice across solver iterations

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
                        print("\n" + _("Multiple versions in preferences:"))
                        sorted_versions = sorted(version_groups.keys())
                        for i, ver in enumerate(sorted_versions, 1):
                            pkgs = version_groups[ver]
                            print(f"  {i}. {ver} ({', '.join(sorted(pkgs)[:3])}{'...' if len(pkgs) > 3 else ''})")

                        while True:
                            try:
                                choice = input(_("Choice?") + f" [1-{len(sorted_versions)}] ")
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
                                print("\n" + _("Aborted"))
                                return None, True

                    preferences_applied = True

                # NOTE: We no longer pre-validate preferences here.
                # Instead, preferences are applied during the iterative resolution
                # when alternatives are encountered (see match_preference() below).
                # This avoids false conflicts from aggressive LOCKing.

        # Pass favored/disfavored to help solver make consistent choices
        # but don't pre-validate - let the iterative process handle conflicts
        # Only FAVOR when user gave explicit --prefer name patterns.
        # Bloc-only choices rely on DISFAVOR to exclude rejected versions;
        # FAVORing compatible providers would auto-resolve alternatives
        # that the user should choose interactively.
        if preferences.name_patterns:
            favored = preferences.resolved_packages | preferences._compatible_providers
        else:
            favored = set()
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

            # Compute hard-dep provides for coherence filtering
            hard_dep_provides = _get_hard_dep_provides(
                packages, resolver.pool, set(alt_caps)
            )

            # Detect blocs among alternatives
            bloc_info = resolver.detect_blocs(alt_caps)

            # Root alternative: the ONE question that matters.
            # Instead of asking "8.4 or 8.5?" abstractly, present the most
            # impactful alternative with ALL its providers (both versions).
            # The user's choice implicitly selects version + mode, and
            # everything else cascades via expand_choice + disfavor.
            if bloc_info['blocs'] and not remembered_bloc:
                root_alt = _find_root_alternative(result.alternatives, bloc_info)

                if root_alt and root_alt.capability not in choices:
                    candidates = list(root_alt.providers)

                    # Apply --prefer version filtering
                    if preferences.version_constraints:
                        ver_filtered = [
                            p for p in candidates
                            if any(v in p.lower()
                                   for v in preferences.version_constraints.values())
                        ]
                        if ver_filtered:
                            candidates = ver_filtered

                    # Apply --prefer name/disfavor filtering
                    candidates = preferences.filter_providers(candidates)

                    # Auto-select if preferences narrow to one
                    pref_matches = [p for p in candidates if match_preference(p)]

                    if len(pref_matches) == 1:
                        chosen_pkg = pref_matches[0]
                    elif len(candidates) == 1:
                        chosen_pkg = candidates[0]
                    elif auto_mode:
                        chosen_pkg = candidates[0]
                    else:
                        # Ask user — the fundamental question
                        if root_alt.required_by:
                            print(f"\n{root_alt.capability} "
                                  f"({_('required by')} {root_alt.required_by}):")
                        else:
                            print(f"\n{root_alt.capability}:")
                        _display_choices(candidates)

                        chosen_pkg = None
                        while True:
                            try:
                                choice = input(
                                    _("Choice?")
                                    + f" [1-{len(candidates)}] ")
                                idx = int(choice) - 1
                                if 0 <= idx < len(candidates):
                                    chosen_pkg = candidates[idx]
                                    break
                            except ValueError:
                                pass
                            except (EOFError, KeyboardInterrupt):
                                print("\n" + _("Aborted"))
                                return result, True

                    choices[root_alt.capability] = chosen_pkg

                    # Propagate bloc disfavor FIRST so expand_choice sees
                    # single-provider requires (e.g., php-fpm → php8.5-fpm only)
                    matched_bloc = _match_provider_to_bloc(chosen_pkg, bloc_info)
                    if matched_bloc:
                        remembered_bloc = matched_bloc
                        _propagate_bloc_choice(
                            bloc_info, matched_bloc, preferences, resolver.pool)

                    expand_choice(chosen_pkg, choices)

                    continue  # Re-resolve with cascaded choices

            # Reorder remaining alternatives: most cascading first
            _sort_alternatives_by_cascade(result.alternatives, resolver.pool)

            # Handle remaining alternatives individually
            if not auto_mode:
                for alt in result.alternatives:
                    # Skip if already chosen
                    if alt.capability in choices:
                        continue

                    # Filter providers based on preferences, then by coherence
                    filtered = preferences.filter_providers(alt.providers)
                    filtered = _filter_by_existing_choices(
                        filtered, choices, resolver.pool, hard_dep_provides
                    )

                    # If only one after filtering, auto-select
                    if len(filtered) == 1:
                        choices[alt.capability] = filtered[0]
                        expand_choice(filtered[0], choices)
                        continue

                    # Try to narrow by preference
                    pref_matches = [p for p in filtered if match_preference(p)]

                    if len(pref_matches) == 1:
                        # Preference narrows to exactly one - auto-select
                        choices[alt.capability] = pref_matches[0]
                        expand_choice(pref_matches[0], choices)
                        continue

                    # Multiple preference matches or none: ask user
                    candidates = pref_matches if len(pref_matches) > 1 else filtered

                    if alt.required_by:
                        print(f"\n{alt.capability} ({_('required by')} {alt.required_by}):")
                    else:
                        print(f"\n{alt.capability}:")
                    _display_choices(candidates)

                    while True:
                        try:
                            choice = input(_("Choice?") + f" [1-{len(candidates)}] ")
                            idx = int(choice) - 1
                            if 0 <= idx < len(candidates):
                                chosen_pkg = candidates[idx]
                                choices[alt.capability] = chosen_pkg
                                expand_choice(chosen_pkg, choices)
                                break
                        except ValueError:
                            pass
                        except (EOFError, KeyboardInterrupt):
                            print("\n" + _("Aborted"))
                            return result, True
                # Re-resolve with new choices
                continue

            else:
                # Auto mode without blocs: use preferences or first choice
                for alt in result.alternatives:
                    filtered = preferences.filter_providers(alt.providers)
                    filtered = _filter_by_existing_choices(
                        filtered, choices, resolver.pool, hard_dep_provides
                    )
                    pref_matches = [p for p in filtered if match_preference(p)]
                    chosen_pkg = pref_matches[0] if len(pref_matches) == 1 else filtered[0]
                    choices[alt.capability] = chosen_pkg
                    expand_choice(chosen_pkg, choices)
                continue

        break  # No more alternatives, exit loop

    return result, False


