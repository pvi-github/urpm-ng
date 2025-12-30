"""
Dependency resolver using libsolv

Uses the SAT-based libsolv library for fast, correct dependency resolution.
"""

import re
import solv
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

try:
    import rpm
    HAS_RPM = True
except ImportError:
    HAS_RPM = False

from .database import PackageDatabase
from .config import get_media_local_path, get_base_dir
from .compression import decompress_stream


# Debug flag for resolver - set to True to enable debug output
DEBUG_RESOLVER = False

# Regex to parse capability strings like "name[op version]"
CAP_REGEX = re.compile(r'^([^\[]+)(?:\[([<>=!]+)\s*(.+)\])?$')

# Map string operators to libsolv flags
OP_FLAGS = {
    '>=': solv.REL_GT | solv.REL_EQ,
    '<=': solv.REL_LT | solv.REL_EQ,
    '==': solv.REL_EQ,
    '>': solv.REL_GT,
    '<': solv.REL_LT,
}


def parse_capability(pool: solv.Pool, cap_str: str) -> solv.Dep:
    """Parse a capability string into a libsolv Dep.

    Handles formats like:
    - "name" -> simple dependency
    - "name[>= 1.0]" -> versioned dependency
    - "name(x86-64)[== 1.0]" -> arch-specific versioned
    - "name[*]" -> scriptlet dependency (treat as name only)
    - "name[*][>= 1.0]" -> scriptlet with version constraint
    - "(pkgA or pkgB)" -> rich/boolean dependency (RPM 4.13+)
    - "(pkgA if pkgB)" -> conditional dependency
    """
    # Handle rich/boolean dependencies (start with parenthesis)
    if cap_str.startswith('('):
        try:
            return pool.parserpmrichdep(cap_str)
        except Exception:
            # Fallback to simple dep on parse error
            return pool.Dep(cap_str)

    # Strip [*] scriptlet marker if present (can appear anywhere)
    cap_str = cap_str.replace('[*]', '')

    # Handle simple case (no brackets left)
    if '[' not in cap_str:
        return pool.Dep(cap_str)

    match = CAP_REGEX.match(cap_str)
    if not match:
        return pool.Dep(cap_str)

    name, op, version = match.groups()

    if op is None:
        # Simple dependency
        return pool.Dep(name)

    # Versioned dependency
    flags = OP_FLAGS.get(op)
    if flags is None:
        return pool.Dep(name)

    return pool.Dep(name).Rel(flags, pool.Dep(version))


class TransactionType(Enum):
    """Type of package transaction."""
    INSTALL = "install"
    REMOVE = "remove"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    REINSTALL = "reinstall"


class InstallReason(Enum):
    """Why a package is being installed."""
    EXPLICIT = "explicit"      # User requested it
    DEPENDENCY = "dependency"  # Required by another package
    RECOMMENDED = "recommended"  # Recommended by another package
    SUGGESTED = "suggested"    # Suggested by another package


@dataclass
class PackageAction:
    """A single package action in a transaction."""
    action: TransactionType
    name: str
    evr: str
    arch: str
    nevra: str
    size: int = 0
    media_name: str = ""
    reason: InstallReason = InstallReason.DEPENDENCY
    from_evr: str = ""  # Previous version for upgrades


@dataclass
class Alternative:
    """An alternative choice for a dependency."""
    capability: str  # The capability being satisfied (e.g., "task-sound")
    required_by: str  # Package that requires this capability
    providers: List[str]  # Package names that can satisfy it


@dataclass
class Resolution:
    """Result of dependency resolution."""
    success: bool
    actions: List[PackageAction]
    problems: List[str]
    install_size: int = 0
    remove_size: int = 0
    alternatives: List[Alternative] = None  # Choices that need user input

    def __post_init__(self):
        if self.alternatives is None:
            self.alternatives = []


