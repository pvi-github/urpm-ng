"""Debug utilities for CLI operations."""

from pathlib import Path

# Debug file paths (in current working directory)
DEBUG_LAST_INSTALLED_DEPS = Path('.last-installed-through-deps.list')
DEBUG_LAST_REMOVED_DEPS = Path('.last-removed-as-deps.list')
DEBUG_INSTALLED_DEPS_COPY = Path('.installed-through-deps.list')
DEBUG_PREV_INSTALLED_DEPS = Path('.prev-installed-through-deps.list')


def write_debug_file(path: Path, packages: list, append: bool = False):
    """Write package names to a debug file."""
    mode = 'a' if append else 'w'
    try:
        with open(path, mode) as f:
            for pkg in sorted(packages):
                f.write(f"{pkg}\n")
    except (IOError, OSError):
        pass  # Ignore errors for debug files


def clear_debug_file(path: Path):
    """Clear a debug file."""
    try:
        path.write_text('')
    except (IOError, OSError):
        pass


def copy_installed_deps_list(root: str = '/', dest: Path = None):
    """Copy installed-through-deps.list to working directory for debug."""
    src = Path(root) / 'var/lib/rpm/installed-through-deps.list'
    if dest is None:
        dest = DEBUG_INSTALLED_DEPS_COPY
    try:
        if src.exists():
            dest.write_text(src.read_text())
        else:
            dest.write_text('')
    except (IOError, OSError):
        pass


def notify_urpmd_cache_invalidate():
    """Notify local urpmd to invalidate its RPM cache index.

    This allows newly downloaded packages to be visible to peer queries.
    Tries both dev and prod ports silently.
    """
    import urllib.request
    import urllib.error
    from ...core.config import DEV_PORT, PROD_PORT

    ports = [DEV_PORT, PROD_PORT]

    for port in ports:
        try:
            url = f"http://127.0.0.1:{port}/api/invalidate-cache"
            req = urllib.request.Request(url, method='POST')
            req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.status == 200:
                    return  # Success, no need to try other port
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue  # Try next port or give up silently


# Backwards compatibility aliases (with underscore prefix)
_write_debug_file = write_debug_file
_clear_debug_file = clear_debug_file
_copy_installed_deps_list = copy_installed_deps_list
_notify_urpmd_cache_invalidate = notify_urpmd_cache_invalidate
