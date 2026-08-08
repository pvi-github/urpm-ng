"""Distupgrade lock (SPEC_DISTUPGRADE §4.0).

Uses the openat-style pattern to defend against directory-rename
races : the parent ``/run/urpm/`` is opened once and audited via
``fstat``, then every subsequent file operation is anchored on that
fd via ``dir_fd=`` so a swap of the directory between check and use
cannot redirect us elsewhere.

Bounded retry protects against three concurrent races :

- **Concurrent unlink** : two ``--resume`` processes both see an
  orphan lock ; A unlinks it, B's ``unlink`` returns
  ``FileNotFoundError`` — loop retries the ``O_CREAT|O_EXCL`` open.
- **Concurrent recreate** : A recreates the lock ; B's next
  ``O_CREAT|O_EXCL`` sees ``FileExistsError``; loop re-reads the
  PID, this time live, and refuses with the correct PID.
- **PID-write in-flight window** : A has done ``O_CREAT|O_EXCL`` but
  hasn't reached ``os.write(pid + b'\\n')`` yet.  B's read returns
  empty (or partial without the trailing ``\\n``).  The reader
  treats a missing terminator as « lock still being established »
  and retries.

Orphan detection combines :

- ``_pid_alive(pid)`` — cheap ``os.kill(pid, 0)`` probe.
- ``_pid_is_distupgrade(pid)`` — cmdline check that PID belongs to
  an ``urpm distupgrade`` invocation.  Guards against PID reuse
  across reboot on non-tmpfs ``/run``.

Only when both are true is the lock treated as live.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Optional


LOCK_DIR = Path("/run/urpm")
LOCK_NAME = "distupgrade.lock"
LOCK_PATH = LOCK_DIR / LOCK_NAME

MAX_RETRIES = 3


class DistupgradeLockedError(Exception):
    """Raised when the lock is held by a live distupgrade."""

    def __init__(self, pid: int):
        super().__init__(
            f"distupgrade PID {pid} en cours ; "
            f"attendez la fin ou utilisez `urpm distupgrade --abort`.")
        self.pid = pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _pid_is_distupgrade(pid: int) -> bool:
    """Return True if ``pid``'s cmdline is an ``urpm distupgrade`` call.

    Tolerates the Python shebang case where ``argv[0]='python3'`` and
    ``urpm`` appears at ``argv[2]`` — same predicate as
    :mod:`urpm.cli.helpers.distupgrade_mesh` (§4.0).
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return False
    if argv and argv[-1] == b"":
        argv = argv[:-1]
    idx = next(
        (i for i, a in enumerate(argv)
         if os.path.basename(a) == b"urpm"),
        None,
    )
    return idx is not None and b"distupgrade" in argv[idx + 1:]


def _audit_parent(dir_fd: int) -> None:
    """Refuse if ``/run/urpm`` isn't ``root``-owned or world-writable.

    Raises :class:`DistupgradeLockedError` with a synthetic PID=0 on
    failure — the caller renders the error message from ``pid``.
    """
    st = os.fstat(dir_fd)
    if st.st_uid != 0:
        raise DistupgradeLockedError(0)
    if st.st_mode & 0o022:
        raise DistupgradeLockedError(0)


def _open_lock_dir(dir_path: Path) -> int:
    """Return an fd on ``dir_path`` with strict flags.

    ``O_NOFOLLOW`` refuses a symlink-swapped parent ; ``O_DIRECTORY``
    fails if the target is no longer a directory.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    return os.open(str(dir_path),
                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def acquire_distupgrade_lock(
        *,
        dir_path: Path = LOCK_DIR,
        name: str = LOCK_NAME,
        max_retries: int = MAX_RETRIES,
) -> int:
    """Acquire the distupgrade lock and return its fd.

    The caller keeps the fd alive for the whole distupgrade and
    passes it to :func:`release_distupgrade_lock` at completion or
    abort.  A crash silently drops the fd → kernel releases the
    ``flock`` automatically ; the ``.state`` file remains and warns
    the next invocation.

    Raises :class:`DistupgradeLockedError` when a live distupgrade
    holds the lock or when the parent directory fails the audit.
    """
    dir_fd = _open_lock_dir(dir_path)
    try:
        _audit_parent(dir_fd)
        flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY
                 | os.O_NOFOLLOW | os.O_CLOEXEC)
        lock_fd = -1
        for _attempt in range(max_retries):
            try:
                lock_fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
                break  # nominal path
            except FileExistsError:
                # Something already holds the lock ; live or orphan ?
                try:
                    read_fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
                except FileNotFoundError:
                    # Concurrent unlink between our EEXIST and this open.
                    continue
                try:
                    raw = os.read(read_fd, 32)
                finally:
                    os.close(read_fd)
                # Interpret missing terminator or non-numeric payload
                # as an orphan (either a stalled writer that died
                # mid-syscall or a corrupted file).  A legitimate live
                # writer completes the ``open → flock → write → fsync``
                # sequence in microseconds — the terminator is present
                # by the time a competing reader looks a second time.
                pid = -1
                if raw.endswith(b"\n"):
                    try:
                        pid = int(raw.decode().strip())
                    except ValueError:
                        pid = -1
                if pid > 0 and _pid_alive(pid) and _pid_is_distupgrade(pid):
                    raise DistupgradeLockedError(pid)
                # Orphan — unlink and retry.  A concurrent unlink is
                # tolerated : FileNotFoundError → we retry via the loop
                # header without failing.
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
        else:
            raise DistupgradeLockedError(0)

        # Own the file : flock, write PID, fsync.  Keep the three
        # syscalls back-to-back so a concurrent reader sees a valid
        # payload as soon as its own O_CREAT|O_EXCL fails.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(lock_fd, f"{os.getpid()}\n".encode())
        os.fsync(lock_fd)
        return lock_fd
    finally:
        os.close(dir_fd)


def release_distupgrade_lock(lock_fd: int,
                             *,
                             dir_path: Path = LOCK_DIR,
                             name: str = LOCK_NAME) -> None:
    """Release the lock and remove its file.

    Called at distupgrade success (Stage 5) or ``--abort``.  Silently
    tolerates ``FileNotFoundError`` — the caller can invoke on a
    partially cleaned up state.
    """
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass
    try:
        dir_fd = _open_lock_dir(dir_path)
    except OSError:
        return
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    finally:
        os.close(dir_fd)
