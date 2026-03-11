"""
File-based lock for media synchronisation.

Prevents concurrent ``urpm media update`` runs (CLI or daemon) from
colliding on the SQLite database.  Uses ``fcntl.flock()`` with a PID
file so that stale locks left by crashed processes are automatically
recovered.

Typical usage::

    from urpm.core.sync_lock import SyncLock

    lock = SyncLock()
    acquired, holder_pid = lock.try_acquire()
    if not acquired:
        print(f"Sync already running (PID {holder_pid})")
        return 0
    try:
        do_sync()
    finally:
        lock.release()

Or as a context manager (blocking)::

    with SyncLock():
        do_sync()
"""

import fcntl
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Lock files — under /run so they disappear on reboot.
# Separate locks allow metadata sync and files.xml sync to run in parallel.
METADATA_LOCK_PATH = Path("/run/urpm/sync-metadata.lock")
FILES_LOCK_PATH = Path("/run/urpm/sync-files.lock")


class SyncLock:
    """Process-level lock for media synchronisation.

    Attributes:
        lock_path: Path to the lock file.
        locked: ``True`` while this instance holds the lock.
    """

    def __init__(self, lock_path: Path = METADATA_LOCK_PATH):
        """Initialise the lock.

        Args:
            lock_path: Where to create the lock file.
                       Parent directories are created automatically.
        """
        self.lock_path = lock_path
        self._fd: Optional[int] = None
        self.locked = False

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def try_acquire(self) -> Tuple[bool, Optional[int]]:
        """Try to acquire the lock without blocking.

        Returns:
            ``(True, None)`` if the lock was acquired, or
            ``(False, holder_pid)`` if another live process holds it
            (``holder_pid`` may be ``None`` if the PID could not be read).
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Open (or create) the lock file.  We keep the fd open for the
        # lifetime of the lock — closing it releases flock automatically.
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock is held by someone else.
            holder = self._read_pid(fd)
            os.close(fd)

            if holder is not None and not _pid_alive(holder):
                # Holder is dead — orphan lock.  Retry once.
                logger.warning("Lock holder PID %d is dead, retaking lock", holder)
                return self._force_acquire()

            return False, holder

        # We got the lock — write our PID.
        self._write_pid(fd)
        self._fd = fd
        self.locked = True
        return True, None

    def release(self):
        """Release the lock (idempotent)."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self.locked = False

    def get_holder_pid(self) -> Optional[int]:
        """Read the PID from the lock file without acquiring the lock.

        Returns:
            PID of the current holder, or ``None`` if unreadable.
        """
        try:
            text = self.lock_path.read_text().strip()
            return int(text) if text else None
        except (OSError, ValueError):
            return None

    # -----------------------------------------------------------------
    # Context-manager (blocking acquire)
    # -----------------------------------------------------------------

    def __enter__(self) -> "SyncLock":
        acquired, holder = self.try_acquire()
        if not acquired:
            # Block until available — simple retry loop.
            import time
            while not acquired:
                logger.info("Waiting for sync lock (held by PID %s)…", holder)
                time.sleep(1)
                acquired, holder = self.try_acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _force_acquire(self) -> Tuple[bool, Optional[int]]:
        """Re-open and try to grab the lock after detecting an orphan."""
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Someone else grabbed it between our check and retry.
            holder = self._read_pid(fd)
            os.close(fd)
            return False, holder

        self._write_pid(fd)
        self._fd = fd
        self.locked = True
        return True, None

    @staticmethod
    def _read_pid(fd: int) -> Optional[int]:
        """Read PID from an already-open file descriptor."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 32).decode().strip()
            return int(data) if data else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_pid(fd: int):
        """Write current PID to an already-open file descriptor."""
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())


def _pid_alive(pid: int) -> bool:
    """Check whether *pid* refers to a running process."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we lack permission to signal it
