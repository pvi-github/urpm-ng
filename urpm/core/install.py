"""
RPM installation module

Handles package installation using python3-rpm bindings.
"""

import logging
import os
import rpm
from dataclasses import dataclass
from pathlib import Path
from typing import List, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class InstallResult:
    """Result of an installation."""
    success: bool
    installed: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


@dataclass
class EraseResult:
    """Result of an erase operation."""
    success: bool
    erased: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class Installer:
    """RPM package installer."""

    def __init__(self, root: str = "/"):
        """Initialize installer.

        Args:
            root: Installation root (default: /)
        """
        self.root = root

    def install(self, rpm_paths: List[Path],
                progress_callback: Callable[[str, int, int], None] = None,
                test: bool = False,
                verify_signatures: bool = True,
                force: bool = False,
                reinstall: bool = False) -> InstallResult:
        """Install RPM packages.

        Args:
            rpm_paths: List of paths to RPM files
            progress_callback: Optional callback(name, current, total)
            test: If True, only check, don't install
            verify_signatures: If True, verify GPG signatures (default: True)
            force: If True, ignore dependency problems and conflicts
            reinstall: If True, reinstall already installed packages

        Returns:
            InstallResult with status
        """
        if not rpm_paths:
            return InstallResult(success=True, installed=0)

        ts = rpm.TransactionSet(self.root)

        if verify_signatures:
            # Verify all signatures and digests
            ts.setVSFlags(0)
        else:
            # Skip signature verification (--nosignature)
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        errors = []
        headers = []

        # Add packages to transaction
        signature_errors = []
        for path in rpm_paths:
            try:
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    hdr = ts.hdrFromFdno(fd)
                    headers.append((path, hdr))
                    # 'u' = upgrade mode (install or upgrade)
                    ts.addInstall(hdr, str(path), 'u')
                finally:
                    os.close(fd)
            except rpm.error as e:
                err_str = str(e).lower()
                if 'signature' in err_str or 'key' in err_str or 'gpg' in err_str:
                    signature_errors.append(path.name)
                    errors.append(f"{path.name}: signature verification failed - {e}")
                else:
                    errors.append(f"{path.name}: {e}")

        if errors:
            if signature_errors and verify_signatures:
                errors.append("Use --nosignature to skip signature verification (not recommended)")
            return InstallResult(success=False, errors=errors)

        # Check dependencies (skip if force)
        if not force:
            unresolved = ts.check()
            if unresolved:
                for prob in unresolved:
                    errors.append(f"Dependency problem: {prob}")
                return InstallResult(success=False, errors=errors)

        # Order the transaction
        ts.order()

        if test:
            return InstallResult(success=True, installed=len(rpm_paths))

        # Set problem filters for force/reinstall mode
        prob_filter = 0
        if force:
            prob_filter |= (
                rpm.RPMPROB_FILTER_REPLACEPKG |
                rpm.RPMPROB_FILTER_OLDPACKAGE |
                rpm.RPMPROB_FILTER_REPLACENEWFILES |
                rpm.RPMPROB_FILTER_REPLACEOLDFILES
            )
        if reinstall:
            prob_filter |= rpm.RPMPROB_FILTER_REPLACEPKG
        if prob_filter:
            ts.setProbFilter(prob_filter)

        # Prepare callback
        total = len(rpm_paths)
        current = [0]  # Use list for closure
        open_fds = {}  # Track open file descriptors by path
        seen_paths = set()  # Track which packages we've already counted

        def callback(reason, amount, total_pkg, key, client_data):
            if reason == rpm.RPMCALLBACK_INST_OPEN_FILE:
                path = key
                # Only count each package once (OPEN_FILE can be called multiple times)
                if path not in seen_paths:
                    seen_paths.add(path)
                    current[0] += 1
                    if progress_callback:
                        name = Path(path).stem.rsplit('-', 2)[0] if path else ''
                        progress_callback(name, current[0], total)
                fd = os.open(path, os.O_RDONLY)
                open_fds[path] = fd
                return fd
            elif reason == rpm.RPMCALLBACK_TRANS_STOP:
                # Transaction finished, RPM is updating database
                if progress_callback and current[0] == total:
                    progress_callback("(updating rpmdb)", total, total)
            elif reason == rpm.RPMCALLBACK_TRANS_PROGRESS:
                # Progress during transaction (db updates)
                if progress_callback and current[0] == total:
                    progress_callback("(rpmdb progress)", total, total)
            elif reason == rpm.RPMCALLBACK_INST_CLOSE_FILE:
                # Close the file descriptor
                path = key
                if path in open_fds:
                    try:
                        os.close(open_fds[path])
                    except OSError:
                        pass
                    del open_fds[path]

        # Run transaction
        try:
            problems = ts.run(callback, '')
        finally:
            # Cleanup any remaining open file descriptors
            for fd in open_fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            open_fds.clear()

        if problems:
            for prob in problems:
                errors.append(str(prob))
            return InstallResult(success=False, installed=current[0], errors=errors)

        return InstallResult(success=True, installed=current[0])

    def _find_sccs(self, graph: dict) -> List[List[str]]:
        """Find strongly connected components using Tarjan's algorithm.

        Returns list of SCCs, each SCC is a list of package names.
        SCCs are returned in reverse topological order (dependencies last).
        """
        index_counter = [0]
        stack = []
        lowlinks = {}
        index = {}
        on_stack = {}
        sccs = []

        def strongconnect(node):
            index[node] = index_counter[0]
            lowlinks[node] = index_counter[0]
            index_counter[0] += 1
            stack.append(node)
            on_stack[node] = True

            # Sort successors for deterministic order
            for successor in sorted(graph.get(node, [])):
                if successor not in index:
                    strongconnect(successor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[successor])
                elif on_stack.get(successor, False):
                    lowlinks[node] = min(lowlinks[node], index[successor])

            if lowlinks[node] == index[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(scc)

        # Sort nodes for deterministic order
        for node in sorted(graph.keys()):
            if node not in index:
                strongconnect(node)

        return sccs

    def _sort_by_dependencies(self, rpm_paths: List[Path]) -> List[Path]:
        """Sort RPM paths by dependencies (dependencies first).

        Uses RPM headers to build a dependency graph. Handles circular
        dependencies by finding strongly connected components (SCCs)
        and keeping them together.
        """
        if len(rpm_paths) <= 1:
            return rpm_paths

        # Read headers and build provides/requires maps
        path_to_name = {}  # path -> package name
        name_to_path = {}  # package name -> path
        provides = {}  # capability -> package name
        requires = {}  # package name -> set of required capabilities

        ts = rpm.TransactionSet(self.root)
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)  # Skip sig check for sorting

        for path in rpm_paths:
            try:
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    hdr = ts.hdrFromFdno(fd)
                    name = hdr[rpm.RPMTAG_NAME]
                    path_to_name[path] = name
                    name_to_path[name] = path

                    # Package provides itself
                    provides[name] = name

                    # Get explicit provides
                    pkg_provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
                    for prov in pkg_provides:
                        provides[prov] = name

                    # Get requires
                    pkg_requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
                    requires[name] = set(pkg_requires)
                finally:
                    os.close(fd)
            except rpm.error:
                # If we can't read header, keep original position
                continue

        # Build dependency graph (only for packages in our set)
        graph = {name: set() for name in name_to_path}
        for name, reqs in requires.items():
            for req in reqs:
                provider = provides.get(req)
                if provider and provider in graph and provider != name:
                    graph[name].add(provider)

        # Find strongly connected components (handles cycles)
        sccs = self._find_sccs(graph)
        # Tarjan outputs SCCs with dependencies first, no reverse needed

        # Flatten SCCs to get sorted package names
        sorted_names = []
        for scc in sccs:
            # Sort within SCC for determinism
            scc.sort()
            sorted_names.extend(scc)

        # Convert back to paths
        sorted_paths = []
        for name in sorted_names:
            if name in name_to_path:
                sorted_paths.append(name_to_path[name])

        # Add any paths we couldn't process
        processed = set(sorted_paths)
        for path in rpm_paths:
            if path not in processed:
                sorted_paths.append(path)

        return sorted_paths

    def _build_dependency_graph(self, rpm_paths: List[Path], debug: bool = False) -> tuple:
        """Build dependency graph from RPM files.

        Maps ALL provides (including virtual provides) to their provider package.

        Returns:
            Tuple of (graph, name_to_path, path_to_name)
            graph: dict mapping package name to set of dependency names
        """
        path_to_name = {}
        name_to_path = {}
        provides = {}  # capability -> provider package name
        requires = {}  # package name -> set of required capabilities

        ts = rpm.TransactionSet(self.root)
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        for path in rpm_paths:
            try:
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    hdr = ts.hdrFromFdno(fd)
                    name = hdr[rpm.RPMTAG_NAME]
                    path_to_name[path] = name
                    name_to_path[name] = path

                    # Package provides itself
                    provides[name] = name

                    # Get ALL explicit provides (includes virtual provides)
                    pkg_provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
                    for prov in pkg_provides:
                        # Don't overwrite if already provided by another package
                        # (first provider wins - deterministic based on processing order)
                        if prov not in provides:
                            provides[prov] = name
                        elif debug:
                            logger.debug(f"Provide '{prov}' already mapped to {provides[prov]}, "
                                        f"ignoring {name}")

                    requires[name] = set(hdr[rpm.RPMTAG_REQUIRENAME] or [])

                    if debug:
                        logger.debug(f"Package {name}: {len(pkg_provides)} provides, "
                                    f"{len(requires[name])} requires")
                finally:
                    os.close(fd)
            except rpm.error as e:
                logger.warning(f"Failed to read {path}: {e}")
                continue

        # Build dependency graph: edges point from package to its dependencies
        graph = {name: set() for name in name_to_path}
        unresolved_count = 0

        for name, reqs in requires.items():
            for req in reqs:
                provider = provides.get(req)
                if provider and provider in graph and provider != name:
                    graph[name].add(provider)
                elif debug and not req.startswith('rpmlib(') and not req.startswith('/'):
                    # Log unresolved deps (ignore rpmlib features and file deps)
                    if provider is None:
                        unresolved_count += 1
                    elif provider not in graph:
                        logger.debug(f"{name} requires '{req}' -> {provider} (not in install set)")

        if debug:
            total_edges = sum(len(deps) for deps in graph.values())
            logger.debug(f"Dependency graph: {len(graph)} packages, {total_edges} edges, "
                        f"{unresolved_count} unresolved deps (external)")

        return graph, name_to_path, path_to_name

    def install_batched(self, rpm_paths: List[Path],
                         batch_size: int = 50,
                         progress_callback: Callable[[str, int, int], None] = None,
                         test: bool = False,
                         verify_signatures: bool = True,
                         force: bool = False,
                         reinstall: bool = False) -> InstallResult:
        """Install RPM packages in a single transaction.

        Note: batch_size parameter is kept for API compatibility but ignored.
        All packages are now installed in one transaction - the slow rpmdb sync
        will be handled in background by the caller.

        Args:
            rpm_paths: List of paths to RPM files
            batch_size: Ignored (kept for API compatibility)
            progress_callback: Optional callback(name, current, total)
            test: If True, only check, don't install
            verify_signatures: If True, verify GPG signatures (default: True)
            force: If True, ignore dependency problems and conflicts
            reinstall: If True, reinstall already installed packages

        Returns:
            InstallResult with status
        """
        # Simply delegate to install() - single transaction, no batching
        return self.install(rpm_paths, progress_callback, test, verify_signatures, force, reinstall)

    def erase(self, package_names: List[str],
              progress_callback: Callable[[str, int, int], None] = None,
              test: bool = False,
              force: bool = False) -> EraseResult:
        """Erase (remove) installed packages.

        Args:
            package_names: List of package names to erase
            progress_callback: Optional callback(name, current, total)
            test: If True, only check, don't erase
            force: If True, ignore dependency problems

        Returns:
            EraseResult with status
        """
        if not package_names:
            return EraseResult(success=True, erased=0)

        ts = rpm.TransactionSet(self.root)

        errors = []
        total = len(package_names)
        found = 0

        # Add packages to erase
        for name in package_names:
            # Find the package in rpmdb
            matches = list(ts.dbMatch('name', name))
            if not matches:
                errors.append(f"Package not installed: {name}")
                continue

            for hdr in matches:
                ts.addErase(hdr)
                found += 1

        if errors and found == 0:
            return EraseResult(success=False, errors=errors)

        # Check dependencies (skip if force)
        if not force:
            unresolved = ts.check()
            if unresolved:
                for prob in unresolved:
                    errors.append(f"Dependency problem: {prob}")
                return EraseResult(success=False, errors=errors)

        # Order the transaction
        ts.order()

        if test:
            return EraseResult(success=True, erased=found)

        # Set problem filters for force mode
        if force:
            ts.setProbFilter(rpm.RPMPROB_FILTER_REPLACEPKG)

        # Progress tracking
        current = [0]
        seen = set()
        rpmdb_notified = [False]

        def callback(reason, amount, total_pkg, key, client_data):
            if reason == rpm.RPMCALLBACK_UNINST_START:
                name = key
                if name and name not in seen:
                    seen.add(name)
                    current[0] += 1
                    if progress_callback:
                        progress_callback(name, current[0], found)
            elif reason == rpm.RPMCALLBACK_UNINST_STOP:
                # After last package is uninstalled, RPM updates database
                if current[0] == found and not rpmdb_notified[0]:
                    rpmdb_notified[0] = True
                    if progress_callback:
                        progress_callback("(updating rpmdb)", found, found)

        # Run transaction
        problems = ts.run(callback, '')

        if problems:
            for prob in problems:
                errors.append(str(prob))
            return EraseResult(success=False, erased=current[0], errors=errors)

        return EraseResult(success=True, erased=current[0])

    def erase_batched(self, package_names: List[str],
                      batch_size: int = 50,
                      progress_callback: Callable[[str, int, int], None] = None,
                      test: bool = False,
                      force: bool = False) -> EraseResult:
        """Erase packages in a single transaction.

        Note: batch_size parameter is kept for API compatibility but ignored.
        All packages are now erased in one transaction - the slow rpmdb sync
        will be handled in background by the caller.

        Args:
            package_names: List of package names to erase
            batch_size: Ignored (kept for API compatibility)
            progress_callback: Optional callback(name, current, total)
            test: If True, only check, don't erase
            force: If True, ignore dependency problems

        Returns:
            EraseResult with status
        """
        # Simply delegate to erase() - single transaction, no batching
        return self.erase(package_names, progress_callback, test, force)


def check_rpm_available() -> bool:
    """Check if rpm module is available."""
    try:
        import rpm
        return True
    except ImportError:
        return False


def check_root() -> bool:
    """Check if running as root."""
    return os.geteuid() == 0
