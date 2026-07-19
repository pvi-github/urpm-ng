"""Resolve ``urpm build`` resource limits from CLI args and host state.

The public entry point is :func:`resolve_build_limits`, which turns the
three related CLI flags (``--build-cpus``, ``--build-memory``,
``--full-throttle``) into concrete values callers can hand to
``Container.run()`` (for the podman ceiling) and to ``rpmbuild -j``.

Defaults follow the principle that a normal ``urpm build`` invocation
should leave the host usable while the build runs:

* CPUs: ``max(1, nproc - 2)``.  Two cores stay free for interactive
  work.  Rationale: on a hot cache, GNOME/plasma + a browser tab already
  peak at two threads; taking the whole CPU makes typing lag.
* Memory: ``max(2G, MemTotal - 2G)``.  Leaves roughly 2 GB for the
  desktop.  Rationale: a running Firefox with a few tabs regularly sits
  at 1.5 GB — 2 GB is the smallest cushion that keeps the shell alive.

``--full-throttle`` skips both defaults: the build takes the whole host.
Individual overrides (``--build-cpus``, ``--build-memory``) win over
``--full-throttle`` for the axis they cover; the other axis stays
unbounded.  That composition lets the packager say "use 4 CPUs, but I
still don't want a memory cap" without repeating themselves.

Memory-swap semantics
=====================

When a memory cap is in force, ``--memory-swap`` is left *unbounded* by
default so the container can spill cold pages to the host swap — mock's
systemd-nspawn does the same thing implicitly, and closing that door is
the reason our containers OOM-kill on builds mock finishes on the same
hardware.  Under ``--strict-memory`` we tie ``--memory-swap`` back to
``--memory`` so nothing can escape into swap: useful in CI, where a
build that quietly starts swapping would show as a spurious timeout.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ─── Default reservations (see module docstring) ─────────────────────
_DEFAULT_CPU_RESERVE = 2
_DEFAULT_MEM_RESERVE_GB = 2
_MIN_CPUS = 1
_MIN_MEMORY_GB = 2


@dataclass(frozen=True)
class BuildLimits:
    """Resolved resource limits for a ``urpm build`` invocation.

    Attributes:
        cpus: Concrete cap on parallel build jobs, or ``None`` to leave
            the container runtime and rpmbuild use their own defaults
            (i.e. the whole host under ``--full-throttle``).
        memory: Concrete container RAM ceiling as a runtime-ready
            string (``"8G"``, ``"14000M"``), or ``None`` for uncapped.
        memory_swap: Value passed to podman's ``--memory-swap``.  The
            resolver picks ``"-1"`` (unlimited swap) when memory is
            capped and ``strict_memory`` is off — mock parity, so the
            container can spill cold pages onto host swap instead of
            being SIGKILL'd at the RAM ceiling.  When ``strict_memory``
            is on, this echoes ``memory`` so no swap is available.
            ``None`` means "let podman default" (typically twice
            ``memory``, but only relevant when memory is uncapped).
    """

    cpus: Optional[int]
    memory: Optional[str]
    memory_swap: Optional[str] = None

    @property
    def smp_mflags(self) -> Optional[str]:
        """Value to hand to ``rpmbuild --define '_smp_mflags ...'``.

        Returns ``None`` when no ``--build-cpus`` cap was requested, so
        the caller can skip the ``--define`` argument entirely.
        """
        return f'-j{self.cpus}' if self.cpus is not None else None


# ─── Memory-string parser ────────────────────────────────────────────

_MEMORY_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([kmgtKMGT]?)([bB]?)\s*$')

_UNIT_TO_BYTES = {
    '': 1,
    'k': 1024,
    'm': 1024 ** 2,
    'g': 1024 ** 3,
    't': 1024 ** 4,
}


def parse_memory(spec: str) -> int:
    """Parse a human memory string to a byte count.

    Accepts the forms podman and docker accept plus a few common
    variants: ``"8g"``, ``"8G"``, ``"8GB"``, ``"12000m"``, ``"12000MB"``.
    Bare numbers are interpreted as bytes (again matching podman).

    Args:
        spec: Human-readable memory size.

    Returns:
        The size in bytes.

    Raises:
        ValueError: If ``spec`` doesn't match a recognised format.
    """
    match = _MEMORY_RE.match(spec)
    if not match:
        raise ValueError(
            f"unrecognised memory spec {spec!r} (expected e.g. '8G', "
            f"'12000M', '16GB')"
        )
    number, unit, _b = match.groups()
    return int(float(number) * _UNIT_TO_BYTES[unit.lower()])


def _format_memory_for_runtime(byte_count: int) -> str:
    """Render ``byte_count`` in a form podman/docker accept back.

    Whole-gigabyte values become ``"NG"`` for readability in logs and
    ``ps`` output; anything not aligned falls back to megabytes.
    """
    gib = 1024 ** 3
    mib = 1024 ** 2
    if byte_count % gib == 0:
        return f"{byte_count // gib}G"
    return f"{byte_count // mib}M"


# ─── Host discovery ──────────────────────────────────────────────────

def _host_cpus() -> int:
    """Return the number of CPUs the current process may schedule on.

    ``os.sched_getaffinity`` respects taskset/cgroups (containers where
    urpm itself runs pinned), which ``os.cpu_count()`` does not.  Falls
    back to ``cpu_count`` on platforms where affinity is unavailable
    (macOS build hosts, some older kernels).
    """
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _host_memory_bytes() -> Optional[int]:
    """Read ``MemTotal`` from ``/proc/meminfo``.

    Returns ``None`` if the file is unreadable (non-Linux hosts, unusual
    sandboxes).  Callers treat ``None`` as "no memory information
    available", which cascades into skipping the memory cap.
    """
    try:
        content = Path('/proc/meminfo').read_text()
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith('MemTotal:'):
            parts = line.split()
            # Format: ``MemTotal:       32748124 kB``
            if len(parts) >= 3 and parts[2].lower() == 'kb':
                return int(parts[1]) * 1024
    return None


# ─── Public resolver ─────────────────────────────────────────────────

def resolve_build_limits(
    *,
    build_cpus: Optional[int],
    build_memory: Optional[str],
    full_throttle: bool,
    strict_memory: bool = False,
) -> BuildLimits:
    """Turn the CLI knobs into concrete container limits.

    Precedence rules:

    * ``--build-cpus`` and ``--build-memory``, when set, always win on
      their own axis — even when ``--full-throttle`` is also set.
    * ``--full-throttle`` alone (or paired with only one of the two)
      leaves the *other* axis uncapped.
    * When neither ``--full-throttle`` nor the explicit flag is set,
      the reservation-based default kicks in.

    ``strict_memory`` only takes effect when a memory cap is in force:
    it ties ``memory_swap`` back to ``memory`` so the container has no
    swap headroom, matching how the pre-refactor code behaved.
    Without it the resolver sets ``memory_swap`` to ``"-1"`` (unlimited
    host swap) so the container can spill cold pages the way a
    mock/systemd-nspawn build does.

    Args:
        build_cpus: Value of ``--build-cpus`` (``None`` when unset).
        build_memory: Value of ``--build-memory`` (``None`` when unset).
        full_throttle: Whether ``--full-throttle`` was passed.
        strict_memory: Whether ``--strict-memory`` was passed.

    Returns:
        A :class:`BuildLimits` with concrete values (or ``None`` where
        the axis should stay uncapped).

    Raises:
        ValueError: If ``build_cpus`` is not a positive integer, or
            ``build_memory`` cannot be parsed.
    """
    if build_cpus is not None and build_cpus < _MIN_CPUS:
        raise ValueError(
            f"--build-cpus must be >= {_MIN_CPUS} (got {build_cpus})"
        )

    if build_cpus is not None:
        cpus: Optional[int] = build_cpus
    elif full_throttle:
        cpus = None
    else:
        cpus = max(_MIN_CPUS, _host_cpus() - _DEFAULT_CPU_RESERVE)

    if build_memory is not None:
        mem_bytes = parse_memory(build_memory)
        memory: Optional[str] = _format_memory_for_runtime(mem_bytes)
    elif full_throttle:
        memory = None
    else:
        host_bytes = _host_memory_bytes()
        if host_bytes is None:
            # Can't discover host RAM — better to leave it uncapped
            # than to guess and starve the build.
            memory = None
        else:
            reserve = _DEFAULT_MEM_RESERVE_GB * (1024 ** 3)
            usable = max(_MIN_MEMORY_GB * (1024 ** 3),
                         host_bytes - reserve)
            memory = _format_memory_for_runtime(usable)

    # Only touch --memory-swap when memory is capped: without a memory
    # ceiling the swap ceiling has no meaning to podman anyway.
    if memory is None:
        memory_swap: Optional[str] = None
    elif strict_memory:
        memory_swap = memory
    else:
        memory_swap = "-1"

    return BuildLimits(cpus=cpus, memory=memory, memory_swap=memory_swap)
