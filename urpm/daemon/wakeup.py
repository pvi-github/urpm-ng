"""Wake-up detection for urpmd.

Detects system resume from suspend/hibernate so that urpmd can
trigger a media refresh and peer re-discovery immediately after
the machine comes back online, rather than waiting for the next
scheduled tick.

Two complementary checks are provided:

- **WakeupDetector** — clock-divergence heuristic that compares
  wall-clock time (`time.time()`) against monotonic time
  (`time.monotonic()`).  On Linux, ``CLOCK_MONOTONIC`` stops
  during suspend while ``CLOCK_REALTIME`` keeps advancing,
  producing a measurable gap when the system wakes up.

- **has_default_route()** — lightweight network-availability
  probe that parses ``/proc/net/route`` for a default gateway.
  Useful to avoid triggering a sync when the network is not yet
  ready after resume.

No external dependencies; pure Python + ``/proc``.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class WakeupDetector:
    """Detect system wake-up from suspend/hibernate.

    Uses clock divergence between wall clock and monotonic clock.
    On Linux, ``CLOCK_MONOTONIC`` stops during suspend while
    ``CLOCK_REALTIME`` advances, creating a measurable gap.

    Typical usage::

        detector = WakeupDetector()
        # inside your main loop / scheduler tick:
        if detector.check():
            trigger_media_refresh()
    """

    DIVERGENCE_THRESHOLD: float = 120.0  # seconds — above this = wake-up

    def __init__(self) -> None:
        self._last_wall: float = time.time()
        self._last_mono: float = time.monotonic()

    def check(self) -> bool:
        """Check for wake-up since last call.  Call once per tick.

        Compares the elapsed wall-clock time with the elapsed
        monotonic time since the previous call.  If the wall clock
        advanced significantly more than the monotonic clock, a
        suspend/resume cycle must have occurred.

        Returns:
            ``True`` if a suspend/resume was detected, ``False``
            otherwise.

        Note:
            The very first call after ``__init__`` will almost
            always return ``False`` because both clocks start in
            sync.  A manual NTP jump larger than the threshold
            could theoretically cause a false positive, but the
            120 s default makes this unlikely in practice.
        """
        now_wall: float = time.time()
        now_mono: float = time.monotonic()

        wall_elapsed: float = now_wall - self._last_wall
        mono_elapsed: float = now_mono - self._last_mono

        # Update reference points *before* the return so that
        # the next call measures from this instant regardless of
        # whether we detected a wake-up.
        self._last_wall = now_wall
        self._last_mono = now_mono

        divergence: float = wall_elapsed - mono_elapsed

        if divergence > self.DIVERGENCE_THRESHOLD:
            logger.info(
                "Wake-up detected: wall elapsed %.1f s, "
                "monotonic elapsed %.1f s, divergence %.1f s",
                wall_elapsed,
                mono_elapsed,
                divergence,
            )
            return True

        return False


def has_default_route() -> bool:
    """Check if a default route exists (network likely available).

    Parses ``/proc/net/route`` looking for a route whose
    destination is ``00000000`` (i.e. the default gateway).
    This is a lightweight, dependency-free check that avoids
    spawning a subprocess or opening a socket.

    Returns:
        ``True`` if a default route was found, ``False`` if no
        default route exists or if ``/proc/net/route`` could not
        be read (e.g. inside a minimal container).
    """
    try:
        with open("/proc/net/route", "r") as fh:
            for line in fh:
                fields = line.strip().split("\t")
                # First line is a header; data lines have >= 11
                # tab-separated fields.  Field 1 is the hex
                # destination — 00000000 means default route.
                if len(fields) >= 2 and fields[1] == "00000000":
                    logger.debug(
                        "Default route found on interface %s",
                        fields[0],
                    )
                    return True
    except (IOError, OSError) as exc:
        logger.debug("Cannot read /proc/net/route: %s", exc)
        return False

    logger.debug("No default route found")
    return False
