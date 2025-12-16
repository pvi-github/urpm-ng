"""
RPM installation module

Handles package installation using python3-rpm bindings.
"""

import os
import rpm
from dataclasses import dataclass
from pathlib import Path
from typing import List, Callable, Optional


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
                verify_signatures: bool = True) -> InstallResult:
        """Install RPM packages.

        Args:
            rpm_paths: List of paths to RPM files
            progress_callback: Optional callback(name, current, total)
            test: If True, only check, don't install
            verify_signatures: If True, verify GPG signatures (default: True)

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

        # Check dependencies
        unresolved = ts.check()
        if unresolved:
            for prob in unresolved:
                errors.append(f"Dependency problem: {prob}")
            return InstallResult(success=False, errors=errors)

        # Order the transaction
        ts.order()

        if test:
            return InstallResult(success=True, installed=len(rpm_paths))

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
              test: bool = False) -> EraseResult:
        """Erase (remove) installed packages.

        Args:
            package_names: List of package names to erase
            progress_callback: Optional callback(name, current, total)
            test: If True, only check, don't erase

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

        # Check dependencies (what would break)
        unresolved = ts.check()
        if unresolved:
            for prob in unresolved:
                errors.append(f"Dependency problem: {prob}")
            return EraseResult(success=False, errors=errors)

        # Order the transaction
        ts.order()

        if test:
            return EraseResult(success=True, erased=found)

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
