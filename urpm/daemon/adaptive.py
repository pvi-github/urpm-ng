"""Adaptive per-media polling frequency module.

Implements an adaptive polling strategy using a **periodic Gaussian** curve
with a **watch mode** fallback. The idea: media repositories tend to update
at regular intervals (e.g. every 6 hours). By learning the historical update
cadence (μ, σ) we can poll aggressively near expected update times and relax
in between, saving bandwidth and server load.

Algorithm overview:
    1. Collect deltas (seconds between consecutive real content changes).
    2. Compute μ (mean) and σ (stddev) from the last MAX_DELTAS deltas.
    3. Model expected updates as periodic peaks at multiples of μ.
    4. Near a peak: use a Gaussian bell → short interval (down to I_MIN).
    5. Far from any peak ("watch zone"): use constant I_WATCH.
    6. Otherwise: Gaussian tail → longer interval (up to I_MAX).

When insufficient history is available (< MIN_DELTAS), fall back to a
conservative constant DEFAULT_PERIOD.
"""

import logging
import math
import statistics
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.database import PackageDatabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

I_MIN: int = 600
"""Minimum polling interval in seconds (10 min) — used at peak expected time."""

I_MAX: int = 21600
"""Maximum polling interval in seconds (6 h) — used far from expected time."""

I_WATCH: int = 3600
"""Watch-mode interval in seconds (1 h) — used when update window was missed."""

DEFAULT_PERIOD: int = 21600
"""Default assumed period in seconds (6 h) when no history is available."""

SAFETY_FLOOR: int = 1800
"""Minimum allowed F(media) in seconds (30 min)."""

MAX_DELTAS: int = 30
"""Maximum number of deltas kept per media (sliding window)."""

MIN_DELTAS: int = 3
"""Minimum number of deltas required before computing real statistics."""


# ---------------------------------------------------------------------------
# Pure computation helpers
# ---------------------------------------------------------------------------

def compute_media_period(deltas: list[int]) -> tuple[float, float, float]:
    """Compute μ, σ, and F(media) from delta history.

    When enough deltas are available, μ and σ are derived from the sample.
    F(media) is a conservative estimate of the true period, defined as
    ``max(SAFETY_FLOOR, μ - 1.28 * σ)`` (≈ 10th percentile assuming
    roughly normal distribution).

    Args:
        deltas: List of ``delta_seconds`` between real content changes.

    Returns:
        Tuple ``(mu, sigma, period)`` where *period* =
        ``max(SAFETY_FLOOR, mu - 1.28 * sigma)``.
        Returns ``(DEFAULT_PERIOD, DEFAULT_PERIOD / 4, DEFAULT_PERIOD)``
        when fewer than :data:`MIN_DELTAS` samples are available.
    """
    if len(deltas) < MIN_DELTAS:
        return (
            float(DEFAULT_PERIOD),
            float(DEFAULT_PERIOD) / 4.0,
            float(DEFAULT_PERIOD),
        )

    mu = statistics.mean(deltas)
    # stdev requires at least 2 values; we have >= MIN_DELTAS (3).
    sigma = statistics.stdev(deltas)

    period = max(float(SAFETY_FLOOR), mu - 1.28 * sigma)
    return (mu, sigma, period)


def compute_polling_interval(
    t_since_last_change: float,
    mu: float,
    sigma: float,
) -> float:
    """Compute adaptive polling interval using periodic Gaussian + watch mode.

    The function models expected updates as periodic peaks at multiples of *μ*.
    A Gaussian bell around each peak drives the interval down to :data:`I_MIN`.
    Between peaks — when the update window has clearly been missed but the next
    one is still far away — the function returns the constant :data:`I_WATCH`
    ("vigilance mode").

    Edge cases:
        - *sigma* = 0 (all deltas identical): returns :data:`I_MIN` exactly at
          peaks and :data:`I_WATCH` everywhere else.
        - *t_since_last_change* = 0: treated as being right at a peak, so the
          interval will be near :data:`I_MIN`.

    Args:
        t_since_last_change: Seconds since last real content change.
        mu: Mean update interval for this media.
        sigma: Standard deviation of update intervals.

    Returns:
        Polling interval in seconds, in the range
        [:data:`I_MIN`, :data:`I_MAX`].
    """
    t = t_since_last_change

    # Guard against degenerate μ
    if mu <= 0:
        return float(I_MAX)

    # Find the nearest expected peak (multiple of μ)
    k = round(t / mu)
    if k < 1:
        k = 1

    distance = t - k * mu

    # Handle sigma == 0: updates are perfectly regular.
    # At the exact peak → I_MIN, otherwise → I_WATCH.
    if sigma <= 0:
        if abs(distance) < 1.0:  # effectively at the peak
            return float(I_MIN)
        return float(I_WATCH)

    next_peak = (k + 1) * mu

    # Check whether we are in the "watch zone": past the current peak's
    # tail (> +2σ) and not yet within the next peak's influence (< -2σ).
    in_watch_zone = (t > k * mu + 2 * sigma) and (t < next_peak - 2 * sigma)

    if in_watch_zone:
        logger.debug(
            "Watch zone: t=%.0fs, nearest peak k=%d (%.0fs), next peak %.0fs",
            t, k, k * mu, next_peak,
        )
        return float(I_WATCH)

    # Gaussian bell centred on the nearest peak
    exponent = -(distance ** 2) / (2 * sigma ** 2)
    interval = I_MAX - (I_MAX - I_MIN) * math.exp(exponent)
    return interval


