"""Stage 0 Phase B safety checks (SPEC_DISTUPGRADE §4.0).

Each check is a plain callable that raises a specific exception with
an actionable message.  A caller (the Stage 0 orchestrator) runs
them in order + aborts on the first failure.

Checks in this module :

- :func:`check_boot_space`  — `/boot` size vs. Σ(kernel Tx A/B) × 2
  + 50 MB margin.  Dracut regenerates the initramfs at ``%posttrans``
  and takes 2× the size during the write ; the ``rpm.
  TransactionSet.check()`` deferral misses this because the
  regenerated initramfs is not owned by any RPM.
- :func:`check_min_kernel` — extracts ``libc.so.6`` from the target
  glibc RPM, parses its ``.note.ABI-tag`` via ``readelf -n``, and
  compares to ``uname -r``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from ...i18n import _, ngettext


# ── /boot check ─────────────────────────────────────────────────────


class BootSpaceError(Exception):
    """``/boot`` is too small for the kernel(s) about to install."""


# Margin added on top of the 2× kernel size, in bytes.
BOOT_MARGIN_BYTES = 50 * 1024 * 1024


def boot_files_size(rpm_path: Path) -> int:
    """Sum of file sizes deposited by ``rpm_path`` under ``/boot/``.

    Reads the file list + sizes directly from the RPM header via the
    ``rpm`` Python bindings — no subprocess, no text parsing.  Values
    are the RPM-declared file sizes ; the payload compression ratio
    is irrelevant to the eventual on-disk cost.

    Returns 0 if the RPM has no ``/boot/*`` payload (e.g. akmod-only
    kernel modules), if the file is missing, or if the header can't
    be parsed — upstream falls back to a « can't verify, play it
    safe » branch.
    """
    try:
        import rpm  # noqa: PLC0415 — kept local to isolate the import
    except ImportError:
        return 0
    # ``rpm.TransactionSet()`` opens rpmdb env even though we only
    # need to route ``hdrFromFdno`` on an on-disk RPM.  Close it
    # explicitly in ``finally`` — see :mod:`urpm.core.rpmdb` for
    # the leak rationale ; this file stays out of that central
    # module because .rpm on-disk parsing is signature-adjacent
    # territory that belongs with the distupgrade / install stack.
    ts = rpm.TransactionSet()
    try:
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
        try:
            fd = os.open(str(rpm_path), os.O_RDONLY)
        except OSError:
            return 0
        try:
            try:
                hdr = ts.hdrFromFdno(fd)
            except rpm.error:
                return 0
        finally:
            os.close(fd)
        names = hdr[rpm.RPMTAG_FILENAMES] or []
        sizes = hdr[rpm.RPMTAG_FILESIZES] or []
        total = 0
        for name, size in zip(names, sizes):
            if name.startswith("/boot/"):
                total += int(size)
        return total
    finally:
        try:
            ts.closeDB()
        except Exception:
            pass


def _boot_free_bytes(boot_path: Path = Path("/boot")) -> int:
    st = os.statvfs(boot_path)
    return st.f_bavail * st.f_frsize


def check_boot_space(kernel_rpms: Iterable[Path],
                     *,
                     boot_path: Path = Path("/boot")) -> None:
    """Refuse the distupgrade if ``/boot`` cannot host the new kernels.

    Formula : ``free_boot ≥ Σ(boot_files_size(rpm)) × 2 + 50 MB``.
    The 2× factor covers dracut keeping the previous initramfs
    around while writing the new one — observed via btrace on
    Mageia 10, non-atomic on ext4/xfs/btrfs.

    Raises :class:`BootSpaceError` with a copy-pasteable
    ``urpm autoremove --oldkernels`` recovery command.
    """
    per_rpm = [boot_files_size(rpm) for rpm in kernel_rpms]
    kernel_bytes = sum(per_rpm)
    if kernel_bytes == 0:
        # Nothing to install under /boot ; skip.
        return

    needed = kernel_bytes * 2 + BOOT_MARGIN_BYTES
    free = _boot_free_bytes(boot_path)
    if free >= needed:
        return

    free_mb = free // (1024 * 1024)
    needed_mb = needed // (1024 * 1024)
    kernel_mb = kernel_bytes // (1024 * 1024)
    raise BootSpaceError(
        _("{boot_path} has only {free_mb} MB free, {needed_mb} MB "
          "are required ({kernel_mb} MB × 2 for the dracut "
          "regeneration, + 50 MB margin). Purge old kernels / "
          "initramfs:").format(
            boot_path=boot_path, free_mb=free_mb,
            needed_mb=needed_mb, kernel_mb=kernel_mb) + "\n\n"
        "  urpm autoremove --oldkernels\n"
        "  dracut --regenerate-all --nolvmconf   # after the purge\n\n"
        + _("then re-run `urpm distupgrade`.")
    )


# ── MIN_KERNEL ─────────────────────────────────────────────────────


class MinKernelError(Exception):
    """Current kernel is too old for the target glibc."""


_ABI_LINE_RE = re.compile(
    r"OS:\s+Linux,?\s+ABI:\s+(\d+)\.(\d+)\.(\d+)"
)


def parse_min_kernel_from_readelf(readelf_output: str) -> Optional[Tuple[int, int, int]]:
    """Return the ``(x, y, z)`` triple from ``readelf -n`` output.

    Handles the standard English format ``OS: Linux, ABI: X.Y.Z``.
    Non-English output returns ``None`` — callers must pin
    ``LC_ALL=C`` (SPEC §0.6).
    """
    for line in readelf_output.splitlines():
        m = _ABI_LINE_RE.search(line)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def parse_running_kernel(uname_r: str) -> Optional[Tuple[int, int, int]]:
    """Return ``(x, y, z)`` from ``uname -r`` output.

    Tolerates Mageia's ``x.y.z-N.mgaN`` suffix.  Non-numeric prefix
    returns ``None``.
    """
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", uname_r.strip())
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def check_min_kernel(libc_path: Path,
                     *,
                     running_kernel: Optional[str] = None,
                     readelf_bin: str = "readelf") -> None:
    """Refuse when the target glibc's ``MIN_KERNEL`` exceeds ``uname -r``.

    ``libc_path`` is a plain ``libc.so.6`` file previously extracted
    from the target glibc RPM.  Extraction itself lives in the
    orchestrator (§4.0 sequence 4-step) so this function stays a
    pure « given X, decide » predicate.

    Raises :class:`MinKernelError` if the running kernel is older
    than the ELF-declared ``MIN_KERNEL`` ; returns silently
    otherwise, including when parsing fails (a soft failure — the
    orchestrator logs a warning and moves on rather than blocking
    a legitimate distupgrade on a locale glitch).
    """
    if not shutil.which(readelf_bin):
        return
    proc = subprocess.run(
        [readelf_bin, "-n", str(libc_path)],
        capture_output=True, text=True, check=False,
        env={"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"},
    )
    if proc.returncode != 0:
        return
    min_kernel = parse_min_kernel_from_readelf(proc.stdout)
    if min_kernel is None:
        return
    running_str = running_kernel
    if running_str is None:
        try:
            running_str = subprocess.run(
                ["uname", "-r"], capture_output=True, text=True,
                check=True,
                env={"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"},
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return
    running = parse_running_kernel(running_str)
    if running is None:
        return
    if running < min_kernel:
        rk = ".".join(str(x) for x in running)
        mk = ".".join(str(x) for x in min_kernel)
        raise MinKernelError(
            _("The running kernel ({rk}) is too old for the target "
              "glibc (which requires at least kernel {mk}).").format(
                rk=rk, mk=mk) + "\n\n"
            + _("After Tx A installs the new glibc, if distupgrade "
                "gets interrupted, the machine would try to reboot "
                "on the current kernel — which the new glibc no "
                "longer supports, leaving it unbootable.") + "\n\n"
            + _("Upgrade the kernel in the current release first:") + "\n\n"
            "  urpm upgrade kernel\n"
            "  reboot\n\n"
            + _("then re-run `urpm distupgrade`.")
        )


# ── PENDING REBOOT ─────────────────────────────────────────────────


class PendingRebootError(Exception):
    """A critical package was installed after the last boot ; running
    the distupgrade against the stale in-memory copy is unsafe."""


# Packages whose in-memory copy is load-bearing for the transaction
# itself : rpm scriptlets link against glibc, systemd runs pid 1, the
# running kernel dictates syscall behaviour.  When any of them was
# replaced under a running system, the kernel keeps executing the
# stale copy in every long-lived process until reboot — running
# distupgrade at that point mixes two glibc ABIs across a single
# ``rpm --root /`` cycle and typically dies mid-transaction on a
# scriptlet SIGABRT or PAM corruption.
_REBOOT_CRITICAL_NAMES = ("glibc", "systemd")


def _boot_time_epoch() -> Optional[int]:
    """Return the last boot's epoch from ``/proc/stat``'s ``btime``.

    Returns ``None`` when the file is unreadable — the caller then
    treats the check as inconclusive and lets the distupgrade proceed
    rather than blocking on a diagnostic gap.
    """
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def check_pending_reboot(
    *,
    boot_time: Optional[int] = None,
    installed_times: Optional[dict] = None,
    root: str = "/",
) -> None:
    """Refuse when a load-bearing package was installed after boot.

    Catches the classic « machine fichue » scenario : the operator
    ran a bulk ``urpmi`` that pulled glibc/systemd in, ignored the
    « please reboot » notice, and now launches ``urpm distupgrade``
    against a system whose in-memory copies of libc/pid-1 no longer
    match what's on disk.  Scriptlets link the new libc while
    long-lived processes still hold the old one ; typical failure
    modes include silent scriptlet crashes, PAM stack corruption,
    and DKMS build failures mid-transaction — leaving the machine
    unbootable.

    Args:
        boot_time: Epoch of the last boot.  Test seam ; live callers
            omit and the function reads ``/proc/stat``'s ``btime``
            line.
        installed_times: Mapping ``name → install_epoch``.  Test seam ;
            live callers omit and the function queries the rpmdb via
            :func:`urpm.core.rpmdb.open_ts`.
        root: Alternative rpmdb root (for ``--root`` scenarios).
            Ignored when ``installed_times`` is provided.

    Raises:
        PendingRebootError: with the offending package(s), the delay
            past-boot, and the one-line remedy.
    """
    if boot_time is None:
        boot_time = _boot_time_epoch()
    if boot_time is None:
        return
    if installed_times is None:
        import rpm  # noqa: PLC0415 — kept local per checks.py idiom
        from ..rpmdb import open_ts
        installed_times = {}
        with open_ts(root) as ts:
            for name in _REBOOT_CRITICAL_NAMES:
                for hdr in ts.dbMatch("name", name):
                    ts_installed = hdr[rpm.RPMTAG_INSTALLTIME]
                    if ts_installed:
                        prev = installed_times.get(name, 0)
                        installed_times[name] = max(prev, int(ts_installed))
    stale = [
        (name, ts) for name, ts in installed_times.items()
        if ts > boot_time
    ]
    if not stale:
        return
    lines = [
        "  " + ngettext(
            "{name} installed {minutes} min after the last boot",
            "{name} installed {minutes} min after the last boot",
            (ts - boot_time) // 60,
        ).format(name=name, minutes=(ts - boot_time) // 60)
        for name, ts in sorted(stale)
    ]
    raise PendingRebootError(
        _("A critical system package was installed since the last "
          "boot without rebooting:") + "\n\n"
        + "\n".join(lines)
        + "\n\n"
        + _("The machine is still running the old in-memory copy "
            "while a newer version sits on disk. Running distupgrade "
            "in this state would mix two glibc ABIs across a single "
            "rpm transaction — crashing scriptlets, corrupted PAM, "
            "DKMS failing mid-run, and a machine that no longer "
            "boots.") + "\n\n"
        + _("Reboot before running `urpm distupgrade`.")
    )
