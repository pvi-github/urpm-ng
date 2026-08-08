"""Stage 0 orchestrator (SPEC_DISTUPGRADE §4.0).

Ties the primitives together and exposes a single ``run_stage0``
entry point for :func:`urpm.cli.commands.distupgrade.cmd_distupgrade`
to call.  Written so a caller can invoke it repeatedly against a
running system without side effects on the rpmdb until Phase A
upgrade fires — every earlier step is either read-only or writes
only to ``/var/lib/urpm/distupgrade.state``.

The orchestrator does NOT own the entire distupgrade flow — Stage 1
(media swap) and beyond live in sibling modules.  Stage 0 stops
right after Phase B checks pass and the state file bump to
``pre_check_done`` is durable.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .checks import (
    check_boot_space,
    check_min_kernel,
)
from .lock import (
    DistupgradeLockedError,
    acquire_distupgrade_lock,
)
from .phase_a import PhaseAError, run_phase_a_refresh, run_phase_a_upgrade
from .stage0 import ClockGateError, check_clock_sanity
from .state import write_state
from .version import ReleaseIdentity, VersionDetectionError, detect_target_release, read_current_release


if TYPE_CHECKING:
    from ..database import PackageDatabase


logger = logging.getLogger(__name__)


class Stage0Result:
    """Return value of :func:`run_stage0` — carries the lock fd + target."""

    def __init__(self, lock_fd: int, target: ReleaseIdentity,
                 current: Optional[str]):
        self.lock_fd = lock_fd
        self.target = target
        self.current = current


class Stage0Error(Exception):
    """Raised for any Stage 0 refusal — carries the user-actionable message."""


def run_stage0(
    db: "PackageDatabase",
    *,
    user_supplied_target: Optional[str] = None,
    kernel_rpms: Optional[list] = None,
    libc_paths: Optional[list] = None,
    skip_phase_a_upgrade: bool = False,
    phase_a_download_progress=None,
    phase_a_install_progress=None,
    phase_a_on_plan_computed=None,
) -> Stage0Result:
    """Run every Stage 0 gate in order and return the acquired lock.

    Steps :

    1. Clock sanity — :func:`check_clock_sanity`.
    2. Version detection — :func:`detect_target_release`.
    3. Distupgrade lock acquisition — :func:`acquire_distupgrade_lock`.
    4. Initial state write (stage=``pre_check_started``).
    5. Phase A refresh — :func:`run_phase_a_refresh`.
    6. Phase A upgrade — :func:`run_phase_a_upgrade` (skippable for
       tests via ``skip_phase_a_upgrade`` kwarg).
    7. Phase B checks — rpmdb integrity, ``/boot`` size, MIN_KERNEL.
    8. State bump to ``pre_check_done``.

    Every failure raises :class:`Stage0Error` with an actionable
    message ready to be printed to stderr.  On success returns a
    :class:`Stage0Result` — the caller is responsible for calling
    :func:`urpm.core.distupgrade.lock.release_distupgrade_lock` on
    the ``lock_fd`` when Stage 5 completes (or on ``--abort``).
    """
    # 1. Clock sanity
    try:
        check_clock_sanity()
    except ClockGateError as exc:
        raise Stage0Error(str(exc)) from exc

    # 2. Version detection
    current = read_current_release()
    try:
        target = detect_target_release(
            current=current, user_supplied=user_supplied_target)
    except VersionDetectionError as exc:
        raise Stage0Error(str(exc)) from exc

    # 3. Lock
    try:
        lock_fd = acquire_distupgrade_lock()
    except DistupgradeLockedError as exc:
        raise Stage0Error(str(exc)) from exc

    # 4. Initial state write
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_state({
        "version_from": current or "unknown",
        "version_to": target.display(),
        "started_at": started_at,
        "stage": "pre_check_started",
    }, db)

    # 5. Phase A refresh (media metadata)
    try:
        run_phase_a_refresh(db)
    except PhaseAError as exc:
        raise Stage0Error(str(exc)) from exc

    # 6. Phase A upgrade — mutant, may be skipped by tests
    if not skip_phase_a_upgrade:
        try:
            run_phase_a_upgrade(
                db,
                progress_callback=phase_a_download_progress,
                install_progress_callback=phase_a_install_progress,
                on_plan_computed=phase_a_on_plan_computed,
            )
        except PhaseAError as exc:
            raise Stage0Error(str(exc)) from exc

    # 7. Phase B checks — rpmdb integrity dropped from v1 (5-10 min
    # walk on ~3000 pkgs and doesn't catch anything not surfaced by
    # the first real transaction).  Kept: boot space + libc/kernel
    # ABI floor.
    if kernel_rpms:
        try:
            check_boot_space([Path(p) for p in kernel_rpms])
        except Exception as exc:  # BootSpaceError
            raise Stage0Error(str(exc)) from exc

    if libc_paths:
        for libc in libc_paths:
            try:
                check_min_kernel(Path(libc))
            except Exception as exc:  # MinKernelError
                raise Stage0Error(str(exc)) from exc

    # 8. State bump to pre_check_done
    write_state({
        "version_from": current or "unknown",
        "version_to": target.display(),
        "started_at": started_at,
        "stage": "pre_check_done",
    }, db)

    return Stage0Result(lock_fd=lock_fd, target=target, current=current)
