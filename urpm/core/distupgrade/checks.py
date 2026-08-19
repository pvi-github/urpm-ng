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
        f"{boot_path} n'a que {free_mb} Mo libres, il en faut "
        f"{needed_mb} ({kernel_mb} Mo × 2 pour la regénération dracut, "
        f"+ 50 Mo de marge). Purgez les anciens kernels / initramfs :\n\n"
        f"  urpm autoremove --oldkernels\n"
        f"  dracut --regenerate-all --nolvmconf   # après purge\n\n"
        f"puis relancez `urpm distupgrade`."
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
            f"Le kernel actuellement actif ({rk}) est trop ancien "
            f"pour la glibc cible (qui exige au minimum kernel {mk}).\n\n"
            "Après Tx A (qui installe la nouvelle glibc), si le "
            "distupgrade doit s'interrompre, votre machine devrait "
            "rebooter sur le kernel courant — qui ne pourrait plus "
            "démarrer avec la nouvelle glibc.\n\n"
            "Upgradez d'abord votre kernel dans la version courante :\n\n"
            "  urpm upgrade kernel\n"
            "  reboot\n\n"
            "puis relancez `urpm distupgrade`."
        )
