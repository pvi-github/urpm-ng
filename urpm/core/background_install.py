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

    def __init__(self):
        self.lock_fd = None
        self.locked = False

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
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

        self.lock_fd = open(LOCK_FILE, 'w')

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

    Forks a child process that:
    1. Acquires the install lock
    2. Runs the RPM transaction
    3. Signals parent when files are installed
    4. Parent exits, child continues with rpmdb sync

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
    import rpm

    if not rpm_paths:
        return True, ""

    # Check for previous background errors
    prev_error = check_background_error()
    if prev_error:
        logger.warning(f"Previous background install had an error: {prev_error}")
        # Don't block - just warn. User can investigate.
        clear_background_error()

    # Create pipe for IPC
    read_fd, write_fd = os.pipe()

    # Fork
    pid = os.fork()

    if pid > 0:
        # Parent process - read progress and display
        os.close(write_fd)
        read_file = os.fdopen(read_fd, 'r')

        success = True
        error_msg = ""

        try:
            for line in read_file:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = ProgressMessage.from_json(line)
                except:
                    continue

                if msg.msg_type == 'progress':
                    if progress_callback:
                        progress_callback(msg.name, msg.current, msg.total)
                elif msg.msg_type == 'done':
                    # Child signaled that files are installed
                    # Parent can exit now
                    break
                elif msg.msg_type == 'error':
                    success = False
                    error_msg = msg.error
                    break
                elif msg.msg_type == 'rpmdb':
                    # Show rpmdb sync message
                    if progress_callback:
                        progress_callback("(rpmdb)", msg.current, msg.total)
        finally:
            read_file.close()

        return success, error_msg

    else:
        # Child process - do the actual install
        os.close(read_fd)
        write_file = os.fdopen(write_fd, 'w', buffering=1)  # Line buffered

        # Detach from parent's process group (daemonize for rpmdb sync)
        os.setsid()

        # Acquire install lock
        lock = InstallLock()
        try:
            lock.acquire(blocking=True)
        except Exception as e:
            write_file.write(ProgressMessage(
                msg_type='error',
                error=f"Failed to acquire lock: {e}"
            ).to_json() + "\n")
            write_file.close()
            os._exit(1)

        try:
            # Set up RPM transaction
            ts = rpm.TransactionSet(root)

            if verify_signatures:
                ts.setVSFlags(0)
            else:
                ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

            # Add packages to transaction
            headers = []
            for path in rpm_paths:
                try:
                    fd = os.open(str(path), os.O_RDONLY)
                    try:
                        hdr = ts.hdrFromFdno(fd)
                        headers.append((path, hdr))
                        ts.addInstall(hdr, str(path), 'u')
                    finally:
                        os.close(fd)
                except rpm.error as e:
                    write_file.write(ProgressMessage(
                        msg_type='error',
                        error=f"{path.name}: {e}"
                    ).to_json() + "\n")
                    write_file.close()
                    lock.release()
                    os._exit(1)

            # Check dependencies
            if not force:
                unresolved = ts.check()
                if unresolved:
                    errors = [f"Dependency: {prob}" for prob in unresolved]
                    write_file.write(ProgressMessage(
                        msg_type='error',
                        error="; ".join(errors)
                    ).to_json() + "\n")
                    write_file.close()
                    lock.release()
                    os._exit(1)

            # Order transaction
            ts.order()

            if test:
                write_file.write(ProgressMessage(
                    msg_type='done',
                    current=len(rpm_paths),
                    total=len(rpm_paths)
                ).to_json() + "\n")
                write_file.close()
                lock.release()
                os._exit(0)

            # Set up callback
            total = len(rpm_paths)
            current = [0]
            open_fds = {}
            files_done = [False]

            def callback(reason, amount, total_pkg, key, client_data):
                if reason == rpm.RPMCALLBACK_INST_OPEN_FILE:
                    path = key
                    current[0] += 1

                    # Send progress to parent (if pipe still open)
                    if not files_done[0]:
                        name = Path(path).stem.rsplit('-', 2)[0] if path else ''
                        write_file.write(ProgressMessage(
                            msg_type='progress',
                            name=name,
                            current=current[0],
                            total=total
                        ).to_json() + "\n")

                    fd = os.open(path, os.O_RDONLY)
                    open_fds[path] = fd
                    return fd

                elif reason == rpm.RPMCALLBACK_INST_CLOSE_FILE:
                    path = key
                    if path in open_fds:
                        try:
                            os.close(open_fds[path])
                        except:
                            pass
                        del open_fds[path]

                    # Check if all files are done
                    if current[0] >= total and not files_done[0]:
                        files_done[0] = True
                        # Signal parent that files are installed
                        write_file.write(ProgressMessage(
                            msg_type='done',
                            current=total,
                            total=total
                        ).to_json() + "\n")
                        write_file.flush()
                        # Close pipe - parent will exit
                        write_file.close()

                elif reason == rpm.RPMCALLBACK_TRANS_STOP:
                    # Transaction complete (after rpmdb sync)
                    _log_background(f"Transaction complete: {total} packages installed")

            # Set problem filters
            prob_filter = 0
            if force:
                prob_filter |= (
                    rpm.RPMPROB_FILTER_REPLACEPKG |
                    rpm.RPMPROB_FILTER_OLDPACKAGE |
                    rpm.RPMPROB_FILTER_REPLACENEWFILES |
                    rpm.RPMPROB_FILTER_REPLACEOLDFILES
                )
            if prob_filter:
                ts.setProbFilter(prob_filter)

            # Run transaction
            _log_background(f"Starting transaction: {total} packages")
            problems = ts.run(callback, '')

            # Clean up any remaining FDs
            for fd in open_fds.values():
                try:
                    os.close(fd)
                except:
                    pass

            if problems:
                error = "; ".join(str(p) for p in problems)
                _set_background_error(f"Transaction failed: {error}")
                lock.release()
                os._exit(1)

            _log_background("Transaction completed successfully")
            lock.release()
            os._exit(0)

        except Exception as e:
            _set_background_error(f"Unexpected error: {e}")
            try:
                write_file.write(ProgressMessage(
                    msg_type='error',
                    error=str(e)
                ).to_json() + "\n")
                write_file.close()
            except:
                pass
            lock.release()
            os._exit(1)


