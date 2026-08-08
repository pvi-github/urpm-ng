"""Distupgrade engine (SPEC_DISTUPGRADE §4).

The submodules split the flow along the spec's stage boundaries :

- :mod:`urpm.core.distupgrade.state` — atomic ``.state`` on disk.
- :mod:`urpm.core.distupgrade.lock`  — ``/run/urpm/distupgrade.lock``
  acquisition with the openat + orphan-retry pattern.
- :mod:`urpm.core.distupgrade.stage0` — pre-checks (clock gate,
  Phase A refresh, Phase B checks) that gate any mutation.

Future submodules will add ``stage1`` (media swap), ``stage2`` (
pre-load), ``stage3`` (Tx A/B) and ``stage4_5`` (post-tx + post-boot).
"""

from .checks import (
    BootSpaceError,
    MinKernelError,
    boot_files_size,
    check_boot_space,
    check_min_kernel,
)
from .orchestrator import (
    Stage0Error,
    Stage0Result,
    run_stage0,
)
from .stage1 import (
    Stage1Error,
    run_stage1,
)
from .stage2 import (
    Stage2Aborted,
    Stage2Error,
    download_plan,
    run_stage2,
    solve_distupgrade,
)
from .manifest import (
    BOOT_CRITICAL_LOCKS_BASE,
    BOOT_CRITICAL_LOCKS_UEFI,
    TRANSACTION_A_PROVIDES,
    boot_critical_locks,
    split_plan_for_tx_a_and_b,
)
from .stage3 import (
    SmokeTestFailure,
    Stage3Error,
    execvp_to_continue,
    run_stage3_tx_a,
    run_stage3_tx_b,
    smoke_test_target_stack,
)
from .stages_4_5 import (
    Stage4Error,
    clear_postboot_marker,
    read_postboot_marker,
    run_stage4,
    run_stage5_if_pending,
    write_postboot_marker,
)
from .phase_a import (
    PhaseAError,
    run_phase_a_refresh,
    run_phase_a_upgrade,
)
from .version import (
    ReleaseIdentity,
    VersionDetectionError,
    detect_target_release,
    parse_release_arg,
    read_current_release,
)
from .lock import (
    DistupgradeLockedError,
    acquire_distupgrade_lock,
    release_distupgrade_lock,
)
from .state import (
    DistupgradeStateError,
    delete_state,
    read_state,
    write_state,
)
from .stage0 import (
    ClockGateError,
    check_clock_sanity,
)

__all__ = [
    "BootSpaceError",
    "ClockGateError",
    "DistupgradeLockedError",
    "DistupgradeStateError",
    "MinKernelError",
    "PhaseAError",
    "ReleaseIdentity",
    "Stage0Error",
    "Stage0Result",
    "Stage1Error",
    "BOOT_CRITICAL_LOCKS_BASE",
    "BOOT_CRITICAL_LOCKS_UEFI",
    "SmokeTestFailure",
    "Stage2Aborted",
    "Stage2Error",
    "Stage3Error",
    "Stage4Error",
    "TRANSACTION_A_PROVIDES",
    "boot_critical_locks",
    "clear_postboot_marker",
    "read_postboot_marker",
    "run_stage4",
    "run_stage5_if_pending",
    "split_plan_for_tx_a_and_b",
    "write_postboot_marker",
    "download_plan",
    "execvp_to_continue",
    "run_stage3_tx_a",
    "run_stage3_tx_b",
    "smoke_test_target_stack",
    "run_stage1",
    "run_stage2",
    "solve_distupgrade",
    "VersionDetectionError",
    "detect_target_release",
    "parse_release_arg",
    "read_current_release",
    "run_phase_a_refresh",
    "run_phase_a_upgrade",
    "run_stage0",
    "acquire_distupgrade_lock",
    "boot_files_size",
    "check_boot_space",
    "check_clock_sanity",
    "check_min_kernel",
    "delete_state",
    "read_state",
    "release_distupgrade_lock",
    "write_state",
]
