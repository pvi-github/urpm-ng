"""
RPM installation module

Handles package installation using python3-rpm bindings.
"""

import logging
import os
import rpm
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Callable, Optional, Tuple

logger = logging.getLogger(__name__)


def wait_rpm_children(timeout: int = 120):
    """Wait for all child processes to finish.

    RPM scriptlets and file triggers may fork child processes.
    Call this after installation when you need to ensure everything
    is complete before continuing (e.g., before deleting a chroot).

    Args:
        timeout: Maximum seconds to wait
    """
    start = time.time()

    while time.time() - start < timeout:
        try:
            # Wait for any child process, non-blocking
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                # No children ready right now
                time.sleep(0.1)
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    # Still no children, we're done
                    break
        except ChildProcessError:
            # No child processes - we're done
            break

    # Final sync to ensure all writes are flushed
    os.sync()


@dataclass
class InstallResult:
    """Result of an install or upgrade operation.

    ``success`` reflects the outcome of the underlying rpm transaction.
    It is **not** a "at least some packages installed" predicate:
    partial failure is signalled by ``success=False`` together with
    ``installed > 0`` and a non-empty ``errors`` list.

    Concretely:

    * **No-op** — nothing to install, queue empty → ``success=True``,
      ``installed=0``.
    * **Full success** — the transaction ran cleanly → ``success=True``,
      ``installed`` = packages processed.
    * **Reduced success** — some packages excluded by the pre-verify /
      retry pipeline, but the reduced transaction succeeded →
      ``success=True``, ``reduced_transaction=True``,
      ``excluded_packages`` populated.
    * **Partial failure** — the transaction ran but some operations
      failed → ``success=False``, ``installed`` may still be ``> 0``,
      ``errors`` describes per-operation failures.
    * **Total failure** — every candidate package failed pre-verification
      and no replacement could be fetched → ``success=False``,
      ``installed=0``, ``errors=['All packages failed verification']``.

    The lower-level :class:`Installer` only fills ``success``,
    ``installed`` and ``errors``.  The resilient pipeline in
    :mod:`urpm.core.resilient_install` additionally populates
    ``excluded_packages``, ``reduced_transaction`` and ``queue_result``.
    """
    success: bool
    installed: int = 0
    errors: List[str] = field(default_factory=list)
    excluded_packages: List[Tuple[str, str]] = field(default_factory=list)
    reduced_transaction: bool = False
    queue_result: Optional[object] = None


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

        errors = []
        headers = []
        signature_errors = []

        # ── Pass 1: signature verification (when enabled) ──
        # Run as a separate ``hdrFromFdno`` with full VSFlags so we
        # can attribute failures unambiguously to sig/digest issues.
        # The earlier ``"signature" in err_str or "key" in err_str``
        # substring check on the rpm error message was fragile to
        # librpm wording / translation changes.
        sig_passed: List[Path] = []
        if verify_signatures:
            from .download import verify_rpm_signature
            for path in rpm_paths:
                ok, sig_err = verify_rpm_signature(path)
                if ok:
                    sig_passed.append(path)
                else:
                    signature_errors.append(path.name)
                    errors.append(
                        f"{path.name}: signature verification failed - "
                        f"{sig_err}"
                    )
            if signature_errors:
                errors.append(
                    "Use --nosignature to skip signature verification "
                    "(not recommended)"
                )
                return InstallResult(success=False, errors=errors)
        else:
            sig_passed = list(rpm_paths)

        # ── Pass 2: read headers and add to transaction ──
        # Signatures are already settled (verified in pass 1, or
        # explicitly skipped by the caller).  Disable rpm-level sig
        # checks here so any error this pass raises is genuinely
        # structural (truncated header, malformed RPM, …) — never a
        # sig issue we would have to disambiguate by string-match.
        ts = rpm.TransactionSet(self.root or '/')
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        for path in sig_passed:
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
                errors.append(f"{path.name}: {e}")

        if errors:
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

        ts = rpm.TransactionSet(self.root or '/')

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
