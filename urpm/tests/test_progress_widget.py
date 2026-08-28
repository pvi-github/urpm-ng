"""Tests for the transaction progress widget (scroll-region variant).

Focus on the scroll-region plumbing added to handle terminal resize
cleanly : setup / teardown / SIGWINCH handling / fallback for non-TTY
and tiny terminals.  The visual output can only really be judged by a
human, but the escape codes emitted at each lifecycle step are stable
enough to assert on.
"""

from __future__ import annotations

import io
import signal
import sys
from unittest.mock import patch

import pytest

from urpm.cli.helpers import progress as pw
from urpm.core.transaction_queue import TransactionPhase, TransactionProgress


def _fake_progress(done=1, total=5, name="pkg-a", phase=None):
    """Build a minimal :class:`TransactionProgress` for one event."""
    if phase is None:
        phase = TransactionPhase.INSTALL
    return TransactionProgress(
        phase=phase,
        package_name=name,
        packages_done=done,
        packages_total=total,
        bytes_done=0,
        bytes_total=100,
        script_name=None,
    )


class _TTYBuf(io.StringIO):
    """StringIO that reports as a TTY, for widget-under-test captures."""

    def isatty(self) -> bool:  # noqa: D401
        return True


class _FakeSys:
    """Minimal sys stand-in : we only care about ``stdout``."""

    def __init__(self, buf):
        self.stdout = buf


@pytest.fixture
def captured_stdout(monkeypatch):
    """Inject a fake ``sys`` into the widget module so we capture writes
    without fighting pytest's own stdout capture.  The real ``sys``
    keeps working for pytest ; the widget sees our TTY-faking buffer.
    """
    buf = _TTYBuf()
    monkeypatch.setattr(pw, "sys", _FakeSys(buf))
    return buf


def _tty_size(rows, cols):
    """Return a monkeypatch closure for ``_detect_term_size``."""
    return lambda: (rows, cols)


class TestFallbackModes:
    """When the environment can't host a scroll region, the widget
    must degrade silently rather than emit half-broken escapes into
    a log file."""

    def test_non_tty_disables_scroll_region(self, monkeypatch):
        # sys.stdout is a real pipe here — isatty() returns False.
        cb = pw.make_progress_callback("Installing {count}", total=3)
        assert cb.state["scroll_region_ok"] is False
        cb.cleanup()

    def test_tiny_terminal_disables_scroll_region(self, monkeypatch, captured_stdout):
        # 5 rows is below the 6-row minimum for the widget.
        monkeypatch.setattr(pw, "_detect_term_size", _tty_size(5, 80))
        cb = pw.make_progress_callback("Installing {count}", total=3)
        assert cb.state["scroll_region_ok"] is False
        # And no escape codes emitted on a callback either.
        cb(_fake_progress())
        assert "\033[" not in captured_stdout.getvalue()
        cb.cleanup()

    def test_fallback_callback_is_a_noop(self, monkeypatch, captured_stdout):
        monkeypatch.setattr(pw, "_detect_term_size", lambda: None)
        cb = pw.make_progress_callback("Installing {count}", total=3)
        cb(_fake_progress())
        assert captured_stdout.getvalue() == ""
        cb.cleanup()


