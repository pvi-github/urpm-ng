"""Tests for the wake-up detection module (urpm.daemon.wakeup).

WakeupDetector detects system suspend/resume by comparing wall clock
(time.time) and monotonic clock (time.monotonic) drift between calls.
has_default_route() checks /proc/net/route for network availability.
"""

from unittest.mock import patch, mock_open

from urpm.daemon.wakeup import WakeupDetector, has_default_route

# Patch targets — patch where the module looks up time, not the time module
_TIME = 'urpm.daemon.wakeup.time.time'
_MONO = 'urpm.daemon.wakeup.time.monotonic'


class TestWakeupDetector:
    """Tests for WakeupDetector."""

    def test_normal_tick_no_wakeup(self):
        """Normal tick (wall and mono advance equally) → no wake-up."""
        with patch(_TIME, return_value=1000.0), \
             patch(_MONO, return_value=1000.0):
            detector = WakeupDetector()
        with patch(_TIME, return_value=1060.0), \
             patch(_MONO, return_value=1060.0):
            assert detector.check() is False

    def test_suspend_detected(self):
        """Wall clock jumps but mono doesn't → wake-up detected."""
        with patch(_TIME, return_value=1000.0), \
             patch(_MONO, return_value=1000.0):
            detector = WakeupDetector()
        # 2h wall, 60s mono → 7140s divergence
        with patch(_TIME, return_value=8200.0), \
             patch(_MONO, return_value=1060.0):
            assert detector.check() is True

    def test_small_divergence_ignored(self):
        """Small NTP-like drift (< threshold) → no wake-up."""
        with patch(_TIME, return_value=1000.0), \
             patch(_MONO, return_value=1000.0):
            detector = WakeupDetector()
        # 5s divergence (NTP correction)
        with patch(_TIME, return_value=1065.0), \
             patch(_MONO, return_value=1060.0):
            assert detector.check() is False

    def test_threshold_boundary(self):
        """Exactly at threshold → not detected (need to exceed)."""
        with patch(_TIME, return_value=1000.0), \
             patch(_MONO, return_value=1000.0):
            detector = WakeupDetector()
        with patch(_TIME, return_value=1180.0), \
             patch(_MONO, return_value=1060.0):
            # divergence = 120, threshold is 120, need > not >=
            assert detector.check() is False

    def test_consecutive_checks_reset(self):
        """After wake-up detection, next normal tick returns False."""
        with patch(_TIME, return_value=1000.0), \
             patch(_MONO, return_value=1000.0):
            detector = WakeupDetector()
        # Wake-up
        with patch(_TIME, return_value=8200.0), \
             patch(_MONO, return_value=1060.0):
            assert detector.check() is True
        # Normal tick after
        with patch(_TIME, return_value=8260.0), \
             patch(_MONO, return_value=1120.0):
            assert detector.check() is False


class TestHasDefaultRoute:
    """Tests for has_default_route()."""

    def test_with_default_route(self):
        """Route table with default route → True."""
        route_content = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\n"
            "eth0\tC0A80100\t00000000\t0001\t0\t0\t100\tFFFFFF00\n"
        )
        with patch('builtins.open', mock_open(read_data=route_content)):
            assert has_default_route() is True

    def test_no_default_route(self):
        """Route table without default route → False."""
        route_content = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\tC0A80100\t00000000\t0001\t0\t0\t100\tFFFFFF00\n"
        )
        with patch('builtins.open', mock_open(read_data=route_content)):
            assert has_default_route() is False

    def test_empty_route_table(self):
        """Only header → False."""
        route_content = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        )
        with patch('builtins.open', mock_open(read_data=route_content)):
            assert has_default_route() is False

    def test_proc_not_available(self):
        """File not found (container?) → False."""
        with patch('builtins.open', side_effect=FileNotFoundError):
            assert has_default_route() is False
