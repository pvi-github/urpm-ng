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
from .config import get_media_local_path, get_base_dir, get_system_version
from .compression import decompress_stream


# Debug flag for resolver - set to True to enable debug output
DEBUG_RESOLVER = False


class SolverDebug:
    """Debug helper for solver resolution.

    Provides structured debug output for remote troubleshooting.
    Usage:
        debug = SolverDebug(enabled=True, watched=['pkg1', 'pkg2'])
        debug.log("Pool created")
        debug.watch("pkg1", "Found installed", "1.0-1.mga10")
    """

    def __init__(self, enabled: bool = False, watched: List[str] = None):
        self.enabled = enabled
        self.watched = set(w.lower() for w in (watched or []))

    def log(self, msg: str, indent: int = 0):
        """Print a debug message."""
        if self.enabled:
            prefix = "  " * indent
            print(f"[SOLVER] {prefix}{msg}")

    def watch(self, pkg_name: str, action: str, detail: str = ""):
        """Print a watched package message."""
        if self.enabled and pkg_name.lower() in self.watched:
            detail_str = f": {detail}" if detail else ""
            print(f"[WATCH:{pkg_name}] {action}{detail_str}")

    def is_watched(self, pkg_name: str) -> bool:
        """Check if a package is being watched."""
        return pkg_name.lower() in self.watched

    def log_pool_stats(self, pool):
        """Log pool statistics."""
        if not self.enabled:
            return
        total_solvables = sum(1 for _ in pool.solvables)
        installed_count = sum(1 for _ in pool.installed.solvables) if pool.installed else 0
        repo_count = len(pool.repos)
        self.log(f"Pool: {total_solvables} solvables, {installed_count} installed, {repo_count} repos")
        for repo in pool.repos:
            count = sum(1 for _ in repo.solvables)
            self.log(f"Repo '{repo.name}': {count} packages", indent=1)

    def log_selection(self, name: str, selection, context: str = ""):
        """Log a selection result."""
        if not self.enabled:
            return
        solvables = list(selection.solvables())
        ctx = f" ({context})" if context else ""
        if not solvables:
            self.log(f"Select '{name}'{ctx}: no match")
        else:
            self.log(f"Select '{name}'{ctx}: {len(solvables)} match(es)")
            # Show first few matches
            for s in solvables[:5]:
                repo_name = s.repo.name if s.repo else "?"
                self.log(f"{s} [{repo_name}]", indent=1)
            if len(solvables) > 5:
                self.log(f"... and {len(solvables) - 5} more", indent=1)

    def log_jobs(self, jobs):
        """Log solver jobs."""
        if not self.enabled:
            return
        self.log(f"Jobs: {len(jobs)}")
        for job in jobs:
            self.log(f"{job}", indent=1)

    def log_problems(self, problems):
        """Log solver problems."""
        if not self.enabled:
            return
        self.log(f"Problems: {len(problems)}")
        for p in problems:
            self.log(f"{p}", indent=1)

    def log_transaction(self, trans):
        """Log transaction details."""
        if not self.enabled:
            return
        if trans.isempty():
            self.log("Transaction: empty (nothing to do)")
            return

        # Count by type
        installs = []
        erases = []
        upgrades = []

        for cl in trans.classify():
            for p in cl.solvables():
                if cl.type == solv.Transaction.SOLVER_TRANSACTION_INSTALL:
                    installs.append(p)
                elif cl.type == solv.Transaction.SOLVER_TRANSACTION_ERASE:
                    erases.append(p)
                elif cl.type in (solv.Transaction.SOLVER_TRANSACTION_UPGRADE,
                                 solv.Transaction.SOLVER_TRANSACTION_DOWNGRADE):
                    upgrades.append(p)

        self.log(f"Transaction: {len(installs)} install, {len(erases)} erase, {len(upgrades)} upgrade")

        # Log watched packages in transaction
        for p in installs + upgrades:
            if self.is_watched(p.name):
                self.watch(p.name, "In transaction", str(p))


# Global debug instance (set by CLI)
_solver_debug = SolverDebug()