class TestScrollRegionSetup:
    """Happy path : real TTY, generous geometry."""

    def test_first_render_installs_region(self, monkeypatch, captured_stdout):
        monkeypatch.setattr(pw, "_detect_term_size", _tty_size(24, 80))
        cb = pw.make_progress_callback("Installing {count}", total=3)
        assert cb.state["scroll_region_ok"] is True
        assert cb.state["region_active"] is False  # deferred to 1st render
        cb(_fake_progress())
        out = captured_stdout.getvalue()
        # DECSTBM : rows 1..(24 - 3) = 1..21
        assert "\033[1;21r" in out
        assert "\033[?25l" in out  # cursor hidden
        assert cb.state["region_active"] is True
        cb.cleanup()

    def test_render_uses_absolute_positioning(self, monkeypatch, captured_stdout):
        monkeypatch.setattr(pw, "_detect_term_size", _tty_size(24, 80))
        cb = pw.make_progress_callback("Installing {count}", total=3)
        cb(_fake_progress())
        out = captured_stdout.getvalue()
        # Widget top row = 24 - 3 + 1 = 22.  Header at row 22, bar 23, sub 24.
        assert "\033[22;1H" in out
        assert "\033[23;1H" in out
        assert "\033[24;1H" in out
        # And DECSC/DECRC bracketing.
        assert "\0337" in out
        assert "\0338" in out
        cb.cleanup()

    def test_cleanup_restores_terminal(self, monkeypatch, captured_stdout):
        monkeypatch.setattr(pw, "_detect_term_size", _tty_size(24, 80))
        cb = pw.make_progress_callback("Installing {count}", total=3)
        cb(_fake_progress())
        captured_stdout.truncate(0)
        captured_stdout.seek(0)
        cb.cleanup()
        out = captured_stdout.getvalue()
        assert "\033[r" in out       # reset scroll region
        assert "\033[?25h" in out    # cursor shown
        assert "\033[24;1H" in out   # cursor below widget area


class TestResize:
    """SIGWINCH plumbing : flag flip + next-render redraw."""

    def test_sigwinch_flips_pending_flag(self, monkeypatch, captured_stdout):
        monkeypatch.setattr(pw, "_detect_term_size", _tty_size(24, 80))
        cb = pw.make_progress_callback("Installing {count}", total=3)
        cb(_fake_progress())
        # Simulate SIGWINCH by directly poking the handler.  The
        # currently-installed SIGWINCH handler is the widget's own
        # chained handler.
        handler = signal.getsignal(signal.SIGWINCH)
        assert callable(handler)
        handler(signal.SIGWINCH, None)
        assert cb.state["resize_pending"] is True
        cb.cleanup()

    def test_next_render_after_resize_redefines_region(
            self, monkeypatch, captured_stdout):
        # Start at 24x80, resize to 30x100.
        size_ref = {"val": (24, 80)}
        monkeypatch.setattr(
            pw, "_detect_term_size", lambda: size_ref["val"])
        cb = pw.make_progress_callback("Installing {count}", total=3)
        cb(_fake_progress(done=1))
        # Simulate resize : geometry changes then SIGWINCH fires.
        size_ref["val"] = (30, 100)
        signal.getsignal(signal.SIGWINCH)(signal.SIGWINCH, None)
        captured_stdout.truncate(0)
        captured_stdout.seek(0)
        cb(_fake_progress(done=2))
        out = captured_stdout.getvalue()
        # Old region reset + new region installed (rows 1..27).
        assert "\033[r" in out
        assert "\033[1;27r" in out
        # New widget top = 30 - 3 + 1 = 28.
        assert "\033[28;1H" in out
        assert cb.state["rows"] == 30
        assert cb.state["cols"] == 100
        cb.cleanup()

    def test_resize_to_tiny_disables_widget(
            self, monkeypatch, captured_stdout):
        size_ref = {"val": (24, 80)}
        monkeypatch.setattr(
            pw, "_detect_term_size", lambda: size_ref["val"])
        cb = pw.make_progress_callback("Installing {count}", total=3)
        cb(_fake_progress(done=1))
        size_ref["val"] = (4, 80)  # below _MIN_TERM_ROWS
        signal.getsignal(signal.SIGWINCH)(signal.SIGWINCH, None)
        captured_stdout.truncate(0)
        captured_stdout.seek(0)
        cb(_fake_progress(done=2))
        # Widget is now disabled — no positioning codes emitted.
        out = captured_stdout.getvalue()
        assert "\033[28;1H" not in out
        assert cb.state["scroll_region_ok"] is False
        cb.cleanup()
