"""Color output support for urpm CLI.

Color palette:
  - Red: errors and alerts
  - Orange: warnings
  - Green: success/ok
  - Blue: contextual information
"""

import os
import sys

# ANSI color codes
_COLORS = {
    'reset': '\033[0m',
    'bold': '\033[1m',
    # Palette
    'red': '\033[91m',      # Bright red for errors/alerts
    'orange': '\033[93m',   # Yellow/orange for warnings (no true orange in ANSI)
    'green': '\033[92m',    # Bright green for success
    'blue': '\033[94m',     # Bright blue for info
    # Additional
    'dim': '\033[2m',
    'cyan': '\033[96m',
}

# Global state
_colors_enabled = True


def init(nocolor: bool = False):
    """Initialize color support.

    Args:
        nocolor: If True, disable colors unconditionally
    """
    global _colors_enabled

    if nocolor:
        _colors_enabled = False
    elif os.environ.get('NO_COLOR'):
        # Respect NO_COLOR environment variable (https://no-color.org/)
        _colors_enabled = False
    elif not sys.stdout.isatty():
        # Disable colors if not a terminal
        _colors_enabled = False
    else:
        _colors_enabled = True


def enabled() -> bool:
    """Return True if colors are enabled."""
    return _colors_enabled


def _wrap(text: str, color: str) -> str:
    """Wrap text with color codes if colors are enabled."""
    if not _colors_enabled:
        return text
    code = _COLORS.get(color, '')
    reset = _COLORS['reset']
    return f"{code}{text}{reset}"


# Semantic color functions
def error(text: str) -> str:
    """Format text as error (red)."""
    return _wrap(text, 'red')


def alert(text: str) -> str:
    """Format text as alert (red)."""
    return _wrap(text, 'red')


def warning(text: str) -> str:
    """Format text as warning (orange/yellow)."""
    return _wrap(text, 'orange')


def success(text: str) -> str:
    """Format text as success (green)."""
    return _wrap(text, 'green')


def ok(text: str) -> str:
    """Format text as ok (green)."""
    return _wrap(text, 'green')


def info(text: str) -> str:
    """Format text as info (blue)."""
    return _wrap(text, 'blue')


def dim(text: str) -> str:
    """Format text as dim/muted."""
    return _wrap(text, 'dim')


def bold(text: str) -> str:
    """Format text as bold."""
    return _wrap(text, 'bold')


def cyan(text: str) -> str:
    """Format text as cyan."""
    return _wrap(text, 'cyan')


# Convenience functions for common patterns
def pkg_install(name: str) -> str:
    """Format package name for installation (green)."""
    return success(name)


def pkg_remove(name: str) -> str:
    """Format package name for removal (red)."""
    return error(name)


def pkg_upgrade(name: str) -> str:
    """Format package name for upgrade (blue)."""
    return info(name)


def count(n: int) -> str:
    """Format a count number."""
    return bold(str(n))