def set_solver_debug(enabled: bool = False, watched: List[str] = None):
    """Set global solver debug options."""
    global _solver_debug, DEBUG_RESOLVER
    _solver_debug = SolverDebug(enabled=enabled, watched=watched)
    if enabled:
        DEBUG_RESOLVER = True


def get_solver_debug() -> SolverDebug:
    """Get global solver debug instance."""
    return _solver_debug


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
    filesize: int = 0
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

    def __init__(self, db: PackageDatabase, arch: str = "x86_64", root: str = None,
                 urpm_root: str = None, install_recommends: bool = True,
                 ignore_installed: bool = False):
        """Initialize resolver.

        Args:
            db: Package database
            arch: System architecture
            root: RPM database root for chroot install (--root)
            urpm_root: Root for both urpm config and RPM (--urpm-root)
            install_recommends: Install recommended packages (default: True)
            ignore_installed: If True, resolve as if nothing is installed (for download-only)
        """
        self.db = db
        self.arch = arch
        # --urpm-root implies --root to same location
        self.root = urpm_root or root
        self.urpm_root = urpm_root
        self.install_recommends = install_recommends
        self.ignore_installed = ignore_installed
        self.pool = None
        self._solvable_to_pkg = {}  # Map solvable id -> pkg dict
        self._installed_count = 0  # Number of installed packages loaded
        self._held_obsolete_warnings = []  # List of (held_pkg, obsoleting_pkg) tuples
        self._held_upgrade_warnings = []  # List of held package names skipped from upgrade

    def _create_pool(self) -> solv.Pool:
        """Create and populate libsolv Pool from database.

        Uses native libsolv methods for optimal performance:
        - add_rpmdb() for installed packages
        - add_mdk() for loading synthesis files directly
        """
        import tempfile
        debug = get_solver_debug()

        pool = solv.Pool()
        pool.setdisttype(solv.Pool.DISTTYPE_RPM)
        pool.setarch(self.arch)
        debug.log(f"Creating pool for arch={self.arch}, root={self.root}, urpm_root={self.urpm_root}")

        # Set root directory for chroot installations
        if self.root:
            pool.set_rootdir(self.root)

        # Load installed packages from rpmdb (skip if ignore_installed for download-only)
        if self.ignore_installed:
            debug.log("ignore_installed=True: skipping rpmdb loading")
            self._installed_count = 0
        else:
            installed = pool.add_repo("@System")
            installed.appdata = {"type": "installed"}
            pool.installed = installed

            if HAS_RPM:
                # Note: libsolv's add_rpmdb() doesn't respect set_rootdir()
                # For chroot installs, we need to use rpm module directly
                if self.root:
                    # Use rpm module to read from chroot's rpmdb
                    import rpm
                    ts = rpm.TransactionSet(self.root or '/')
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)

                    # Map RPM flags to libsolv relation flags
                    def rpm_flags_to_solv(flags):
                        rel = 0
                        if flags & rpm.RPMSENSE_LESS:
                            rel |= solv.REL_LT
                        if flags & rpm.RPMSENSE_GREATER:
                            rel |= solv.REL_GT
                        if flags & rpm.RPMSENSE_EQUAL:
                            rel |= solv.REL_EQ
                        return rel

                    for hdr in ts.dbMatch():
                        s = installed.add_solvable()
                        s.name = hdr[rpm.RPMTAG_NAME]
                        epoch = hdr[rpm.RPMTAG_EPOCH] or 0
                        version = hdr[rpm.RPMTAG_VERSION]
                        release = hdr[rpm.RPMTAG_RELEASE]
                        s.evr = f"{epoch}:{version}-{release}" if epoch else f"{version}-{release}"
                        s.arch = hdr[rpm.RPMTAG_ARCH] or "noarch"

                        # Add provides
                        prov_names = hdr[rpm.RPMTAG_PROVIDENAME] or []
                        prov_flags = hdr[rpm.RPMTAG_PROVIDEFLAGS] or []
                        prov_vers = hdr[rpm.RPMTAG_PROVIDEVERSION] or []
                        for i, pname in enumerate(prov_names):
                            pflags = prov_flags[i] if i < len(prov_flags) else 0
                            pver = prov_vers[i] if i < len(prov_vers) else ''
                            if pver and pflags:
                                dep_id = pool.rel2id(
                                    pool.str2id(pname),
                                    pool.str2id(pver),
                                    rpm_flags_to_solv(pflags)
                                )
                            else:
                                dep_id = pool.str2id(pname)
                            s.add_deparray(solv.SOLVABLE_PROVIDES, dep_id)

                    self._installed_count = installed.nsolvables
                    debug.log(f"Loaded {self._installed_count} installed packages from chroot rpmdb")
                else:
                    # Normal case: use libsolv's native method
                    installed.add_rpmdb()
                    self._installed_count = installed.nsolvables
            else:
                self._installed_count = 0

        # Load available packages from synthesis files (much faster than SQLite)
        # Use urpm_root for config paths if specified
        base_dir = get_base_dir(urpm_root=self.urpm_root)
        debug.log(f"Base dir for synthesis: {base_dir}")

        # Get system version for media filtering (partition by distro version)
        system_version = get_system_version(self.root)
        debug.log(f"System version: {system_version}")

        media_list = self.db.list_media()
        debug.log(f"Found {len(media_list)} media in database")

        for media in media_list:
            if not media['enabled']:
                debug.log(f"Skipping disabled media: {media['name']}")
                continue

            # Filter by Mageia version - only load media matching system version
            media_version = media.get('mageia_version')
            if system_version and media_version and media_version != system_version:
                debug.log(f"Skipping media {media['name']}: version {media_version} != system {system_version}")
                continue

            repo = pool.add_repo(media['name'])
            repo.appdata = {"type": "available", "media": media}

            # Try to load from synthesis file first
            media_path = get_media_local_path(media, base_dir)
            synthesis_path = media_path / "media_info" / "synthesis.hdlist.cz"
            debug.log(f"Media {media['name']}: looking for {synthesis_path} (exists: {synthesis_path.exists()})")

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
                            'filesize': s.lookup_num(solv.SOLVABLE_DOWNLOADSIZE) or 0,
                            'media_name': repo.name,
                        }
                    debug.log(f"Loaded {repo.nsolvables} packages from synthesis")
                except Exception as e:
                    debug.log(f"Failed to load synthesis for {media['name']}: {e}")
                    # Fallback to SQLite loading
                    self._load_repo_packages(pool, repo, media['id'])
            else:
                # No synthesis file, fallback to SQLite
                debug.log(f"No synthesis, falling back to SQLite for {media['name']}")
                self._load_repo_packages(pool, repo, media['id'])

        pool.createwhatprovides()
        debug.log_pool_stats(pool)
        return pool

    def _load_repo_packages(self, pool: solv.Pool, repo: solv.Repo, media_id: int):
        """Load packages from database into libsolv repo.

        Uses bulk loading for performance.
        """
        # Load all packages first
        cursor = self.db.conn.execute("""
            SELECT id, name, epoch, version, release, arch, nevra, summary, size, filesize
            FROM packages WHERE media_id = ?
        """, (media_id,))

        pkg_id_to_solvable = {}

        for row in cursor:
            pkg_id, name, epoch, version, release, arch, nevra, summary, size, filesize = row

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
                'filesize': filesize or 0,
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
        ts = rpm.TransactionSet(self.root or '/')

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
                'filesize': size,
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

    def add_local_rpms(self, rpm_infos: List[Dict]) -> None:
        """Add local RPM files to the resolver pool.

        This creates a special @LocalRPMs repository containing the local
        packages so they can be resolved alongside repository packages.

        Args:
            rpm_infos: List of dicts from read_rpm_header() with package metadata
        """
        if self.pool is None:
            self.pool = self._create_pool()

        # Create local RPMs repository
        local_repo = self.pool.add_repo("@LocalRPMs")
        local_repo.appdata = {"type": "local"}

        for info in rpm_infos:
            s = local_repo.add_solvable()
            s.name = info['name']
            epoch = info.get('epoch', 0) or 0
            if epoch:
                s.evr = f"{epoch}:{info['version']}-{info['release']}"
            else:
                s.evr = f"{info['version']}-{info['release']}"
            s.arch = info['arch']

            # Versioned self-provide
            s.add_deparray(solv.SOLVABLE_PROVIDES,
                self.pool.Dep(info['name']).Rel(solv.REL_EQ, self.pool.Dep(s.evr)))

            # Add provides
            for cap in info.get('provides', []):
                if cap and not cap.startswith('rpmlib('):
                    s.add_deparray(solv.SOLVABLE_PROVIDES, parse_capability(self.pool, cap))

            # Add requires
            for cap in info.get('requires', []):
                if cap and not cap.startswith('rpmlib(') and not cap.startswith('/'):
                    s.add_deparray(solv.SOLVABLE_REQUIRES, parse_capability(self.pool, cap))

            # Add conflicts
            for cap in info.get('conflicts', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_CONFLICTS, parse_capability(self.pool, cap))

            # Add obsoletes
            for cap in info.get('obsoletes', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_OBSOLETES, parse_capability(self.pool, cap))

            # Add weak dependencies
            for cap in info.get('recommends', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_RECOMMENDS, parse_capability(self.pool, cap))
            for cap in info.get('suggests', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_SUGGESTS, parse_capability(self.pool, cap))

            # Store metadata including the local path
            self._solvable_to_pkg[s.id] = {
                'name': info['name'],
                'evr': s.evr,
                'arch': info['arch'],
                'nevra': info['nevra'],
                'size': info.get('size', 0),
                'filesize': info.get('filesize', 0),
                'media_name': '@LocalRPMs',
                'local_path': info['path'],  # Critical: path to the RPM file
            }

        # Rebuild whatprovides index
        self.pool.createwhatprovides()

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
                        preference_patterns: list = None,
                        local_packages: set = None) -> Resolution:
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
            local_packages: Optional set of package names from local RPM files
                     (uses SOLVER_UPDATE to allow upgrading installed packages)

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
        if local_packages is None:
            local_packages = set()

        # Preserve pool only if it has @LocalRPMs repo (for local RPM installation)
        has_local_rpms = self.pool is not None and any(
            r.name == '@LocalRPMs' for r in self.pool.repos
        )
        if not has_local_rpms:
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

            if sel.isempty() and name not in local_packages:
                not_found.append(name)
            elif name in local_packages:
                # For local packages, find directly in @LocalRPMs repo (pool.select doesn't work)
                local_solvable = None
                for repo in self.pool.repos:
                    if repo.name == '@LocalRPMs':
                        for s in repo.solvables:
                            if s.name == name:
                                local_solvable = s
                                break
                        break
                if local_solvable:
                    jobs.append(self.pool.Job(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE, local_solvable.id))
                else:
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
        # Allow removing packages that are obsoleted by installed packages
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        # Handle obsoletes (e.g., dhcpcd obsoletes dhcp-client)
        solver.set_flag(solv.Solver.SOLVER_FLAG_YUM_OBSOLETES, 1)
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
        remove_size = 0

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

            size = pkg_info.get('filesize', 0)
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
                reason=reason,
            ))

        # Detect alternatives: packages that could satisfy the same dependency
        # Filter out alternatives where user already made a choice
        all_alternatives = self._find_alternatives(solver, trans, actions)
        alternatives = [alt for alt in all_alternatives if alt.capability not in choices]

        # Filter out alternatives where a provider is explicitly requested by user
        # e.g., if user requests vim-minimal, don't ask them to choose between vim providers
        requested_names = {n.split()[0].split('<')[0].split('>')[0].split('=')[0].strip()
                          for n in package_names}
        alternatives = [alt for alt in alternatives
                       if not any(p in requested_names for p in alt.providers)]

        # If there are unresolved alternatives, return them for user choice
        if alternatives:
            return Resolution(
                success=False,
                actions=actions,
                problems=[],
                install_size=install_size,
                remove_size=remove_size,
                alternatives=alternatives
            )

        return Resolution(
            success=True,
            actions=actions,
            problems=[],
            install_size=install_size,
            remove_size=remove_size
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
        import os

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
                                resolved_packages: List[str] = None) -> Tuple[List[PackageAction], List[Alternative]]:
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

    def resolve_upgrade(self, package_names: List[str] = None,
                        local_packages: set = None) -> Resolution:
        """Resolve packages to upgrade.

        Args:
            package_names: List of package names to upgrade (None = all)
            local_packages: Set of package names from local RPM files

        Returns:
            Resolution with success status and package actions
        """
        debug = get_solver_debug()
        debug.log("=== resolve_upgrade() ===")
        debug.log(f"package_names: {package_names}")
        debug.log(f"local_packages: {local_packages}")

        if local_packages is None:
            local_packages = set()

        # Preserve pool only if it has @LocalRPMs repo (for local RPM upgrade)
        has_local_rpms = self.pool is not None and any(
            r.name == '@LocalRPMs' for r in self.pool.repos
        )
        if not has_local_rpms:
            self._solvable_to_pkg = {}
            self.pool = self._create_pool()
            debug.log_pool_stats(self.pool)

        jobs = []

        if package_names:
            # Upgrade specific packages
            not_found = []
            not_installed = []
            for name in package_names:
                debug.log(f"Processing package: {name}")
                debug.watch(name, "Processing for upgrade")
                # Local packages: find in @LocalRPMs repo and use SOLVER_INSTALL
                if name in local_packages:
                    local_solvable = None
                    for repo in self.pool.repos:
                        if repo.name == '@LocalRPMs':
                            for s in repo.solvables:
                                if s.name == name:
                                    local_solvable = s
                                    break
                            break
                    if local_solvable:
                        jobs.append(self.pool.Job(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE, local_solvable.id))
                    else:
                        not_found.append(name)
                    continue

                # First check if it's installed
                inst_flags = (solv.Selection.SELECTION_NAME |
                             solv.Selection.SELECTION_CANON |
                             solv.Selection.SELECTION_DOTARCH |
                             solv.Selection.SELECTION_INSTALLED_ONLY)
                inst_sel = self.pool.select(name, inst_flags)
                debug.log_selection(name, inst_sel, "installed, exact")

                if inst_sel.isempty():
                    # Try glob
                    inst_sel = self.pool.select(name, solv.Selection.SELECTION_GLOB |
                                                solv.Selection.SELECTION_INSTALLED_ONLY)
                    debug.log_selection(name, inst_sel, "installed, glob")

                if inst_sel.isempty():
                    debug.log(f"NOT INSTALLED: {name}")
                    debug.watch(name, "Not found in installed packages")
                    not_installed.append(name)
                    continue

                # Log what we found installed
                for s in inst_sel.solvables():
                    debug.watch(name, f"Found installed: {s}")

                # Now select from ALL repos (not just installed) for the update
                flags = (solv.Selection.SELECTION_NAME |
                        solv.Selection.SELECTION_CANON |
                        solv.Selection.SELECTION_DOTARCH)
                sel = self.pool.select(name, flags)
                debug.log_selection(name, sel, "all repos, exact")

                if sel.isempty():
                    sel = self.pool.select(name, solv.Selection.SELECTION_GLOB)
                    debug.log_selection(name, sel, "all repos, glob")

                if sel.isempty():
                    debug.log(f"NOT FOUND in any repo: {name}")
                    not_found.append(name)
                else:
                    new_jobs = sel.jobs(solv.Job.SOLVER_UPDATE)
                    debug.log(f"Adding {len(new_jobs)} UPDATE job(s) for {name}")
                    jobs += new_jobs

            if not_installed:
                debug.log(f"FAILED: packages not installed: {not_installed}")
                return Resolution(
                    success=False,
                    actions=[],
                    problems=[f"Package not installed: {n}" for n in not_installed]
                )

            if not_found:
                debug.log(f"FAILED: packages not found: {not_found}")
                return Resolution(
                    success=False,
                    actions=[],
                    problems=[f"Package not found: {n}" for n in not_found]
                )
        else:
            # Find installed packages that have updates available
            # and create SOLVER_INSTALL jobs for the newer versions
            debug.log("Full system upgrade: scanning for available updates...")
            held_packages = self.db.get_held_packages_set()
            held_upgrade_warnings = []
            updates_found = 0
            for installed_pkg in self.pool.installed.solvables:
                is_watched = debug.is_watched(installed_pkg.name)
                is_held = installed_pkg.name in held_packages

                # Find the best available version of this package
                # For UPGRADES, prefer same architecture as installed package
                # (but solver can still INSTALL other arches as new dependencies)
                sel = self.pool.select(installed_pkg.name, solv.Selection.SELECTION_NAME)
                best_available = None
                available_versions = []
                installed_arch = installed_pkg.arch

                for s in sel.solvables():
                    if s.repo != self.pool.installed:
                        available_versions.append(s)
                        if best_available is None:
                            best_available = s
                        else:
                            # Priority: same arch > different arch, then higher version
                            s_same_arch = (s.arch == installed_arch)
                            best_same_arch = (best_available.arch == installed_arch)

                            if s_same_arch and not best_same_arch:
                                # s is same arch, best isn't -> prefer s
                                best_available = s
                            elif s_same_arch == best_same_arch:
                                # Both same arch status, compare version
                                if s.evrcmp(best_available) > 0:
                                    best_available = s
                            # else: s is different arch, best is same arch -> keep best

                # Debug output for watched packages
                if is_watched:
                    debug.watch(installed_pkg.name, f"Installed: {installed_pkg}")
                    if available_versions:
                        for av in available_versions[:5]:
                            debug.watch(installed_pkg.name, f"Available: {av} [{av.repo.name}]")
                        if len(available_versions) > 5:
                            debug.watch(installed_pkg.name, f"... and {len(available_versions) - 5} more versions")
                    else:
                        debug.watch(installed_pkg.name, "No versions available in repos")

                    if best_available:
                        cmp_result = best_available.evrcmp(installed_pkg)
                        if cmp_result > 0:
                            debug.watch(installed_pkg.name, f"UPGRADE: {installed_pkg.evr} -> {best_available.evr}")
                        elif cmp_result == 0:
                            debug.watch(installed_pkg.name, f"SAME VERSION: {installed_pkg.evr} == {best_available.evr}")
                        else:
                            debug.watch(installed_pkg.name, f"DOWNGRADE (skipped): {installed_pkg.evr} > {best_available.evr}")
                    else:
                        debug.watch(installed_pkg.name, "NO UPGRADE: no available version found")

                # If there's a newer version available, add an install job for it
                # (unless the package is held)
                if best_available and best_available.evrcmp(installed_pkg) > 0:
                    if is_held:
                        # Only warn if there's actually an upgrade to skip
                        held_upgrade_warnings.append(installed_pkg.name)
                        debug.log(f"HELD: {installed_pkg.name} upgrade skipped ({installed_pkg.evr} -> {best_available.evr})")
                    else:
                        jobs.append(self.pool.Job(
                            solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE,
                            best_available.id
                        ))
                        updates_found += 1

            debug.log(f"Found {updates_found} packages with updates available")
            if held_upgrade_warnings:
                debug.log(f"Skipped {len(held_upgrade_warnings)} held packages from upgrade")

            # Scan for packages that obsolete installed packages
            # This handles cases like dhcpcd obsoleting dhcp-client
            debug.log("Scanning for packages that obsolete installed packages...")
            # Reuse held_packages from above
            held_warnings = []
            obsoletes_found = 0

            already_warned = set(held_upgrade_warnings)  # Avoid duplicate warnings
            seen_obsoletes = set()  # Track what we've already processed
            # Build set of installed package names for fast lookup
            installed_names = {pkg.name for pkg in self.pool.installed.solvables}

            for repo in self.pool.repos:
                if repo == self.pool.installed:
                    continue  # Skip installed repo
                for s in repo.solvables:
                    # Check if this package obsoletes any installed package
                    obsoletes = s.lookup_idarray(solv.SOLVABLE_OBSOLETES)
                    if not obsoletes:
                        continue

                    for obs_id in obsoletes:
                        # Use whatprovides to find packages matching this obsolete
                        # This is much faster than iterating all installed packages
                        for provider in self.pool.whatprovides(obs_id):
                            if provider.repo != self.pool.installed:
                                continue  # Only care about installed packages

                            # Skip self-obsoletes (same package name)
                            # This happens when a package has Obsoletes: pkgname < version
                            # to clean up upgrades - it's not a replacement, just an upgrade
                            if s.name == provider.name:
                                continue

                            # Skip if obsoleting package is already installed
                            # This prevents "downgrade" scenarios where mageia-release-common
                            # obsoletes mageia-release-Default but is already installed
                            if s.name in installed_names:
                                debug.log(f"SKIP: {s.name} obsoletes {provider.name} but is already installed")
                                continue

                            # Skip if already warned in upgrade loop
                            if provider.name in already_warned:
                                continue

                            # Skip duplicates (same obsolete pair)
                            obs_key = (provider.name, s.name)
                            if obs_key in seen_obsoletes:
                                continue
                            seen_obsoletes.add(obs_key)

                            # Check if obsoleted package is held
                            if provider.name in held_packages:
                                held_warnings.append((provider.name, s.name))
                                debug.log(f"HELD: {provider.name} would be obsoleted by {s.name} (skipped)")
                            else:
                                # Add install job for the obsoleting package
                                jobs.append(self.pool.Job(
                                    solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE,
                                    s.id
                                ))
                                obsoletes_found += 1
                                debug.log(f"OBSOLETES: {s.name} obsoletes installed {provider.name}")
                            break  # Found a match, move to next obsolete

            debug.log(f"Found {obsoletes_found} packages that obsolete installed packages")
            if held_warnings:
                debug.log(f"Skipped {len(held_warnings)} obsoletes due to held packages")
            # Store held warnings for later display (both upgrade and obsoletes)
            self._held_obsolete_warnings = held_warnings
            self._held_upgrade_warnings = held_upgrade_warnings

            if updates_found == 0 and obsoletes_found == 0:
                debug.log("No updates or obsoletes found, returning empty resolution")
                return Resolution(
                    success=True,
                    actions=[],
                    problems=[],
                )

        # Solve
        debug.log_jobs(jobs)
        debug.log("Running solver...")
        solver = self.pool.Solver()
        # Allow vendor changes and arch changes for upgrades
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_VENDORCHANGE, 1)
        # Prefer packages compatible with already installed packages
        solver.set_flag(solv.Solver.SOLVER_FLAG_FOCUS_INSTALLED, 1)
        # Allow removing packages with broken dependencies
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        # Handle obsoletes during updates (package name changes like lib64gdal37 → lib64gdal38)
        solver.set_flag(solv.Solver.SOLVER_FLAG_YUM_OBSOLETES, 1)
        # Handle weak dependencies (Recommends/Suggests)
        if not self.install_recommends:
            solver.set_flag(solv.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 1)

        problems = solver.solve(jobs)

        if problems:
            debug.log_problems(problems)
            return Resolution(
                success=False,
                actions=[],
                problems=[str(p) for p in problems]
            )

        # Get transaction
        trans = solver.transaction()
        debug.log_transaction(trans)
        if trans.isempty():
            debug.log("Transaction is empty, nothing to do")
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
                filesize=pkg_info.get('filesize', 0),
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
                    filesize=pkg_info.get('filesize', 0)
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

        ts = rpm.TransactionSet(self.root or '/')

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
        root = self.root or '/'
        return Path(root) / 'var/lib/rpm/installed-through-deps.list'

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

        ts = rpm.TransactionSet(self.root or '/')

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

        ts = rpm.TransactionSet(self.root or '/')

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
            keep_suggests: If True, SUGGESTS also block removal

        Returns:
            List of PackageAction for packages that will become orphans
        """
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