class Resolver:
    """Dependency resolver using libsolv."""

    def __init__(self, db: PackageDatabase, arch: str = "x86_64", root: str = "/",
                 install_recommends: bool = True):
        """Initialize resolver.

        Args:
            db: Package database
            arch: System architecture
            root: RPM database root (default: /)
            install_recommends: Install recommended packages (default: True)
        """
        self.db = db
        self.arch = arch
        self.root = root
        self.install_recommends = install_recommends
        self.pool = None
        self._solvable_to_pkg = {}  # Map solvable id -> pkg dict
        self._installed_count = 0  # Number of installed packages loaded

    def _create_pool(self) -> solv.Pool:
        """Create and populate libsolv Pool from database.

        Uses native libsolv methods for optimal performance:
        - add_rpmdb() for installed packages
        - add_mdk() for loading synthesis files directly
        """
        import tempfile

        pool = solv.Pool()
        pool.setdisttype(solv.Pool.DISTTYPE_RPM)
        pool.setarch(self.arch)

        # Load installed packages from rpmdb using native method
        installed = pool.add_repo("@System")
        installed.appdata = {"type": "installed"}
        pool.installed = installed

        if HAS_RPM:
            installed.add_rpmdb()
            self._installed_count = installed.nsolvables
        else:
            self._installed_count = 0

        # Load available packages from synthesis files (much faster than SQLite)
        base_dir = get_base_dir()
        media_list = self.db.list_media()

        for media in media_list:
            if not media['enabled']:
                continue

            repo = pool.add_repo(media['name'])
            repo.appdata = {"type": "available", "media": media}

            # Try to load from synthesis file first
            media_path = get_media_local_path(media, base_dir)
            synthesis_path = media_path / "media_info" / "synthesis.hdlist.cz"

            if synthesis_path.exists():
                try:
                    # Decompress and load with add_mdk
                    stream = decompress_stream(synthesis_path)
                    data = stream.read()

                    with tempfile.NamedTemporaryFile(suffix='.hdlist', delete=False) as tmp:
                        tmp.write(data)
                        tmp_path = tmp.name

                    f = solv.xfopen(tmp_path)
                    repo.add_mdk(f)
                    f.close()
                    Path(tmp_path).unlink()

                    # Populate _solvable_to_pkg mapping for the loaded packages
                    for s in repo.solvables:
                        self._solvable_to_pkg[s.id] = {
                            'name': s.name,
                            'evr': s.evr,
                            'arch': s.arch,
                            'nevra': f"{s.name}-{s.evr}.{s.arch}",
                            'summary': s.lookup_str(solv.SOLVABLE_SUMMARY) or "",
                            'size': s.lookup_num(solv.SOLVABLE_INSTALLSIZE) or 0,
                            'media_name': repo.name,
                        }
                except Exception as e:
                    if DEBUG_RESOLVER:
                        print(f"[RESOLVER] Failed to load synthesis for {media['name']}: {e}")
                    # Fallback to SQLite loading
                    self._load_repo_packages(pool, repo, media['id'])
            else:
                # No synthesis file, fallback to SQLite
                self._load_repo_packages(pool, repo, media['id'])

        pool.createwhatprovides()
        return pool

    def _load_repo_packages(self, pool: solv.Pool, repo: solv.Repo, media_id: int):
        """Load packages from database into libsolv repo.

        Uses bulk loading for performance.
        """
        # Load all packages first
        cursor = self.db.conn.execute("""
            SELECT id, name, epoch, version, release, arch, nevra, summary, size
            FROM packages WHERE media_id = ?
        """, (media_id,))

        pkg_id_to_solvable = {}

        for row in cursor:
            pkg_id, name, epoch, version, release, arch, nevra, summary, size = row

            # Skip src packages
            if arch in ('src', 'nosrc'):
                continue

            s = repo.add_solvable()
            s.name = name
            if epoch and epoch > 0:
                s.evr = f"{epoch}:{version}-{release}"
            else:
                s.evr = f"{version}-{release}"
            s.arch = arch

            # Versioned self-provide (essential for version comparison in conflicts)
            s.add_deparray(solv.SOLVABLE_PROVIDES,
                pool.Dep(name).Rel(solv.REL_EQ, pool.Dep(s.evr)))

            pkg_id_to_solvable[pkg_id] = s

            self._solvable_to_pkg[s.id] = {
                'id': pkg_id,
                'name': name,
                'evr': s.evr,
                'arch': arch,
                'nevra': nevra,
                'summary': summary or "",
                'size': size or 0,
                'media_name': repo.name,
            }

        # Bulk load provides
        cursor = self.db.conn.execute("""
            SELECT p.pkg_id, p.capability
            FROM provides p
            JOIN packages pkg ON p.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_PROVIDES, parse_capability(pool, cap))

        # Bulk load requires
        cursor = self.db.conn.execute("""
            SELECT r.pkg_id, r.capability
            FROM requires r
            JOIN packages pkg ON r.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            # Skip rpmlib() deps - handled by rpm itself
            # Skip file deps - not in synthesis, assume installed system provides them
            if cap.startswith("rpmlib(") or cap.startswith("/"):
                continue
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_REQUIRES, parse_capability(pool, cap))

        # Bulk load conflicts
        cursor = self.db.conn.execute("""
            SELECT c.pkg_id, c.capability
            FROM conflicts c
            JOIN packages pkg ON c.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_CONFLICTS, parse_capability(pool, cap))

        # Bulk load obsoletes
        cursor = self.db.conn.execute("""
            SELECT o.pkg_id, o.capability
            FROM obsoletes o
            JOIN packages pkg ON o.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_OBSOLETES, parse_capability(pool, cap))

        # Bulk load weak dependencies (RPM 4.12+)
        for table, solv_type in [
            ('recommends', solv.SOLVABLE_RECOMMENDS),
            ('suggests', solv.SOLVABLE_SUGGESTS),
            ('supplements', solv.SOLVABLE_SUPPLEMENTS),
            ('enhances', solv.SOLVABLE_ENHANCES),
        ]:
            cursor = self.db.conn.execute(f"""
                SELECT d.pkg_id, d.capability
                FROM {table} d
                JOIN packages pkg ON d.pkg_id = pkg.id
                WHERE pkg.media_id = ?
            """, (media_id,))
            for pkg_id, cap in cursor:
                s = pkg_id_to_solvable.get(pkg_id)
                if s:
                    s.add_deparray(solv_type, parse_capability(pool, cap))

    def _load_rpmdb(self, pool: solv.Pool, repo: solv.Repo) -> int:
        """Load installed packages from rpmdb into libsolv repo.

        Args:
            pool: libsolv Pool
            repo: libsolv Repo to populate (@System)

        Returns:
            Number of packages loaded
        """
        if not HAS_RPM:
            return 0

        count = 0
        ts = rpm.TransactionSet(self.root)

        # Iterate over all installed packages
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            epoch = hdr[rpm.RPMTAG_EPOCH] or 0
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or "noarch"
            size = hdr[rpm.RPMTAG_SIZE] or 0

            # Skip gpg-pubkey pseudo-packages
            if name == "gpg-pubkey":
                continue

            s = repo.add_solvable()
            s.name = name
            if epoch and epoch > 0:
                s.evr = f"{epoch}:{version}-{release}"
            else:
                s.evr = f"{version}-{release}"
            s.arch = arch

            # Versioned self-provide
            s.add_deparray(solv.SOLVABLE_PROVIDES,
                pool.Dep(name).Rel(solv.REL_EQ, pool.Dep(s.evr)))

            # Add provides
            provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
            provide_vers = hdr[rpm.RPMTAG_PROVIDEVERSION] or []
            provide_flags = hdr[rpm.RPMTAG_PROVIDEFLAGS] or []

            for i, prov in enumerate(provides):
                if i < len(provide_vers) and provide_vers[i]:
                    flags = provide_flags[i] if i < len(provide_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(prov).Rel(solv_flags, pool.Dep(provide_vers[i]))
                    else:
                        dep = pool.Dep(prov)
                else:
                    dep = pool.Dep(prov)
                s.add_deparray(solv.SOLVABLE_PROVIDES, dep)

            # Add requires
            requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
            require_vers = hdr[rpm.RPMTAG_REQUIREVERSION] or []
            require_flags = hdr[rpm.RPMTAG_REQUIREFLAGS] or []

            for i, req in enumerate(requires):
                # Skip rpmlib() and file deps
                if req.startswith("rpmlib(") or req.startswith("/"):
                    continue

                if i < len(require_vers) and require_vers[i]:
                    flags = require_flags[i] if i < len(require_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(req).Rel(solv_flags, pool.Dep(require_vers[i]))
                    else:
                        dep = pool.Dep(req)
                else:
                    dep = pool.Dep(req)
                s.add_deparray(solv.SOLVABLE_REQUIRES, dep)

            # Add conflicts
            conflicts = hdr[rpm.RPMTAG_CONFLICTNAME] or []
            conflict_vers = hdr[rpm.RPMTAG_CONFLICTVERSION] or []
            conflict_flags = hdr[rpm.RPMTAG_CONFLICTFLAGS] or []

            for i, conf in enumerate(conflicts):
                if i < len(conflict_vers) and conflict_vers[i]:
                    flags = conflict_flags[i] if i < len(conflict_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(conf).Rel(solv_flags, pool.Dep(conflict_vers[i]))
                    else:
                        dep = pool.Dep(conf)
                else:
                    dep = pool.Dep(conf)
                s.add_deparray(solv.SOLVABLE_CONFLICTS, dep)

            # Add obsoletes
            obsoletes = hdr[rpm.RPMTAG_OBSOLETENAME] or []
            obsolete_vers = hdr[rpm.RPMTAG_OBSOLETEVERSION] or []
            obsolete_flags = hdr[rpm.RPMTAG_OBSOLETEFLAGS] or []

            for i, obs in enumerate(obsoletes):
                if i < len(obsolete_vers) and obsolete_vers[i]:
                    flags = obsolete_flags[i] if i < len(obsolete_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(obs).Rel(solv_flags, pool.Dep(obsolete_vers[i]))
                    else:
                        dep = pool.Dep(obs)
                else:
                    dep = pool.Dep(obs)
                s.add_deparray(solv.SOLVABLE_OBSOLETES, dep)

            # Add weak dependencies (RPM 4.12+)
            weak_deps = [
                (rpm.RPMTAG_RECOMMENDNAME, rpm.RPMTAG_RECOMMENDVERSION,
                 rpm.RPMTAG_RECOMMENDFLAGS, solv.SOLVABLE_RECOMMENDS),
                (rpm.RPMTAG_SUGGESTNAME, rpm.RPMTAG_SUGGESTVERSION,
                 rpm.RPMTAG_SUGGESTFLAGS, solv.SOLVABLE_SUGGESTS),
                (rpm.RPMTAG_SUPPLEMENTNAME, rpm.RPMTAG_SUPPLEMENTVERSION,
                 rpm.RPMTAG_SUPPLEMENTFLAGS, solv.SOLVABLE_SUPPLEMENTS),
                (rpm.RPMTAG_ENHANCENAME, rpm.RPMTAG_ENHANCEVERSION,
                 rpm.RPMTAG_ENHANCEFLAGS, solv.SOLVABLE_ENHANCES),
            ]

            for name_tag, ver_tag, flag_tag, solv_type in weak_deps:
                names = hdr[name_tag] or []
                versions = hdr[ver_tag] or []
                flags_list = hdr[flag_tag] or []

                for i, dep_name in enumerate(names):
                    if i < len(versions) and versions[i]:
                        flags = flags_list[i] if i < len(flags_list) else 0
                        solv_flags = self._rpm_flags_to_solv(flags)
                        if solv_flags:
                            dep = pool.Dep(dep_name).Rel(solv_flags, pool.Dep(versions[i]))
                        else:
                            dep = pool.Dep(dep_name)
                    else:
                        dep = pool.Dep(dep_name)
                    s.add_deparray(solv_type, dep)

            # Store mapping
            self._solvable_to_pkg[s.id] = {
                'name': name,
                'evr': s.evr,
                'arch': arch,
                'nevra': f"{name}-{s.evr}.{arch}",
                'size': size,
                'media_name': '@System',
            }
            count += 1

        return count

    def _rpm_flags_to_solv(self, flags: int) -> int:
        """Convert RPM dependency flags to libsolv flags."""
        solv_flags = 0
        if flags & rpm.RPMSENSE_LESS:
            solv_flags |= solv.REL_LT
        if flags & rpm.RPMSENSE_GREATER:
            solv_flags |= solv.REL_GT
        if flags & rpm.RPMSENSE_EQUAL:
            solv_flags |= solv.REL_EQ
        return solv_flags

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

    def resolve_install(self, package_names: List[str],
                        choices: Dict[str, str] = None,
                        favored_packages: set = None,
                        explicit_disfavor: set = None,
                        preference_patterns: list = None) -> Resolution:
        """Resolve packages to install.

        Args:
            package_names: List of package names to install
            choices: Optional dict mapping capability -> chosen package name
                     for resolving alternatives (e.g., {"task-sound": "task-pulseaudio"})
            favored_packages: Optional set of package names to favor (from preferences)
            explicit_disfavor: Optional set of package names to explicitly disfavor
                     (from negative preferences like -apache-mod_php)
            preference_patterns: Optional list of name patterns from user preferences
                     (packages matching ALL patterns get INSTALL jobs when competing)

        Returns:
            Resolution with success status and package actions.
            If alternatives need user input, success=False and alternatives is populated.
        """
        if choices is None:
            choices = {}
        if favored_packages is None:
            favored_packages = set()
        if explicit_disfavor is None:
            explicit_disfavor = set()
        if preference_patterns is None:
            preference_patterns = []

        # Reuse existing pool if available, otherwise create new one
        if self.pool is None:
            self._solvable_to_pkg = {}
            self.pool = self._create_pool()

        jobs = []
        not_found = []

        # Process choices FIRST to identify alternatives that shouldn't be favored
        favored = set()
        disfavored = set()
        chosen_packages = set(choices.values())

        # Add explicit disfavor packages (from negative preferences like -apache-mod_php)
        for pkg_name in explicit_disfavor:
            disfavored.add(pkg_name)
        if DEBUG_RESOLVER and explicit_disfavor:
            print(f"DEBUG RESOLVER: explicit_disfavor = {explicit_disfavor}")

        # Convert favored_packages to lowercase for comparison
        favored_packages_lower = {p.lower() for p in favored_packages}

        # For each choice, DISFAVOR alternatives (packages providing same capability)
        # BUT don't disfavor packages that are explicitly in favored_packages
        for cap, pkg_name in choices.items():
            chosen_packages.add(pkg_name)
            cap_dep = self.pool.Dep(cap)
            if cap_dep:
                for provider in self.pool.whatprovides(cap_dep):
                    if provider.repo and provider.repo.name != '@System':
                        if provider.name != pkg_name:
                            # Don't disfavor if it's in favored_packages
                            if provider.name.lower() not in favored_packages_lower:
                                disfavored.add(provider.name)

        # Collect capabilities provided by explicitly disfavored packages
        # We'll use this to know which favored packages should get INSTALL jobs
        disfavored_caps = set()
        for pkg_name in explicit_disfavor:
            sel = self.pool.select(pkg_name, solv.Selection.SELECTION_NAME)
            for s in sel.solvables():
                if s.repo and s.repo.name != '@System':
                    for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                        cap = str(dep).split()[0]
                        if not cap.startswith(('rpmlib(', '/', 'lib', 'pkgconfig(')):
                            disfavored_caps.add(cap)

        def pkg_matches_preferences(solvable, patterns: list) -> bool:
            """Check if package REQUIRES or PROVIDES all preference capabilities.

            A package matches if for each pattern, it either:
            - REQUIRES a capability matching the pattern, OR
            - PROVIDES a capability matching the pattern
            """
            if not patterns:
                return False

            # Get package's requires and provides
            pkg_requires = set()
            pkg_provides = set()
            for dep in solvable.lookup_deparray(solv.SOLVABLE_REQUIRES):
                pkg_requires.add(str(dep).split()[0].lower())
            for dep in solvable.lookup_deparray(solv.SOLVABLE_PROVIDES):
                pkg_provides.add(str(dep).split()[0].lower())

            # Check each pattern - package must require OR provide it
            for pattern in patterns:
                pattern_lower = pattern.lower()
                if pattern_lower not in pkg_requires and pattern_lower not in pkg_provides:
                    return False
            return True

        # Add favored packages
        for pkg_name in favored_packages:
            if pkg_name in favored:
                continue
            favored.add(pkg_name)
            sel = self.pool.select(pkg_name, solv.Selection.SELECTION_NAME)
            if not sel.isempty():
                jobs += sel.jobs(solv.Job.SOLVER_FAVOR)

                # Only add INSTALL job for packages that:
                # 1. REQUIRE or PROVIDE all preference capabilities
                # 2. Share capabilities with disfavored packages
                for s in sel.solvables():
                    if s.repo and s.repo.name != '@System':
                        if preference_patterns and pkg_matches_preferences(s, preference_patterns):
                            pkg_caps = set()
                            for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                                cap = str(dep).split()[0]
                                if not cap.startswith(('rpmlib(', '/', 'lib', 'pkgconfig(')):
                                    pkg_caps.add(cap)
                            if pkg_caps & disfavored_caps:
                                jobs += sel.jobs(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_WEAK)
                                if DEBUG_RESOLVER:
                                    print(f"DEBUG RESOLVER: FAVOR+INSTALL for {pkg_name} (requires/provides all patterns)")
                        break

        if DEBUG_RESOLVER:
            apache_mod_in_disfavored = [p for p in disfavored if 'apache-mod_php' in p]
            if apache_mod_in_disfavored:
                print(f"DEBUG RESOLVER: apache-mod packages in disfavored: {apache_mod_in_disfavored}")

        # Apply DISFAVOR jobs for all disfavored packages
        for pkg_name in disfavored:
            dis_sel = self.pool.select(pkg_name, solv.Selection.SELECTION_NAME)
            if not dis_sel.isempty():
                jobs += dis_sel.jobs(solv.Job.SOLVER_DISFAVOR)

        # Add explicit choices with INSTALL job
        for cap, pkg_name in choices.items():
            sel = self.pool.select(pkg_name, solv.Selection.SELECTION_NAME)
            if not sel.isempty():
                jobs += sel.jobs(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_WEAK)
                if pkg_name not in favored:
                    jobs += sel.jobs(solv.Job.SOLVER_FAVOR)
                    favored.add(pkg_name)

        for name in package_names:
            # Use multiple selection flags for flexibility
            flags = (solv.Selection.SELECTION_NAME |
                    solv.Selection.SELECTION_CANON |
                    solv.Selection.SELECTION_DOTARCH |
                    solv.Selection.SELECTION_REL)
            sel = self.pool.select(name, flags)

            if sel.isempty():
                # Try glob match
                sel = self.pool.select(name, solv.Selection.SELECTION_GLOB |
                                       solv.Selection.SELECTION_CANON)
            if sel.isempty():
                # Try provides match
                sel = self.pool.select(name, solv.Selection.SELECTION_PROVIDES)

                if not sel.isempty() and name not in choices:
                    # Check if multiple different packages provide this capability
                    provider_names = set()
                    for s in sel.solvables():
                        if s.repo and s.repo != self.pool.installed:
                            provider_names.add(s.name)

                    if len(provider_names) > 1:
                        # Multiple providers - need user choice before resolving
                        # Sort by version (descending) to show newest first
                        sorted_providers = self._prioritize_providers(
                            list(provider_names), max_count=10
                        )
                        return Resolution(
                            success=False,
                            actions=[],
                            problems=[],
                            alternatives=[Alternative(
                                capability=name,
                                required_by="",  # User's request, not a dependency
                                providers=sorted_providers
                            )]
                        )

            if sel.isempty():
                not_found.append(name)
            else:
                jobs += sel.jobs(solv.Job.SOLVER_INSTALL)

        if not_found:
            return Resolution(
                success=False,
                actions=[],
                problems=[f"Package not found: {n}" for n in not_found]
            )

        # Solve
        solver = self.pool.Solver()
        # Prefer packages compatible with already installed packages
        # This helps select php8.5-opcache when php8.5-* is already installed
        solver.set_flag(solv.Solver.SOLVER_FLAG_FOCUS_INSTALLED, 1)
        # Handle weak dependencies (Recommends/Suggests)
        if not self.install_recommends:
            solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 1)

        problems = solver.solve(jobs)

        if problems:
            problem_strs = []
            for problem in problems:
                problem_strs.append(str(problem))
            return Resolution(
                success=False,
                actions=[],
                problems=problem_strs
            )

        # Get transaction and order it for correct install sequence
        trans = solver.transaction()
        trans.order()

        # Build set of explicitly requested package names (lowercase)
        explicit_names = set(n.lower() for n in package_names)

        actions = []
        install_size = 0

        for s in trans.steps():
            pkg_info = self._solvable_to_pkg.get(s.id, {})
            step_type = trans.steptype(s, solv.Transaction.SOLVER_TRANSACTION_SHOW_ACTIVE)

            if step_type == solv.Transaction.SOLVER_TRANSACTION_IGNORE:
                continue
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_INSTALL:
                action = TransactionType.INSTALL
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_ERASE:
                action = TransactionType.REMOVE
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_UPGRADE:
                action = TransactionType.UPGRADE
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_DOWNGRADE:
                action = TransactionType.DOWNGRADE
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_REINSTALL:
                action = TransactionType.REINSTALL
            else:
                continue

            # Determine install reason
            reason = InstallReason.DEPENDENCY
            if s.name.lower() in explicit_names:
                reason = InstallReason.EXPLICIT
            elif action == TransactionType.INSTALL:
                # Check solver's decision reason
                decision_reason, rule = solver.describe_decision(s)
                if decision_reason == solv.Solver.SOLVER_REASON_RECOMMENDED:
                    reason = InstallReason.RECOMMENDED
                elif decision_reason == solv.Solver.SOLVER_REASON_WEAKDEP:
                    reason = InstallReason.RECOMMENDED
                elif decision_reason == solv.Solver.SOLVER_REASON_RESOLVE_JOB:
                    reason = InstallReason.EXPLICIT

            size = pkg_info.get('size', 0)
            if action in (TransactionType.INSTALL, TransactionType.UPGRADE):
                install_size += size

            actions.append(PackageAction(
                action=action,
                name=s.name,
                evr=s.evr,
                arch=s.arch,
                nevra=f"{s.name}-{s.evr}.{s.arch}",
                size=size,
                media_name=pkg_info.get('media_name', ''),
                reason=reason,
            ))

        # Detect alternatives: packages that could satisfy the same dependency
        # Filter out alternatives where user already made a choice
        all_alternatives = self._find_alternatives(solver, trans, actions)
        alternatives = [alt for alt in all_alternatives if alt.capability not in choices]

        # If there are unresolved alternatives, return them for user choice
        if alternatives:
            return Resolution(
                success=False,
                actions=actions,
                problems=[],
                install_size=install_size,
                alternatives=alternatives
            )

        return Resolution(
            success=True,
            actions=actions,
            problems=[],
            install_size=install_size
        )

    def build_dependency_graph(self, resolution: Resolution,
                               requested_names: List[str]) -> Dict[str, List[str]]:
        """Build dependency graph from a resolution.

        Only shows relationships between packages that are actually in the resolution.
        This ensures we don't show fake dependencies.

        Args:
            resolution: The Resolution from resolve_install()
            requested_names: List of package names explicitly requested

        Returns:
            Dict mapping package name to list of package names it depends on
            (only packages that are in the resolution)
        """
        if self.pool is None:
            self.pool = self._create_pool()

        # Get set of resolved package names
        resolved_names = {a.name for a in resolution.actions
                        if a.action in (TransactionType.INSTALL, TransactionType.UPGRADE)}

        # Build map of capability -> resolved provider
        cap_to_provider = {}
        for name in resolved_names:
            sel = self.pool.select(name, solv.Selection.SELECTION_NAME)
            for s in sel.solvables():
                if s.repo and s.repo.name != '@System':
                    # Get all provides of this package
                    for dep in s.lookup_deparray(solv.SOLVABLE_PROVIDES):
                        cap_str = str(dep).split()[0]  # Remove version constraints
                        # First provider wins (matches solver behavior)
                        if cap_str not in cap_to_provider:
                            cap_to_provider[cap_str] = name
                    break

        # Build dependency graph
        graph = {name: [] for name in resolved_names}
        requested_lower = {n.lower() for n in requested_names}

        for name in resolved_names:
            sel = self.pool.select(name, solv.Selection.SELECTION_NAME)
            for s in sel.solvables():
                if s.repo and s.repo.name != '@System':
                    # Get requires
                    for dep in s.lookup_deparray(solv.SOLVABLE_REQUIRES):
                        dep_str = str(dep)
                        cap = dep_str.split()[0]  # Remove version constraints

                        # Skip rpmlib and file deps
                        if cap.startswith('rpmlib(') or cap.startswith('/'):
                            continue

                        # Find which resolved package provides this
                        provider = cap_to_provider.get(cap)
                        if provider and provider != name and provider in resolved_names:
                            if provider not in graph[name]:
                                graph[name].append(provider)
                    break

        return graph

    def _find_alternatives(self, solver, trans, actions: List[PackageAction],
                           max_providers: int = 10) -> List[Alternative]:
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

    def _prioritize_providers(self, providers: List[str], max_count: int) -> List[str]:
        """Prioritize providers based on locale and common usage.

        Args:
            providers: List of provider package names
            max_count: Maximum number to return

        Returns:
            Sorted and limited list of providers
        """
        import locale
        import os

        # Get system locale
        try:
            lang = os.environ.get('LANG', 'en_US.UTF-8').split('_')[0].lower()
        except Exception:
            lang = 'en'

        # Common/popular language codes to prioritize
        common_langs = ['en', 'fr', 'de', 'es', 'it', 'pt', 'ru', 'zh', 'ja', 'ko']

        def sort_key(name: str) -> tuple:
            name_lower = name.lower()

            # Check if it matches system locale (highest priority)
            if f'-{lang}' in name_lower or name_lower.endswith(f'_{lang}'):
                return (0, name)

            # Check if it's a common language
            for i, common in enumerate(common_langs):
                if f'-{common}' in name_lower or name_lower.endswith(f'_{common}'):
                    return (1, i, name)

            # Everything else alphabetically
            return (2, 0, name)

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
                                resolved_packages: List[str] = None) -> List[PackageAction]:
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
            List of PackageAction for available suggested packages
        """
        if not self.pool:
            return []

        if choices is None:
            choices = {}

        suggests = []
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
                    # Find packages that satisfy this suggest
                    providers = self.pool.whatprovides(dep)
                    if DEBUG_RESOLVER and pkg_name == 'phpmyadmin':
                        prov_names = [p.name for p in providers if p.repo and p.repo.name != '@System']
                        if prov_names:
                            print(f"  {dep} -> providers: {prov_names[:5]}")
                    for provider in providers:
                        # Skip if already installed or already in our list
                        if provider.name.lower() in installed_names:
                            if DEBUG_RESOLVER and pkg_name == 'phpmyadmin':
                                print(f"    SKIP {provider.name}: already installed")
                            continue
                        if provider.name.lower() in seen:
                            if DEBUG_RESOLVER and pkg_name == 'phpmyadmin':
                                print(f"    SKIP {provider.name}: already seen")
                            continue
                        # Skip if it's a src package
                        if provider.arch in ('src', 'nosrc'):
                            continue
                        # Skip suggests that require rejected packages
                        # (packages that conflict with user's choices)
                        debug_this = (pkg_name == 'phpmyadmin' and provider.name in ('php8.4-bz2', 'php8.4-zip', 'php8.5-bz2', 'php8.5-zip'))
                        if self._requires_rejected(provider, rejected_packages, debug=debug_this):
                            if pkg_name == 'phpmyadmin':
                                print(f"    SKIP {provider.name}: requires rejected package")
                            continue

                        seen.add(provider.name.lower())
                        pkg_info = self._solvable_to_pkg.get(provider.id, {})

                        suggests.append(PackageAction(
                            action=TransactionType.INSTALL,
                            name=provider.name,
                            evr=provider.evr,
                            arch=provider.arch,
                            nevra=f"{provider.name}-{provider.evr}.{provider.arch}",
                            size=pkg_info.get('size', 0),
                            media_name=pkg_info.get('media_name', ''),
                            reason=InstallReason.SUGGESTED,
                        ))

        return suggests

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

    def resolve_upgrade(self, package_names: List[str] = None) -> Resolution:
        """Resolve packages to upgrade.

        Args:
            package_names: List of package names to upgrade (None = all)

        Returns:
            Resolution with success status and package actions
        """
        self._solvable_to_pkg = {}
        self.pool = self._create_pool()

        jobs = []

        if package_names:
            # Upgrade specific packages
            not_found = []
            not_installed = []
            for name in package_names:
                # First check if it's installed
                inst_flags = (solv.Selection.SELECTION_NAME |
                             solv.Selection.SELECTION_CANON |
                             solv.Selection.SELECTION_DOTARCH |
                             solv.Selection.SELECTION_INSTALLED_ONLY)
                inst_sel = self.pool.select(name, inst_flags)

                if inst_sel.isempty():
                    # Try glob
                    inst_sel = self.pool.select(name, solv.Selection.SELECTION_GLOB |
                                                solv.Selection.SELECTION_INSTALLED_ONLY)

                if inst_sel.isempty():
                    not_installed.append(name)
                    continue

                # Now select from ALL repos (not just installed) for the update
                flags = (solv.Selection.SELECTION_NAME |
                        solv.Selection.SELECTION_CANON |
                        solv.Selection.SELECTION_DOTARCH)
                sel = self.pool.select(name, flags)

                if sel.isempty():
                    sel = self.pool.select(name, solv.Selection.SELECTION_GLOB)

                if sel.isempty():
                    not_found.append(name)
                else:
                    jobs += sel.jobs(solv.Job.SOLVER_UPDATE)

            if not_installed:
                return Resolution(
                    success=False,
                    actions=[],
                    problems=[f"Package not installed: {n}" for n in not_installed]
                )

            if not_found:
                return Resolution(
                    success=False,
                    actions=[],
                    problems=[f"Package not found: {n}" for n in not_found]
                )
        else:
            # Upgrade all installed packages using DISTUPGRADE
            # DISTUPGRADE handles broken dependencies and removes orphaned packages
            jobs.append(self.pool.Job(solv.Job.SOLVER_DISTUPGRADE | solv.Job.SOLVER_SOLVABLE_ALL, 0))

        # Solve
        solver = self.pool.Solver()
        # Allow vendor changes and arch changes for upgrades
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_VENDORCHANGE, 1)
        # Prefer packages compatible with already installed packages
        solver.set_flag(solv.Solver.SOLVER_FLAG_FOCUS_INSTALLED, 1)
        # Allow removing packages with broken dependencies
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        # Handle weak dependencies (Recommends/Suggests)
        if not self.install_recommends:
            solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 1)

        problems = solver.solve(jobs)

        if problems:
            return Resolution(
                success=False,
                actions=[],
                problems=[str(p) for p in problems]
            )

        # Get transaction
        trans = solver.transaction()
        if trans.isempty():
            return Resolution(
                success=True,
                actions=[],
                problems=[],
            )

        trans.order()

        actions = []
        install_size = 0
        remove_size = 0

        for s in trans.steps():
            pkg_info = self._solvable_to_pkg.get(s.id, {})
            # Use SHOW_ACTIVE to get the "active" side of the transaction
            # (new packages for upgrades, not the old ones being removed)
            step_type = trans.steptype(s, solv.Transaction.SOLVER_TRANSACTION_SHOW_ACTIVE)

            if step_type == solv.Transaction.SOLVER_TRANSACTION_IGNORE:
                continue
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_INSTALL:
                action = TransactionType.INSTALL
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_ERASE:
                action = TransactionType.REMOVE
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_UPGRADE:
                action = TransactionType.UPGRADE
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_DOWNGRADE:
                action = TransactionType.DOWNGRADE
            elif step_type == solv.Transaction.SOLVER_TRANSACTION_REINSTALL:
                action = TransactionType.REINSTALL
            else:
                continue

            size = pkg_info.get('size', 0)
            if action in (TransactionType.INSTALL, TransactionType.UPGRADE):
                install_size += size
            elif action == TransactionType.REMOVE:
                remove_size += size

            actions.append(PackageAction(
                action=action,
                name=s.name,
                evr=s.evr,
                arch=s.arch,
                nevra=f"{s.name}-{s.evr}.{s.arch}",
                size=size,
                media_name=pkg_info.get('media_name', ''),
            ))

        return Resolution(
            success=True,
            actions=actions,
            problems=[],
            install_size=install_size,
            remove_size=remove_size
        )

    def resolve_remove(self, package_names: List[str], clean_deps: bool = True) -> Resolution:
        """Resolve packages to remove.

        Args:
            package_names: List of package names/NEVRAs to remove
            clean_deps: Also remove orphaned dependencies (iteratively)

        Returns:
            Resolution with success status and package actions
        """
        self._solvable_to_pkg = {}
        self.pool = self._create_pool()

        jobs = []
        not_found = []

        for name in package_names:
            # Try multiple selection methods to handle name, NEVRA, etc.
            flags = (solv.Selection.SELECTION_NAME |
                    solv.Selection.SELECTION_CANON |
                    solv.Selection.SELECTION_DOTARCH |
                    solv.Selection.SELECTION_INSTALLED_ONLY)
            sel = self.pool.select(name, flags)

            if sel.isempty():
                # Try glob match
                sel = self.pool.select(name, solv.Selection.SELECTION_GLOB |
                                       solv.Selection.SELECTION_INSTALLED_ONLY)

            if sel.isempty():
                # Try provides match (e.g., "nvim" -> neovim)
                sel = self.pool.select(name, solv.Selection.SELECTION_PROVIDES |
                                       solv.Selection.SELECTION_INSTALLED_ONLY)

            if sel.isempty():
                not_found.append(name)
            else:
                # Just erase the requested package(s)
                # Orphan detection is handled separately by find_erase_orphans()
                jobs += sel.jobs(solv.Job.SOLVER_ERASE)

        if not_found:
            return Resolution(
                success=False,
                actions=[],
                problems=[f"Package not installed: {n}" for n in not_found]
            )

        solver = self.pool.Solver()

        # Allow removing packages that depend on what we're removing (reverse deps)
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)

        problems = solver.solve(jobs)

        if problems:
            return Resolution(
                success=False,
                actions=[],
                problems=[str(p) for p in problems]
            )

        trans = solver.transaction()
        actions = []
        remove_size = 0

        for cl in trans.classify():
            for s in cl.solvables():
                pkg_info = self._solvable_to_pkg.get(s.id, {})
                size = pkg_info.get('size', 0)
                remove_size += size

                actions.append(PackageAction(
                    action=TransactionType.REMOVE,
                    name=s.name,
                    evr=s.evr,
                    arch=s.arch,
                    nevra=f"{s.name}-{s.evr}.{s.arch}",
                    size=size,
                ))

        return Resolution(
            success=True,
            actions=actions,
            problems=[],
            remove_size=remove_size
        )

    def _find_orphans_iterative(self, initial_removes: set) -> List[PackageAction]:
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

        ts = rpm.TransactionSet(self.root)

        # Build complete picture of installed packages
        installed_pkgs = {}  # name -> {provides: set, requires: set, hdr: header}
        provides_map = {}    # capability -> set of package names that provide it

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            provides = set()
            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                # Strip version from provide for simpler matching
                base_prov = prov.split('(')[0] if '(' in prov else prov
                provides.add(base_prov)
                provides.add(prov)  # Also keep full provide
                if base_prov not in provides_map:
                    provides_map[base_prov] = set()
                provides_map[base_prov].add(name)

            requires = set()
            for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                if req.startswith('rpmlib(') or req.startswith('/'):
                    continue
                # Strip version for matching
                base_req = req.split('(')[0] if '(' in req else req
                requires.add(base_req)

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
                    base_prov = prov.split('(')[0] if '(' in prov else prov
                    # Check all installed packages
                    for other_name, other_pkg in installed_pkgs.items():
                        if other_name == name:
                            continue
                        if other_name in to_remove:
                            continue
                        if base_prov in other_pkg['requires']:
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
        return Path(self.root) / 'var/lib/rpm/installed-through-deps.list'

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
        previously installed as a dependency.

        Args:
            package_names: List of package names to mark as explicit

        Returns:
            True if successful
        """
        unrequested = self._get_unrequested_packages()
        for name in package_names:
            unrequested.discard(name.lower())
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

    def find_all_orphans(self) -> List[PackageAction]:
        """Find ALL orphan packages in the system.

        Algorithm: For each package in unrequested (installed as dependency):
        - Walk UP reverse dependencies (who requires/recommends this package)
        - If any path leads to a package NOT in unrequested → keep it
        - If ALL paths only lead to other unrequested packages → orphan

        Returns:
            List of PackageAction for orphan packages
        """
        if not HAS_RPM:
            return []

        # Get packages that were installed as dependencies
        unrequested = self._get_unrequested_packages()
        if not unrequested:
            # No tracking file or empty - can't determine orphans reliably
            return []

        ts = rpm.TransactionSet(self.root)

        # Build package info and reverse dependency map
        installed_pkgs = {}  # name -> {provides, hdr}
        provides_map = {}    # capability -> set of package names providing it
        reverse_deps = {}    # name -> set of names that require/recommend it

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name == 'gpg-pubkey':
                continue

            # Collect provides
            provides = set()
            for prov in (hdr[rpm.RPMTAG_PROVIDENAME] or []):
                provides.add(prov)
                base_prov = prov.split('(')[0] if '(' in prov else prov
                provides.add(base_prov)
                # Map capability -> provider
                if base_prov not in provides_map:
                    provides_map[base_prov] = set()
                provides_map[base_prov].add(name)

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
                base_req = req.split('(')[0] if '(' in req else req
                # Find who provides this
                for provider in provides_map.get(base_req, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

            # Process Recommends
            for rec in (hdr[rpm.RPMTAG_RECOMMENDNAME] or []):
                base_rec = rec.split('(')[0] if '(' in rec else rec
                for provider in provides_map.get(base_rec, set()):
                    if provider != name:
                        reverse_deps[provider].add(name)

        # Build lowercase -> actual name mapping for unrequested lookup
        name_to_lower = {name: name.lower() for name in installed_pkgs}

        # For each unrequested package, check if it leads to an explicit package
        def has_explicit_ancestor(pkg_name: str, visited: set) -> bool:
            """Walk up reverse deps to find if any explicit package depends on this."""
            if pkg_name in visited:
                return False
            visited.add(pkg_name)

            for dep_name in reverse_deps.get(pkg_name, set()):
                # Found an explicitly installed package that needs this
                if name_to_lower.get(dep_name, dep_name.lower()) not in unrequested:
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

    def find_orphans(self, exclude_names: List[str] = None) -> List[PackageAction]:
        """Find orphan packages (installed as deps but no longer needed).

        Args:
            exclude_names: Package names to exclude from orphan check

        Returns:
            List of PackageAction for orphan packages
        """
        if not HAS_RPM:
            return []

        exclude = set(n.lower() for n in (exclude_names or []))
        orphans = []

        # Get all installed packages and their reverse deps
        ts = rpm.TransactionSet(self.root)

        # Build a map of what each package requires
        required_by = {}  # package_name -> set of packages that need it

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            requires = hdr[rpm.RPMTAG_REQUIRENAME] or []

            for req in requires:
                # Skip rpmlib, file deps, and self-requires
                if req.startswith("rpmlib(") or req.startswith("/"):
                    continue
                # Extract base name from capability (remove version stuff)
                req_name = req.split("(")[0] if "(" in req else req

                if req_name not in required_by:
                    required_by[req_name] = set()
                required_by[req_name].add(name)

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
                prov_name = prov.split("(")[0] if "(" in prov else prov
                if prov_name in required_by:
                    # Check if any requirer is still installed (not in exclude list)
                    requirers = required_by[prov_name]
                    for req in requirers:
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

    def find_upgrade_orphans(self, upgrade_actions: List[PackageAction]) -> List[PackageAction]:
        """Find packages that will become orphans after an upgrade.

        Compares requires of old (installed) vs new (to be installed) packages
        to find dependencies that are no longer needed.

        Args:
            upgrade_actions: List of PackageAction with action=UPGRADE

        Returns:
            List of PackageAction for packages that will become orphans
        """
        if not HAS_RPM:
            return []

        # Get packages installed as dependencies
        unrequested = self._get_unrequested_packages()
        if not unrequested:
            return []

        ts = rpm.TransactionSet(self.root)

        # Step 1: Collect old requires and new requires for upgraded packages
        old_requires = set()  # Base capability names from old packages
        new_requires = set()  # Base capability names from new packages
        upgraded_names = set()

        for action in upgrade_actions:
            if action.action != TransactionType.UPGRADE:
                continue

            upgraded_names.add(action.name)

            # Get OLD requires from librpm
            for hdr in ts.dbMatch('name', action.name):
                for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                    if not req.startswith('rpmlib('):
                        old_requires.add(self._extract_cap_name(req))
                break  # Just first match

            # Get NEW requires from our database
            pkg = self.db.get_package(action.name)
            if pkg and pkg.get('requires'):
                for req in pkg['requires']:
                    new_requires.add(self._extract_cap_name(req))

        # Step 2: Find capabilities that disappeared
        lost_caps = old_requires - new_requires
        if not lost_caps:
            return []

        # Step 3: Build reverse-requires map for post-upgrade state
        # We need to know what each package will require AFTER the upgrade
        post_upgrade_requires = {}  # pkg_name -> set of base cap names

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            if name in upgraded_names:
                # This package is being upgraded, use new requires
                pkg = self.db.get_package(name)
                if pkg and pkg.get('requires'):
                    caps = set(self._extract_cap_name(r) for r in pkg['requires'])
                    post_upgrade_requires[name] = caps
            else:
                # This package stays as-is, use current requires
                caps = set()
                for req in (hdr[rpm.RPMTAG_REQUIRENAME] or []):
                    if not req.startswith('rpmlib('):
                        caps.add(self._extract_cap_name(req))
                post_upgrade_requires[name] = caps

        # Step 4: For each lost capability, find what provides it
        # and check if that provider becomes orphan
        orphan_candidates = set()

        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]

            # Only consider packages installed as dependencies
            if name not in unrequested:
                continue

            # Skip packages being upgraded (they're not orphans, they're updated)
            if name in upgraded_names:
                continue

            # Check if this package provides any lost capability
            provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
            provides_lost = False
            for prov in provides:
                prov_base = self._extract_cap_name(prov)
                if prov_base in lost_caps:
                    provides_lost = True
                    break

            if not provides_lost:
                continue

            # This package provides something that was lost
            # Check if it's still required by anyone after upgrade
            is_still_required = False
            pkg_provides = set(self._extract_cap_name(p) for p in provides)

            for other_name, other_requires in post_upgrade_requires.items():
                if other_name == name:
                    continue
                # Check if other package requires any of our provides
                if pkg_provides & other_requires:
                    is_still_required = True
                    break

            if not is_still_required:
                orphan_candidates.add(name)

        # Step 5: Build PackageAction list for orphans
        orphans = []
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

        return orphans

    def find_erase_orphans(self, erase_names: List[str], erase_recommends: bool = False, keep_suggests: bool = False) -> List[PackageAction]:
        """Find packages that will become orphans after erasing packages.

        Strategy:
        1. Build the forward dependency tree of packages being erased
        2. For each package in the tree: if ALL its reverse-deps are also
           in the tree, it can be removed (it's an orphan)
        3. Only packages in unrequested can be auto-removed (except explicit ones)

        Args:
            erase_names: List of package names being erased (including reverse deps)
            erase_recommends: If True, RECOMMENDS don't block removal (only REQUIRES do)
            keep_suggests: If True, SUGGESTS also block removal (like RECOMMENDS)

        Returns:
            List of PackageAction for packages that will become orphans
        """
        if not HAS_RPM:
            return []

        # Get packages installed as dependencies (not explicitly requested)
        unrequested = self._get_unrequested_packages()

        ts = rpm.TransactionSet(self.root)

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

        # Helper: get deps including recommends and suggests (for dep_tree building)
        def get_all_deps(pkg_name: str) -> set:
            """Get packages that pkg_name depends on, recommends, or suggests."""
            deps = set()
            # REQUIRES
            for cap in pkg_requires.get(pkg_name, set()):
                provider = resolve_cap_to_pkg(cap)
                if provider and provider != pkg_name:
                    deps.add(provider)
            # RECOMMENDS
            for cap in pkg_recommends.get(pkg_name, set()):
                provider = resolve_cap_to_pkg(cap)
                if provider and provider != pkg_name:
                    deps.add(provider)
            # SUGGESTS
            for cap in pkg_suggests.get(pkg_name, set()):
                provider = resolve_cap_to_pkg(cap)
                if provider and provider != pkg_name:
                    deps.add(provider)
            return deps

        # Helper: get reverse dependencies of a package (as package names)
        def get_reverse_deps(pkg_name: str) -> dict:
            """Get packages that depend on pkg_name (respecting erase_recommends/keep_suggests).

            Default: REQUIRES + RECOMMENDS block removal, SUGGESTS does NOT
            erase_recommends=True: only REQUIRES blocks removal
            keep_suggests=True: REQUIRES + RECOMMENDS + SUGGESTS all block removal

            Returns: dict of {pkg_name: dep_type} where dep_type is 'R', 'M', or 'S'
            """
            rdeps = {}  # pkg_name -> dep_type ('R'equires, 'M'=recoMmends, 'S'uggests)
            my_provides = pkg_provides.get(pkg_name, set())
            for other_name in all_installed:
                if other_name == pkg_name:
                    continue
                other_requires = pkg_requires.get(other_name, set())
                other_recommends = pkg_recommends.get(other_name, set())
                other_suggests = pkg_suggests.get(other_name, set())
                # Check if other_name requires, recommends, or suggests any capability that pkg_name provides
                for cap in my_provides:
                    # REQUIRES always blocks removal
                    if cap in other_requires:
                        rdeps[other_name] = 'R'
                        break
                    # RECOMMENDS blocks removal unless --erase-recommends is set
                    if not erase_recommends and cap in other_recommends:
                        rdeps[other_name] = 'M'
                        break
                    # SUGGESTS blocks removal only if --keep-suggests is set
                    if keep_suggests and cap in other_suggests:
                        rdeps[other_name] = 'S'
                        break
            return rdeps

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
        import logging
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
        # A package can be removed if ALL its reverse deps are also being removed.
        # We iteratively remove packages from candidates that have rdeps outside candidates.

        candidates = set(to_remove)
        candidates_lower = {p.lower() for p in candidates}
        removed_from_candidates = {}  # For debug: pkg -> (blocker, dep_type)

        # Iterate until stable
        changed = True
        iteration = 0
        while changed:
            changed = False
            iteration += 1
            for pkg_name in list(candidates):
                if pkg_name in erase_set_original:
                    continue  # Always remove explicitly requested packages

                rdeps = get_reverse_deps(pkg_name)  # dict: pkg -> dep_type

                for rdep, dep_type in rdeps.items():
                    rdep_lower = rdep.lower()
                    # Package must stay if it has a rdep that will remain installed.
                    # A rdep remains installed if it's NOT in candidates.
                    if rdep_lower not in candidates_lower:
                        candidates.remove(pkg_name)
                        candidates_lower.remove(pkg_name.lower())
                        removed_from_candidates[pkg_name] = (rdep, dep_type)
                        changed = True
                        break

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
                        blocker, dep_type = removed_from_candidates[pkg]
                        f.write(f"  {pkg} <-[{dep_type}]- {blocker}\n")
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


def format_size(size_bytes: int) -> str:
    """Format size in human readable form."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    else:
        return f"{size_bytes / 1024 / 1024 / 1024:.1f} GB"
