"""Show progress of a background (async) transaction."""

import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import _

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

TRANSACTION_PROGRESS_FILE = Path("/run/urpm/transaction.json")


def _read_progress() -> dict | None:
    """Read and parse the async progress file.

    Returns None if no transaction is running or the file is unreadable.
    """
    try:
        return json.loads(TRANSACTION_PROGRESS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check whether a PID is still running.

    Uses /proc instead of os.kill() because the transaction child
    typically runs as root while this command runs as a normal user.
    """
    try:
        return Path(f"/proc/{pid}").exists()
    except (ValueError, OSError):
        return False


def _format_bar(current: int, total: int, pkg: str, width: int) -> str:
    """Build a full-width progress bar string.

    Example output::

        [████░░░░░░] 3/10 lib64smi2 (30%)
    """
    pct = int(current * 100 / total) if total else 0
    suffix = f" {current}/{total} {pkg} ({pct}%)"
    bar_width = width - len(suffix) - 2
    if bar_width < 10:
        bar_width = 10
    filled = int(bar_width * pct / 100)
    return f"[{'█' * filled}{'░' * (bar_width - filled)}]{suffix}"


def _format_script_bar(current: int, total: int, script_pkg: str,
                       width: int) -> str:
    """Build a progress bar for the 'script' phase (scriptlet running).

    Shows a full bar with the scriptlet package name instead of a
    percentage, since the transaction is blocked waiting for the
    scriptlet to finish.

    Example output::

        [██████████] 10/10 Running: shared-mime-info
    """
    suffix = f" {current}/{total} Running: {script_pkg}"
    bar_width = width - len(suffix) - 2
    if bar_width < 10:
        bar_width = 10
    # Full bar — the transaction is paused on a scriptlet
    return f"[{'█' * bar_width}]{suffix}"


def _display_once(state: dict) -> None:
    """Print a single snapshot of the current progress.

    Adapts the output depending on the transaction *phase*:

    - ``verify`` / ``prepare``: short status line, no progress bar.
    - ``install`` / ``erase``: normal progress bar with package count.
    - ``script``: full bar with the name of the package whose scriptlet
      is running.
    - ``script_done``: treated like install/erase (back to normal bar).
    """
    error = state.get('error')
    if error:
        print(_("Transaction error: %s") % error)
        return

    phase = state.get('phase', '')
    current = state.get('current', 0)
    total = state.get('total', 0)
    pkg = state.get('current_package', '')

    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 80

    # Preparation phases — no meaningful counter yet
    if phase in ('verify', 'prepare'):
        print(f"\r\033[K" + _("Preparing transaction..."),
              end='', flush=True)
        return

    # Scriptlet running — show which package blocks the transaction
    if phase == 'script':
        script_pkg = state.get('script', pkg)
        line = _format_script_bar(current, total, script_pkg, width)
        print(f"\r\033[K{line}", end='', flush=True)
        return

    # Normal progress (install, erase, script_done, or unknown phase)
    bar = _format_bar(current, total, pkg, width)
    print(f"\r\033[K{bar}", end='', flush=True)


def _watch(state: dict) -> None:
    """Continuously display progress until the transaction finishes.

    Disables terminal echo so that stray keypresses (Enter, etc.)
    don't break the single-line progress bar.
    """
    import termios
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else -1
    old_attrs = None

    # Suppress terminal echo during watch
    if stdin_fd >= 0:
        try:
            old_attrs = termios.tcgetattr(stdin_fd)
            new_attrs = termios.tcgetattr(stdin_fd)
            new_attrs[3] &= ~(termios.ECHO | termios.ICANON)  # no echo, no line buffering
            termios.tcsetattr(stdin_fd, termios.TCSANOW, new_attrs)
        except termios.error:
            old_attrs = None

    try:
        while True:
            state = _read_progress()
            if state is None:
                print(f"\r\033[K" + _("Transaction completed."))
                return

            _display_once(state)

            child_pid = state.get('pid')
            if child_pid and not _is_pid_alive(child_pid):
                print(f"\r\033[K" + _("Transaction process (PID %s) is no longer running.") % child_pid)
                return

            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSANOW, old_attrs)


def cmd_progress(args, db: "PackageDatabase") -> int:
    """Entry point for ``urpm progress``."""
    state = _read_progress()

    if state is None:
        print(_("No background transaction is running."))
        return 0

    pid = state.get('pid', '?')
    txn_type = state.get('type', 'transaction')
    print(_("Background %s in progress (PID %s)") % (txn_type, pid))

    if getattr(args, 'watch', False):
        _watch(state)
    else:
        _display_once(state)
        print()  # Newline after progress bar
        print(_("Use --watch / -w to follow in real time."))

    return 0