def run_erase_background(
    package_names: List[str],
    progress_callback: Callable[[str, int, int], None] = None,
    test: bool = False,
    force: bool = False,
    root: str = "/"
) -> Tuple[bool, str]:
    """Run RPM erase transaction with background rpmdb sync.

    Same architecture as run_transaction_background but for erasing.

    Args:
        package_names: List of package names to erase
        progress_callback: Called with (name, current, total)
        test: Test mode - don't actually erase
        force: Force erase despite problems
        root: Root directory

    Returns:
        Tuple of (success, error_message)
    """
    import rpm

    if not package_names:
        return True, ""

    # Check for previous background errors
    prev_error = check_background_error()
    if prev_error:
        logger.warning(f"Previous background operation had an error: {prev_error}")
        clear_background_error()

    # Create pipe for IPC
    read_fd, write_fd = os.pipe()

    # Fork
    pid = os.fork()

    if pid > 0:
        # Parent process
        os.close(write_fd)
        read_file = os.fdopen(read_fd, 'r')

        success = True
        error_msg = ""

        try:
            for line in read_file:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = ProgressMessage.from_json(line)
                except:
                    continue

                if msg.msg_type == 'progress':
                    if progress_callback:
                        progress_callback(msg.name, msg.current, msg.total)
                elif msg.msg_type == 'done':
                    break
                elif msg.msg_type == 'error':
                    success = False
                    error_msg = msg.error
                    break
        finally:
            read_file.close()

        return success, error_msg

    else:
        # Child process
        os.close(read_fd)
        write_file = os.fdopen(write_fd, 'w', buffering=1)

        os.setsid()

        lock = InstallLock()
        try:
            lock.acquire(blocking=True)
        except Exception as e:
            write_file.write(ProgressMessage(
                msg_type='error',
                error=f"Failed to acquire lock: {e}"
            ).to_json() + "\n")
            write_file.close()
            os._exit(1)

        try:
            ts = rpm.TransactionSet(root)
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

            # Find installed packages
            found = []
            for name in package_names:
                mi = ts.dbMatch('name', name)
                for hdr in mi:
                    found.append((name, hdr))
                    ts.addErase(hdr)
                    break  # Only first match

            if not found:
                write_file.write(ProgressMessage(
                    msg_type='error',
                    error="No packages found to erase"
                ).to_json() + "\n")
                write_file.close()
                lock.release()
                os._exit(1)

            # Check dependencies
            if not force:
                unresolved = ts.check()
                if unresolved:
                    errors = [f"Dependency: {prob}" for prob in unresolved]
                    write_file.write(ProgressMessage(
                        msg_type='error',
                        error="; ".join(errors)
                    ).to_json() + "\n")
                    write_file.close()
                    lock.release()
                    os._exit(1)

            ts.order()

            if test:
                write_file.write(ProgressMessage(
                    msg_type='done',
                    current=len(found),
                    total=len(found)
                ).to_json() + "\n")
                write_file.close()
                lock.release()
                os._exit(0)

            # Callback
            total = len(found)
            current = [0]
            erase_done = [False]

            def callback(reason, amount, total_pkg, key, client_data):
                if reason == rpm.RPMCALLBACK_UNINST_START:
                    current[0] += 1
                    name = key if isinstance(key, str) else str(key)
                    write_file.write(ProgressMessage(
                        msg_type='progress',
                        name=name,
                        current=current[0],
                        total=total
                    ).to_json() + "\n")

                elif reason == rpm.RPMCALLBACK_UNINST_STOP:
                    if current[0] >= total and not erase_done[0]:
                        erase_done[0] = True
                        write_file.write(ProgressMessage(
                            msg_type='done',
                            current=total,
                            total=total
                        ).to_json() + "\n")
                        write_file.flush()
                        write_file.close()

                elif reason == rpm.RPMCALLBACK_TRANS_STOP:
                    _log_background(f"Erase complete: {total} packages removed")

            if force:
                ts.setProbFilter(rpm.RPMPROB_FILTER_REPLACEPKG)

            _log_background(f"Starting erase: {total} packages")
            problems = ts.run(callback, '')

            if problems:
                error = "; ".join(str(p) for p in problems)
                _set_background_error(f"Erase failed: {error}")
                lock.release()
                os._exit(1)

            _log_background("Erase completed successfully")
            lock.release()
            os._exit(0)

        except Exception as e:
            _set_background_error(f"Unexpected error: {e}")
            try:
                write_file.write(ProgressMessage(
                    msg_type='error',
                    error=str(e)
                ).to_json() + "\n")
                write_file.close()
            except:
                pass
            lock.release()
            os._exit(1)
