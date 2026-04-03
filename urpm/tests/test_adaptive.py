"""Tests for the adaptive media polling module.

The adaptive polling system adjusts how often urpmd checks for media
updates based on observed update frequency.  The two core functions are
pure math — no I/O, no mocking needed.
"""

import pytest

from urpm.daemon.adaptive import (
    compute_media_period,
    compute_polling_interval,
    DEFAULT_PERIOD,
    I_MAX,
    I_MIN,
    I_WATCH,
    SAFETY_FLOOR,
)


# ---------------------------------------------------------------------------
# compute_media_period()
# ---------------------------------------------------------------------------


class TestComputeMediaPeriod:
    """Tests for compute_media_period()."""

    def test_no_history(self):
        """No deltas → DEFAULT_PERIOD."""
        mu, sigma, period = compute_media_period([])
        assert period == DEFAULT_PERIOD

    def test_few_deltas(self):
        """Fewer than MIN_DELTAS → DEFAULT_PERIOD."""
        mu, sigma, period = compute_media_period([3600, 3600])
        assert period == DEFAULT_PERIOD

    def test_normal_distribution(self):
        """Known deltas → correct μ, σ, period = max(SAFETY_FLOOR, μ - 1.28σ)."""
        # 48h ± 6h
        deltas = [48 * 3600, 42 * 3600, 54 * 3600, 48 * 3600, 45 * 3600, 51 * 3600]
        mu, sigma, period = compute_media_period(deltas)
        assert abs(mu - 48 * 3600) < 1 * 3600  # roughly 48h
        assert sigma > 0
        assert period == max(SAFETY_FLOOR, mu - 1.28 * sigma)

    def test_safety_floor(self):
        """Very small/consistent deltas → period capped at SAFETY_FLOOR."""
        deltas = [600, 600, 600, 600, 600]  # 10 min intervals, σ≈0
        mu, sigma, period = compute_media_period(deltas)
        assert period == SAFETY_FLOOR

    def test_identical_deltas(self):
        """All same → σ=0, period = max(SAFETY_FLOOR, μ)."""
        deltas = [7200, 7200, 7200, 7200]
        mu, sigma, period = compute_media_period(deltas)
        assert sigma == 0
        assert period == max(SAFETY_FLOOR, mu)


# ---------------------------------------------------------------------------
# compute_polling_interval()
# ---------------------------------------------------------------------------


class TestComputePollingInterval:
    """Tests for compute_polling_interval()."""

    def test_at_zero(self):
        """t=0, just after sync → interval close to I_MAX."""
        mu, sigma = 48 * 3600, 6 * 3600
        interval = compute_polling_interval(0, mu, sigma)
        # distance to nearest peak (k=1, peak at 48h) is large → Gaussian ≈ 0 → I_MAX
        assert interval > I_MAX * 0.9

    def test_at_peak(self):
        """t=μ, exactly at expected time → interval close to I_MIN."""
        mu, sigma = 48 * 3600, 6 * 3600
        interval = compute_polling_interval(mu, mu, sigma)
        # distance = 0 → Gaussian = 1 → I_MIN
        assert interval < I_MIN * 1.1

    def test_at_second_peak(self):
        """t=2μ, at second expected time → interval close to I_MIN."""
        mu, sigma = 48 * 3600, 6 * 3600
        interval = compute_polling_interval(2 * mu, mu, sigma)
        assert interval < I_MIN * 1.1

    def test_between_peaks_watch_mode(self):
        """t between peaks, past 2σ window → I_WATCH."""
        mu, sigma = 48 * 3600, 2 * 3600  # small sigma
        # t at k=1 peak + 3σ → well into watch zone
        t = mu + 3 * sigma
        interval = compute_polling_interval(t, mu, sigma)
        assert interval == I_WATCH

    def test_approaching_peak(self):
        """t approaching μ → interval decreasing."""
        mu, sigma = 48 * 3600, 6 * 3600
        i_far = compute_polling_interval(24 * 3600, mu, sigma)
        i_near = compute_polling_interval(44 * 3600, mu, sigma)
        assert i_far > i_near

    def test_after_peak_symmetry(self):
        """Gaussian is symmetric around peak."""
        mu, sigma = 48 * 3600, 6 * 3600
        i_before = compute_polling_interval(mu - 3 * 3600, mu, sigma)
        i_after = compute_polling_interval(mu + 3 * 3600, mu, sigma)
        assert abs(i_before - i_after) < 60  # nearly equal

    def test_sigma_zero(self):
        """σ=0 (perfectly regular media) → I_MIN at peaks, graceful elsewhere."""
        mu = 48 * 3600
        # At peak: should return I_MIN
        interval = compute_polling_interval(mu, mu, 0)
        assert interval <= I_MIN * 1.1
        # Away from peak: should not crash, return I_WATCH or I_MAX
        interval = compute_polling_interval(mu + 3600, mu, 0)
        assert interval >= I_WATCH

    def test_weekly_media_missed_friday(self):
        """Weekly media, missed update → watch mode → repeak next week."""
        mu, sigma = 7 * 24 * 3600, 2 * 3600  # weekly, σ=2h
        # At expected time (Friday): aggressive
        i_friday = compute_polling_interval(mu, mu, sigma)
        assert i_friday < I_MIN * 1.1
        # Saturday (missed, in watch zone): I_WATCH
        i_saturday = compute_polling_interval(mu + 24 * 3600, mu, sigma)
        assert i_saturday == I_WATCH
        # Next Friday (2μ): aggressive again
        i_next_friday = compute_polling_interval(2 * mu, mu, sigma)
        assert i_next_friday < I_MIN * 1.1
