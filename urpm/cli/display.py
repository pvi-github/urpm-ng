"""Display utilities for urpm CLI.

Provides flexible package list display with multiple output modes:
- columns: Multi-column layout (default, human-friendly)
- flat: One item per line (parsable by scripts)
- json: JSON output (programmatic consumption)
"""

import json
import shutil
from enum import Enum
from typing import List, Optional, Callable, Any, Dict


class DisplayMode(Enum):
    """Output display mode."""
    COLUMNS = "columns"  # Multi-column, human-friendly (default)
    FLAT = "flat"        # One per line, parsable
    JSON = "json"        # JSON output


# Global display settings
_display_mode = DisplayMode.COLUMNS
_show_all = False


def init(mode: str = "columns", show_all: bool = False):
    """Initialize display settings.

    Args:
        mode: Display mode ("columns", "flat", "json")
        show_all: If True, never truncate output
    """
    global _display_mode, _show_all
    _display_mode = DisplayMode(mode) if mode else DisplayMode.COLUMNS
    _show_all = show_all


def get_mode() -> DisplayMode:
    """Get current display mode."""
    return _display_mode


def get_show_all() -> bool:
    """Get current show_all setting."""
    return _show_all


def get_terminal_width() -> int:
    """Get terminal width, with fallback to 80 columns."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def format_package_list(
    packages: List[str],
    max_lines: int = 10,
    show_all: Optional[bool] = None,
    indent: int = 2,
    column_gap: int = 2,
    color_func: Optional[Callable[[str], str]] = None,
    mode: Optional[DisplayMode] = None,
    terminal_width: Optional[int] = None
) -> List[str]:
    """Format a list of packages according to display mode.

    Args:
        packages: List of package names to display
        max_lines: Maximum lines before truncation (default: 10, columns mode only)
        show_all: Override global show_all setting
        indent: Spaces to indent (default: 2, columns mode only)
        column_gap: Gap between columns (default: 2, columns mode only)
        color_func: Optional colorize function (columns mode only)
        mode: Override global display mode
        terminal_width: Override terminal width (for testing)

    Returns:
        List of formatted lines ready to print
    """
    if not packages:
        return []

    # Use global settings if not overridden
    effective_mode = mode if mode is not None else _display_mode
    effective_show_all = show_all if show_all is not None else _show_all

    if effective_mode == DisplayMode.JSON:
        return [json.dumps(packages, ensure_ascii=False)]

    if effective_mode == DisplayMode.FLAT:
        return list(packages)

    # COLUMNS mode (default)
    return _format_columns(
        packages,
        max_lines=max_lines,
        show_all=effective_show_all,
        indent=indent,
        column_gap=column_gap,
        color_func=color_func,
        terminal_width=terminal_width
    )


def _format_columns(
    packages: List[str],
    max_lines: int,
    show_all: bool,
    indent: int,
    column_gap: int,
    color_func: Optional[Callable[[str], str]],
    terminal_width: Optional[int]
) -> List[str]:
    """Format packages in multi-column layout."""
    # Get terminal width
    width = terminal_width or get_terminal_width()
    usable_width = width - indent

    # Find longest package name
    max_pkg_len = max(len(p) for p in packages)

    # Calculate column width and number of columns
    col_width = max_pkg_len + column_gap
    num_cols = max(1, usable_width // col_width)

    # Calculate how many lines we need for all packages
    total_packages = len(packages)
    total_lines_needed = (total_packages + num_cols - 1) // num_cols

    # Determine how many lines to actually display
    if show_all:
        lines_to_show = total_lines_needed
        hidden_count = 0
    else:
        lines_to_show = min(max_lines, total_lines_needed)
        # Calculate how many packages we can show
        packages_shown = lines_to_show * num_cols
        hidden_count = max(0, total_packages - packages_shown)

    # Build output lines
    result = []
    prefix = " " * indent

    for line_idx in range(lines_to_show):
        cols = []
        for col_idx in range(num_cols):
            pkg_idx = line_idx * num_cols + col_idx
            if pkg_idx < total_packages:
                pkg = packages[pkg_idx]
                # Apply color if provided
                if color_func:
                    display_pkg = color_func(pkg)
                    # Pad based on raw length, not colored length
                    padding = " " * (col_width - len(pkg))
                    cols.append(display_pkg + padding)
                else:
                    cols.append(pkg.ljust(col_width))
        if cols:
            result.append(prefix + "".join(cols).rstrip())

    # Add "and X more" message if truncated
    if hidden_count > 0:
        result.append(prefix + f"... and {hidden_count} more")

    return result


def print_package_list(
    packages: List[str],
    max_lines: int = 10,
    show_all: Optional[bool] = None,
    indent: int = 2,
    column_gap: int = 2,
    color_func: Optional[Callable[[str], str]] = None,
    mode: Optional[DisplayMode] = None
) -> None:
    """Print a list of packages according to display mode.

    Args:
        packages: List of package names to display
        max_lines: Maximum lines before truncation (default: 10)
        show_all: Override global show_all setting
        indent: Spaces to indent (default: 2)
        column_gap: Gap between columns (default: 2)
        color_func: Optional colorize function (columns mode only)
        mode: Override global display mode
    """
    lines = format_package_list(
        packages,
        max_lines=max_lines,
        show_all=show_all,
        indent=indent,
        column_gap=column_gap,
        color_func=color_func,
        mode=mode
    )
    for line in lines:
        print(line)


def format_inline(
    packages: List[str],
    max_count: int = 5,
    show_all: Optional[bool] = None,
    separator: str = ", ",
    color_func: Optional[Callable[[str], str]] = None,
    mode: Optional[DisplayMode] = None
) -> str:
    """Format packages as inline list (for summaries, history, etc.).

    Args:
        packages: List of package names
        max_count: Max packages before truncation (columns mode)
        show_all: Override global show_all setting
        separator: Separator between packages
        color_func: Optional colorize function
        mode: Override global display mode

    Returns:
        Formatted string like "pkg1, pkg2, pkg3 (+5 more)"
    """
    if not packages:
        return ""

    effective_mode = mode if mode is not None else _display_mode
    effective_show_all = show_all if show_all is not None else _show_all

    if effective_mode == DisplayMode.JSON:
        return json.dumps(packages, ensure_ascii=False)

    if effective_mode == DisplayMode.FLAT:
        return "\n".join(packages)

    # COLUMNS mode - inline comma-separated
    total = len(packages)

    if effective_show_all or total <= max_count:
        display_pkgs = packages
        suffix = ""
    else:
        display_pkgs = packages[:max_count]
        hidden = total - max_count
        suffix = f" (+{hidden} more)"

    if color_func:
        formatted = separator.join(color_func(p) for p in display_pkgs)
    else:
        formatted = separator.join(display_pkgs)

    return formatted + suffix


def format_dict_as_json(data: Dict[str, Any]) -> str:
    """Format a dictionary as JSON (for --json mode complex output)."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def print_json(data: Any) -> None:
    """Print data as JSON."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def format_size(size_bytes: float) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes:.0f}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def format_speed(bytes_per_sec: float) -> str:
    """Format speed as human-readable."""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f}B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f}KB/s"
    else:
        return f"{bytes_per_sec / (1024 * 1024):.1f}MB/s"


def format_duration(seconds: float) -> str:
    """Format duration as human-readable string.

    Examples:
        45 -> "45s"
        90 -> "1min 30s"
        3665 -> "1h 1min 5s"
    """
    if seconds < 60:
        return f"{int(seconds)}s"

    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        if secs > 0:
            return f"{minutes}min {secs}s"
        return f"{minutes}min"

    hours, mins = divmod(minutes, 60)
    parts = [f"{hours}h"]
    if mins > 0:
        parts.append(f"{mins}min")
    if secs > 0:
        parts.append(f"{secs}s")
    return " ".join(parts)


class DownloadProgressDisplay:
    """Handles multi-line download progress display with proper terminal control.

    Displays parallel downloads in a clean, aligned format:
      [12/14] 14% 12.3MB/s
              #1 neovim-data [██░░░░░░░░░░░░░░░░░░] 0.4/4.3MB (belnet)
              #2
              #3 blablabla   [█████░░░░░░░░░░░░░░░] 0.4/4.3MB (ftp.proxad.net)
              #4 prout       [███████████░░░░░░░░░] 1.0/1.6MB (peer@192.168.1.5)
    """

    def __init__(self, num_workers: int = 4, bar_width: int = 20, name_width: int = 20):
        """Initialize display.

        Args:
            num_workers: Number of download slots to display
            bar_width: Width of progress bar in characters
            name_width: Fixed width for package names (will truncate/pad)
        """
        self.num_workers = num_workers
        self.bar_width = bar_width
        self.name_width = name_width
        self.last_lines_count = 0

    def render(self, pkg_num: int, pkg_total: int, bytes_done: int, bytes_total: int,
               slots_status: List, global_speed: float = 0.0) -> str:
        """Render the download progress display.

        Args:
            pkg_num: Current package number (completed)
            pkg_total: Total packages to download
            bytes_done: Total bytes downloaded so far
            bytes_total: Total bytes to download
            slots_status: List of (slot, DownloadProgress or None) for each worker
            global_speed: Combined download speed in bytes/sec

        Returns:
            Multi-line string to display
        """
        lines = []

        # Global progress line
        pct = (bytes_done * 100 // bytes_total) if bytes_total > 0 else 0
        speed_str = format_speed(global_speed) if global_speed > 0 else ""
        header = f"  [{pkg_num}/{pkg_total}] {pct}%"
        if speed_str:
            header += f" {speed_str}"
        lines.append(header)

        # Calculate padding for alignment
        # Header is like "  [12/14] 14%" - we want slots to align after it
        header_padding = " " * 14  # Approximate alignment

        # Ensure we always have num_workers slots (fill missing with None)
        slots_dict = {slot: prog for slot, prog in slots_status} if slots_status else {}
        full_slots = [(i, slots_dict.get(i)) for i in range(self.num_workers)]

        # Worker slots (always show all slots for consistent line count)
        for slot, progress in full_slots:
            slot_num = f"#{slot + 1}"

            if progress is None:
                # Empty slot
                lines.append(f"{header_padding}{slot_num}")
            else:
                # Active download
                name = progress.name
                if len(name) > self.name_width:
                    name = name[:self.name_width - 1] + "…"
                name = name.ljust(self.name_width)

                # Progress bar
                if progress.bytes_total > 0:
                    filled = min(self.bar_width, max(0, progress.bytes_done * self.bar_width // progress.bytes_total))
                    bar = '█' * filled + '░' * (self.bar_width - filled)

                    # Size info
                    done_str = format_size(progress.bytes_done)
                    total_str = format_size(progress.bytes_total)
                    size_info = f"{done_str}/{total_str}"

                    # Source (shortened)
                    source = progress.source
                    if len(source) > 20:
                        source = source[:17] + "..."

                    line = f"{header_padding}{slot_num} {name} [{bar}] {size_info} ({source})"
                else:
                    line = f"{header_padding}{slot_num} {name} (starting...)"

                lines.append(line)

        return "\n".join(lines)

    def update(self, pkg_num: int, pkg_total: int, bytes_done: int, bytes_total: int,
               slots_status: List, global_speed: float = 0.0):
        """Update the display in-place.

        Args:
            Same as render()
        """

        # Get terminal width to truncate lines (avoid wrapping issues)
        term_width = get_terminal_width()

        # Render first to know how many lines we'll have
        output = self.render(pkg_num, pkg_total, bytes_done, bytes_total,
                            slots_status, global_speed)
        output_lines = output.split('\n')
        num_lines = len(output_lines)

        # Move cursor to start of our display block
        if self.last_lines_count > 0:
            # Move up to the first line of our block
            print(f"\033[{self.last_lines_count}F", end='', flush=True)
        # else: first time, we're already at the right position

        # Print all lines
        for i, line in enumerate(output_lines):
            # Truncate to terminal width to prevent wrapping
            if len(line) > term_width - 1:
                line = line[:term_width - 4] + "..."
            # Clear line and print content
            print(f"\033[K{line}", flush=True)

        self.last_lines_count = num_lines

    def finish(self):
        """Finish display - clear the progress lines."""
        if self.last_lines_count > 0:
            # Move cursor up to the start of our display block
            print(f"\033[{self.last_lines_count}F", end='', flush=True)
            # Clear all the lines we used
            for _ in range(self.last_lines_count):
                print("\033[K", end='')  # Clear line
                print("\033[1B", end='')  # Move down one line
            # Move back up so next print starts at the right place
            print(f"\033[{self.last_lines_count}F", end='', flush=True)
        self.last_lines_count = 0
