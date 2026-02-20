"""Capability and dependency query operations."""

import re
from typing import Dict, List

import solv


class QueriesMixin:
    """Mixin providing capability and dependency query operations.

    Requires:
        - self.pool: solv.Pool instance (may be None, will be created)
        - self._create_pool(): method to create pool
    """

    def get_providers(self, capability: str, include_installed: bool = False) -> List[str]:
        """Find all packages that provide a capability.

        Args:
            capability: The capability to search for (e.g., 'php-filter')
            include_installed: If True, include installed packages

        Returns:
            List of package names that provide this capability
        """
        if self.pool is None:
            self.pool = self._create_pool()

        dep = self.pool.Dep(capability)
        providers = self.pool.whatprovides(dep)

        provider_names = set()
        for p in providers:
            if p.repo:
                if include_installed or p.repo.name != '@System':
                    provider_names.add(p.name)

        return sorted(provider_names)

    def get_package_requires(self, package_name: str) -> List[str]:
        """Get the requires of a package.

        Args:
            package_name: The package name

        Returns:
            List of capability strings that the package requires
        """
        if self.pool is None:
            self.pool = self._create_pool()

        sel = self.pool.select(package_name, solv.Selection.SELECTION_NAME)
        requires = []

        for s in sel.solvables():
            for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                dep_str = str(dep)
                # Skip rpmlib deps and file deps
                if not dep_str.startswith('rpmlib(') and not dep_str.startswith('/'):
                    requires.append(dep_str)
            break  # Just first match

        return requires

    def get_package_recommends(self, package_name: str) -> List[str]:
        """Get the recommends of a package.

        Args:
            package_name: The package name

        Returns:
            List of capability strings that the package recommends
        """
        if self.pool is None:
            self.pool = self._create_pool()

        sel = self.pool.select(package_name, solv.Selection.SELECTION_NAME)
        recommends = []

        for s in sel.solvables():
            for dep in s.lookup_deparray(solv.SOLVABLE_RECOMMENDS):
                dep_str = str(dep)
                recommends.append(dep_str)
            break  # Just first match

        return recommends

    def get_package_suggests(self, package_name: str) -> List[str]:
        """Get the suggests of a package.

        Args:
            package_name: The package name

        Returns:
            List of capability strings that the package suggests
        """
        if self.pool is None:
            self.pool = self._create_pool()

        sel = self.pool.select(package_name, solv.Selection.SELECTION_NAME)
        suggests = []

        for s in sel.solvables():
            for dep in s.lookup_deparray(solv.SOLVABLE_SUGGESTS):
                dep_str = str(dep)
                suggests.append(dep_str)
            break  # Just first match

        return suggests

    def _get_versioned_requires(self, solvable) -> Dict[str, str]:
        """Extract versioned requires from a solvable as {capability: version}.

        Only returns requires with exact version matches (=) that look like
        major.minor version patterns, which typically define blocs.
        """
        versioned = {}
        for dep in solvable.lookup_deparray(solv.SOLVABLE_REQUIRES):
            dep_str = str(dep)

            # Skip noise (libraries, config, rpmlib, file paths)
            if dep_str.startswith(('lib', 'ld-', 'config(', 'rpmlib(', '/')):
                continue

            # Parse "capability = version" or "capability >= version"
            match = re.match(r'^([a-zA-Z0-9_-]+)\s*(=|>=|<=|>|<)\s*(.+)$', dep_str)
            if match:
                cap, op, ver = match.groups()
                # Normalize version to epoch:major.minor pattern
                ver_match = re.search(r'(\d+:\d+\.\d+)', ver)
                if ver_match:
                    versioned[cap] = ver_match.group(1)

        return versioned

    def detect_blocs(self, capabilities: List[str]) -> Dict[str, Dict]:
        """Detect blocs (groups of interdependent packages) from capabilities.

        Blocs are detected by finding capabilities that different providers
        require with different versions. For example, php8.4-filter requires
        php-common = 3:8.4 while php8.5-filter requires php-common = 3:8.5.

        Args:
            capabilities: List of capability names to analyze

        Returns:
            Dict with structure:
            {
                'bloc_defining_caps': {cap: [version1, version2, ...]},
                'blocs': {
                    version_key: {
                        capability: [provider_names],
                        ...
                    }
                },
                'providers_info': {provider_name: {cap: version, ...}}
            }
        """
        if self.pool is None:
            self.pool = self._create_pool()

        from collections import defaultdict

        # Step 1: Collect all providers and their versioned requires
        providers_info = {}  # {provider_name: {cap: version}}

        for cap in capabilities:
            dep = self.pool.Dep(cap)
            providers = self.pool.whatprovides(dep)

            for p in providers:
                if p.repo and p.repo.name != '@System':
                    if p.name not in providers_info:
                        providers_info[p.name] = self._get_versioned_requires(p)

        # Step 2: Detect bloc-defining capabilities
        # A capability is bloc-defining if different providers require it with
        # different versions
        cap_versions = defaultdict(set)  # {capability: {version1, version2, ...}}

        for prov_name, versioned_reqs in providers_info.items():
            for cap, ver in versioned_reqs.items():
                cap_versions[cap].add(ver)

        bloc_defining = {cap: sorted(versions) for cap, versions in cap_versions.items()
                         if len(versions) > 1}

        # Step 3: Group providers by bloc
        # Use the first bloc-defining capability's version as bloc key
        blocs = defaultdict(lambda: defaultdict(list))  # {bloc_key: {capability: [providers]}}

        for cap in capabilities:
            dep = self.pool.Dep(cap)
            providers = self.pool.whatprovides(dep)

            for p in providers:
                if p.repo and p.repo.name != '@System':
                    versioned_reqs = providers_info.get(p.name, {})

                    # Get bloc key from versioned requires
                    bloc_key = None
                    for bc in sorted(bloc_defining.keys()):
                        if bc in versioned_reqs:
                            bloc_key = versioned_reqs[bc]
                            break

                    if bloc_key:
                        blocs[bloc_key][cap].append(p.name)

        # Convert defaultdicts to regular dicts for cleaner output
        blocs_dict = {k: dict(v) for k, v in blocs.items()}

        return {
            'bloc_defining_caps': bloc_defining,
            'blocs': blocs_dict,
            'providers_info': providers_info
        }
