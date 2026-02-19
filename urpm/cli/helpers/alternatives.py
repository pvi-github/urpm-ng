"""Alternative handling and preferences matching for package resolution.

This module provides:
- PreferencesMatcher: Match packages against user preferences (--prefer)
- _resolve_with_alternatives: Interactive resolution with alternative handling
- _handle_bloc_choices: Handle bloc-based alternative choices
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase
    from ...core.resolver import Resolver, Resolution

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
    from .. import colors

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
    from .. import colors

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