# ---------------------------------------------------------------------------
# Stateful helpers (interact with the database)
# ---------------------------------------------------------------------------

def record_content_change(
    db: "PackageDatabase",
    media_id: int,
    now: float | None = None,
) -> None:
    """Record that media content actually changed.

    Performs the following sequence:

    1. Compute the delta from the previous change (``adaptive_last_changed``
       stored on the media row).
    2. Insert the delta into ``media_update_deltas``.
    3. Prune old deltas beyond :data:`MAX_DELTAS`.
    4. Recompute μ, σ, and period — cache them on the media row.
    5. Update ``adaptive_last_changed`` to *now*.

    If no previous change is recorded (first observation), we only update
    ``adaptive_last_changed`` without inserting a delta.

    Args:
        db: Database instance exposing ``get_media_by_id()``,
            ``record_media_update_delta()``, ``get_media_update_deltas()``,
            ``prune_media_update_deltas()``, and
            ``update_media_adaptive_state()`` methods.
        media_id: Media ID.
        now: Current timestamp (defaults to ``time.time()``).
    """
    if now is None:
        now = time.time()

    media = db.get_media_by_id(media_id)
    if media is None:
        logger.warning("record_content_change: unknown media_id=%d", media_id)
        return

    last_changed = media.get("adaptive_last_changed")

    if last_changed is not None and last_changed > 0:
        delta = int(now - last_changed)
        if delta > 0:
            db.record_media_update_delta(media_id, int(now), delta)
            db.prune_media_update_deltas(media_id, MAX_DELTAS)
            logger.info(
                "Media %d: recorded delta %ds (%.1fh)",
                media_id, delta, delta / 3600.0,
            )
        else:
            logger.debug(
                "Media %d: non-positive delta %ds ignored", media_id, delta,
            )
    else:
        logger.info(
            "Media %d: first content change observed, no delta yet", media_id,
        )

    # Recompute statistics from the full (pruned) history
    deltas = db.get_media_update_deltas(media_id)
    mu, sigma, period = compute_media_period(deltas)

    db.update_media_adaptive_state(
        media_id,
        period=int(period),
        mu=mu,
        sigma=sigma,
        last_changed=int(now),
    )

    logger.info(
        "Media %d: adaptive state updated — μ=%.0fs (%.1fh), σ=%.0fs, "
        "F=%.0fs (%.1fh), %d deltas",
        media_id, mu, mu / 3600.0, sigma, period, period / 3600.0,
        len(deltas),
    )


def get_adaptive_interval(
    db: "PackageDatabase",
    media_id: int,
    now: float | None = None,
) -> float:
    """Get the current adaptive polling interval for a media.

    Reads the cached adaptive state (μ, σ, last-changed timestamp) from the
    media row and delegates to :func:`compute_polling_interval`.  Falls back
    to :data:`I_MAX` when no history is available (conservative constant
    polling).

    Args:
        db: Database instance exposing ``get_media_by_id()``.
        media_id: Media ID.
        now: Current timestamp (defaults to ``time.time()``).

    Returns:
        Polling interval in seconds.
    """
    if now is None:
        now = time.time()

    media = db.get_media_by_id(media_id)
    if media is None:
        logger.warning("get_adaptive_interval: unknown media_id=%d", media_id)
        return float(I_MAX)

    mu = media.get("adaptive_mu")
    sigma = media.get("adaptive_sigma")
    last_changed = media.get("adaptive_last_changed")

    # No adaptive state yet — conservative fallback
    if mu is None or sigma is None or last_changed is None:
        return float(I_MAX)
    if mu <= 0:
        return float(I_MAX)

    t_since = now - last_changed
    if t_since < 0:
        # Clock skew or bogus timestamp — be conservative
        logger.warning(
            "Media %d: negative t_since_last_change (%.0fs), using I_MAX",
            media_id, t_since,
        )
        return float(I_MAX)

    interval = compute_polling_interval(t_since, mu, sigma)
    logger.debug(
        "Media %d: t_since=%.0fs, μ=%.0fs, σ=%.0fs → interval=%.0fs",
        media_id, t_since, mu, sigma, interval,
    )
    return interval
