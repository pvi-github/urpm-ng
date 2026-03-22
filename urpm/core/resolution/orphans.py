"""Orphan package detection operations."""

import logging
from pathlib import Path
from typing import List, Optional

try:
    import rpm
    HAS_RPM = True
except ImportError:
    HAS_RPM = False


class OrphansMixin:
    """Mixin providing orphan package detection operations.

    Requires:
        - self.root: Optional[str] chroot path
        - self.db: PackageDatabase instance
        - TransactionType, PackageAction from resolver
    """

    def _find_orphans_iterative(self, initial_removes: set) -> list:
        """Find orphaned dependencies of removed packages iteratively.

        Strategy:
        1. Find all dependencies (direct and indirect) of packages being removed
        2. For each dependency, check if it's still required by remaining packages
        3. If not required by anyone else AND was installed as dependency, it's an orphan
        4. Repeat until no new orphans found

        Args:
            initial_removes: Set of package names being removed

        Returns:
            List of PackageAction for orphan packages
        """
        from ..resolver import TransactionType, PackageAction

        if not HAS_RPM:
            return []

        # Get packages installed as dependencies (not explicitly requested)
        # Only these can be considered orphans
        unrequested = self._get_unrequested_packages()
        if not unrequested:
            # No tracking - can't determine orphans reliably
            return []

        # Base packages that should never be considered orphans
        base_packages = {
            'basesystem', 'filesystem', 'setup', 'glibc', 'bash',
            'coreutils', 'rpm', 'systemd', 'dbus', 'util-linux',
            'shadow-utils', 'pam', 'ncurses', 'readline', 'zlib',
            'bzip2', 'xz', 'openssl', 'ca-certificates', 'krb5-libs',
            'libgcc', 'libstdc++', 'glib2', 'dbus-libs', 'audit-libs',
            'libselinux', 'pcre', 'pcre2', 'libcap', 'libacl', 'libattr',
            'expat', 'libffi', 'sqlite', 'nspr', 'nss', 'nss-util',
            'nss-softokn', 'nss-sysinit', 'p11-kit', 'p11-kit-trust',
        }

        ts = rpm.TransactionSet(self.root or '/')

        # Build complete picture of installed packages
        installed_pkgs = {}  # name -> {provides: set, requires: set, hdr: header}
        provides_map = {}    # capability -> set of package names that provide it

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            # Collect provides — use the full capability name including
            # parenthesised qualifiers like devel(libeconf(64bit)).
            # The parentheses are part of the capability name, NOT version
            # info (versions are tracked separately via PROVIDEVERSION).
            provides = set()
            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                provides.add(prov)
                provides_map.setdefault(prov, set()).add(name)

            requires = set()
            for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                if req.startswith('rpmlib(') or req.startswith('/'):
                    continue
                requires.add(req)

            installed_pkgs[name] = {
                'provides': provides,
                'requires': requires,
                'hdr': hdr,
            }

        # Step 1: Find all dependencies of packages being removed
        to_remove = set(initial_removes)
        candidate_orphans = set()

        # Collect all direct dependencies of removed packages
        # Only consider packages that were installed as dependencies (in unrequested)
        for name in list(to_remove):
            if name in installed_pkgs:
                for req in installed_pkgs[name]['requires']:
                    # Find what provides this requirement
                    providers = provides_map.get(req, set())
                    for provider in providers:
                        # Only consider as orphan candidate if:
                        # - Not already being removed
                        # - Not a base package
                        # - Was installed as a dependency (in unrequested)
                        if (provider not in to_remove and
                            provider not in base_packages and
                            provider.lower() in unrequested):
                            candidate_orphans.add(provider)

        # Step 2: Iteratively find orphans
        orphans = []
        max_iterations = 50

        for _ in range(max_iterations):
            new_orphans = []

            for name in list(candidate_orphans):
                if name in to_remove:
                    continue
                if name in base_packages:
                    continue
                if name not in installed_pkgs:
                    continue

                pkg = installed_pkgs[name]

                # Check if any remaining package requires this one
                is_required = False
                for prov in pkg['provides']:
                    for other_name, other_pkg in installed_pkgs.items():
                        if other_name == name:
                            continue
                        if other_name in to_remove:
                            continue
                        if prov in other_pkg['requires']:
                            is_required = True
                            break
                    if is_required:
                        break

                if not is_required:
                    # This package is an orphan
                    hdr = pkg['hdr']
                    epoch = hdr[rpm.RPMTAG_EPOCH] or 0
                    version = hdr[rpm.RPMTAG_VERSION]
                    release = hdr[rpm.RPMTAG_RELEASE]
                    arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                    size = hdr[rpm.RPMTAG_SIZE] or 0

                    if epoch and epoch > 0:
                        evr = f"{epoch}:{version}-{release}"
                    else:
                        evr = f"{version}-{release}"

                    new_orphans.append(PackageAction(
                        action=TransactionType.REMOVE,
                        name=name,
                        evr=evr,
                        arch=arch,
                        nevra=f"{name}-{evr}.{arch}",
                        size=size,
                    ))
                    to_remove.add(name)
                    candidate_orphans.discard(name)

                    # Add this orphan's dependencies as new candidates
                    # (only if they were installed as dependencies)
                    for req in pkg['requires']:
                        providers = provides_map.get(req, set())
                        for provider in providers:
                            if (provider not in to_remove and
                                provider not in base_packages and
                                provider.lower() in unrequested):
                                candidate_orphans.add(provider)

            if not new_orphans:
                break

            orphans.extend(new_orphans)

        return orphans

    def _get_unrequested_file(self) -> Path:
        """Get path to the installed-through-deps.list file."""
        root = self.root or '/'
        return Path(root) / 'var/lib/rpm/installed-through-deps.list'

    def _get_builddeps_file(self) -> Path:
        """Get path to the installed-through-builddeps.list file."""
        root = self.root or '/'
        return Path(root) / 'var/lib/rpm/installed-through-builddeps.list'

    def _get_builddep_packages(self) -> dict:
        """Read packages installed as build dependencies.

        Returns:
            Dict mapping lowercase package name to source (spec/srpm basename).
        """
        bd_file = self._get_builddeps_file()
        result = {}
        if bd_file.exists():
            try:
                for line in bd_file.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        parts = line.split('\t')
                        name = parts[0].lower()
                        source = parts[1] if len(parts) > 1 else ''
                        result[name] = source
            except (IOError, OSError):
                pass
        return result

    def _save_builddep_packages(self, packages: dict) -> bool:
        """Save the list of packages installed as build dependencies.

        Args:
            packages: Dict mapping lowercase package name to source basename.

        Returns:
            True if successful, False otherwise.
        """
        bd_file = self._get_builddeps_file()
        try:
            bd_file.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"{name}\t{source}" for name, source in sorted(packages.items())]
            bd_file.write_text('\n'.join(lines) + '\n' if lines else '')
            return True
        except (IOError, OSError, PermissionError):
            return False

    def mark_as_builddep(self, package_names: List[str], source: str) -> bool:
        """Mark packages as installed through build dependencies.

        Does not demote packages that are already explicitly installed
        (i.e. not in unrequested and not already in builddeps).

        Args:
            package_names: Package names to mark.
            source: Basename of the .spec or .src.rpm that required them.

        Returns:
            True if successful.
        """
        builddeps = self._get_builddep_packages()
        unrequested = self._get_unrequested_packages()
        for name in package_names:
            lower = name.lower()
            # Don't demote an explicitly installed package to builddep
            if lower not in unrequested and lower not in builddeps:
                continue
            builddeps[lower] = source
        return self._save_builddep_packages(builddeps)

    def unmark_builddep_packages(self, package_names: List[str]) -> bool:
        """Remove packages from the builddeps tracking list.

        Args:
            package_names: Package names to remove from builddeps tracking.

        Returns:
            True if successful.
        """
        builddeps = self._get_builddep_packages()
        changed = False
        for name in package_names:
            if name.lower() in builddeps:
                del builddeps[name.lower()]
                changed = True
        if changed:
            return self._save_builddep_packages(builddeps)
        return True

    def _get_unrequested_packages(self) -> set:
        """Read the list of packages installed as dependencies (not explicitly requested).

        This file is maintained by urpmi/urpm and contains package names that were
        pulled in as dependencies, not explicitly installed by the user.

        Returns:
            Set of package names that were installed as dependencies
        """
        unrequested_file = self._get_unrequested_file()
        unrequested = set()

        if unrequested_file.exists():
            try:
                for line in unrequested_file.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Remove any trailing comments like " (reason)" or "\t(reason)"
                        # split() without args handles all whitespace (spaces, tabs, etc.)
                        parts = line.split()
                        if parts:
                            unrequested.add(parts[0].lower())  # Normalize to lowercase
            except (IOError, OSError):
                pass

        return unrequested

    def _save_unrequested_packages(self, packages: set) -> bool:
        """Save the list of packages installed as dependencies.

        Args:
            packages: Set of package names to save

        Returns:
            True if successful, False otherwise
        """
        unrequested_file = self._get_unrequested_file()
        try:
            # Ensure directory exists
            unrequested_file.parent.mkdir(parents=True, exist_ok=True)
            # Write sorted list
            content = '\n'.join(sorted(packages)) + '\n' if packages else ''
            unrequested_file.write_text(content)
            return True
        except (IOError, OSError, PermissionError):
            return False

    def mark_as_dependency(self, package_names: List[str]) -> bool:
        """Mark packages as installed through dependencies.

        Args:
            package_names: List of package names to mark as dependencies

        Returns:
            True if successful
        """
        unrequested = self._get_unrequested_packages()
        unrequested.update(n.lower() for n in package_names)
        return self._save_unrequested_packages(unrequested)

    def mark_as_explicit(self, package_names: List[str]) -> bool:
        """Mark packages as explicitly installed (remove from deps list).

        Call this when a user explicitly installs a package that was
        previously installed as a dependency. Also removes the package
        from the builddeps list — explicit install takes precedence.

        Args:
            package_names: List of package names to mark as explicit

        Returns:
            True if successful
        """
        unrequested = self._get_unrequested_packages()
        for name in package_names:
            unrequested.discard(name.lower())

        # Also remove from builddeps — explicit install takes precedence
        builddeps = self._get_builddep_packages()
        bd_changed = False
        for name in package_names:
            if name.lower() in builddeps:
                del builddeps[name.lower()]
                bd_changed = True
        if bd_changed:
            self._save_builddep_packages(builddeps)

        return self._save_unrequested_packages(unrequested)

    def unmark_packages(self, package_names: List[str]) -> bool:
        """Remove packages from the tracking list (when uninstalled).

        Args:
            package_names: List of package names to remove from tracking

        Returns:
            True if successful
        """
        unrequested = self._get_unrequested_packages()
        for name in package_names:
            unrequested.discard(name.lower())
        return self._save_unrequested_packages(unrequested)

    def find_all_orphans(self) -> list:
        """Find ALL orphan packages in the system.

        Algorithm: For each package in unrequested (installed as dependency):
        - Walk UP reverse dependencies (who requires/recommends this package)
        - If any path leads to a package NOT in unrequested → keep it
        - If ALL paths only lead to other unrequested packages → orphan

        Returns:
            List of PackageAction for orphan packages
        """
        from ..resolver import TransactionType, PackageAction

        if not HAS_RPM:
            return []

        # Get packages that were installed as dependencies
        unrequested = self._get_unrequested_packages()
        if not unrequested:
            # No tracking file or empty - can't determine orphans reliably
            return []

        ts = rpm.TransactionSet(self.root or '/')

        # Build package info and reverse dependency map
        installed_pkgs = {}  # name -> {provides, hdr}
        provides_map = {}    # capability -> set of package names providing it
        reverse_deps = {}    # name -> set of names that require/recommend it

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            # Collect provides — use the full capability name.
            # Parentheses are part of the name (e.g. devel(libeconf(64bit))),
            # NOT version info.  Truncating with split('(') would merge
            # unrelated capabilities like devel(libfoo) and devel(libbar)
            # into a single "devel" bucket, creating phantom dependencies.
            provides = set()
            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                provides.add(prov)
                provides_map.setdefault(prov, set()).add(name)

            installed_pkgs[name] = {
                'provides': provides,
                'hdr': hdr,
            }
            reverse_deps[name] = set()

        # Build reverse dependency graph (who requires/recommends each package)
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            # Process Requires
            for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                if req.startswith('rpmlib(') or req.startswith('/'):
                    continue
                for provider in provides_map.get(req, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

            # Process Recommends
            for rec in (hdr[rpm.RPMTAG_RECOMMENDNAME] or []):
                for provider in provides_map.get(rec, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

        # Build lowercase -> actual name mapping for unrequested lookup
        name_to_lower = {name: name.lower() for name in installed_pkgs}

        # Builddep packages block orphan detection (they are intentionally installed)
        builddeps_set = set(self._get_builddep_packages().keys())

        # For each unrequested package, check if it leads to an explicit package
        def has_explicit_ancestor(pkg_name: str, visited: set) -> bool:
            """Walk up reverse deps to find if any explicit or builddep package depends on this."""
            if pkg_name in visited:
                return False
            visited.add(pkg_name)

            for dep_name in reverse_deps.get(pkg_name, set()):
                dep_lower = name_to_lower.get(dep_name, dep_name.lower())
                # Found an explicitly installed or builddep package that needs this
                if dep_lower not in unrequested or dep_lower in builddeps_set:
                    return True
                # Recurse up
                if has_explicit_ancestor(dep_name, visited):
                    return True

            return False

        # Find orphans - iterate through installed packages that are in unrequested
        orphans = []
        for name in installed_pkgs:
            if name.lower() not in unrequested:
                # Not a dependency - explicitly installed
                continue

            # Builddep packages have their own cleanup (autoremove --buildrequires)
            if name.lower() in builddeps_set:
                continue

            # Check if any explicit package depends on this (directly or indirectly)
            if not has_explicit_ancestor(name, set()):
                # No explicit package needs this -> orphan
                hdr = installed_pkgs[name]['hdr']
                epoch = hdr[rpm.RPMTAG_EPOCH] or 0
                version = hdr[rpm.RPMTAG_VERSION]
                release = hdr[rpm.RPMTAG_RELEASE]
                arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
                size = hdr[rpm.RPMTAG_SIZE] or 0

                if epoch and epoch > 0:
                    evr = f"{epoch}:{version}-{release}"
                else:
                    evr = f"{version}-{release}"

                orphans.append(PackageAction(
                    action=TransactionType.REMOVE,
                    name=name,
                    evr=evr,
                    arch=arch,
                    nevra=f"{name}-{evr}.{arch}",
                    size=size,
                ))

        return orphans

    def find_all_builddep_orphans(self) -> list:
        """Find builddep packages that can be safely removed.

        A builddep is removable if it has no explicit ancestor outside
        the builddeps and unrequested sets (i.e. nothing explicitly
        installed by the user depends on it).

        Returns:
            List of PackageAction for removable builddep packages.
        """
        from ..resolver import TransactionType, PackageAction

        if not HAS_RPM:
            return []

        builddeps = self._get_builddep_packages()
        if not builddeps:
            return []

        unrequested = self._get_unrequested_packages()
        builddeps_set = set(builddeps.keys())

        ts = rpm.TransactionSet(self.root or '/')

        # Build package info and reverse dependency map
        installed_pkgs = {}
        provides_map = {}
        reverse_deps = {}

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            provides = set()
            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                provides.add(prov)
                provides_map.setdefault(prov, set()).add(name)

            installed_pkgs[name] = {
                'provides': provides,
                'hdr': hdr,
            }
            reverse_deps[name] = set()

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                if req.startswith('rpmlib(') or req.startswith('/'):
                    continue
                for provider in provides_map.get(req, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

            for rec in (hdr[rpm.RPMTAG_RECOMMENDNAME] or []):
                for provider in provides_map.get(rec, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

        name_to_lower = {name: name.lower() for name in installed_pkgs}

        def has_real_explicit_ancestor(pkg_name: str, visited: set) -> bool:
            """Check if an explicit package (not builddep, not unrequested) depends on this."""
            if pkg_name in visited:
                return False
            visited.add(pkg_name)

            for dep_name in reverse_deps.get(pkg_name, set()):
                dep_lower = name_to_lower.get(dep_name, dep_name.lower())
                # A real explicit package: not in unrequested AND not a builddep
                if dep_lower not in unrequested and dep_lower not in builddeps_set:
                    return True
                if has_real_explicit_ancestor(dep_name, visited):
                    return True

            return False

        orphans = []
        for name in installed_pkgs:
            if name.lower() not in builddeps_set:
                continue

            if has_real_explicit_ancestor(name, set()):
                continue

            hdr = installed_pkgs[name]['hdr']
            epoch = hdr[rpm.RPMTAG_EPOCH] or 0
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
            size = hdr[rpm.RPMTAG_SIZE] or 0

            if epoch and epoch > 0:
                evr = f"{epoch}:{version}-{release}"
            else:
                evr = f"{version}-{release}"

            orphans.append(PackageAction(
                action=TransactionType.REMOVE,
                name=name,
                evr=evr,
                arch=arch,
                nevra=f"{name}-{evr}.{arch}",
                size=size,
            ))

        return orphans

    def find_orphans(self, exclude_names: List[str] = None) -> list:
        """Find orphan packages (installed as deps but no longer needed).

        Args:
            exclude_names: Package names to exclude from orphan check

        Returns:
            List of PackageAction for orphan packages
        """
        from ..resolver import TransactionType, PackageAction

        if not HAS_RPM:
            return []

        exclude = set(n.lower() for n in (exclude_names or []))
        orphans = []

        # Get all installed packages and their reverse deps
        ts = rpm.TransactionSet(self.root or '/')

        # Build a map of what each package requires
        required_by = {}  # package_name -> set of packages that need it

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            requires = hdr[rpm.RPMTAG_REQUIRENAME] or []

            for req in requires:
                # Skip rpmlib, file deps, and self-requires
                if req.startswith("rpmlib(") or req.startswith("/"):
                    continue
                # Use the full capability name — parentheses are part of
                # the name (e.g. devel(libeconf(64bit))), not version info.
                required_by.setdefault(req, set()).add(name)

        # Find packages that nothing requires (potential orphans)
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]

            # Skip excluded packages
            if name.lower() in exclude:
                continue

            # Skip base system packages (heuristic: packages starting with these are likely essential)
            if name in ('glibc', 'bash', 'coreutils', 'filesystem', 'setup', 'basesystem'):
                continue

            # Check if any installed package requires this one
            provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
            is_required = False

            for prov in provides:
                if prov in required_by:
                    # Check if any requirer is still installed (not in exclude list)
                    for req in required_by[prov]:
                        if req.lower() not in exclude and req != name:
                            is_required = True
                            break
                if is_required:
                    break

            if not is_required:
                # This package might be an orphan
                epoch = hdr[rpm.RPMTAG_EPOCH] or 0
                version = hdr[rpm.RPMTAG_VERSION]
                release = hdr[rpm.RPMTAG_RELEASE]
                arch = hdr[rpm.RPMTAG_ARCH] or "noarch"
                size = hdr[rpm.RPMTAG_SIZE] or 0

                if epoch and epoch > 0:
                    evr = f"{epoch}:{version}-{release}"
                else:
                    evr = f"{version}-{release}"

                orphans.append(PackageAction(
                    action=TransactionType.REMOVE,
                    name=name,
                    evr=evr,
                    arch=arch,
                    nevra=f"{name}-{evr}.{arch}",
                    size=size,
                ))

        return orphans

    def _extract_cap_name(self, cap: str) -> str:
        """Extract base capability name from a versioned capability string.

        Examples:
            "libpng[>= 1.6.0]" -> "libpng"
            "perl(Foo::Bar)" -> "perl(Foo::Bar)"
            "libfoo.so.1()(64bit)" -> "libfoo.so.1()(64bit)"
        """
        # Handle [version] suffix
        if '[' in cap:
            cap = cap.split('[')[0]
        return cap

    def find_upgrade_orphans(self, all_actions: list) -> list:
        """Find packages that will become orphans after a transaction.

        Simulates the post-transaction state (applying upgrades, installs,
        and removes) then checks which unrequested packages are no longer
        required by anyone.

        Args:
            all_actions: All PackageActions in the transaction (upgrades,
                         installs, removes, downgrades).

        Returns:
            List of PackageAction for packages that will become orphans
        """
        from ..resolver import TransactionType, PackageAction

        if not HAS_RPM:
            return []

        unrequested = self._get_unrequested_packages()
        if not unrequested:
            return []

        ts = rpm.TransactionSet(self.root or '/')

        # Classify transaction actions
        upgraded_names = set()   # Packages being upgraded (same name, new version)
        installed_names = set()  # New packages being installed
        removed_names = set()    # Packages being removed (obsoleted, etc.)

        for action in all_actions:
            if action.action == TransactionType.UPGRADE:
                upgraded_names.add(action.name)
            elif action.action == TransactionType.INSTALL:
                installed_names.add(action.name)
            elif action.action == TransactionType.REMOVE:
                removed_names.add(action.name)

        # If nothing changes, no orphans
        if not upgraded_names and not installed_names and not removed_names:
            return []

        # Build post-transaction state: {name -> (requires, provides)}
        # Start with current installed packages
        post_state = {}  # name -> {'requires': set, 'provides': set}

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            # Skip packages being removed
            if name in removed_names:
                continue

            if name in upgraded_names:
                # Use NEW requires/provides from the database
                pkg = self.db.get_package(name)
                requires = set()
                provides = {name}  # Self-provide
                if pkg:
                    for r in pkg.get('requires', []):
                        cap = self._extract_cap_name(r)
                        if not cap.startswith('rpmlib('):
                            requires.add(cap)
                    for p in pkg.get('provides', []):
                        provides.add(self._extract_cap_name(p))
                post_state[name] = {'requires': requires, 'provides': provides}
            else:
                # Package stays as-is
                requires = set()
                for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                    if not req.startswith('rpmlib('):
                        requires.add(self._extract_cap_name(req))
                provides = {name}
                for p in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                    provides.add(self._extract_cap_name(p))
                post_state[name] = {'requires': requires, 'provides': provides}

        # Add newly installed packages (not in rpmdb yet)
        for name in installed_names:
            if name in post_state:
                continue  # Already handled (shouldn't happen)
            pkg = self.db.get_package(name)
            requires = set()
            provides = {name}
            if pkg:
                for r in pkg.get('requires', []):
                    cap = self._extract_cap_name(r)
                    if not cap.startswith('rpmlib('):
                        requires.add(cap)
                for p in pkg.get('provides', []):
                    provides.add(self._extract_cap_name(p))
            post_state[name] = {'requires': requires, 'provides': provides}

        # Build reverse-provides map: capability -> set of package names
        cap_providers = {}  # cap -> {pkg_names}
        for name, info in post_state.items():
            for cap in info['provides']:
                cap_providers.setdefault(cap, set()).add(name)

        # Build reverse-dependency graph: who requires each package?
        reverse_deps = {name: set() for name in post_state}
        for name, info in post_state.items():
            for req in info['requires']:
                for provider in cap_providers.get(req, set()):
                    if provider != name:
                        reverse_deps.setdefault(provider, set()).add(name)

        # Extend unrequested with newly installed packages (they are deps,
        # not explicitly requested by the user)
        effective_unrequested = set(unrequested)
        for name in installed_names:
            effective_unrequested.add(name.lower())

        # Find orphans: unrequested packages with no path to an explicit package
        def has_explicit_ancestor(pkg_name: str, visited: set) -> bool:
            """Walk up reverse deps to find any explicit (non-unrequested) package."""
            if pkg_name in visited:
                return False
            visited.add(pkg_name)
            for dep_name in reverse_deps.get(pkg_name, set()):
                if dep_name.lower() not in effective_unrequested:
                    return True
                if has_explicit_ancestor(dep_name, visited):
                    return True
            return False

        # Check all unrequested packages in the post-transaction state
        orphan_candidates = set()
        for name in post_state:
            if name.lower() not in effective_unrequested:
                continue
            if not has_explicit_ancestor(name, set()):
                orphan_candidates.add(name)

        # Build PackageAction list for orphans
        orphans = []
        seen_orphans = set()

        # Orphans already in rpmdb (upgraded or unchanged packages)
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name not in orphan_candidates:
                continue

            epoch = hdr[rpm.RPMTAG_EPOCH] or 0
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
            size = hdr[rpm.RPMTAG_SIZE] or 0

            if epoch and epoch > 0:
                evr = f"{epoch}:{version}-{release}"
            else:
                evr = f"{version}-{release}"

            orphans.append(PackageAction(
                action=TransactionType.REMOVE,
                name=name,
                evr=evr,
                arch=arch,
                nevra=f"{name}-{evr}.{arch}",
                size=size,
            ))
            seen_orphans.add(name)

        # Orphans that are new installs (not yet in rpmdb) — these should
        # be excluded from the transaction rather than erased
        for action in all_actions:
            if action.name in orphan_candidates and action.name not in seen_orphans:
                orphans.append(PackageAction(
                    action=TransactionType.REMOVE,
                    name=action.name,
                    evr=action.evr,
                    arch=action.arch,
                    nevra=action.nevra,
                    size=action.size,
                ))

        return orphans

    def find_erase_orphans(self, erase_names: List[str], erase_recommends: bool = False,
                           keep_suggests: bool = False) -> list:
        """Find packages that will become orphans after erasing packages.

        Strategy:
        1. Build the forward dependency tree of packages being erased
        2. For each package in the tree: if ALL its reverse-deps are also
           in the tree, it can be removed (it's an orphan)
        3. Only packages in unrequested can be auto-removed (except explicit ones)

        Args:
            erase_names: List of package names being erased (including reverse deps)
            erase_recommends: If True, RECOMMENDS don't block removal (only REQUIRES do)
            keep_suggests: If True, SUGGESTS also block removal

        Returns:
            List of PackageAction for packages that will become orphans
        """
        from ..resolver import TransactionType, PackageAction, DEBUG_RESOLVER

        if not HAS_RPM:
            return []

        # Get packages installed as dependencies (not explicitly requested)
        unrequested = self._get_unrequested_packages()

        ts = rpm.TransactionSet(self.root or '/')

        # Build maps for all installed packages
        pkg_provides = {}  # name -> set of capability names
        pkg_requires = {}  # name -> set of capability names (raw, not resolved)
        pkg_recommends = {}  # name -> set of recommended capability names
        pkg_suggests = {}  # name -> set of suggested capability names
        cap_to_pkg = {}    # capability -> set of package names providing it
        name_to_original = {}  # lowercase name -> original case name
        pkg_headers = {}   # name -> rpm header
        all_installed = set()

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            all_installed.add(name)
            name_to_original[name.lower()] = name
            pkg_headers[name] = hdr

            provides = set()
            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                cap = self._extract_cap_name(prov)
                provides.add(cap)
                if cap not in cap_to_pkg:
                    cap_to_pkg[cap] = set()
                cap_to_pkg[cap].add(name)
            pkg_provides[name] = provides

            requires = set()
            for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                if not req.startswith('rpmlib(') and not req.startswith('/'):
                    requires.add(self._extract_cap_name(req))
            pkg_requires[name] = requires

            # Also collect RECOMMENDS for dep_tree building
            recommends = set()
            for rec in (hdr[rpm.RPMTAG_RECOMMENDNAME] or []):
                if not rec.startswith('rpmlib(') and not rec.startswith('/'):
                    recommends.add(self._extract_cap_name(rec))
            pkg_recommends[name] = recommends

            # Also collect SUGGESTS for dep_tree building
            suggests = set()
            for sug in (hdr[rpm.RPMTAG_SUGGESTNAME] or []):
                if not sug.startswith('rpmlib(') and not sug.startswith('/'):
                    suggests.add(self._extract_cap_name(sug))
            pkg_suggests[name] = suggests

        # Helper: resolve a capability to the installed package that provides it
        def resolve_cap_to_pkg(cap: str) -> Optional[str]:
            """Find which installed package provides this capability."""
            providers = cap_to_pkg.get(cap, set())
            if len(providers) == 1:
                return next(iter(providers))
            elif len(providers) > 1:
                # Multiple providers - return the first one (all are installed)
                return next(iter(providers))
            return None


        # Helper: get direct dependencies of a package (as package names)
        def get_direct_deps(pkg_name: str) -> set:
            """Get packages that pkg_name directly depends on (REQUIRES only)."""
            deps = set()
            for cap in pkg_requires.get(pkg_name, set()):
                provider = resolve_cap_to_pkg(cap)
                if provider and provider != pkg_name:  # Skip self-deps
                    deps.add(provider)
            return deps

        # Helper: get deps including recommends (for dep_tree building)
        # Note: SUGGESTS are NOT followed because they are not installed by default
        def get_all_deps(pkg_name: str) -> set:
            """Get packages that pkg_name depends on or recommends (not suggests)."""
            deps = set()
            # REQUIRES
            for cap in pkg_requires.get(pkg_name, set()):
                provider = resolve_cap_to_pkg(cap)
                if provider and provider != pkg_name:
                    deps.add(provider)
            # RECOMMENDS (installed by default)
            for cap in pkg_recommends.get(pkg_name, set()):
                provider = resolve_cap_to_pkg(cap)
                if provider and provider != pkg_name:
                    deps.add(provider)
            # SUGGESTS are NOT followed - they are not installed by default
            return deps

        # Build reverse index: capability -> list of (pkg_that_needs_it, dep_type)
        # This is much faster than iterating all packages for each candidate
        cap_needed_by = {}  # cap -> [(pkg, dep_type), ...]
        for pkg_name in all_installed:
            for cap in pkg_requires.get(pkg_name, set()):
                if cap not in cap_needed_by:
                    cap_needed_by[cap] = []
                cap_needed_by[cap].append((pkg_name, 'R'))
            if not erase_recommends:
                for cap in pkg_recommends.get(pkg_name, set()):
                    if cap not in cap_needed_by:
                        cap_needed_by[cap] = []
                    cap_needed_by[cap].append((pkg_name, 'M'))
            if keep_suggests:
                for cap in pkg_suggests.get(pkg_name, set()):
                    if cap not in cap_needed_by:
                        cap_needed_by[cap] = []
                    cap_needed_by[cap].append((pkg_name, 'S'))

        # Normalize erase_names to original case
        erase_set_original = set()
        for name in erase_names:
            orig = name_to_original.get(name.lower())
            if orig:
                erase_set_original.add(orig)

        # STEP 1: Build the forward dependency tree (including RECOMMENDS)
        # Start from packages being erased and follow their deps + recommends recursively
        dep_tree = set(erase_set_original)
        to_process = list(erase_set_original)

        while to_process:
            pkg = to_process.pop()
            for dep in get_all_deps(pkg):
                if dep not in dep_tree:
                    dep_tree.add(dep)
                    to_process.append(dep)

        # STEP 2: Find orphans
        # A package is an orphan if:
        # 1. It's in dep_tree (dependency of something being removed)
        # 2. It's in unrequested (was installed as a dependency)
        # 3. No package that will REMAIN installed requires it

        # Initial set of packages to remove
        to_remove = set(erase_set_original)  # Always include explicitly requested
        for pkg in dep_tree:
            if pkg.lower() in unrequested:
                to_remove.add(pkg)

        # Debug: write initial state
        logger = logging.getLogger(__name__)
        logger.debug(f"Orphan detection: dep_tree={len(dep_tree)}, unrequested={len(unrequested)}, initial to_remove={len(to_remove)}")

        if DEBUG_RESOLVER:
            try:
                with open('.debug-orphans.log', 'w') as f:
                    f.write(f"dep_tree size: {len(dep_tree)}\n")
                    f.write(f"unrequested size: {len(unrequested)}\n")
                    f.write(f"initial to_remove size: {len(to_remove)}\n")
                    f.write(f"all_installed size: {len(all_installed)}\n")
                    f.write(f"cap_to_pkg size: {len(cap_to_pkg)}\n\n")

                    # Check .so capability resolution
                    test_cap = "libKF6CoreAddons.so.6()(64bit)"
                    if test_cap in cap_to_pkg:
                        f.write(f"'{test_cap}' -> {cap_to_pkg[test_cap]}\n")
                    else:
                        f.write(f"'{test_cap}' NOT in cap_to_pkg\n")
                        similar = [c for c in cap_to_pkg.keys() if 'KF6CoreAddons' in c]
                        f.write(f"Similar caps: {similar[:10]}\n")
                    f.write("\n")

                    # Check if lib64kf6coreaddons6 is in dep_tree and to_remove
                    f.write(f"lib64kf6coreaddons6 in dep_tree: {'lib64kf6coreaddons6' in dep_tree}\n")
                    f.write(f"kcoreaddons in dep_tree: {'kcoreaddons' in dep_tree}\n")
                    f.write(f"lib64kf6coreaddons6 in to_remove: {'lib64kf6coreaddons6' in to_remove}\n")
                    f.write(f"kcoreaddons in to_remove: {'kcoreaddons' in to_remove}\n")
                    f.write(f"'lib64kf6coreaddons6' in unrequested: {'lib64kf6coreaddons6' in unrequested}\n\n")

                    # Check which dep_tree packages are NOT in unrequested
                    not_in_unrequested = [p for p in dep_tree if p.lower() not in unrequested]
                    f.write(f"dep_tree packages NOT in unrequested ({len(not_in_unrequested)}):\n")
                    for p in sorted(not_in_unrequested)[:50]:
                        f.write(f"  {p}\n")
                    if len(not_in_unrequested) > 50:
                        f.write(f"  ... and {len(not_in_unrequested) - 50} more\n")
                    f.write("\n")
            except:
                pass

        # Orphan detection algorithm:
        # A package can be removed if ALL its reverse deps are also being removed,
        # OR if there are other providers of the required capability that remain.
        # We iteratively remove packages from candidates that have blocking rdeps.

        candidates = set(to_remove)
        candidates_lower = {p.lower() for p in candidates}
        removed_from_candidates = {}  # For debug: pkg -> (blocker, dep_type, capability)

        # Iterate until stable (use sorted for determinism)
        changed = True
        iteration = 0
        while changed:
            changed = False
            iteration += 1
            for pkg_name in sorted(candidates):
                if pkg_name in erase_set_original:
                    continue  # Always remove explicitly requested packages

                # Check if any package outside candidates needs a capability we provide
                # and we are the only remaining provider
                dominated = False
                blocker_info = None
                for cap in pkg_provides.get(pkg_name, set()):
                    for dependent, dep_type in cap_needed_by.get(cap, []):
                        if dependent == pkg_name:
                            continue
                        dep_lower = dependent.lower()
                        # Skip if dependent is also being removed
                        if dep_lower in candidates_lower:
                            continue
                        # dependent needs cap and is NOT being removed
                        # Check if there are other providers that remain
                        providers = cap_to_pkg.get(cap, set())
                        remaining = [p for p in providers
                                     if p != pkg_name and p.lower() not in candidates_lower]
                        if not remaining:
                            # No other provider - this blocks removal
                            dominated = True
                            blocker_info = (dependent, dep_type, cap)
                            break
                    if dominated:
                        break

                if dominated:
                    candidates.remove(pkg_name)
                    candidates_lower.remove(pkg_name.lower())
                    removed_from_candidates[pkg_name] = blocker_info
                    changed = True

        to_remove = candidates

        if DEBUG_RESOLVER:
            try:
                with open('.debug-orphans.log', 'a') as f:
                    f.write(f"Options: erase_recommends={erase_recommends}, keep_suggests={keep_suggests}\n")
                    f.write(f"Iterations: {iteration}\n")
                    f.write(f"Initial candidates: {len(to_remove) + len(removed_from_candidates)}\n")
                    f.write(f"Removed from candidates: {len(removed_from_candidates)}\n")
                    f.write(f"Final to_remove: {len(to_remove)}\n\n")
                    f.write(f"Packages that must stay (R=Requires, M=Recommends, S=Suggests):\n")
                    for pkg in sorted(removed_from_candidates.keys()):
                        blocker, dep_type, cap = removed_from_candidates[pkg]
                        f.write(f"  {pkg} <-[{dep_type}]- {blocker} (via {cap})\n")
            except:
                pass

        logger.debug(f"Orphan detection: iterations={iteration}, kept={len(removed_from_candidates)}, final={len(to_remove)}")

        # Build PackageAction list (exclude the explicitly erased packages)
        erase_set_lower = set(n.lower() for n in erase_names)
        orphans = []

        for name in to_remove:
            if name.lower() in erase_set_lower:
                continue

            hdr = pkg_headers.get(name)
            if not hdr:
                continue

            epoch = hdr[rpm.RPMTAG_EPOCH] or 0
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or 'noarch'
            size = hdr[rpm.RPMTAG_SIZE] or 0

            if epoch and epoch > 0:
                evr = f"{epoch}:{version}-{release}"
            else:
                evr = f"{version}-{release}"

            orphans.append(PackageAction(
                action=TransactionType.REMOVE,
                name=name,
                evr=evr,
                arch=arch,
                nevra=f"{name}-{evr}.{arch}",
                size=size,
            ))

        return orphans
