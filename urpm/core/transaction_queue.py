"""
Transaction Queue for RPM operations

Provides a generic queue system for executing multiple RPM operations
(install, erase) sequentially in a single forked child process.

This solves the race condition where the parent would start a new operation
while the previous one was still doing rpmdb sync in the background.

Architecture:
    urpm (parent)                    child process
        |                                |
        |-- fork() -------------------->|
        |                                |
        | reads progress via pipe        | acquire lock ONCE
        | for ALL operations             | for op in queue:
        |                                |   execute op
        |   "queue_done" received        |   send progress
        |   exit(0)                      | release lock
        |                                | sync rpmdb (once, at end)
        |                                |
        |<-------------------------------|
                                         exit(0)
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

logger = logging.getLogger(__name__)


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


@dataclass
class OperationResult:
    """Result of a single operation."""
    operation_id: str
    op_type: OperationType
    success: bool
    count: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class QueueResult:
    """Result of the entire queue execution."""
    success: bool
    operations: List[OperationResult] = field(default_factory=list)
    overall_error: str = ""


@dataclass
class QueueProgressMessage:
    """Message sent from child to parent for queue execution."""
    # msg_type values:
    # 'op_start', 'progress', 'op_done', 'op_error' - operation lifecycle
    # 'parent_can_exit' - parent can exit, remaining ops run in background
    # 'queue_done', 'queue_error' - queue lifecycle
    msg_type: str
    operation_id: str = ""
    op_type: str = ""  # 'install' or 'erase'
    name: str = ""
    current: int = 0
    total: int = 0
    count: int = 0
    error: str = ""
    errors: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            'type': self.msg_type,
            'operation_id': self.operation_id,
            'op_type': self.op_type,
            'name': self.name,
            'current': self.current,
            'total': self.total,
            'count': self.count,
            'error': self.error,
            'errors': self.errors,
        })

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

    def __init__(self, root: str = "/"):
        self.root = root
        self.operations: List[QueuedOperation] = []

    def add_install(
        self,
        rpm_paths: List[Path],
        operation_id: str = "",
        verify_signatures: bool = True,
        force: bool = False,
        test: bool = False,
        erase_names: List[str] = None
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
        progress_callback: Callable[[str, str, int, int], None] = None
    ) -> QueueResult:
        """Execute all queued operations sequentially.

        Forks a child process that executes all operations with a single lock.
        Parent receives progress for all operations via pipe.

        Args:
            progress_callback: Called with (operation_id, name, current, total)

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

        # Fork
        pid = os.fork()

        if pid > 0:
            # Parent process
            return self._parent_process(read_fd, write_fd, progress_callback)
        else:
            # Child process - never returns
            self._child_process(read_fd, write_fd)

    def _parent_process(
        self,
        read_fd: int,
        write_fd: int,
        progress_callback: Callable[[str, str, int, int], None]
    ) -> QueueResult:
        """Parent: read progress messages and build result."""
        os.close(write_fd)
        read_file = os.fdopen(read_fd, 'r')

        results: List[OperationResult] = []
        current_op_result: Optional[OperationResult] = None
        overall_error = ""

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
                    # New operation starting
                    current_op_result = OperationResult(
                        operation_id=msg.operation_id,
                        op_type=OperationType(msg.op_type),
                        success=True
                    )

                elif msg.msg_type == 'progress':
                    if progress_callback:
                        progress_callback(
                            msg.operation_id,
                            msg.name,
                            msg.current,
                            msg.total
                        )

                elif msg.msg_type == 'op_done':
                    # Operation completed successfully
                    if current_op_result:
                        current_op_result.count = msg.count
                        results.append(current_op_result)
                    current_op_result = None

                elif msg.msg_type == 'op_error':
                    # Operation failed - stop immediately
                    if current_op_result:
                        current_op_result.success = False
                        current_op_result.errors = msg.errors or [msg.error]
                        results.append(current_op_result)
                    current_op_result = None
                    # Stop on first error
                    break

                elif msg.msg_type == 'parent_can_exit':
                    # Parent can exit, child continues with background ops
                    break

                elif msg.msg_type == 'queue_done':
                    # All operations complete
                    break

                elif msg.msg_type == 'queue_error':
                    # Fatal queue error
                    overall_error = msg.error
                    break

        finally:
            read_file.close()

        all_success = all(r.success for r in results) and not overall_error
        return QueueResult(
            success=all_success,
            operations=results,
            overall_error=overall_error
        )

    def _child_process(self, read_fd: int, write_fd: int):
        """Child: execute operations sequentially."""
        import rpm

        os.close(read_fd)
        write_file = os.fdopen(write_fd, 'w', buffering=1)  # Line buffered

        # Detach from parent's process group
        os.setsid()

        # Acquire install lock ONCE for all operations
        lock = InstallLock()
        try:
            lock.acquire(blocking=True)
        except Exception as e:
            write_file.write(QueueProgressMessage(
                msg_type='queue_error',
                error=f"Failed to acquire lock: {e}"
            ).to_json() + "\n")
            write_file.close()
            os._exit(1)

        try:
            # Track pipe state in a mutable container so callbacks can modify it
            pipe_state = {'closed': False, 'file': write_file}

            # Check if there are background operations after foreground ones
            has_background_after = any(op.background for op in self.operations)

            for i, op in enumerate(self.operations):
                # Skip if this is a background op and we've already released parent
                is_last_foreground = (
                    has_background_after and
                    not op.background and
                    (i + 1 >= len(self.operations) or self.operations[i + 1].background)
                )

                # Signal operation start (only if parent still listening)
                if not pipe_state['closed']:
                    write_file.write(QueueProgressMessage(
                        msg_type='op_start',
                        operation_id=op.operation_id,
                        op_type=op.op_type.value
                    ).to_json() + "\n")

                if op.op_type == OperationType.INSTALL:
                    # Always release parent after last install package
                    # (before rpmdb sync which can take 30-60 seconds)
                    is_last_install = (i + 1 >= len(self.operations) or
                                       self.operations[i + 1].op_type != OperationType.INSTALL)
                    success, count, errors = self._execute_install(
                        op,
                        pipe_state,
                        release_parent_after=(is_last_foreground or is_last_install)
                    )
                else:
                    # For erase: release parent after if it's background OR if it's the last operation
                    is_last_op = (i + 1 >= len(self.operations))
                    success, count, errors = self._execute_erase(
                        op,
                        pipe_state,
                        release_parent_after=((op.background or is_last_op) and not pipe_state['closed'])
                    )

                if not pipe_state['closed']:
                    if success:
                        write_file.write(QueueProgressMessage(
                            msg_type='op_done',
                            operation_id=op.operation_id,
                            count=count
                        ).to_json() + "\n")
                    else:
                        write_file.write(QueueProgressMessage(
                            msg_type='op_error',
                            operation_id=op.operation_id,
                            error=errors[0] if errors else "Unknown error",
                            errors=errors
                        ).to_json() + "\n")
                        # Stop on first error (for foreground ops)
                        break
                else:
                    # Log background operation result
                    if success:
                        _log_background(f"Background op {op.operation_id}: {count} packages")
                    else:
                        _log_background(f"Background op {op.operation_id} failed: {errors}")

            # Signal queue complete (if parent still listening)
            if not pipe_state['closed']:
                write_file.write(QueueProgressMessage(
                    msg_type='queue_done'
                ).to_json() + "\n")
                write_file.flush()
                write_file.close()

            _log_background(f"Queue complete: {len(self.operations)} operations")
            lock.release()
            os._exit(0)

        except Exception as e:
            _set_background_error(f"Queue error: {e}")
            try:
                write_file.write(QueueProgressMessage(
                    msg_type='queue_error',
                    error=str(e)
                ).to_json() + "\n")
                write_file.close()
            except Exception:
                pass
            lock.release()
            os._exit(1)

    def _execute_install(
        self,
        op: QueuedOperation,
        pipe_state: dict,
        release_parent_after: bool = False
    ) -> Tuple[bool, int, List[str]]:
        """Execute an install operation.

        Args:
            op: The operation to execute
            pipe_state: Dict with 'closed' bool and 'file' write handle
            release_parent_after: If True, send parent_can_exit after last package
        """
        import rpm

        rpm_paths = op.targets
        erase_names = getattr(op, 'erase_names', [])
        errors = []

        ts = rpm.TransactionSet(self.root)

        if op.verify_signatures:
            ts.setVSFlags(0)
        else:
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        # Add packages to install
        open_fds = {}
        for path in rpm_paths:
            try:
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    hdr = ts.hdrFromFdno(fd)
                    ts.addInstall(hdr, str(path), 'u')
                finally:
                    os.close(fd)
            except rpm.error as e:
                errors.append(f"{Path(path).name}: {e}")
                return False, 0, errors

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
            unresolved = ts.check()
            if unresolved:
                errors = [f"Dependency: {prob}" for prob in unresolved]
                return False, 0, errors

        # Order transaction
        ts.order()

        if op.test:
            return True, len(rpm_paths), []

        # Set up callback
        total = len(rpm_paths)
        current = [0]
        closed_count = [0]  # Track closed packages
        seen_paths = set()  # Track already-counted packages

        def callback(reason, amount, total_pkg, key, client_data):
            if reason == rpm.RPMCALLBACK_INST_OPEN_FILE:
                path = key

                # Only count each package once
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    current[0] += 1

                    # Send progress (if parent still listening)
                    if not pipe_state['closed']:
                        name = Path(path).stem.rsplit('-', 2)[0] if path else ''
                        pipe_state['file'].write(QueueProgressMessage(
                            msg_type='progress',
                            operation_id=op.operation_id,
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
                    except Exception:
                        pass
                    del open_fds[path]

                # Track closed packages
                closed_count[0] += 1

                # Release parent after last package is closed (before rpmdb sync)
                if (release_parent_after and
                    closed_count[0] >= total and
                    not pipe_state['closed']):
                    # Send success message and release parent
                    pipe_state['file'].write(QueueProgressMessage(
                        msg_type='op_done',
                        operation_id=op.operation_id,
                        count=total
                    ).to_json() + "\n")
                    pipe_state['file'].write(QueueProgressMessage(
                        msg_type='parent_can_exit'
                    ).to_json() + "\n")
                    pipe_state['file'].flush()
                    pipe_state['file'].close()
                    pipe_state['closed'] = True
                    _log_background("Parent released after install, continuing with rpmdb sync...")

            elif reason == rpm.RPMCALLBACK_TRANS_STOP:
                _log_background(f"Install complete: {total} packages")

        # Set problem filters
        prob_filter = 0
        if op.force:
            prob_filter |= (
                rpm.RPMPROB_FILTER_REPLACEPKG |
                rpm.RPMPROB_FILTER_OLDPACKAGE |
                rpm.RPMPROB_FILTER_REPLACENEWFILES |
                rpm.RPMPROB_FILTER_REPLACEOLDFILES
            )
        if prob_filter:
            ts.setProbFilter(prob_filter)

        # Run transaction
        _log_background(f"Starting install: {total} packages")
        problems = ts.run(callback, '')

        # Clean up any remaining FDs
        for fd in open_fds.values():
            try:
                os.close(fd)
            except Exception:
                pass

        if problems:
            errors = [str(p) for p in problems]
            return False, current[0], errors

        return True, total, []

    def _execute_erase(
        self,
        op: QueuedOperation,
        pipe_state: dict,
        release_parent_after: bool = False
    ) -> Tuple[bool, int, List[str]]:
        """Execute an erase operation.

        Args:
            op: The operation to execute
            pipe_state: Dict with 'closed' bool and 'file' write handle
            release_parent_after: If True, send parent_can_exit before starting
        """
        import rpm

        # For background erase, release parent immediately (before erase starts)
        if release_parent_after and not pipe_state['closed']:
            pipe_state['file'].write(QueueProgressMessage(
                msg_type='parent_can_exit'
            ).to_json() + "\n")
            pipe_state['file'].flush()
            pipe_state['file'].close()
            pipe_state['closed'] = True
            _log_background("Parent released, starting background erase...")

        package_names = op.targets
        errors = []

        ts = rpm.TransactionSet(self.root)
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
            errors.append("No packages found to erase")
            return False, 0, errors

        # Check dependencies
        if not op.force:
            unresolved = ts.check()
            if unresolved:
                errors = [f"Dependency: {prob}" for prob in unresolved]
                return False, 0, errors

        ts.order()

        if op.test:
            return True, len(found), []

        # Callback
        total = len(found)
        current = [0]
        seen_names = set()  # Track already-counted packages

        def callback(reason, amount, total_pkg, key, client_data):
            if reason == rpm.RPMCALLBACK_UNINST_START:
                name = key if isinstance(key, str) else str(key)
                # Only count each package once
                if name and name not in seen_names:
                    seen_names.add(name)
                    current[0] += 1
                    # Send progress (if parent still listening)
                    if not pipe_state['closed']:
                        pipe_state['file'].write(QueueProgressMessage(
                            msg_type='progress',
                            operation_id=op.operation_id,
                            name=name,
                            current=current[0],
                            total=total
                        ).to_json() + "\n")

            elif reason == rpm.RPMCALLBACK_TRANS_STOP:
                _log_background(f"Erase complete: {total} packages")

        if op.force:
            ts.setProbFilter(rpm.RPMPROB_FILTER_REPLACEPKG)

        _log_background(f"Starting erase: {total} packages")
        problems = ts.run(callback, '')

        if problems:
            errors = [str(p) for p in problems]
            return False, current[0], errors

        return True, total, []
