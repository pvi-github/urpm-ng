"""
Background RPM installation module

Forks a child process to handle RPM transactions, allowing the parent
to return control to the user while the slow rpmdb sync happens in background.

Architecture:
    urpm (parent)                    child process
        │                                │
        ├── fork() ──────────────────────┤
        │                                │
        │   reads progress via pipe      ts.run()
        │   displays progress            ├── install files
        │   ...                          ├── run scriptlets
        │   "done" signal received       ├── (signals parent: done)
        │   exit(0)                      └── sync rpmdb (slow)
        │                                │
        └────────────────────────────────┘
                                         exit(0)
"""

import fcntl
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lock file for preventing concurrent installs
LOCK_FILE = Path("/var/lib/rpm/.urpm-install.lock")
# Error flag file for background failures
ERROR_FLAG = Path("/var/lib/rpm/.urpm-background-error")
# Log file for background operations
BACKGROUND_LOG = Path("/var/log/urpm-background.log")


@dataclass
class ProgressMessage:
    """Message sent from child to parent via pipe."""
    msg_type: str  # 'progress', 'done', 'error'
    name: str = ""
    current: int = 0
    total: int = 0
    error: str = ""

    def to_json(self) -> str:
        return json.dumps({
            'type': self.msg_type,
            'name': self.name,
            'current': self.current,
            'total': self.total,
            'error': self.error
        })

    @classmethod
    def from_json(cls, data: str) -> 'ProgressMessage':
        d = json.loads(data)
        return cls(
            msg_type=d['type'],
            name=d.get('name', ''),
            current=d.get('current', 0),
            total=d.get('total', 0),
            error=d.get('error', '')
        )


class InstallLock:
    """Manages the install lock file."""

    def __init__(self, root: str = None):
        """Initialize lock with optional root path.

        Args:
            root: If set, use this as root for lock file path (for chroot installs).
        """
        self.lock_fd = None
        self.locked = False
        if root:
            self.lock_file = Path(root) / "var/lib/rpm/.urpm-install.lock"
        else:
            self.lock_file = LOCK_FILE

    def acquire(self, blocking: bool = True,
                wait_callback: Callable[[int], None] = None) -> bool:
        """Acquire the install lock.

        Args:
            blocking: If True, wait for lock. If False, return immediately.
            wait_callback: Called with PID of holder while waiting.

        Returns:
            True if lock acquired, False if non-blocking and lock held.
        """
        # Create lock file if needed
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        self.lock_fd = open(self.lock_file, 'w')

        while True:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Got the lock - write our PID
                self.lock_fd.truncate(0)
                self.lock_fd.seek(0)
                self.lock_fd.write(str(os.getpid()))
                self.lock_fd.flush()
                self.locked = True
                return True
            except BlockingIOError:
                if not blocking:
                    self.lock_fd.close()
                    self.lock_fd = None
                    return False

                # Lock is held - get holder PID
                holder_pid = self._get_holder_pid()
                if wait_callback and holder_pid:
                    wait_callback(holder_pid)

                # Check if holder is still alive
                if holder_pid and not self._pid_exists(holder_pid):
                    # Holder died - try to steal lock
                    logger.warning(f"Lock holder PID {holder_pid} is dead, stealing lock")
                    continue

                time.sleep(0.5)

    def release(self):
        """Release the install lock."""
        if self.lock_fd:
            if self.locked:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                except:
                    pass
            self.lock_fd.close()
            self.lock_fd = None
            self.locked = False

    def _get_holder_pid(self) -> Optional[int]:
        """Get PID of current lock holder."""
        try:
            with open(LOCK_FILE, 'r') as f:
                return int(f.read().strip())
        except:
            return None

    def _pid_exists(self, pid: int) -> bool:
        """Check if a process exists."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Process exists but we can't signal it

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def check_background_error() -> Optional[str]:
    """Check if a previous background install failed.

    Returns:
        Error message if there was a failure, None otherwise.
    """
    if ERROR_FLAG.exists():
        try:
            error = ERROR_FLAG.read_text().strip()
            return error
        except:
            return "Unknown background install error"
    return None


def clear_background_error():
    """Clear the background error flag."""
    try:
        ERROR_FLAG.unlink(missing_ok=True)
    except:
        pass


def _log_background(message: str, level: str = "INFO"):
    """Log a message to the background log file."""
    try:
        BACKGROUND_LOG.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(BACKGROUND_LOG, 'a') as f:
            f.write(f"{timestamp} [{level}] {message}\n")
    except:
        pass


def _set_background_error(error: str):
    """Set the background error flag."""
    try:
        ERROR_FLAG.write_text(f"{error}\n")
        _log_background(f"ERROR: {error}", "ERROR")
    except:
        pass


def run_transaction_background(
    rpm_paths: List[Path],
    progress_callback: Callable[[str, int, int], None] = None,
    verify_signatures: bool = True,
    force: bool = False,
    test: bool = False,
    root: str = "/"
) -> Tuple[bool, str]:
    """Run RPM transaction with background rpmdb sync.

    DEPRECATED: Use TransactionQueue for new code. This function is kept
    for backward compatibility and now delegates to TransactionQueue.

    Args:
        rpm_paths: List of RPM file paths to install
        progress_callback: Called with (name, current, total)
        verify_signatures: Whether to verify GPG signatures
        force: Force install despite problems
        test: Test mode - don't actually install
        root: Root directory for installation

    Returns:
        Tuple of (success, error_message)
    """
    from .transaction_queue import TransactionQueue

    if not rpm_paths:
        return True, ""

    queue = TransactionQueue(root=root)
    queue.add_install(
        rpm_paths,
        operation_id="install",
        verify_signatures=verify_signatures,
        force=force,
        test=test
    )

    # Wrap the callback to match the old signature
    def wrapped_callback(op_id: str, name: str, current: int, total: int):
        if progress_callback:
            progress_callback(name, current, total)

    result = queue.execute(progress_callback=wrapped_callback)

    if result.operations:
        op = result.operations[0]
        if op.success:
            return True, ""
        else:
            return False, "; ".join(op.errors) if op.errors else "Unknown error"

    if result.overall_error:
        return False, result.overall_error

    return result.success, ""


def run_erase_background(
    package_names: List[str],
    progress_callback: Callable[[str, int, int], None] = None,
    test: bool = False,
    force: bool = False,
    root: str = "/"
) -> Tuple[bool, str]:
    """Run RPM erase transaction with background rpmdb sync.

    DEPRECATED: Use TransactionQueue for new code. This function is kept
    for backward compatibility and now delegates to TransactionQueue.

    Args:
        package_names: List of package names to erase
        progress_callback: Called with (name, current, total)
        test: Test mode - don't actually erase
        force: Force erase despite problems
        root: Root directory

    Returns:
        Tuple of (success, error_message)
    """
    from .transaction_queue import TransactionQueue

    if not package_names:
        return True, ""

    queue = TransactionQueue(root=root)
    queue.add_erase(
        package_names,
        operation_id="erase",
        force=force,
        test=test
    )

    # Wrap the callback to match the old signature
    def wrapped_callback(op_id: str, name: str, current: int, total: int):
        if progress_callback:
            progress_callback(name, current, total)

    result = queue.execute(progress_callback=wrapped_callback)

    if result.operations:
        op = result.operations[0]
        if op.success:
            return True, ""
        else:
            return False, "; ".join(op.errors) if op.errors else "Unknown error"

    if result.overall_error:
        return False, result.overall_error

    return result.success, ""
