"""Color palette for rpmdrake-ng.

Provides dual light/dark palettes for package state colors,
auto-detected from the current Qt theme at render time.
"""

from .compat import QApplication, QColor, QPalette

__all__ = [
    "get_state_colors",
    "PHASE_COLORS",
    "DIALOG_COLORS",
    "button_stylesheet",
]


# --- State colors -------------------------------------------------------------

# Light theme (bright background): dark, saturated colors for legibility.
# Color coherence rule: button color == badge color for the same concept.
#   Install button (green)  ↔  installed state (green)
#   Update button  (orange) ↔  update state   (orange)
#   Remove button  (red)    ↔  conflict state  (red)
_LIGHT: dict[str, QColor | None] = {
    "installed": QColor("#2e7d32"),   # Dark green   — explicitly installed (matches install button)
    "dep":       QColor("#546e7a"),   # Blue-gray    — auto-installed dependency
    "orphan":    QColor("#6a1b9a"),   # Purple       — no longer needed (review)
    "update":    QColor("#fb8c00"),   # Orange       — upgrade available (matches update button)
    "available": None,                # Default text color — not installed
    "conflict":  QColor("#c62828"),   # Dark red     — conflict (matches remove button)
}

# Dark theme (dark background): lighter, pastel tones for legibility.
_DARK: dict[str, QColor | None] = {
    "installed": QColor("#a5d6a7"),   # Light green
    "dep":       QColor("#90a4ae"),   # Light blue-gray
    "orphan":    QColor("#ce93d8"),   # Light purple
    "update":    QColor("#ffa726"),   # Warm amber
    "available": None,                # Default text color
    "conflict":  QColor("#ef9a9a"),   # Light red / salmon
}


def get_state_colors() -> dict[str, QColor | None]:
    """Return the state color palette matching the current Qt theme.

    Detects light vs. dark mode via the luminosity of the window background
    reported by QPalette. Called at render time so it always reflects the
    active theme, including live theme switches.

    Returns:
        Dict mapping state key to QColor (or None = use default text color).
        Keys: 'installed', 'dep', 'orphan', 'update', 'available', 'conflict'.
    """
    bg = QApplication.palette().color(QPalette.ColorRole.Window)
    return _DARK if bg.lightness() < 128 else _LIGHT


# --- Phase colors (download/install progress) ---------------------------------

# Keyed by ProgressPhase.value (lowercase string) to avoid a circular import
# with download_progress.py.
PHASE_COLORS: dict[str, str] = {
    "download":   "#2196f3",  # Blue
    "install":    "#e67c00",  # Orange
    "erase":      "#f44336",  # Red
    "rpmdb_sync": "#757575",  # Gray
}


# --- Dialog colors ------------------------------------------------------------

# Fixed colors for dialog buttons — not split by theme because dialog buttons
# sit on their own OS-managed background that provides sufficient contrast.
DIALOG_COLORS: dict[str, str] = {
    "info":    "#2196f3",  # Blue
    "error":   "#c62828",  # Dark red
    "warning": "#f57c00",  # Orange
}


# --- Button stylesheet helper -------------------------------------------------

def button_stylesheet(
    base: str,
    hover: str,
    pressed: str,
    disabled: str | None = None,
) -> str:
    """Generate a QPushButton stylesheet for a solid-color action button.

    Args:
        base: Normal background color (hex string, e.g. '#4caf50').
        hover: Hover background color.
        pressed: Pressed background color.
        disabled: Optional disabled background color.

    Returns:
        CSS stylesheet string ready for QPushButton.setStyleSheet().
    """
    disabled_rule = (
        f"\nQPushButton:disabled {{ background-color: {disabled}; }}"
        if disabled
        else ""
    )
    return (
        f"QPushButton {{"
        f" background-color: {base};"
        f" color: white;"
        f" font-weight: bold;"
        f" padding: 6px 16px;"
        f" border: none;"
        f" border-radius: 4px;"
        f" }}"
        f"\nQPushButton:hover {{ background-color: {hover}; }}"
        f"\nQPushButton:pressed {{ background-color: {pressed}; }}"
        f"{disabled_rule}"
    )
