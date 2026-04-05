"""Color palette for rpmdrake-ng.

Provides dual light/dark palettes for package state colors,
auto-detected from the current Qt theme at render time.
"""

from .compat import QApplication, QColor, QPalette

__all__ = [
    "get_state_colors",
    "get_secondary_colors",
    "is_dark_theme",
    "PHASE_COLORS",
    "DIALOG_COLORS",
    "button_stylesheet",
    "darken",
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


# --- Theme detection ----------------------------------------------------------

def is_dark_theme() -> bool:
    """Return True when the current Qt theme uses a dark background."""
    bg = QApplication.palette().color(QPalette.ColorRole.Window)
    return bg.lightness() < 128


# --- Secondary UI colors (borders, muted text, surfaces) ---------------------
#
# These colors guarantee sufficient contrast in both light and dark themes.
# They are computed at call time so they track live theme switches.
#
# Usage:
#   colors = get_secondary_colors()
#   widget.setStyleSheet(f"color: {colors['text_muted']};")

_SECONDARY_LIGHT: dict[str, str] = {
    "text_muted": "#666666",   # Muted text (readable on white)
    "border":     "#cccccc",   # Subtle borders / separators
    "surface":    "#f5f5f5",   # Elevated panel background
    "hover":      "#e0e0e0",   # Hover state for clickable areas
    "peer":       "#388e3c",   # Peer/LAN source (dark green)
}

_SECONDARY_DARK: dict[str, str] = {
    "text_muted": "#999999",   # Muted text (readable on dark bg)
    "border":     "#555555",   # Subtle borders / separators
    "surface":    "#383838",   # Elevated panel background
    "hover":      "#4a4a4a",   # Hover state for clickable areas
    "peer":       "#81c784",   # Peer/LAN source (light green)
}


def get_secondary_colors() -> dict[str, str]:
    """Return theme-aware colors for secondary UI elements.

    Provides consistent, readable colors for borders, muted text, and
    panel backgrounds that work in both light and dark themes.

    Returns:
        Dict with keys: 'text_muted', 'border', 'surface', 'hover', 'peer'.
    """
    return _SECONDARY_DARK if is_dark_theme() else _SECONDARY_LIGHT


# --- Phase colors (download/install progress) ---------------------------------

# Keyed by ProgressPhase.value (lowercase string) to avoid a circular import
# with download_progress.py.
PHASE_COLORS: dict[str, str] = {
    "download":   "#2196f3",  # Blue
    "install":    "#e67c00",  # Orange
    "erase":      "#f44336",  # Red
    "triggers":   "#8e24aa",  # Purple
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

def darken(hex_color: str, factor: float = 0.5) -> str:
    """Darken a hex color by the given factor (0 = black, 1 = unchanged)."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r, g, b = int(r * factor), int(g * factor), int(b * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


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
    focus_border = darken(base, 0.5)
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
        f" border: 3px solid transparent;"
        f" border-radius: 4px;"
        f" }}"
        f"\nQPushButton:hover {{ background-color: {hover}; }}"
        f"\nQPushButton:focus {{ border-color: {focus_border}; }}"
        f"\nQPushButton:pressed {{ background-color: {pressed}; }}"
        f"{disabled_rule}"
    )
