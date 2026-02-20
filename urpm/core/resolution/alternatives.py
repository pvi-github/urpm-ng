"""Alternative selection logic for dependency resolution."""

import os
from typing import Dict, List, Optional, Tuple

import solv


class AlternativesMixin:
    """Mixin providing alternative selection operations.

    Requires:
        - self.pool: solv.Pool instance
        - self._solvable_to_pkg: dict mapping solvable IDs to package info
        - TransactionType, PackageAction, Alternative, InstallReason from resolver
    """

    def _find_alternatives(self, solver, trans, actions: list,
                           max_providers: int = 10) -> list:
        """Find cases where multiple packages could satisfy a dependency.

        Uses two approaches:
        1. PROVIDES-based: for virtual provides like task-sound, check what each
           package provides and if multiple packages provide the same capability
        2. REQUIRES-based: for each package's requirements, check if multiple
           packages can satisfy them

        Args:
            solver: The libsolv solver
            trans: The transaction
            actions: List of package actions
            max_providers: Maximum number of providers to show per alternative
        """
        from ..resolver import TransactionType, Alternative

        alternatives = []
        seen_caps = set()  # Avoid duplicate alternatives

        # Get names of packages being installed
        installing = {a.name for a in actions if a.action == TransactionType.INSTALL}

        for s in trans.steps():
            step_type = trans.steptype(s, solv.Transaction.SOLVER_TRANSACTION_SHOW_ACTIVE)
            if step_type != solv.Transaction.SOLVER_TRANSACTION_INSTALL:
                continue

            # APPROACH 1: Check what this package PROVIDES (for virtual provides)
            for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                cap_str = str(dep)

                # Extract base capability name (remove version constraints like [== 1.0])
                base_cap = cap_str.split('[')[0].strip() if '[' in cap_str else cap_str

                # Skip if it's the package name itself or already seen
                if base_cap == s.name or base_cap in seen_caps:
                    continue

                # Skip arch-specific and perl/python modules
                if '(' in base_cap:
                    continue

                # Find all providers of this capability (use base name, not versioned dep)
                base_dep = self.pool.Dep(base_cap)
                providers = self.pool.whatprovides(base_dep)

                # Skip if capability is already satisfied by an installed package
                if any(p.repo == self.pool.installed for p in providers):
                    continue

                provider_names = set()
                for p in providers:
                    if p.repo and p.repo != self.pool.installed:
                        provider_names.add(p.name)

                # If multiple different packages provide this, it's an alternative
                if len(provider_names) > 1:
                    is_valid = self._is_valid_alternative(base_cap, provider_names, installing)
                    if not is_valid:
                        continue

                    seen_caps.add(base_cap)

                    # Find what requires this capability (for display purposes)
                    required_by = self._find_requirer(base_cap, installing)
                    # Even if we can't find the requirer, it's still an alternative
                    # (the package s provides this capability, so something must need it)
                    if not required_by:
                        required_by = "dependency"

                    sorted_providers = self._prioritize_providers(
                        list(provider_names), max_providers
                    )
                    alternatives.append(Alternative(
                        capability=base_cap,
                        required_by=required_by,
                        providers=sorted_providers
                    ))

            # APPROACH 2: Check what this package REQUIRES (and RECOMMENDS)
            dep_types = [solv.SOLVABLE_REQUIRES, solv.SOLVABLE_RECOMMENDS]
            for dep_type in dep_types:
                for dep in s.lookup_deparray(dep_type):
                    cap_str = str(dep)

                    if cap_str in seen_caps:
                        continue

                    # Extract base capability name (remove version constraints like [>= 1.0])
                    base_cap = cap_str.split('[')[0].split()[0] if '[' in cap_str else (
                        cap_str.split()[0] if ' ' in cap_str else cap_str
                    )

                    # Skip perl/python modules and other parenthesized deps
                    if '(' in base_cap:
                        continue

                    if base_cap in seen_caps:
                        continue

                    providers = self.pool.whatprovides(dep)

                    # Skip if capability is already satisfied by an installed package
                    if any(p.repo == self.pool.installed for p in providers):
                        continue

                    provider_names = set()
                    for p in providers:
                        if p.repo and p.repo != self.pool.installed:
                            provider_names.add(p.name)

                    if len(provider_names) > 1:
                        if not self._is_valid_alternative(base_cap, provider_names, installing):
                            continue

                        seen_caps.add(base_cap)
                        seen_caps.add(cap_str)

                        sorted_providers = self._prioritize_providers(
                            list(provider_names), max_providers
                        )
                        alternatives.append(Alternative(
                            capability=base_cap,
                            required_by=s.name,
                            providers=sorted_providers
                        ))

        return alternatives

    def _is_valid_alternative(self, capability: str, provider_names: set,
                              installing: set) -> bool:
        """Check if this is a valid user-facing alternative."""
        # Filter: capability name matches a provider (not a virtual provide)
        if capability in provider_names:
            return False

        # Filter: all providers are library packages
        if all(self._is_library_package(p) for p in provider_names):
            return False

        # Filter: provider name contains the capability (e.g., lib64digikamcore for digikam-core)
        # But NOT if providers have different functional suffixes (like php8.5-cgi vs php8.5-cli)
        cap_normalized = capability.replace('-', '').replace('_', '').lower()
        matching_providers = [p for p in provider_names
                              if cap_normalized in p.replace('-', '').replace('_', '').lower()]
        if matching_providers:
            # Extract functional suffixes (remove digits to ignore version differences)
            suffixes = set()
            for p in matching_providers:
                p_norm = p.replace('-', '').replace('_', '').lower()
                idx = p_norm.find(cap_normalized)
                if idx >= 0:
                    suffix = p_norm[idx + len(cap_normalized):]
                    # Remove version numbers to get the functional suffix
                    suffix = ''.join(c for c in suffix if not c.isdigit())
                    suffixes.add(suffix)

            # If only one suffix pattern (or just version differences), not a real choice
            # e.g., lib64digikamcore → suffix "" → 1 suffix → exclude
            # But php8.5-cgi, php8.5-cli → suffixes "cgi", "cli" → 2 suffixes → include
            if len(suffixes) <= 1:
                return False

        return True

    def _find_requirer(self, capability: str, installing: set) -> Optional[str]:
        """Find which package being installed requires a capability."""
        dep = self.pool.Dep(capability)

        # Check packages that require this capability
        for req in self.pool.whatmatchesdep(solv.SOLVABLE_REQUIRES, dep):
            if req.name in installing:
                return req.name

        # Also check recommends
        for req in self.pool.whatmatchesdep(solv.SOLVABLE_RECOMMENDS, dep):
            if req.name in installing:
                return req.name

        return None

    def _count_missing_deps(self, pkg_name: str, depth: int = 3) -> int:
        """Count how many dependencies of a package are not yet installed.

        This is used to prioritize providers that require fewer new packages.
        For example, if Qt libs are already installed, a Qt-based app will
        have fewer missing deps than a GTK-based app.

        Args:
            pkg_name: Package name to check
            depth: How deep to recurse (1 = direct deps only, 2+ = transitive)

        Returns:
            Estimated number of missing dependencies
        """
        if not self.pool or not self.pool.installed:
            return 0

        # Get installed package names
        installed = {s.name.lower() for s in self.pool.installed.solvables}

        missing = set()
        checked = set()  # Avoid infinite loops

        def count_deps_recursive(name: str, current_depth: int):
            """Recursively count missing deps."""
            if current_depth <= 0 or name.lower() in checked:
                return
            checked.add(name.lower())

            sel = self.pool.select(name, solv.Selection.SELECTION_NAME)
            if sel.isempty():
                return

            for s in sel.solvables():
                if s.repo and s.repo.name != '@System':
                    for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                        dep_str = str(dep).split()[0]
                        if dep_str.startswith(('rpmlib(', '/', 'config(')):
                            continue

                        dep_obj = self.pool.Dep(dep_str)
                        providers = self.pool.whatprovides(dep_obj)

                        # Check if any provider is installed
                        is_satisfied = False
                        for p in providers:
                            if p.name.lower() in installed:
                                is_satisfied = True
                                break

                        if not is_satisfied:
                            # Find first available provider
                            for p in providers:
                                if p.repo and p.repo.name != '@System':
                                    if p.name.lower() not in missing:
                                        missing.add(p.name.lower())
                                        # Recurse into this dependency
                                        count_deps_recursive(p.name, current_depth - 1)
                                    break
                    break  # Only process first available solvable

        count_deps_recursive(pkg_name, depth)

        return len(missing)

    def _prioritize_providers(self, providers: List[str], max_count: int) -> List[str]:
        """Prioritize providers based on missing dependencies, locale, and common usage.

        Providers requiring fewer new dependencies are shown first.

        Args:
            providers: List of provider package names
            max_count: Maximum number to return

        Returns:
            Sorted and limited list of providers
        """
        # Get system locale
        try:
            lang = os.environ.get('LANG', 'en_US.UTF-8').split('_')[0].lower()
        except Exception:
            lang = 'en'

        # Common/popular language codes to prioritize
        common_langs = ['en', 'fr', 'de', 'es', 'it', 'pt', 'ru', 'zh', 'ja', 'ko']

        # Pre-calculate missing deps count for each provider
        missing_deps_cache = {}
        for name in providers:
            missing_deps_cache[name] = self._count_missing_deps(name)

        def sort_key(name: str) -> tuple:
            name_lower = name.lower()
            missing_count = missing_deps_cache.get(name, 999)

            # Primary sort: by number of missing dependencies (fewer = better)
            # Secondary sort: locale matching
            # Tertiary sort: alphabetical

            locale_score = 2  # Default: no locale match
            if f'-{lang}' in name_lower or name_lower.endswith(f'_{lang}'):
                locale_score = 0
            else:
                for i, common in enumerate(common_langs):
                    if f'-{common}' in name_lower or name_lower.endswith(f'_{common}'):
                        locale_score = 1
                        break

            return (missing_count, locale_score, name)

        sorted_providers = sorted(providers, key=sort_key)
        return sorted_providers[:max_count]

    def _is_library_package(self, name: str) -> bool:
        """Check if a package name looks like a library package.

        Library packages are typically not user-facing choices for alternatives.
        """
        name_lower = name.lower()
        # Common library prefixes
        if name_lower.startswith(('lib64', 'lib32', 'libx')):
            return True
        # Libraries with version suffixes like libfoo1, libbar2.0
        if name_lower.startswith('lib') and any(c.isdigit() for c in name_lower[3:]):
            return True
        return False

    def find_available_suggests(self, package_names: List[str],
                                choices: Dict[str, str] = None,
                                resolved_packages: List[str] = None) -> Tuple[list, list]:
        """Find packages that are suggested by the given packages.

        Suggests are not automatically installed by libsolv, so we need to
        find them separately and offer them to the user.

        Args:
            package_names: List of package names to check suggests for
            choices: Dict mapping capability -> chosen package name.
                     Used to filter out suggests that conflict with choices.
            resolved_packages: List of package names already in the transaction.
                     Used to filter out suggests that will be installed anyway.

        Returns:
            Tuple of (suggests, alternatives):
            - suggests: List of PackageAction for available suggested packages
            - alternatives: List of Alternative for suggests with multiple providers
        """
        from ..resolver import TransactionType, PackageAction, Alternative, InstallReason, DEBUG_RESOLVER

        if not self.pool:
            return [], []

        if choices is None:
            choices = {}

        suggests = []
        alternatives = []
        seen = set()
        installed_names = set()

        # Get names of installed packages
        if self.pool.installed:
            for s in self.pool.installed.solvables:
                installed_names.add(s.name.lower())

        # Also consider packages already in the transaction as "installed"
        if resolved_packages:
            for pkg_name in resolved_packages:
                installed_names.add(pkg_name.lower())

        # Build set of "rejected" packages - alternatives that weren't chosen
        # e.g., if user chose pulseaudio for pulseaudio-daemon, reject pipewire-pulseaudio
        rejected_packages = set()

        # Internal RPM/systemd triggers - not user-facing capabilities
        # These are provided by many unrelated packages and should not be used
        # for alternative selection
        internal_caps = {
            'should-restart',       # systemd restart trigger (glibc, dbus, systemd...)
            'postshell',            # post-install shell requirement
            'config',               # generic config capability
            'bundled',              # bundled library marker
            'debuginfo',            # debug info marker
        }

        for cap, chosen in choices.items():
            # Skip internal triggers - they're not real alternatives
            if cap in internal_caps:
                continue

            # Find all providers of this capability
            dep = self.pool.Dep(cap)
            for p in self.pool.whatprovides(dep):
                if p.name != chosen:
                    rejected_packages.add(p.name.lower())

        if DEBUG_RESOLVER and 'phpmyadmin' in package_names:
            print(f"DEBUG rejected_packages from choices: {sorted(rejected_packages)[:20]}")

        # For each package, find its suggests
        for pkg_name in package_names:
            # Find the package in available repos
            flags = solv.Selection.SELECTION_NAME | solv.Selection.SELECTION_CANON
            sel = self.pool.select(pkg_name, flags)

            for s in sel.solvables():
                # Get suggests deps
                suggests_deps = s.lookup_deparray(solv.SOLVABLE_SUGGESTS)
                if DEBUG_RESOLVER and pkg_name == 'phpmyadmin':
                    print(f"DEBUG SUGGESTS: {pkg_name} has {len(suggests_deps)} suggests")
                    for d in suggests_deps:
                        print(f"  - {d}")
                for dep in suggests_deps:
                    dep_str = str(dep).split()[0]  # Extract capability name

                    # Skip if this capability was already processed
                    if dep_str in seen:
                        continue

                    # Find packages that satisfy this suggest
                    providers = self.pool.whatprovides(dep)
                    if DEBUG_RESOLVER and pkg_name == 'phpmyadmin':
                        prov_names = [p.name for p in providers if p.repo and p.repo.name != '@System']
                        if prov_names:
                            print(f"  {dep} -> providers: {prov_names[:5]}")

                    # Collect valid providers for this capability
                    valid_providers = []
                    for provider in providers:
                        # Skip if already installed
                        if provider.name.lower() in installed_names:
                            if DEBUG_RESOLVER and pkg_name == 'phpmyadmin':
                                print(f"    SKIP {provider.name}: already installed")
                            continue
                        # Skip if it's a src package
                        if provider.arch in ('src', 'nosrc'):
                            continue
                        # Skip suggests that require rejected packages
                        debug_this = (pkg_name == 'phpmyadmin' and provider.name in ('php8.4-bz2', 'php8.4-zip', 'php8.5-bz2', 'php8.5-zip'))
                        if self._requires_rejected(provider, rejected_packages, debug=debug_this):
                            if pkg_name == 'phpmyadmin':
                                print(f"    SKIP {provider.name}: requires rejected package")
                            continue
                        valid_providers.append(provider)

                    if not valid_providers:
                        continue

                    seen.add(dep_str)

                    # Deduplicate by name (keep first/best version)
                    unique_providers = {}
                    for p in valid_providers:
                        if p.name not in unique_providers:
                            unique_providers[p.name] = p
                    valid_providers = list(unique_providers.values())

                    # Check if user already made a choice for this capability
                    if dep_str in choices:
                        chosen_name = choices[dep_str]
                        chosen_provider = next((p for p in valid_providers if p.name == chosen_name), None)
                        if chosen_provider:
                            valid_providers = [chosen_provider]

                    if len(valid_providers) == 1:
                        # Single provider - add directly to suggests
                        provider = valid_providers[0]
                        pkg_info = self._solvable_to_pkg.get(provider.id, {})
                        suggests.append(PackageAction(
                            action=TransactionType.INSTALL,
                            name=provider.name,
                            evr=provider.evr,
                            arch=provider.arch,
                            nevra=f"{provider.name}-{provider.evr}.{provider.arch}",
                            size=pkg_info.get('size', 0),
                            filesize=pkg_info.get('filesize', 0),
                            media_name=pkg_info.get('media_name', ''),
                            reason=InstallReason.SUGGESTED,
                        ))
                    else:
                        # Multiple providers - create an alternative for user choice
                        # Sort by missing deps count (fewer deps = shown first)
                        provider_names = [p.name for p in valid_providers]
                        sorted_providers = self._prioritize_providers(provider_names, len(provider_names))
                        alternatives.append(Alternative(
                            capability=dep_str,
                            required_by=f"suggested by {pkg_name}",
                            providers=sorted_providers
                        ))

        return suggests, alternatives

    def _requires_rejected(self, solvable, rejected_packages: set, debug=False) -> bool:
        """Check if a solvable requires any rejected package.

        A package is "rejected" if it was an alternative that the user
        did not choose. For example, if user chose pulseaudio over
        pipewire-pulseaudio, then pipewire-pulseaudio is rejected.

        Args:
            solvable: The solvable to check
            rejected_packages: Set of rejected package names (lowercase)
            debug: If True, print debug info

        Returns:
            True if the solvable requires a rejected package
        """
        for dep in solvable.lookup_deparray(solv.SOLVABLE_REQUIRES):
            # Check if any provider of this dependency is rejected
            providers = self.pool.whatprovides(dep)
            provider_names = {p.name.lower() for p in providers}

            # If ALL providers are rejected, or if the only provider is rejected
            if provider_names and provider_names.issubset(rejected_packages):
                if debug:
                    print(f"      {solvable.name} rejected: dep {dep} has all providers in rejected: {provider_names}")
                return True

            # Also check if the dependency itself is a rejected package name
            dep_name = str(dep).split()[0].lower()
            if dep_name in rejected_packages:
                if debug:
                    print(f"      {solvable.name} rejected: dep name {dep_name} is in rejected_packages")
                return True

        return False
