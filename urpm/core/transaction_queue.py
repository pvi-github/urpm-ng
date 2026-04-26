"""
Transaction Queue for RPM operations.

Provides a queue system for executing RPM operations (install, erase)
sequentially in a forked child process, with two execution modes:

**Sync mode** (default for ``urpm install``):
    Parent waits for full ts.run() completion via pipe.  Progress is
    displayed in real-time.  README shown in pager after completion.

**Async mode** (default for ``urpm upgrade``, ``urpm remove``):
    Parent returns immediately after fork.  Child writes progress to
    ``/run/urpm/transaction.json``.  User queries with ``urpm progress``.
    README stored in DB, read later with ``urpm readme``.

Architecture (sync)::

    urpm (parent)                    child process
        |-- fork() -------------------->|
        | reads progress via pipe        | acquire lock
        |   progress / op_done           | for op in queue:
        |   queue_done                   |   ts.run() with callbacks
        |<-------------------------------|   send progress/results
        | waitpid()                      | store README in DB
                                         | exit(0)

Architecture (async)::

    urpm (parent)                    child process
        |-- fork() -------------------->|
        | close pipe, return             | acquire lock
        | (prompt returns)               | redirect stdout/stderr
                                         | for op in queue:
                                         |   ts.run()
                                         |   write transaction.json
                                         | notify-send
                                         | exit(0)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from .background_install import (
    InstallLock,
    check_background_error,
    clear_background_error,
    _log_background,
    _set_background_error,
)

DEBUG_EXECINSTALL = False
DEBUG_TSRUN = False
DEBUG_USERNS = False

# Per-process debug file handle (opened once, shared across callbacks)
_debug_file = None


def set_tsrun_debug(enabled: bool = False):
    """Enable/disable ts.run() callback debug logging.

    Activated via ``--debug tsrun`` or ``--debug all``.
    Logs every RPM callback (except INST_PROGRESS flood) with full
    state to ``/tmp/debug-urpm-<pid>.txt``.
    """
    global DEBUG_TSRUN, DEBUG_EXECINSTALL
    DEBUG_TSRUN = enabled
    if enabled:
        DEBUG_EXECINSTALL = True


def _debug_write(message: str):
    """Write a timestamped line to the per-process debug file.

    The file is created lazily on first call at
    ``/tmp/debug-urpm-<pid>.txt``.  Writes are flushed immediately
    so the file is readable even if the process crashes.
    """
    global _debug_file
    if _debug_file is None:
        _debug_file = open(f"/tmp/debug-urpm-{os.getpid()}.txt", 'a')
    import time
    _debug_file.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    _debug_file.flush()

logger = logging.getLogger(__name__)

# Async progress file path
TRANSACTION_PROGRESS_DIR = Path("/run/urpm")
TRANSACTION_PROGRESS_FILE = TRANSACTION_PROGRESS_DIR / "transaction.json"


def _clean_script_key(key) -> str:
    """Extract a clean package name from an RPM callback key.

    RPM passes the package name for repo packages (e.g. ``shared-mime-info``)
    but the full file path for local RPMs (e.g.
    ``/tmp/urpm-ng-core-0.6.19-1.mga10.x86_64.rpm``).  Strip the directory
    and ``.rpm`` suffix to get a usable display name.
    """
    name = key if isinstance(key, str) else str(key or '')
    if '/' in name:
        name = name.rsplit('/', 1)[-1]
    if name.endswith('.rpm'):
        name = name[:-4]
    return name


def _init_async_progress(meta: dict = None) -> dict:
    """Create the initial async progress file.

    Returns a mutable state dict used by _update_async_progress().
    """
    import time as _time
    meta = meta or {}
    state = {
        'transaction_id': meta.get('transaction_id'),
        'type': meta.get('type', 'unknown'),
        'total': meta.get('total', 0),
        'current': 0,
        'current_package': '',
        'phase': 'verify',
        'script': '',
        'bytes_done': 0,
        'bytes_total': 0,
        'started_at': int(_time.time()),
        'pid': os.getpid(),
        'has_readmes': meta.get('has_readmes', False),
        'error': None,
    }
    _write_progress_file(state)
    return state


def _update_async_progress(state: dict, **kwargs):
    """Update the async progress file atomically."""
    state.update(kwargs)
    _write_progress_file(state)


def _write_progress_file(state: dict):
    """Write progress state to /run/urpm/transaction.json atomically."""
    try:
        TRANSACTION_PROGRESS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = TRANSACTION_PROGRESS_FILE.with_suffix('.tmp')
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, json.dumps(state).encode())
        os.close(fd)
        tmp.rename(TRANSACTION_PROGRESS_FILE)
    except OSError:
        pass  # Non-critical — progress display is best-effort


def _cleanup_async_progress():
    """Remove the async progress file after transaction completes."""
    try:
        TRANSACTION_PROGRESS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _send_desktop_notification(total: int, meta: dict = None):
    """Send a desktop notification via D-Bus (best-effort, no dependency).

    Uses the freedesktop.org Notifications spec via subprocess to avoid
    any hard dependency on dbus-python or gi.
    """
    import shutil
    if not shutil.which('notify-send'):
        return
    try:
        import subprocess
        op_type = (meta or {}).get('type', 'transaction')
        summary = f"urpm: {op_type} terminé"
        body = f"{total} paquet(s) traité(s)."
        subprocess.Popen(
            ['notify-send', '-a', 'urpm', '-i', 'system-software-install',
             summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _list_rpmnew_files(root: str = "/") -> set:
    """List all .rpmnew files in /etc under the given root.

    Used to detect which .rpmnew files were created during a transaction
    by comparing before/after sets.
    """
    etc_path = Path(root) / "etc"
    if not etc_path.exists():
        return set()
    return {str(p) for p in etc_path.glob("**/*.rpmnew")}


class TransactionPhase(str, Enum):
    """Phase of an RPM transaction lifecycle.

    The lifecycle follows this order::

        VERIFY → PREPARE → INSTALL → SCRIPT → (done)
                            ERASE  → SCRIPT → (done)

    VERIFY:  Header signature verification (OPEN/CLOSE fire but are NOT real installs).
    PREPARE: Transaction element ordering (TRANS_START/PROGRESS/STOP).
    INSTALL: Package extraction — cpio payload written to disk.
    SCRIPT:  Pre/post-install scriptlets and file triggers (often the slowest phase).
    ERASE:   Package removal.
    """
    VERIFY = 'verify'
    PREPARE = 'prepare'
    INSTALL = 'install'
    SCRIPT = 'script'
    ERASE = 'erase'


@dataclass(frozen=True)
class TransactionProgress:
    """Structured progress update from an RPM transaction.

    Replaces the old ``(op_id, name, current, total)`` callback signature
    with a single object that carries all relevant information.

    Consumers should check ``phase`` to decide which fields are meaningful:

    - **VERIFY/PREPARE**: ``packages_done`` / ``packages_total`` show
      verification or preparation progress.
    - **INSTALL**: ``packages_done`` / ``packages_total`` for package-level,
      ``bytes_done`` / ``bytes_total`` for cpio extraction within a package.
    - **SCRIPT**: ``script_name`` identifies which package's scriptlet is
      running.  ``packages_done`` stays at the last package count.
    - **ERASE**: ``packages_done`` / ``packages_total`` for removal progress.
    """
    phase: TransactionPhase

    # Package being processed (current extraction target or script owner)
    package_name: str = ""

    # Package-level counters
    packages_done: int = 0      # How many packages completed so far
    packages_total: int = 0     # Total packages in this transaction

    # Byte-level progress (INSTALL phase: cpio extraction within one package)
    bytes_done: int = 0
    bytes_total: int = 0

    # Script info (SCRIPT phase only)
    script_name: str = ""       # Package whose scriptlet is running
    script_type: int = 0        # RPM script tag (1023=%pre, 1024=%post, etc.)

    # Operation context (set by the parent, not the RPM callback)
    operation_id: str = ""


class OperationType(Enum):
    """Type of RPM operation."""
    INSTALL = "install"
    ERASE = "erase"


@dataclass
class QueuedOperation:
    """A single operation in the queue."""
    op_type: OperationType
    # For INSTALL: List[Path] of RPM files
    # For ERASE: List[str] of package names
    targets: Union[List[Path], List[str]]
    operation_id: str
    verify_signatures: bool = True
    force: bool = False
    test: bool = False
    background: bool = False  # If True, parent doesn't wait for this operation
    reinstall: bool = False  # If True, allow reinstalling same version
    noscripts: bool = False  # If True, skip pre/post install scripts
    actions: list = field(default_factory=list)  # PackageAction list from resolver (for README collection)


@dataclass
class OperationResult:
    """Result of a single operation."""
    operation_id: str
    op_type: OperationType
    success: bool
    count: int = 0
    errors: List[str] = field(default_factory=list)
    rpmnew_files: List[str] = field(default_factory=list)  # Config files saved as .rpmnew
    readme_messages: list = field(default_factory=list)  # README.urpmi [{package, content}]


@dataclass
class QueueResult:
    """Result of the entire queue execution."""
    success: bool
    operations: List[OperationResult] = field(default_factory=list)
    overall_error: str = ""
    scriptlet_output: str = ""  # Captured stdout from RPM scriptlets
    script_error_packages: List[str] = field(default_factory=list)  # Packages with scriptlet errors


@dataclass
class QueueProgressMessage:
    """Message sent from child to parent via pipe.

    Message types (lifecycle):
        op_start     — new operation beginning
        progress     — per-package progress update
        op_done      — operation completed (count, rpmnew, readmes)
        op_error     — operation failed
        queue_done   — all operations complete
        queue_error  — fatal queue-level error
    """
    msg_type: str
    operation_id: str = ""
    op_type: str = ""  # 'install' or 'erase'
    name: str = ""
    current: int = 0
    total: int = 0
    count: int = 0
    error: str = ""
    errors: List[str] = field(default_factory=list)
    rpmnew_files: List[str] = field(default_factory=list)
    readme_messages: list = field(default_factory=list)
    # Enriched progress fields
    phase: str = ""          # 'prepare', 'install', 'erase', 'script'
    bytes_done: int = 0      # Bytes processed for current package
    bytes_total: int = 0     # Total bytes for current package
    script: str = ""         # Scriptlet phase (e.g. 'post-install')
    scriptlet_output: str = ""  # Captured stdout from RPM scriptlets
    script_errors: List[str] = field(default_factory=list)  # Packages with scriptlet errors

    def to_json(self) -> str:
        d = {
            'type': self.msg_type,
            'operation_id': self.operation_id,
            'op_type': self.op_type,
            'name': self.name,
            'current': self.current,
            'total': self.total,
            'count': self.count,
            'error': self.error,
            'errors': self.errors,
            'rpmnew_files': self.rpmnew_files,
            'readme_messages': self.readme_messages,
            'phase': self.phase,
            'bytes_done': self.bytes_done,
            'bytes_total': self.bytes_total,
            'script': self.script,
        }
        if self.scriptlet_output:
            d['scriptlet_output'] = self.scriptlet_output
        if self.script_errors:
            d['script_errors'] = self.script_errors
        return json.dumps(d)

    @classmethod
    def from_json(cls, data: str) -> 'QueueProgressMessage':
        d = json.loads(data)
        return cls(
            msg_type=d['type'],
            operation_id=d.get('operation_id', ''),
            op_type=d.get('op_type', ''),
            name=d.get('name', ''),
            current=d.get('current', 0),
            total=d.get('total', 0),
            count=d.get('count', 0),
            error=d.get('error', ''),
            errors=d.get('errors', []),
            rpmnew_files=d.get('rpmnew_files', []),
            readme_messages=d.get('readme_messages', []),
            phase=d.get('phase', ''),
            bytes_done=d.get('bytes_done', 0),
            bytes_total=d.get('bytes_total', 0),
            script=d.get('script', ''),
            scriptlet_output=d.get('scriptlet_output', ''),
            script_errors=d.get('script_errors', []),
        )


_PHASE_MAP = {
    'verify': TransactionPhase.VERIFY,
    'prepare': TransactionPhase.PREPARE,
    'install': TransactionPhase.INSTALL,
    'install_done': TransactionPhase.INSTALL,
    'script': TransactionPhase.SCRIPT,
    'script_done': TransactionPhase.SCRIPT,
    'erase': TransactionPhase.ERASE,
}


def _msg_to_progress(msg: QueueProgressMessage) -> TransactionProgress:
    """Convert an internal pipe message to a public TransactionProgress."""
    phase = _PHASE_MAP.get(msg.phase, TransactionPhase.INSTALL)
    return TransactionProgress(
        phase=phase,
        package_name=msg.name,
        packages_done=msg.current,
        packages_total=msg.total,
        bytes_done=msg.bytes_done,
        bytes_total=msg.bytes_total,
        script_name=msg.script,
        operation_id=msg.operation_id,
    )


class TransactionQueue:
    """Queue multiple RPM operations for sequential execution in one process.

    Usage:
        queue = TransactionQueue()
        queue.add_install(rpm_paths, "upgrade")
        queue.add_erase(orphan_names, "cleanup")

        result = queue.execute(progress_callback=my_callback)
        if result.success:
            for op in result.operations:
                print(f"{op.operation_id}: {op.count} packages")
    """

    def __init__(self, root: str = "/", use_userns: bool = False):
        self.root = root
        self.use_userns = use_userns
        self.operations: List[QueuedOperation] = []
        # Sentinel for the scriptlet-marker capture fd. Set to a real fd in
        # _child_process() (root branch). _child_process_standalone() leaves
        # it at -1; the callbacks' os.write will then raise OSError(EBADF),
        # which is already swallowed by the surrounding try/except.
        self._capture_fd = -1

    @staticmethod
    def _userns_available() -> bool:
        """Check if podman unshare is available for proper UID/GID mapping."""
        import subprocess
        import shutil
        # We need podman unshare for proper subuid/subgid mapping
        # Simple 'unshare --user --map-root-user' doesn't work because
        # chown operations fail for UIDs outside the single-user mapping
        if not shutil.which('podman'):
            return False
        try:
            result = subprocess.run(
                ['podman', 'unshare', 'true'],
                capture_output=True, timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def add_install(
        self,
        rpm_paths: List[Path],
        operation_id: str = "",
        verify_signatures: bool = True,
        force: bool = False,
        test: bool = False,
        erase_names: List[str] = None,
        reinstall: bool = False,
        noscripts: bool = False,
        actions: list = None
    ) -> 'TransactionQueue':
        """Add an install operation to the queue.

        Args:
            rpm_paths: List of RPM file paths to install
            operation_id: Identifier for progress tracking
            verify_signatures: Whether to verify GPG signatures
            force: Force install despite problems
            test: Test mode - don't actually install
            erase_names: List of package names to erase in the SAME transaction
                        (for obsoleted packages that must be removed atomically)
            reinstall: Allow reinstalling same version without --force
            noscripts: Skip pre/post install scripts
            actions: PackageAction list from resolver (used for README.urpmi
                     collection in the child process after transaction completes)

        Returns:
            self for method chaining
        """
        if rpm_paths or erase_names:
            op = QueuedOperation(
                op_type=OperationType.INSTALL,
                targets=rpm_paths or [],
                operation_id=operation_id or f"install_{len(self.operations)}",
                verify_signatures=verify_signatures,
                force=force,
                test=test,
                reinstall=reinstall,
                noscripts=noscripts,
                actions=actions or [],
            )
            # Store erase_names as extra attribute
            op.erase_names = erase_names or []
            self.operations.append(op)
        return self

    def add_erase(
        self,
        package_names: List[str],
        operation_id: str = "",
        force: bool = False,
        test: bool = False,
        background: bool = False
    ) -> 'TransactionQueue':
        """Add an erase operation to the queue.

        Args:
            package_names: List of package names to erase
            operation_id: Identifier for progress tracking
            force: Force erase despite problems
            test: Test mode - don't actually erase
            background: If True, parent won't wait for this operation

        Returns:
            self for method chaining
        """
        if package_names:
            self.operations.append(QueuedOperation(
                op_type=OperationType.ERASE,
                targets=package_names,
                operation_id=operation_id or f"erase_{len(self.operations)}",
                verify_signatures=True,  # Not used for erase
                force=force,
                test=test,
                background=background,
            ))
        return self

    def is_empty(self) -> bool:
        """Check if the queue has no operations."""
        return len(self.operations) == 0

    def execute(
        self,
        progress_callback: Callable[['TransactionProgress'], None] = None,
        full_sync: bool = False,
        transaction_meta: dict = None,
    ) -> QueueResult:
        """Execute all queued operations sequentially.

        Forks a child process that executes all operations with a single lock.
        Parent receives progress for all operations via pipe.

        **Smart sync** (default, ``full_sync=False``):
            Parent waits for package extraction to complete, then returns.
            Post-install triggers (file triggers, ``%posttrans``) run in
            the background — they only rebuild caches (MIME, icons, etc.)
            and are not critical.  The child writes trigger progress to
            ``/run/urpm/transaction.json`` for ``urpm progress``.

        **Full sync** (``full_sync=True``, via ``--sync``):
            Parent waits for everything including post-install triggers.
            Required when packages need a reboot (``should-restart:system``).

        Args:
            progress_callback: Called with a single :class:`TransactionProgress`
                object for each progress update.  Check ``progress.phase`` to
                determine the current transaction phase (verify, prepare,
                install, script, erase).
            full_sync: If True, wait for all operations to complete including
                  post-install triggers.  Default is smart sync.
            transaction_meta: Dict with metadata for async progress file
                  (type, total, has_readmes).

        Returns:
            QueueResult with results for each operation
        """
        if not self.operations:
            return QueueResult(success=True)

        # Check for previous background errors
        prev_error = check_background_error()
        if prev_error:
            logger.warning(f"Previous background operation had error: {prev_error}")
            clear_background_error()

        # Create pipe for IPC
        read_fd, write_fd = os.pipe()

        # For non-root chroot installs, use user namespaces
        if self.use_userns and os.geteuid() != 0:
            if self._userns_available():
                return self._execute_with_userns(read_fd, write_fd, progress_callback, full_sync)
            else:
                os.close(read_fd)
                os.close(write_fd)
                return QueueResult(
                    success=False,
                    overall_error="podman unshare not available. Install 'podman' or run as root."
                )

        # Fork
        pid = os.fork()

        if pid > 0:
            # Parent process — smart sync or full sync
            return self._parent_process(read_fd, write_fd, progress_callback,
                                        full_sync, pid)
        else:
            # Child process - never returns
            self._child_process(read_fd, write_fd, transaction_meta)

    def _execute_with_userns(
        self,
        read_fd: int,
        write_fd: int,
        progress_callback: Callable[['TransactionProgress'], None],
        full_sync: bool
    ) -> QueueResult:
        """Execute operations in a user namespace (for non-root chroot installs)."""
        import pickle
        import subprocess
        import tempfile

        # Serialize queue state to temp file (mode 0o600 to prevent TOCTOU)
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pkl',
                                         prefix='urpm-') as f:
            os.fchmod(f.fileno(), 0o600)
            state_file = f.name
            pickle.dump({
                'root': self.root,
                'operations': self.operations,
                'write_fd': write_fd,
            }, f)

        try:
            # Close pipe FDs - we use subprocess stdout instead
            os.close(write_fd)
            os.close(read_fd)

            # Python code to run in the user namespace
            child_code = f'''
import os
import sys
import pickle

# Load queue state
with open("{state_file}", "rb") as f:
    state = pickle.load(f)

# Import after loading (avoid import issues)
from urpm.core.transaction_queue import TransactionQueue

# Recreate queue
queue = TransactionQueue(root=state["root"])
queue.operations = state["operations"]

# Run child process logic (writes to stdout)
queue._child_process_standalone()
'''

            # Run under podman unshare for proper UID/GID mapping
            # podman unshare uses /etc/subuid and /etc/subgid to map
            # a range of UIDs/GIDs, allowing chown operations to work
            proc = subprocess.Popen(
                ['podman', 'unshare', 'python3', '-c', child_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=()
            )

            # Read from subprocess stdout
            read_file = proc.stdout

            results: List[OperationResult] = []
            current_op_result: Optional[OperationResult] = None
            overall_error = ""

            for line in read_file:
                line = line.decode('utf-8').strip()
                if not line:
                    continue

                try:
                    msg = QueueProgressMessage.from_json(line)
                except Exception:
                    continue

                if msg.msg_type == 'op_start':
                    current_op_result = OperationResult(
                        operation_id=msg.operation_id,
                        op_type=OperationType(msg.op_type),
                        success=True
                    )
                elif msg.msg_type == 'progress':
                    if progress_callback:
                        progress_callback(
                            _msg_to_progress(msg)
                        )
                elif msg.msg_type == 'op_done':
                    if current_op_result:
                        current_op_result.count = msg.count or 0
                        current_op_result.rpmnew_files = msg.rpmnew_files or []
                        current_op_result.readme_messages = msg.readme_messages or []
                        results.append(current_op_result)
                        current_op_result = None
                elif msg.msg_type == 'op_error':
                    if current_op_result:
                        current_op_result.success = False
                        # Prefer the full `errors` list (e.g. every file
                        # conflict in an rpm transaction); fall back to
                        # the single `error` field for older messages.
                        if msg.errors:
                            current_op_result.errors = list(msg.errors)
                        elif msg.error:
                            current_op_result.errors = [msg.error]
                        else:
                            current_op_result.errors = []
                        results.append(current_op_result)
                        current_op_result = None
                elif msg.msg_type == 'queue_error':
                    overall_error = msg.error
                elif msg.msg_type == 'parent_can_exit':
                    pass  # Ignore in userns mode

            # Wait for subprocess to finish
            # In sync mode, show a message since scriptlets may take time.
            # Always wait — proc.returncode is None until wait() is called.
            if progress_callback and hasattr(progress_callback, 'cleanup'):
                progress_callback.cleanup()
            if full_sync:
                # Newline to close the progress display before the wait message
                print(f"\n\033[33m  Waiting for scriptlets to complete...\033[0m",
                      flush=True)
            proc.wait()

            # Collect stderr (RPM warnings, systemd inhibition messages, etc.)
            # Filter out benign chroot/userns warnings that clutter the output
            stderr_output = proc.stderr.read().decode('utf-8').strip() if proc.stderr else ""
            if stderr_output:
                _benign = (
                    'Unable to get systemd shutdown inhibition lock',
                    'Failed to connect to bus',
                )
                filtered = '\n'.join(
                    line for line in stderr_output.splitlines()
                    if not any(pat in line for pat in _benign)
                ).strip()
                if filtered:
                    import sys
                    print(f"\033[33m  [userns stderr]:\n{filtered}\033[0m",
                          file=sys.stderr, flush=True)

            # Only treat as error if the child process actually failed
            if proc.returncode != 0:
                if stderr_output:
                    overall_error = stderr_output
                else:
                    overall_error = f"unshare process exited with code {proc.returncode}"

            all_success = all(r.success for r in results) and not overall_error
            return QueueResult(success=all_success, operations=results, overall_error=overall_error)

        finally:
            # Cleanup temp file
            try:
                os.unlink(state_file)
            except OSError:
                pass

    def _child_process_standalone(self):
        """Child process logic for userns mode - writes to stdout."""
        import sys
        import rpm

        write_file = sys.stdout

        # Debug: show what we're about to do
        if DEBUG_USERNS:
            print(f"[userns child] root={self.root}, ops={len(self.operations)}", file=sys.stderr)
            for op in self.operations:
                print(f"[userns child]   op: {op.op_type.value} targets={len(op.targets)}", file=sys.stderr)
                if op.targets:
                    print(f"[userns child]     first: {op.targets[0]}", file=sys.stderr)
            sys.stderr.flush()

        # Acquire install lock
        lock = InstallLock(root=self.root if self.root != "/" else None)
        try:
            lock.acquire(blocking=True)
        except Exception as e:
            write_file.write(QueueProgressMessage(
                msg_type='queue_error',
                error=f"Failed to acquire lock: {e}"
            ).to_json() + "\n")
            write_file.flush()
            sys.exit(1)

        try:
            pipe_state = {'closed': False, 'file': write_file}

            for i, op in enumerate(self.operations):
                # Signal operation start
                write_file.write(QueueProgressMessage(
                    msg_type='op_start',
                    operation_id=op.operation_id,
                    op_type=op.op_type.value
                ).to_json() + "\n")
                write_file.flush()

                if op.op_type == OperationType.INSTALL:
                    if DEBUG_USERNS:
                        print(f"[userns child] executing install...", file=sys.stderr)
                        sys.stderr.flush()
                    success, count, errors, rpmnew_files = self._execute_install(op, pipe_state)
                    if DEBUG_USERNS:
                        print(f"[userns child] install result: success={success} count={count} errors={errors} rpmnew={len(rpmnew_files)}", file=sys.stderr)
                        sys.stderr.flush()
                else:
                    success, count, errors = self._execute_erase(op, pipe_state)
                    rpmnew_files = []

                if success:
                    write_file.write(QueueProgressMessage(
                        msg_type='op_done',
                        operation_id=op.operation_id,
                        count=count,
                        rpmnew_files=rpmnew_files
                    ).to_json() + "\n")
                else:
                    write_file.write(QueueProgressMessage(
                        msg_type='op_error',
                        operation_id=op.operation_id,
                        error=errors[0] if errors else "Unknown error"
                    ).to_json() + "\n")
                    break

                write_file.flush()

            # Signal queue complete
            write_file.write(QueueProgressMessage(msg_type='queue_done').to_json() + "\n")
            write_file.flush()
            lock.release()

        except Exception as e:
            write_file.write(QueueProgressMessage(
                msg_type='queue_error',
                error=str(e)
            ).to_json() + "\n")
            write_file.flush()
            lock.release()
            sys.exit(1)

    def _parent_process(
        self,
        read_fd: int,
        write_fd: int,
        progress_callback: Callable[['TransactionProgress'], None],
        full_sync: bool,
        child_pid: int
    ) -> QueueResult:
        """Parent: read progress messages from child and build result.

        Two modes:

        **Smart sync** (``full_sync=False``, the default):
            Parent reads progress until all packages are extracted
            (``phase=script`` with ``packages_done == packages_total``).
            At that point, closes the pipe and returns — the child
            continues running post-install triggers in the background.
            The child switches to writing ``/run/urpm/transaction.json``
            when the pipe breaks.

        **Full sync** (``full_sync=True``, via ``--sync``):
            Parent reads the full pipe until ``queue_done``, including
            all script phases.  Waits for child to exit.
        """
        os.close(write_fd)
        read_file = os.fdopen(read_fd, 'r')

        results: List[OperationResult] = []
        current_op_result: Optional[OperationResult] = None
        overall_error = ""
        scriptlet_output = ""
        script_error_packages: List[str] = []
        smart_released = False

        try:
            for line in read_file:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = QueueProgressMessage.from_json(line)
                except Exception:
                    continue

                if msg.msg_type == 'op_start':
                    current_op_result = OperationResult(
                        operation_id=msg.operation_id,
                        op_type=OperationType(msg.op_type),
                        success=True
                    )

                elif msg.msg_type == 'progress':
                    if progress_callback:
                        progress_callback(
                            _msg_to_progress(msg)
                        )

                    # Capture README messages sent after all packages are
                    # extracted (phase='install_done').  This arrives before
                    # the first SCRIPT_START, so smart sync gets them.
                    if msg.readme_messages and current_op_result:
                        current_op_result.readme_messages = msg.readme_messages

                    # ── Smart sync release point ──
                    # When all packages are extracted and we enter script
                    # phase, the parent can release.  The child continues
                    # running triggers in the background.
                    if (not full_sync
                            and not smart_released
                            and msg.phase in ('script', 'script_done')
                            and msg.total > 0
                            and msg.current >= msg.total):
                        smart_released = True
                        # Build result from what we know
                        if current_op_result:
                            current_op_result.count = msg.total
                            results.append(current_op_result)
                        break

                elif msg.msg_type == 'op_done':
                    if current_op_result:
                        current_op_result.count = msg.count
                        current_op_result.rpmnew_files = msg.rpmnew_files or []
                        current_op_result.readme_messages = msg.readme_messages or []
                        results.append(current_op_result)
                    current_op_result = None

                elif msg.msg_type == 'op_error':
                    if current_op_result:
                        current_op_result.success = False
                        current_op_result.errors = msg.errors or [msg.error]
                        results.append(current_op_result)
                    current_op_result = None
                    break

                elif msg.msg_type == 'scriptlet_output':
                    scriptlet_output = msg.scriptlet_output
                    script_error_packages = msg.script_errors or []

                elif msg.msg_type == 'queue_done':
                    break

                elif msg.msg_type == 'queue_error':
                    overall_error = msg.error
                    break

        finally:
            read_file.close()

        if full_sync or not smart_released:
            # Wait for child process to fully exit
            try:
                _, status = os.waitpid(child_pid, 0)
                if os.WIFSIGNALED(status) or (
                        os.WIFEXITED(status) and os.WEXITSTATUS(status) != 0):
                    if not overall_error:
                        overall_error = "Child process failed"
            except ChildProcessError:
                pass
        # In smart sync, don't waitpid — child runs triggers in background

        # Detect incomplete transaction (child crashed before sending results)
        if current_op_result is not None and current_op_result not in results:
            current_op_result.success = False
            if not current_op_result.errors:
                current_op_result.errors = ["Transaction interrupted"]
            results.append(current_op_result)

        all_success = all(r.success for r in results) and not overall_error
        return QueueResult(
            success=all_success,
            operations=results,
            overall_error=overall_error,
            scriptlet_output=scriptlet_output,
            script_error_packages=script_error_packages,
        )

    def _child_process(self, read_fd: int, write_fd: int,
                       transaction_meta: dict = None):
        """Child: execute operations sequentially.

        Starts by sending progress via pipe to the parent.  In smart
        sync mode the parent closes the pipe once extraction is done —
        the child catches BrokenPipeError in ``_send_progress`` and
        gracefully switches to ``/run/urpm/transaction.json`` for the
        remaining post-install triggers.
        """
        import rpm
        import signal as _signal

        os.close(read_fd)
        write_file = os.fdopen(write_fd, 'w', buffering=1)

        # Detach from parent's process group so we survive parent exit
        os.setsid()

        # Redirect stdout to a temp file so RPM scriptlet output (ldconfig,
        # update-mime-database, etc.) doesn't leak to the terminal and
        # corrupt the parent's progress bar.  The captured output is sent
        # back to the parent via the pipe for post-progress display.
        # stderr is kept for debug prints.
        import tempfile as _tempfile
        _stdout_capture = _tempfile.NamedTemporaryFile(
            mode='w+', prefix='urpm-scripts-', suffix='.log', delete=False,
        )
        _stdout_capture_path = _stdout_capture.name
        _capture_fd = _stdout_capture.fileno()
        self._capture_fd = _capture_fd  # accessible from install/erase callbacks
        self._script_error_packages = set()  # track packages with scriptlet errors
        os.dup2(_capture_fd, 1)  # stdout → temp file
        os.dup2(_capture_fd, 2)  # stderr → temp file
        # Update Python-level objects so print(..., file=sys.stderr) also
        # goes to the capture file instead of the terminal.
        import sys as _sys
        _sys.stdout = os.fdopen(1, 'w', buffering=1)
        _sys.stderr = os.fdopen(2, 'w', buffering=1)

        # Ignore SIGPIPE — we handle broken pipe in _send_progress
        _signal.signal(_signal.SIGPIPE, _signal.SIG_IGN)

        pipe_state = {'closed': False, 'file': write_file}

        # Acquire install lock ONCE for all operations
        lock = InstallLock(root=self.root if self.root != "/" else None)
        try:
            lock.acquire(blocking=True)
        except Exception as e:
            if not pipe_state['closed']:
                write_file.write(QueueProgressMessage(
                    msg_type='queue_error',
                    error=f"Failed to acquire lock: {e}"
                ).to_json() + "\n")
                write_file.close()
            os._exit(1)

        # Async progress file state — initialized lazily when pipe breaks
        # (smart sync) or eagerly if transaction_meta is provided.
        progress_file_state = None

        # SIGTERM handler for clean shutdown (e.g. during system reboot)
        import signal
        def _sigterm_handler(signum, frame):
            if progress_file_state:
                _update_async_progress(progress_file_state,
                                       error="Transaction interrupted (SIGTERM)")
            _set_background_error("Transaction interrupted by signal")
            lock.release()
            os._exit(1)
        signal.signal(signal.SIGTERM, _sigterm_handler)

        def _pipe_write(msg_json: str):
            """Write to pipe, catching BrokenPipeError (parent released)."""
            if pipe_state['closed']:
                return
            try:
                pipe_state['file'].write(msg_json + "\n")
            except (BrokenPipeError, OSError):
                pipe_state['closed'] = True

        try:
            for i, op in enumerate(self.operations):
                # Signal operation start
                _pipe_write(QueueProgressMessage(
                    msg_type='op_start',
                    operation_id=op.operation_id,
                    op_type=op.op_type.value
                ).to_json())

                if op.op_type == OperationType.INSTALL:
                    success, count, errors, rpmnew_files = self._execute_install(
                        op, pipe_state,
                        async_progress=progress_file_state,
                    )
                else:
                    success, count, errors = self._execute_erase(
                        op, pipe_state,
                        async_progress=progress_file_state,
                    )
                    rpmnew_files = []

                if success:
                    _pipe_write(QueueProgressMessage(
                        msg_type='op_done',
                        operation_id=op.operation_id,
                        count=count,
                        rpmnew_files=rpmnew_files
                    ).to_json())
                else:
                    error_msg = errors[0] if errors else "Unknown error"
                    _pipe_write(QueueProgressMessage(
                        msg_type='op_error',
                        operation_id=op.operation_id,
                        error=error_msg,
                        errors=errors
                    ).to_json())
                    # If pipe is closed (smart sync), store error for next run
                    if pipe_state['closed']:
                        if progress_file_state:
                            _update_async_progress(progress_file_state,
                                                   error=error_msg)
                        _set_background_error(error_msg)
                    break

                # Store README in DB after each successful install operation
                if success and op.op_type == OperationType.INSTALL:
                    self._store_readmes_in_db(op)

            # Send captured scriptlet output to parent, grouped by package.
            # Markers injected during SCRIPT_START delimit each section.
            _MARKER_PREFIX = '__URPM_SCRIPT:'
            try:
                _sys.stdout.flush()
                _sys.stderr.flush()
                with open(_stdout_capture_path, 'r',
                          errors='replace') as _cap:
                    _captured = _cap.read()
                if _captured.strip():
                    import json as _json
                    import re as _re
                    _script_outputs = {}
                    # Split on markers, keeping the marker content as keys
                    _parts = _re.split(
                        r'^' + _re.escape(_MARKER_PREFIX)
                        + r'(.+?)__$',
                        _captured, flags=_re.MULTILINE)
                    # _parts = [pre, name1, body1, name2, body2, ...]
                    # Pre-marker output (index 0)
                    _pre = _parts[0].strip()
                    if _pre:
                        _script_outputs[''] = _pre
                    # Paired (name, body) from index 1 onward
                    for _i in range(1, len(_parts) - 1, 2):
                        _pkg = _parts[_i]
                        _body = _parts[_i + 1].strip()
                        if _body:
                            if _pkg in _script_outputs:
                                _script_outputs[_pkg] += '\n' + _body
                            else:
                                _script_outputs[_pkg] = _body
                    _script_errs = list(self._script_error_packages)
                    if _script_outputs or _script_errs:
                        _pipe_write(QueueProgressMessage(
                            msg_type='scriptlet_output',
                            scriptlet_output=_json.dumps(_script_outputs) if _script_outputs else '',
                            script_errors=_script_errs,
                        ).to_json())
                    else:
                        # Markers only, no actual output — skip
                        pass
            except OSError:
                pass
            finally:
                try:
                    os.unlink(_stdout_capture_path)
                except OSError:
                    pass

            # Signal queue complete
            _pipe_write(QueueProgressMessage(msg_type='queue_done').to_json())
            if not pipe_state['closed']:
                try:
                    write_file.flush()
                    write_file.close()
                except (BrokenPipeError, OSError):
                    pass

            # Clean up async progress file
            if progress_file_state:
                _cleanup_async_progress()

            # Redirect stderr now that triggers are done
            # (stdout was already redirected at child start)
            if pipe_state['closed']:
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 2)
                    os.close(devnull)
                except OSError:
                    pass

            _log_background(f"Queue complete: {len(self.operations)} operations")
            lock.release()
            os._exit(0)

        except Exception as e:
            _set_background_error(f"Queue error: {e}")
            if progress_file_state:
                _update_async_progress(progress_file_state,
                                       error=str(e))
            _pipe_write(QueueProgressMessage(
                msg_type='queue_error',
                error=str(e)
            ).to_json())
            try:
                if not pipe_state['closed']:
                    write_file.close()
            except Exception:
                pass
            lock.release()
            os._exit(1)

    def _store_readmes_in_db(self, op: QueuedOperation):
        """Store README messages in the database for later retrieval.

        Called from the child process after a successful install operation.
        This enables ``urpm readme`` to display README messages from past
        transactions, regardless of sync/async mode.
        """
        readme_data = self._collect_readme_messages(op)
        if not readme_data:
            return
        try:
            import time as _time
            from .database import PackageDatabase
            db = PackageDatabase()
            conn = db._get_connection()
            # Get the most recent transaction ID
            cursor = conn.execute(
                "SELECT id FROM history ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return
            txn_id = row[0]
            for msg in readme_data:
                conn.execute(
                    """INSERT INTO transaction_readmes
                       (transaction_id, package_name, readme_type, content,
                        created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (txn_id, msg['package'], 'generic', msg['content'],
                     int(_time.time()))
                )
            conn.commit()
            _log_background(f"Stored {len(readme_data)} README(s) in DB")
        except Exception as e:
            _log_background(f"README DB storage error: {e}")

    def _collect_readme_messages(self, op: QueuedOperation) -> list:
        """Collect README.urpmi messages after a successful transaction.

        Called from the child process where installed files are guaranteed
        to be on disk.

        Args:
            op: The completed operation (uses ``op.actions`` for README filtering).

        Returns:
            List of dicts with 'package' and 'content' keys.
        """
        if not op.actions:
            _log_background("README: no actions on op")
            return []
        try:
            from .readme import collect_readme_messages
            msgs = collect_readme_messages(op.actions, root=self.root)
            _log_background(f"README: collected {len(msgs)} messages from {len(op.actions)} actions")
            return [
                {'package': m.package, 'content': m.content}
                for m in msgs
            ]
        except Exception as e:
            _log_background(f"README collection error: {e}")
            return []

    def _execute_install(
        self,
        op: QueuedOperation,
        pipe_state: dict,
        async_progress: dict = None,
    ) -> Tuple[bool, int, List[str], List[str]]:
        """Execute an install operation.

        Sends enriched progress messages via pipe (sync mode) or writes
        to the async progress file (async mode).  Handles all RPM callbacks
        including INST_PROGRESS and SCRIPT_START.

        Args:
            op: The operation to execute.
            pipe_state: Dict with 'closed' bool and 'file' write handle.
            async_progress: Async progress file state (None in sync mode).

        Returns:
            Tuple of (success, count, errors, rpmnew_files).
        """
        import rpm
        import sys

        rpm_paths = op.targets
        erase_names = getattr(op, 'erase_names', [])
        errors = []

        if DEBUG_EXECINSTALL:
            _debug_write(f"[install] root={self.root} paths={len(rpm_paths)} noscripts={op.noscripts}")

        ts = rpm.TransactionSet(self.root or '/')

        if op.verify_signatures:
            ts.setVSFlags(0)
        else:
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        # Add packages to install
        open_fds = {}
        added_count = 0
        for path in rpm_paths:
            try:
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    hdr = ts.hdrFromFdno(fd)
                    ts.addInstall(hdr, str(path), 'u')
                    added_count += 1
                    if DEBUG_EXECINSTALL:
                        _debug_write(f"[install] added: {Path(path).name}")
                finally:
                    os.close(fd)
            except rpm.error as e:
                print(f"[_execute_install] ERROR adding {path}: {e}", file=sys.stderr)
                sys.stderr.flush()
                errors.append(f"{Path(path).name}: {e}")
                return False, 0, errors, []

        if DEBUG_EXECINSTALL:
            _debug_write(f"[install] added {added_count} packages to transaction")

        # Add packages to erase in the SAME transaction (for obsoleted packages)
        erased_count = 0
        for name in erase_names:
            mi = ts.dbMatch('name', name)
            for hdr in mi:
                ts.addErase(hdr)
                erased_count += 1
                _log_background(f"Adding erase to transaction: {name}")
                break  # Only first match

        # Check dependencies
        if not op.force:
            if DEBUG_EXECINSTALL:
                _debug_write("[install] checking dependencies...")
            unresolved = ts.check()
            if unresolved:
                # Separate deps from OLD packages being replaced (scriptlet
                # deps — safe to ignore, RPM skips the scriptlet if the
                # interpreter is missing) from deps of NEW packages (fatal).
                # ts.check() returns: ((N, V, R), (depN, depV), flags, suggest, sense)
                new_names = {Path(p).name.rsplit('-', 2)[0] for p in rpm_paths}
                fatal = []
                warned = []
                for prob in unresolved:
                    pkg_name = prob[0][0] if isinstance(prob[0], tuple) else str(prob[0])
                    if pkg_name in new_names:
                        fatal.append(prob)
                    else:
                        warned.append(prob)
                if warned:
                    print(f"[_execute_install] scriptlet deps (ignored): {warned}",
                          file=sys.stderr)
                    sys.stderr.flush()
                if fatal:
                    from urpm.core.resolution.diagnose import (
                        format_dependency_issue,
                        from_rpmlib_tuple,
                    )
                    print(f"[_execute_install] unresolved deps: {fatal}", file=sys.stderr)
                    sys.stderr.flush()
                    errors = [
                        format_dependency_issue(from_rpmlib_tuple(prob))
                        for prob in fatal
                    ]
                    return False, 0, errors, []
            if DEBUG_EXECINSTALL:
                _debug_write("[install] dependencies OK")

        # Order transaction
        ts.order()
        if DEBUG_EXECINSTALL:
            _debug_write("[install] transaction ordered")

        if op.test:
            return True, len(rpm_paths), [], []

        # RPM callback state machine
        #
        # ts.run() fires callbacks in four phases (from trace analysis):
        #
        #   1. VERIFY:  VERIFY_START → per-pkg [VERIFY_PROGRESS, OPEN, CLOSE] → VERIFY_STOP
        #               OPEN/CLOSE here are for header verification — NOT real installs.
        #               amount=0, total=0 on OPEN/CLOSE during this phase.
        #
        #   2. PREPARE: TRANS_START → TRANS_PROGRESS 0..N → TRANS_STOP
        #               Transaction element ordering.
        #
        #   3. INSTALL: per-pkg [OPEN → ELEM_PROGRESS(index, total)
        #                        → INST_START → INST_PROGRESS flood → INST_STOP → CLOSE
        #                        → SCRIPT_START %post → SCRIPT_STOP]
        #               ELEM_PROGRESS is the reliable package counter (0-based index).
        #               %post scripts run INTERLEAVED with extractions (after each pkg).
        #
        #   4. POSTTRANS + FILE TRIGGERS: SCRIPT_START/STOP pairs.
        #               Fires AFTER all packages are extracted and their %post ran.
        #               key = triggering package name (e.g. "shared-mime-info").
        #               This is often the slowest phase (e.g. 39s for shared-mime-info).
        #
        total = len(rpm_paths)
        packages_done = [0]       # Unique packages with extraction complete
        seen_paths = set()        # Paths already counted (dedup multi-installed)
        extraction_error = [False]
        current_pkg_name = ['']
        in_verify = [True]        # True until VERIFY_STOP fires

        def _send_progress(**kwargs):
            """Send progress via pipe or async progress file.

            In smart sync mode the parent may close its pipe read-end
            once extraction is done.  When that happens the next write
            raises BrokenPipeError — we catch it, mark the pipe closed,
            and fall through to the async progress file so the child
            continues reporting via ``/run/urpm/transaction.json``.
            """
            nonlocal async_progress
            if not pipe_state['closed']:
                try:
                    pipe_state['file'].write(QueueProgressMessage(
                        msg_type='progress',
                        operation_id=op.operation_id,
                        **kwargs
                    ).to_json() + "\n")
                except (BrokenPipeError, OSError):
                    # Parent released (smart sync) — switch to async file
                    pipe_state['closed'] = True
                    if not async_progress:
                        async_progress = _init_async_progress({
                            'type': op.op_type.value,
                            'total': total,
                        })
            if async_progress:
                _update_async_progress(async_progress,
                                       current=kwargs.get('current', 0),
                                       current_package=kwargs.get('name', ''),
                                       phase=kwargs.get('phase', ''),
                                       script=kwargs.get('script', ''),
                                       bytes_done=kwargs.get('bytes_done', 0),
                                       bytes_total=kwargs.get('bytes_total', 0))

        # ── Debug: reverse map RPM callback reason int → name ──
        if DEBUG_TSRUN:
            _CB_NAMES = {}
            for _attr in dir(rpm):
                if _attr.startswith('RPMCALLBACK_'):
                    _CB_NAMES[getattr(rpm, _attr)] = _attr[12:]

        def callback(reason, amount, total_pkg, key, client_data):
            # ── Debug logging (--debug tsrun) ──
            if DEBUG_TSRUN and reason != rpm.RPMCALLBACK_INST_PROGRESS:
                cb_name = _CB_NAMES.get(reason, f'UNKNOWN({reason})')
                pname = Path(key).name if key and '/' in str(key) else (str(key) if key else '')
                _debug_write(
                    f"CB {cb_name}: amount={amount} total_pkg={total_pkg} "
                    f"key={pname} in_verify={in_verify[0]} "
                    f"done={packages_done[0]} total={total}"
                )

            # ── VERIFY phase: header signature checks ──
            if reason == rpm.RPMCALLBACK_VERIFY_START:
                in_verify[0] = True
                _send_progress(name='', current=0, total=total,
                               phase='verify')
                return

            if reason == rpm.RPMCALLBACK_VERIFY_STOP:
                in_verify[0] = False
                return

            if reason == rpm.RPMCALLBACK_VERIFY_PROGRESS:
                _send_progress(name='', current=amount, total=total_pkg,
                               phase='verify')
                return

            # ── OPEN/CLOSE FILE: verify phase vs real install ──
            if reason == rpm.RPMCALLBACK_INST_OPEN_FILE:
                path = key
                fd = os.open(path, os.O_RDONLY)
                open_fds[path] = fd
                if not in_verify[0]:
                    name = Path(path).stem.rsplit('-', 2)[0] if path else ''
                    current_pkg_name[0] = name
                return fd

            if reason == rpm.RPMCALLBACK_INST_CLOSE_FILE:
                path = key
                if path in open_fds:
                    try:
                        os.close(open_fds[path])
                    except Exception:
                        pass
                    del open_fds[path]
                if not in_verify[0] and path not in seen_paths:
                    # Count each unique path once — multi-installed packages
                    # trigger OPEN/CLOSE multiple times for the same file.
                    seen_paths.add(path)
                    packages_done[0] += 1
                    _send_progress(name=current_pkg_name[0],
                                   current=packages_done[0], total=total,
                                   phase='install')
                    # All packages extracted — collect READMEs now, before
                    # triggers start.  In smart sync the parent releases at
                    # the first SCRIPT_START; sending readmes here ensures
                    # the parent has them before breaking out of the loop.
                    if packages_done[0] == total:
                        readme_data_early = self._collect_readme_messages(op)
                        if readme_data_early and not pipe_state['closed']:
                            try:
                                pipe_state['file'].write(QueueProgressMessage(
                                    msg_type='progress',
                                    operation_id=op.operation_id,
                                    name='',
                                    current=total, total=total,
                                    phase='install_done',
                                    readme_messages=readme_data_early,
                                ).to_json() + "\n")
                            except (BrokenPipeError, OSError):
                                pipe_state['closed'] = True
                return

            # ── ELEM_PROGRESS: fires at start of each pkg (install + erase) ──
            # RPM's amount/total_pkg here count ALL elements (installs + erases),
            # which conflicts with our install-only packages_done/total counters.
            # Only use this to track the current package name.
            if reason == rpm.RPMCALLBACK_ELEM_PROGRESS:
                if not in_verify[0]:
                    name = Path(key).stem.rsplit('-', 2)[0] if key else ''
                    current_pkg_name[0] = name
                    _send_progress(name=name, current=packages_done[0],
                                   total=total, phase='install')
                return

            # ── INST_PROGRESS: byte-level extraction progress ──
            if reason == rpm.RPMCALLBACK_INST_PROGRESS:
                if not in_verify[0]:
                    _send_progress(name=current_pkg_name[0],
                                   current=packages_done[0], total=total,
                                   phase='install',
                                   bytes_done=amount,
                                   bytes_total=total_pkg)
                return

            # ── CPIO extraction error ──
            if reason == rpm.RPMCALLBACK_CPIO_ERROR:
                extraction_error[0] = True
                _log_background(f"CPIO extraction error: {key}")
                return

            # ── TRANS_START/PROGRESS/STOP: transaction preparation ──
            if reason == rpm.RPMCALLBACK_TRANS_START:
                _send_progress(name='', current=0, total=total,
                               phase='prepare')
                return

            if reason == rpm.RPMCALLBACK_TRANS_PROGRESS:
                _send_progress(name='', current=amount, total=total_pkg,
                               phase='prepare')
                return

            if reason == rpm.RPMCALLBACK_TRANS_STOP:
                _log_background(f"Transaction preparation complete: {total} packages")
                return

            # ── SCRIPT_START/STOP: scriptlets and file triggers ──
            if reason == rpm.RPMCALLBACK_SCRIPT_START:
                script_name = _clean_script_key(key)
                # Inject marker for per-package output grouping (direct fd write,
                # no sys.stdout interaction, no interference with RPM).
                try:
                    os.write(self._capture_fd,
                             f"__URPM_SCRIPT:{script_name}__\n".encode())
                except OSError:
                    pass
                _send_progress(name=script_name,
                               current=packages_done[0], total=total,
                               phase='script', script=script_name)
                return

            if reason == rpm.RPMCALLBACK_SCRIPT_STOP:
                script_name = _clean_script_key(key)
                _send_progress(name=script_name,
                               current=packages_done[0], total=total,
                               phase='script_done', script=script_name)
                return

            if reason == rpm.RPMCALLBACK_SCRIPT_ERROR:
                script_name = _clean_script_key(key)
                _log_background(f"Scriptlet error: {script_name}")
                self._script_error_packages.add(script_name)
                return

        # Set problem filters
        # Always skip disk space check - RPM's check can be unreliable
        # (reports false positives with plenty of space available)
        # Always allow replacing same package - handles PackageKit double-calls
        # where second call arrives before first transaction completes
        prob_filter = rpm.RPMPROB_FILTER_DISKSPACE | rpm.RPMPROB_FILTER_REPLACEPKG
        if op.force:
            prob_filter |= (
                rpm.RPMPROB_FILTER_OLDPACKAGE |
                rpm.RPMPROB_FILTER_REPLACENEWFILES |
                rpm.RPMPROB_FILTER_REPLACEOLDFILES
            )
        ts.setProbFilter(prob_filter)

        # Set transaction flags (noscripts for chroot/container builds)
        if op.noscripts:
            ts.setFlags(rpm.RPMTRANS_FLAG_NOSCRIPTS)
            _log_background("Skipping pre/post scripts (--noscripts)")

        # Run transaction
        if DEBUG_EXECINSTALL:
            _debug_write(f"[install] calling ts.run() with {total} packages")
        _log_background(f"Starting install: {total} packages")

        # Track .rpmnew files created during this transaction
        rpmnew_before = _list_rpmnew_files(self.root or "/")
        problems = ts.run(callback, '')
        rpmnew_after = _list_rpmnew_files(self.root or "/")
        new_rpmnew_files = list(rpmnew_after - rpmnew_before)

        if DEBUG_EXECINSTALL:
            _debug_write(f"[install] ts.run() returned: problems={problems}")
            if new_rpmnew_files:
                _debug_write(f"[install] new .rpmnew files: {new_rpmnew_files}")

        # Clean up any remaining FDs
        for fd in open_fds.values():
            try:
                os.close(fd)
            except Exception:
                pass

        if problems:
            # For upgrade operations, "already installed" errors are benign
            if op.operation_id == "upgrade":
                real_problems = []
                for p in problems:
                    msg = str(p) if isinstance(p, str) else (p[0] if isinstance(p, tuple) else str(p))
                    if "is already installed" not in msg:
                        real_problems.append(p)
                    else:
                        _log_background(f"NOTE: {msg} (ignored for upgrade)")
                if not real_problems:
                    _log_background(f"Upgrade completed: {total} packages (some already at target version)")
                    readme_data = self._collect_readme_messages(op)
                    try:
                        if not pipe_state['closed']:
                            pipe_state['file'].write(QueueProgressMessage(
                                msg_type='op_done',
                                operation_id=op.operation_id,
                                count=total,
                                rpmnew_files=new_rpmnew_files,
                                readme_messages=readme_data
                            ).to_json() + "\n")
                            pipe_state['file'].flush()
                    except (BrokenPipeError, OSError):
                        pipe_state['closed'] = True
                    return True, total, [], new_rpmnew_files
                problems = real_problems

            # Filter "needed by (installed)" — safe for split transactions
            remaining = []
            for p in problems:
                msg = str(p) if isinstance(p, str) else (
                    p[0] if isinstance(p, tuple) else str(p))
                if "is needed by (installed)" in msg:
                    _log_background(f"NOTE: {msg} (ignored — installed dep)")
                else:
                    remaining.append(p)
            if not remaining:
                _log_background("All ts.run() problems were installed-dep warnings")
                readme_data = self._collect_readme_messages(op)
                try:
                    if not pipe_state['closed']:
                        pipe_state['file'].write(QueueProgressMessage(
                            msg_type='op_done',
                            operation_id=op.operation_id,
                            count=total,
                            rpmnew_files=new_rpmnew_files,
                            readme_messages=readme_data
                        ).to_json() + "\n")
                        pipe_state['file'].flush()
                except (BrokenPipeError, OSError):
                    pipe_state['closed'] = True
                return True, total, [], new_rpmnew_files
            problems = remaining

            _log_background(f"Transaction failed: {problems}")
            errors = [str(p) for p in problems]
            return False, packages_done[0], errors, new_rpmnew_files

        _log_background(f"Transaction completed: {total} packages")

        readme_data = self._collect_readme_messages(op)

        # Send results via pipe — may be closed in smart sync mode
        try:
            if not pipe_state['closed']:
                pipe_state['file'].write(QueueProgressMessage(
                    msg_type='op_done',
                    operation_id=op.operation_id,
                    count=total,
                    rpmnew_files=new_rpmnew_files,
                    readme_messages=readme_data
                ).to_json() + "\n")
                pipe_state['file'].flush()
        except (BrokenPipeError, OSError):
            pipe_state['closed'] = True

        return True, total, [], new_rpmnew_files

    def _execute_erase(
        self,
        op: QueuedOperation,
        pipe_state: dict,
        async_progress: dict = None,
    ) -> Tuple[bool, int, List[str]]:
        """Execute an erase operation.

        Args:
            op: The operation to execute.
            pipe_state: Dict with 'closed' bool and 'file' write handle.
            async_progress: Async progress file state (None in sync mode).
        """
        import rpm

        package_names = op.targets
        errors = []

        ts = rpm.TransactionSet(self.root or '/')
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        found = []
        for name in package_names:
            mi = ts.dbMatch('name', name)
            for hdr in mi:
                found.append((name, hdr))
                ts.addErase(hdr)
                break

        if not found:
            errors.append("No packages found to erase")
            return False, 0, errors

        if not op.force:
            unresolved = ts.check()
            if unresolved:
                errors = [f"Dependency: {prob}" for prob in unresolved]
                return False, 0, errors

        ts.order()

        if op.test:
            return True, len(found), []

        total = len(found)
        completed = [0]
        current_erase_name = ['']

        def _erase_pipe_write(msg_json: str):
            """Write to pipe, catching BrokenPipeError (parent released)."""
            if pipe_state['closed']:
                return
            try:
                pipe_state['file'].write(msg_json + "\n")
            except (BrokenPipeError, OSError):
                pipe_state['closed'] = True

        def callback(reason, amount, total_pkg, key, client_data):
            if reason == rpm.RPMCALLBACK_UNINST_START:
                name = key if isinstance(key, str) else str(key)
                current_erase_name[0] = name
                _erase_pipe_write(QueueProgressMessage(
                    msg_type='progress',
                    operation_id=op.operation_id,
                    name=name,
                    current=completed[0],
                    total=total,
                    phase='erase',
                ).to_json())
                if async_progress:
                    _update_async_progress(async_progress,
                                           current=completed[0],
                                           current_package=name,
                                           phase='erase')

            elif reason == rpm.RPMCALLBACK_UNINST_STOP:
                completed[0] += 1
                _erase_pipe_write(QueueProgressMessage(
                    msg_type='progress',
                    operation_id=op.operation_id,
                    name=current_erase_name[0],
                    current=completed[0],
                    total=total,
                    phase='erase',
                ).to_json())
                if async_progress:
                    _update_async_progress(async_progress,
                                           current=completed[0],
                                           current_package=current_erase_name[0],
                                           phase='erase')

            elif reason == rpm.RPMCALLBACK_UNINST_PROGRESS:
                if async_progress:
                    _update_async_progress(async_progress,
                                           current=completed[0],
                                           bytes_done=amount,
                                           bytes_total=total_pkg,
                                           phase='erase')

            elif reason == rpm.RPMCALLBACK_SCRIPT_START:
                script_name = _clean_script_key(key)
                try:
                    os.write(self._capture_fd,
                             f"__URPM_SCRIPT:{script_name}__\n".encode())
                except OSError:
                    pass
                _erase_pipe_write(QueueProgressMessage(
                    msg_type='progress',
                    operation_id=op.operation_id,
                    name=script_name,
                    current=completed[0],
                    total=total,
                    phase='script',
                    script=script_name,
                ).to_json())
                if async_progress:
                    _update_async_progress(async_progress,
                                           current=completed[0],
                                           current_package=script_name,
                                           phase='script',
                                           script=script_name)

            elif reason == rpm.RPMCALLBACK_SCRIPT_STOP:
                script_name = _clean_script_key(key)
                _erase_pipe_write(QueueProgressMessage(
                    msg_type='progress',
                    operation_id=op.operation_id,
                    name=script_name,
                    current=completed[0],
                    total=total,
                    phase='script_done',
                    script=script_name,
                ).to_json())
                if async_progress:
                    _update_async_progress(async_progress,
                                           current=completed[0],
                                           phase='script_done',
                                           script=script_name)

            elif reason == rpm.RPMCALLBACK_SCRIPT_ERROR:
                script_name = _clean_script_key(key)
                _log_background(f"Scriptlet error: {script_name}")
                self._script_error_packages.add(script_name)

            elif reason == rpm.RPMCALLBACK_TRANS_STOP:
                _log_background(f"Erase complete: {total} packages")

        if op.force:
            ts.setProbFilter(rpm.RPMPROB_FILTER_REPLACEPKG)

        _log_background(f"Starting erase: {total} packages")
        problems = ts.run(callback, '')

        if problems:
            errors = [str(p) for p in problems]
            return False, completed[0], errors

        return True, total, []
