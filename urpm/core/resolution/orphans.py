"""Orphan package detection operations."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import rpm
    HAS_RPM = True
except ImportError:
    HAS_RPM = False


# --- Module-level constants for versioned capability handling ---------------
#
# A "sense" is an RPMSENSE_* bitmask describing a dependency comparison
# operator (e.g. ``>=`` is ``RPMSENSE_GREATER | RPMSENSE_EQUAL``).  The
# mask below isolates the three comparison bits from the other RPMSENSE
# flags (PREREQ, SCRIPT_PRE, …) which do not affect satisfiability.

if HAS_RPM:
    _SENSE_MASK = (
        rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL | rpm.RPMSENSE_GREATER
    )
    _SYNTHESIS_SENSE_MAP = {
        '<':  rpm.RPMSENSE_LESS,
        '<=': rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL,
        '=':  rpm.RPMSENSE_EQUAL,
        '==': rpm.RPMSENSE_EQUAL,
        '>=': rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL,
        '>':  rpm.RPMSENSE_GREATER,
    }
else:  # pragma: no cover - rpm is always available in production
    _SENSE_MASK = 0
    _SYNTHESIS_SENSE_MAP = {}


@dataclass
class UpgradeOrphanPlan:
    """Plan for handling orphans discovered during an upgrade transaction.

    :meth:`OrphansMixin.find_upgrade_orphans` partitions new orphans
    into two disjoint concerns so the caller can project them cleanly
    onto the transaction:

    Attributes:
        removes: ``PackageAction`` instances (always ``REMOVE``) for
            packages that are **currently installed in the rpmdb** and
            become orphaned by the transaction.  The caller should
            erase them.  For a package being upgraded-then-orphaned the
            action carries the **old** (rpmdb) EVR — the caller must
            additionally skip the new version (see
            :attr:`cancelled_new_versions`).
        cancelled_new_versions: Lowercase names of packages whose new
            version must not be installed because it would be orphaned
            on arrival.  This covers both:

            * brand-new ``INSTALL`` actions whose only requester is
              another orphan (e.g. a dep rename without ``Obsoletes``);
              and
            * ``UPGRADE``/``DOWNGRADE``/``REINSTALL`` actions whose
              target version has no legitimate requester (its companion
              old version, if any, is in :attr:`removes`).

            The caller must drop the corresponding action from the
            transaction plan *and* skip the matching downloaded RPM.

    The historical flat ``List[PackageAction]`` return type conflated
    the two concerns and caused silent no-op transactions when libsolv
    was asked to simultaneously install and remove the same package
    name.
    """

    removes: List = field(default_factory=list)
    cancelled_new_versions: Set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.removes or self.cancelled_new_versions)


def _parse_synthesis_cap(cap: str) -> Tuple[str, int, str]:
    """Parse a synthesis capability string into ``(name, sense, evr)``.

    The Mageia synthesis format stores a versioned capability with a
    bracket suffix: ``libpng[>= 1.6.0]``.  Non-versioned capabilities
    have no bracket, e.g. ``perl(Foo::Bar)`` or
    ``libfoo.so.1()(64bit)``.  Brackets never appear inside unversioned
    capability names in this format, so a plain ``find('[')`` is
    unambiguous.

    Args:
        cap: Raw capability string from ``synthesis.hdlist.cz``.

    Returns:
        A tuple ``(name, sense, evr)`` where ``sense`` is an RPMSENSE
        bitmask (``0`` for an unversioned capability) and ``evr`` is the
        version string (empty for an unversioned capability).
    """
    idx = cap.find('[')
    if idx < 0:
        return cap, 0, ''
    name = cap[:idx]
    end = cap.rfind(']')
    inside = cap[idx + 1:end] if end > idx else cap[idx + 1:]
    parts = inside.split(None, 1)
    if len(parts) != 2:
        return name, 0, ''
    op, evr = parts
    return name, _SYNTHESIS_SENSE_MAP.get(op, 0), evr


def _evr_tuple(evr: str) -> Tuple[str, str, str]:
    """Split an ``epoch:version-release`` string for :func:`rpm.labelCompare`.

    Missing epoch defaults to ``'0'``; missing release to ``''``.
    """
    if ':' in evr:
        epoch, rest = evr.split(':', 1)
    else:
        epoch, rest = '0', evr
    if '-' in rest:
        version, release = rest.split('-', 1)
    else:
        version, release = rest, ''
    return (epoch, version, release)


def _provider_satisfies(prov_evr: str, req_sense: int, req_evr: str) -> bool:
    """Return ``True`` iff a provider's EVR satisfies a versioned require.

    The check mirrors rpm's own ``rpmdsCompare`` semantics:

    * An unversioned require (no comparison bit set) is satisfied by
      any provider — the same answer ``_SENSE_MASK``-masking would
      give.
    * A versioned require against an unversioned provider fails.
      RPM's auto-generated ``Provides: NAME = EVR`` covers almost every
      real package, so an unversioned provider here typically means an
      explicit ``Provides: foo`` without a version, which cannot be
      compared against a version constraint.
    * Otherwise the two EVRs are compared with :func:`rpm.labelCompare`
      and the result cross-referenced against the require's sense
      bits.  Granularity matches rpm: if the require omits the release
      component (``Requires: foo = 1`` instead of ``= 1-1``), the
      provider's release is ignored so the comparison degenerates to
      version-only equality.  This is how rpm accepts ``foo-1-5`` as a
      valid provider for ``Requires: foo = 1``.
    """
    if not HAS_RPM:  # pragma: no cover - rpm is always available in production
        return True
    if not (req_sense & _SENSE_MASK):
        return True
    if not prov_evr:
        return False
    p_epoch, p_ver, p_rel = _evr_tuple(prov_evr)
    r_epoch, r_ver, r_rel = _evr_tuple(req_evr)
    if not r_rel:
        p_rel = ''
    result = rpm.labelCompare((p_epoch, p_ver, p_rel), (r_epoch, r_ver, r_rel))
    if result == 0:
        return bool(req_sense & rpm.RPMSENSE_EQUAL)
    if result < 0:
        return bool(req_sense & rpm.RPMSENSE_LESS)
    return bool(req_sense & rpm.RPMSENSE_GREATER)


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
            return []

        ts = rpm.TransactionSet(self.root or '/')

        # Single-pass: build provides map, reverse deps, and collect headers
        installed_pkgs = {}  # name -> hdr
        provides_map = {}    # capability -> set of package names
        reverse_deps = {}    # name -> set of names that require/recommend it
        pkg_requires = {}    # name -> list of capability names
        pkg_recommends = {}  # name -> list of capability names

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            installed_pkgs[name] = hdr
            reverse_deps[name] = set()

            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                provides_map.setdefault(prov, set()).add(name)

            pkg_requires[name] = [
                r for r in (hdr[rpm.RPMTAG_REQUIRENAME] or [])
                if not r.startswith('rpmlib(') and not r.startswith('/')
            ]
            pkg_recommends[name] = list(hdr[rpm.RPMTAG_RECOMMENDNAME] or [])

        # Build reverse dependency graph from cached requires/recommends
        for name in installed_pkgs:
            for req in pkg_requires[name]:
                for provider in provides_map.get(req, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)
            for rec in pkg_recommends[name]:
                for provider in provides_map.get(rec, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

        # Builddep packages block orphan detection
        builddeps_set = set(self._get_builddep_packages().keys())

        # Cached DFS: only True results are cached.  False is never cached
        # because it may be path-dependent: cycle detection returns False
        # when a node is already in the current DFS path, but the node may
        # be reachable via a different path that avoids the cycle.  Since
        # set iteration order is non-deterministic (Python hash seed), caching
        # False would produce random false-positive orphans.
        _cache_true: set = set()

        def has_explicit_ancestor(pkg_name: str, path: set) -> bool:
            """Walk up reverse deps with memoization (True-only cache)."""
            if pkg_name in _cache_true:
                return True
            if pkg_name in path:
                return False
            path.add(pkg_name)

            for dep_name in reverse_deps.get(pkg_name, set()):
                dep_lower = dep_name.lower()
                if dep_lower not in unrequested or dep_lower in builddeps_set:
                    _cache_true.add(pkg_name)
                    return True
                if has_explicit_ancestor(dep_name, path):
                    _cache_true.add(pkg_name)
                    return True

            return False

        # Find orphans
        orphans = []
        for name, hdr in installed_pkgs.items():
            name_lower = name.lower()
            if name_lower not in unrequested:
                continue
            if name_lower in builddeps_set:
                continue

            if not has_explicit_ancestor(name, set()):
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

    def find_upgrade_orphans(self, all_actions: list,
                             obsoleted_names: set = None) -> "UpgradeOrphanPlan":
        """Find packages that become newly orphaned by a transaction.

        Implements the formal specification::

            new_orphans(T) = { P ∈ S_post |
                unrequested(P) ∧ orphan(P, S_post)
                ∧ ¬(P ∈ S_pre ∧ orphan(P, S_pre)) }

        where ``orphan(P, S) ≡ ∀Q ∈ S, P ∉ Requires(Q) ∪ Recommends(Q)``.

        Pre-existing orphans (already orphan before the transaction) are
        NOT flagged — that is the job of ``urpm autoremove``.  Weak deps
        (Recommends) count as real dependency edges for this
        computation: a package pulled only by Recommends is a
        legitimate child of its parent, and must be flagged if the
        parent stops recommending it.

        Dependency edges are **version-aware**: a ``Requires: tt >= 2``
        against a provider ``tt1`` that only advertises ``Provides: tt =
        1`` does NOT establish an edge.  Without this filter a virtual
        rename (``tt1`` being replaced by ``tt2``, both providing
        ``tt``) would leave the old provider undetected as orphan.

        Args:
            all_actions: All ``PackageAction`` instances in the
                transaction (upgrades, installs, removes, downgrades).
            obsoleted_names: Package names being replaced via Obsoletes.
                These are removed implicitly by rpm (not in
                ``all_actions`` with SHOW_ACTIVE) and must not be
                classified as orphans.

        Returns:
            An :class:`UpgradeOrphanPlan` partitioning the new orphans
            into:

            * ``removes`` — REMOVE actions for orphans currently in the
              rpmdb, ready to append to the transaction.
            * ``cancelled_new_versions`` — lowercase names whose
              INSTALL/UPGRADE/DOWNGRADE/REINSTALL action must be dropped
              from the plan (and whose downloaded RPM must be skipped)
              because the new version would be orphan-on-arrival.

            Both fields are populated for a package being
            upgraded-then-orphaned: ``removes`` erases the old version
            and the name appears in ``cancelled_new_versions`` so the
            caller does not install the new version.
        """
        from ..resolver import TransactionType, PackageAction

        if not HAS_RPM:
            return UpgradeOrphanPlan()

        unrequested = self._get_unrequested_packages()
        if not unrequested:
            return UpgradeOrphanPlan()

        # Exclude builddeps — managed separately via autoremove --buildrequires
        builddeps = self._get_builddep_packages()
        unrequested -= set(builddeps.keys())

        ts = rpm.TransactionSet(self.root or '/')

        upgraded_names = set()
        installed_names = set()
        removed_names = set()

        for action in all_actions:
            if action.action == TransactionType.UPGRADE:
                upgraded_names.add(action.name)
            elif action.action == TransactionType.INSTALL:
                installed_names.add(action.name)
            elif action.action == TransactionType.REMOVE:
                removed_names.add(action.name)

        # Packages replaced via Obsoletes are removed implicitly by rpm
        # but do not appear in all_actions (SHOW_ACTIVE hides them).
        # Include them in removed_names so they are excluded from
        # post_state, and track them separately to avoid misclassifying
        # them as orphans.
        if obsoleted_names:
            removed_names |= obsoleted_names

        if not (upgraded_names or installed_names or removed_names):
            return UpgradeOrphanPlan()

        logger = logging.getLogger(__name__)

        def _collect_from_header(hdr):
            """Extract versioned requires/recommends/provides from a header.

            Returns ``(requires, provides)`` where:

            * ``requires`` is a ``list[(name, sense, evr)]`` — an edge
              is only satisfied by a provider whose EVR matches the
              ``(sense, evr)`` constraint.  Recommends are merged in
              because they are real dependency edges for orphan
              computation.
            * ``provides`` is a ``list[(name, evr)]``.

            ``rpmlib(…)`` build-time capabilities are stripped.
            """
            reqs: List[Tuple[str, int, str]] = []
            req_names = hdr[rpm.RPMTAG_REQUIRENAME] or []
            req_vers = hdr[rpm.RPMTAG_REQUIREVERSION] or []
            req_flags = hdr[rpm.RPMTAG_REQUIREFLAGS] or []
            for i, req_name in enumerate(req_names):
                if req_name.startswith('rpmlib('):
                    continue
                ver = req_vers[i] if i < len(req_vers) else ''
                flag_raw = req_flags[i] if i < len(req_flags) else 0
                reqs.append((req_name, flag_raw & _SENSE_MASK, ver or ''))

            rec_names = hdr[rpm.RPMTAG_RECOMMENDNAME] or []
            rec_vers = hdr[rpm.RPMTAG_RECOMMENDVERSION] or []
            rec_flags = hdr[rpm.RPMTAG_RECOMMENDFLAGS] or []
            for i, rec_name in enumerate(rec_names):
                ver = rec_vers[i] if i < len(rec_vers) else ''
                flag_raw = rec_flags[i] if i < len(rec_flags) else 0
                reqs.append((rec_name, flag_raw & _SENSE_MASK, ver or ''))

            provs: List[Tuple[str, str]] = []
            prov_names = hdr[rpm.RPMTAG_PROVIDENAME] or []
            prov_vers = hdr[rpm.RPMTAG_PROVIDEVERSION] or []
            for i, prov_name in enumerate(prov_names):
                ver = prov_vers[i] if i < len(prov_vers) else ''
                provs.append((prov_name, ver or ''))

            return reqs, provs

        def _collect_from_synthesis(pkg):
            """Extract versioned requires/recommends/provides from a synthesis dict.

            Parses ``name[op evr]`` capability strings stored by
            :mod:`urpm.core.synthesis` into the same tuple layout used
            by :func:`_collect_from_header`.  Missing ``pkg`` produces
            empty lists.
            """
            reqs: List[Tuple[str, int, str]] = []
            provs: List[Tuple[str, str]] = []
            if not pkg:
                return reqs, provs

            for r in pkg.get('requires', []):
                name, sense, evr = _parse_synthesis_cap(r)
                if name.startswith('rpmlib('):
                    continue
                reqs.append((name, sense, evr))
            for r in pkg.get('recommends', []):
                name, sense, evr = _parse_synthesis_cap(r)
                reqs.append((name, sense, evr))
            for p in pkg.get('provides', []):
                name, _sense, evr = _parse_synthesis_cap(p)
                provs.append((name, evr))

            return reqs, provs

        def _merge_pool_requires(pkg_name, reqs, provs):
            """Supplement synthesis requires/provides with pool data.

            Synthesis strips library requires (``lib*.so`` sonames),
            which breaks the post-state reverse-dep graph and causes
            spurious orphan-on-arrival classifications for newly
            installed libraries.  The solver pool has the full RPM
            metadata.

            We only ADD missing capabilities — synthesis is the trusted
            baseline.  If the pool returns a wrong-version solvable
            (multiple versions across media), extra requires/provides
            are harmless (they can only protect more packages from
            orphan status, which is the safe direction).

            Mutates ``reqs`` and ``provs`` in place.
            """
            pool = getattr(self, 'pool', None)
            if not pool:
                return
            try:
                import solv
                from urpm.core.resolution.pool import lookup_all_requires
                sel = pool.select(pkg_name, solv.Selection.SELECTION_NAME)
                # Iterate ALL non-System solvables — adding extra deps
                # from a wrong-version solvable is safe.
                existing_req = {r[0] for r in reqs}
                existing_prov = {p[0] for p in provs}
                for s in sel.solvables():
                    if s.repo.name == '@System':
                        continue
                    for dep in lookup_all_requires(s):
                        dep_str = str(dep)
                        if dep_str.startswith('rpmlib('):
                            continue
                        parts = dep_str.split(None, 2)
                        name = parts[0]
                        if name in existing_req:
                            continue
                        if len(parts) >= 3:
                            reqs.append((
                                name,
                                _SYNTHESIS_SENSE_MAP.get(parts[1], 0),
                                parts[2],
                            ))
                        else:
                            reqs.append((name, 0, ''))
                        existing_req.add(name)
                    for dep in s.lookup_deparray(solv.SOLVABLE_RECOMMENDS):
                        dep_str = str(dep)
                        parts = dep_str.split(None, 2)
                        name = parts[0]
                        if name in existing_req:
                            continue
                        if len(parts) >= 3:
                            reqs.append((
                                name,
                                _SYNTHESIS_SENSE_MAP.get(parts[1], 0),
                                parts[2],
                            ))
                        else:
                            reqs.append((name, 0, ''))
                        existing_req.add(name)
                    for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                        dep_str = str(dep)
                        parts = dep_str.split(None, 2)
                        name = parts[0]
                        if name in existing_prov:
                            continue
                        evr = parts[2] if len(parts) >= 3 else ''
                        provs.append((name, evr))
                        existing_prov.add(name)
            except Exception as exc:
                logger.debug(
                    "Pool merge for %s failed: %s",
                    pkg_name, exc,
                )

        def _pkg_self_evr(pkg):
            """Build the EVR string for a synthesis pkg dict (may be empty)."""
            if not pkg:
                return ''
            epoch = pkg.get('epoch', 0) or 0
            try:
                epoch_int = int(epoch)
            except (TypeError, ValueError):
                epoch_int = 0
            version = pkg.get('version', '') or ''
            release = pkg.get('release', '') or ''
            if epoch_int > 0:
                return f"{epoch_int}:{version}-{release}"
            return f"{version}-{release}"

        # Build S_pre (current rpmdb) and S_post (rpmdb + upgrades applied,
        # removals excluded, new installs added) in a single rpmdb pass.
        #
        # Each state entry stores requires as list[(name, sense, evr)] and
        # provides as list[(name, evr)] so the reverse-dep graph can be
        # filtered by version constraints.  The auto-generated self-provide
        # ``name = EVR`` is always present in well-formed RPM headers and
        # synthesis entries, but we re-append it here as a safety net for
        # sparsely populated fixtures (see ``test_orphans.py``).
        pre_state = {}
        post_state = {}

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            epoch = hdr[rpm.RPMTAG_EPOCH] or 0
            version = hdr[rpm.RPMTAG_VERSION] or ''
            release = hdr[rpm.RPMTAG_RELEASE] or ''
            self_evr = (
                f"{epoch}:{version}-{release}" if epoch
                else f"{version}-{release}"
            )

            rpm_reqs, rpm_provs = _collect_from_header(hdr)
            rpm_provs.append((name, self_evr))
            pre_state[name] = {'requires': rpm_reqs, 'provides': rpm_provs}

            if name in removed_names:
                continue

            if name in upgraded_names:
                new_pkg = self.db.get_package(name)
                new_reqs, new_provs = _collect_from_synthesis(new_pkg)
                new_provs.append((name, _pkg_self_evr(new_pkg)))
                # Supplement with pool data — synthesis strips library
                # requires (lib* deps), which breaks the reverse-dep
                # graph and causes spurious orphan-on-arrival for new
                # libs pulled in by upgrades.  We MERGE rather than
                # replace: synthesis has non-library requires reliably,
                # pool may pick a wrong version when multiple exist
                # across media.
                _merge_pool_requires(name, new_reqs, new_provs)
                post_state[name] = {
                    'requires': new_reqs, 'provides': new_provs,
                }
            else:
                post_state[name] = {
                    'requires': rpm_reqs, 'provides': rpm_provs,
                }

        for name in installed_names:
            if name in post_state:
                continue
            new_pkg = self.db.get_package(name)
            reqs, provs = _collect_from_synthesis(new_pkg)
            provs.append((name, _pkg_self_evr(new_pkg)))
            _merge_pool_requires(name, reqs, provs)
            post_state[name] = {'requires': reqs, 'provides': provs}

        def _build_reverse_deps(state):
            """Build a reverse-dependency graph with version-constraint filtering.

            For each package ``P``, return the set of packages that
            require ``P`` — but only when the require edge is
            version-satisfied.  A provider whose EVR fails to match a
            require's ``(sense, evr)`` constraint is not credited with
            an in-edge, which is what lets :func:`find_upgrade_orphans`
            detect a virtual rename such as ``tt1`` (``Provides: tt =
            1``) → ``tt2`` (``Provides: tt = 2``) against a
            ``Requires: tt >= 2``.
            """
            cap_providers: dict = {}
            for name, info in state.items():
                for cap_name, prov_evr in info['provides']:
                    cap_providers.setdefault(cap_name, []).append(
                        (name, prov_evr)
                    )

            rev = {name: set() for name in state}
            for name, info in state.items():
                for req_name, req_sense, req_evr in info['requires']:
                    for provider, prov_evr in cap_providers.get(req_name, ()):
                        if provider == name:
                            continue
                        if _provider_satisfies(prov_evr, req_sense, req_evr):
                            rev[provider].add(name)
            return rev

        pre_reverse = _build_reverse_deps(pre_state)
        post_reverse = _build_reverse_deps(post_state)

        # Newly installed packages are not yet in the persisted "unrequested"
        # set (mark_dependencies runs AFTER the transaction).  Treat them
        # as unrequested for this computation so that:
        #   1. they become valid orphan candidates when nothing ends up
        #      requiring them, and
        #   2. graph traversal walks THROUGH them instead of stopping
        #      (they must not count as "explicit" ancestors for their
        #      own dependencies).
        effective_unrequested = set(unrequested)
        effective_unrequested |= {n.lower() for n in installed_names}

        def _is_orphan(pkg_name, rev_deps):
            """True iff no explicit (non-unrequested) ancestor reaches pkg_name.

            Iterative DFS to avoid recursion limits on large dep graphs.
            """
            visited = {pkg_name}
            stack = [pkg_name]
            while stack:
                current = stack.pop()
                for parent in rev_deps.get(current, set()):
                    if parent.lower() not in effective_unrequested:
                        return False
                    if parent not in visited:
                        visited.add(parent)
                        stack.append(parent)
            return True

        # Literal translation of the formal spec:
        #   P ∈ S_post ∧ unrequested(P)
        #   ∧ orphan(P, S_post)
        #   ∧ ¬(P ∈ S_pre ∧ orphan(P, S_pre))
        orphan_candidates = set()
        for name in post_state:
            if name.lower() not in effective_unrequested:
                continue
            if not _is_orphan(name, post_reverse):
                continue
            if name in pre_state and _is_orphan(name, pre_reverse):
                continue
            orphan_candidates.add(name)

        # Safety net: synthesis data (used for post_state) strips library
        # requires (lib* deps).  A new dependency like lib64bind9.20.22
        # can appear orphaned because bind-utils's require for
        # libdns-9.20.22.so()(64bit) is absent from synthesis.
        # Cross-check orphan candidates against the solver pool's full
        # metadata: if any transaction package requires a capability
        # that a candidate provides, it is not truly orphaned.
        if getattr(self, 'pool', None) and orphan_candidates:
            try:
                import solv
                from urpm.core.resolution.pool import lookup_all_requires
                from ..resolver import get_solver_debug
                _debug = get_solver_debug()

                txn_names = {
                    a.name for a in all_actions
                    if a.action in (
                        TransactionType.INSTALL, TransactionType.UPGRADE,
                        TransactionType.DOWNGRADE, TransactionType.REINSTALL,
                    )
                }
                txn_req_caps: set = set()
                for tname in txn_names:
                    sel = self.pool.select(
                        tname, solv.Selection.SELECTION_NAME,
                    )
                    for s in sel.solvables():
                        for dep in lookup_all_requires(s):
                            txn_req_caps.add(str(dep).split()[0])

                # Widen the capability set with requires from packages that
                # remain installed unchanged (not in any txn action). Without
                # this, an installed package that still depends on a candidate
                # via a synthesis-stripped capability (e.g. `devel(libSM(64bit))`
                # required by `lib64xt-devel`) lets the candidate be removed
                # as orphan, and rpm's ts.check() later fails.
                # post_state already holds their requires (rpmdb-derived for
                # unchanged packages, pool-merged for upgraded/installed ones).
                for _pname, _pinfo in post_state.items():
                    if _pname in txn_names or _pname in removed_names:
                        continue
                    for _req_name, _sense, _req_evr in _pinfo.get(
                        'requires', []
                    ):
                        txn_req_caps.add(_req_name)

                # Protect candidates whose provides satisfy a txn require
                pool_protected: set = set()
                for oname in orphan_candidates:
                    sel = self.pool.select(
                        oname, solv.Selection.SELECTION_NAME,
                    )
                    for s in sel.solvables():
                        for dep in s.lookup_deparray(
                            solv.SOLVABLE_PROVIDES,
                        ):
                            if str(dep).split()[0] in txn_req_caps:
                                pool_protected.add(oname)
                                break
                        if oname in pool_protected:
                            break

                if pool_protected:
                    _debug.log(
                        f"Pool cross-check protected "
                        f"{len(pool_protected)} candidates"
                    )
                    orphan_candidates -= pool_protected
            except Exception as exc:
                logger.debug(
                    "Pool cross-check failed: %s", exc,
                )

        # Build the plan: rpmdb-side removes and cancelled new versions.
        plan = UpgradeOrphanPlan()

        # Category 1 — orphans currently in the rpmdb are erased with their
        # old (pre-transaction) EVR.  A package being upgraded-then-orphaned
        # lands here too: its old version appears in removes, and its new
        # version is cancelled in the second loop below.
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

            plan.removes.append(PackageAction(
                action=TransactionType.REMOVE,
                name=name,
                evr=evr,
                arch=arch,
                nevra=f"{name}-{evr}.{arch}",
                size=size,
            ))

        # Category 2 — orphans whose new version would be installed by the
        # current transaction must have that install cancelled.  Previously
        # this path emitted a REMOVE action for the newly-installed name,
        # which caused rpm's transaction engine to silently no-op the
        # simultaneous install+remove (cf. ``test_auto_select_f``).  We now
        # return the names separately so the caller can drop the action
        # and skip the downloaded RPM without asking rpm to install+remove
        # the same NVRA in one shot.
        new_version_actions = {
            TransactionType.INSTALL,
            TransactionType.UPGRADE,
            TransactionType.DOWNGRADE,
            TransactionType.REINSTALL,
        }
        for action in all_actions:
            if action.action not in new_version_actions:
                continue
            if action.name in orphan_candidates:
                plan.cancelled_new_versions.add(action.name.lower())

        return plan

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
        # Exclude builddeps — managed separately via autoremove --buildrequires
        unrequested = self._get_unrequested_packages()
        builddeps = self._get_builddep_packages()
        unrequested -= set(builddeps.keys())

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
