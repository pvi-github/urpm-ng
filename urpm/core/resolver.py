"""
Dependency resolver using libsolv

Uses the SAT-based libsolv library for fast, correct dependency resolution.
"""

import re
import solv
from typing import List, Dict
from dataclasses import dataclass
from enum import Enum

try:
    import rpm
    HAS_RPM = True
except ImportError:
    HAS_RPM = False

from .database import PackageDatabase
from .resolution import PoolMixin, QueriesMixin, AlternativesMixin, OrphansMixin


class VersionConflictError(Exception):
    """Raised when media configuration has ambiguous version mix (e.g., both mga10 and cauldron)."""

    def __init__(self, message: str, conflict_info: dict = None):
        super().__init__(message)
        self.conflict_info = conflict_info or {}


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

# Regex to parse RPM-style capability strings like "name >= version"
RPM_CAP_REGEX = re.compile(r'^(\S+)\s+(>=|<=|=|>|<)\s+(.+)$')

# Map string operators to libsolv flags
OP_FLAGS = {
    '>=': solv.REL_GT | solv.REL_EQ,
    '<=': solv.REL_LT | solv.REL_EQ,
    '==': solv.REL_EQ,
    '=': solv.REL_EQ,
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

    # Handle RPM-style format: "name >= version" or "name = version"
    if '[' not in cap_str and ' ' in cap_str:
        rpm_match = RPM_CAP_REGEX.match(cap_str)
        if rpm_match:
            name, op, version = rpm_match.groups()
            flags = OP_FLAGS.get(op)
            if flags:
                return pool.Dep(name).Rel(flags, pool.Dep(version))

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


class Resolver(PoolMixin, QueriesMixin, AlternativesMixin, OrphansMixin):
    """Dependency resolver using libsolv.

    Functionality is provided by mixins:
    - PoolMixin: Pool creation and loading
    - QueriesMixin: Capability and dependency queries
    - AlternativesMixin: Alternative selection logic
    - OrphansMixin: Orphan package detection
    """

    def __init__(self, db: PackageDatabase, arch: str = "x86_64", root: str = None,
                 urpm_root: str = None, install_recommends: bool = True,
                 ignore_installed: bool = False,
                 allowed_arches: list = None):
        """Initialize resolver.

        Args:
            db: Package database
            arch: System architecture
            root: RPM database root for chroot install (--root)
            urpm_root: Root for both urpm config and RPM (--urpm-root)
            install_recommends: Install recommended packages (default: True)
            ignore_installed: If True, resolve as if nothing is installed (for download-only)
            allowed_arches: List of allowed package architectures.
                           Default: [arch, 'noarch'] (system arch + noarch)
                           Use --allow-arch to add more (e.g., i686 for wine/steam)
        """
        self.db = db
        self.arch = arch
        # --urpm-root implies --root to same location
        self.root = urpm_root or root
        self.urpm_root = urpm_root
        self.install_recommends = install_recommends
        self.ignore_installed = ignore_installed
        # Default allowed architectures: system arch + noarch
        self.allowed_arches = allowed_arches if allowed_arches is not None else [arch, 'noarch']
        self.pool = None
        self._solvable_to_pkg = {}  # Map solvable id -> pkg dict
        self._installed_count = 0  # Number of installed packages loaded
        self._held_obsolete_warnings = []  # List of (held_pkg, obsoleting_pkg) tuples
        self._held_upgrade_warnings = []  # List of held package names skipped from upgrade

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
            # Parse version constraint if present (formats: "name >= ver" or "name[>= ver]")
            base_name = name
            # TODO: version_constraint isn't used yet
            version_constraint = None

            # Handle "name op version" format (space-separated)
            space_match = re.match(r'^(.+?)\s+(>=|<=|>|<|==|=)\s+(.+)$', name)
            if space_match:
                base_name = space_match.group(1)
                version_constraint = (space_match.group(2), space_match.group(3))
            else:
                # Handle "name[op version]" format (bracket)
                bracket_match = re.match(r'^([^\[]+)\[([<>=!]+)\s*(.+)\]$', name)
                if bracket_match:
                    base_name = bracket_match.group(1)
                    version_constraint = (bracket_match.group(2), bracket_match.group(3))

            # Use multiple selection flags for flexibility
            flags = (solv.Selection.SELECTION_NAME |
                    solv.Selection.SELECTION_CANON |
                    solv.Selection.SELECTION_DOTARCH |
                    solv.Selection.SELECTION_REL)
            sel = self.pool.select(base_name, flags)

            if sel.isempty():
                # Try glob match
                sel = self.pool.select(base_name, solv.Selection.SELECTION_GLOB |
                                       solv.Selection.SELECTION_CANON)
            if sel.isempty():
                # Try provides match
                sel = self.pool.select(base_name, solv.Selection.SELECTION_PROVIDES)

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

            if sel.isempty() and base_name not in local_packages:
                not_found.append(name)
            elif base_name in local_packages:
                # For local packages, find directly in @LocalRPMs repo (pool.select doesn't work)
                local_solvable = None
                for repo in self.pool.repos:
                    if repo.name == '@LocalRPMs':
                        for s in repo.solvables:
                            if s.name == base_name:
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
        # TODO: requested_lower isn't used yet
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
            # If ALL packages are not installed, return success with "nothing to do"
            # (handles PackageKit calling remove twice, second time package is gone)
            if len(not_found) == len(package_names):
                return Resolution(
                    success=True,
                    actions=[],
                    problems=[]
                )
            # If only some packages not found, that's an error
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

        # Find orphaned dependencies if requested
        if clean_deps and actions:
            initial_removes = {a.name for a in actions}
            orphan_actions = self._find_orphans_iterative(initial_removes)
            if orphan_actions:
                actions.extend(orphan_actions)
                remove_size += sum(a.size or 0 for a in orphan_actions)

        return Resolution(
            success=True,
            actions=actions,
            problems=[],
            remove_size=remove_size
        )


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
