"""
Central configuration for urpm paths.

Mode detection:
    1. Find project root (parent of bin/ where urpm/urpmd are located)
    2. If .urpm.local exists in project root → read config from it (DEV mode)
    3. If /usr/bin/urpm exists → PROD mode (system installation)
    4. Otherwise → DEV mode (development)

PROD mode: /var/lib/urpm/
DEV mode:  /var/lib/urpm-dev/

Structure:
    <base_dir>/packages.db                              - Package database
    <base_dir>/medias/<hostname>/<media>/               - Media mirror
    <base_dir>/medias/<hostname>/<media>/media_info/    - Synthesis, hdlist, MD5SUM
    <base_dir>/medias/<hostname>/<media>/*.rpm          - Mirrored RPMs (served to peers)

.urpm.local format (optional, one setting per line):
    base_dir=/path/to/custom/dir
    # Comments start with #
"""

import os
import sys
from pathlib import Path
from typing import Optional

# Config file name
LOCAL_CONFIG_FILE = ".urpm.local"

# PROD paths (system-wide, requires root)
PROD_BASE_DIR = Path("/var/lib/urpm")
PROD_DB_PATH = PROD_BASE_DIR / "packages.db"
PROD_PID_FILE = Path("/run/urpmd.pid")
PROD_PORT = 9876

# DEV paths (separate directory, also requires root but isolated from prod)
DEV_BASE_DIR = Path("/var/lib/urpm-dev")
DEV_DB_PATH = DEV_BASE_DIR / "packages.db"
DEV_PID_FILE = DEV_BASE_DIR / "urpmd.pid"
DEV_PORT = 9877  # Different port so both daemons can coexist

# Cache for detected mode (avoid repeated filesystem checks)
_cached_config: Optional[dict] = None


def _get_project_root() -> Optional[Path]:
    """Find project root by looking at where the script is located.

    If running from ./bin/urpm or ./bin/urpmd, project root is the parent
    of bin/ directory.

    Returns:
        Project root path, or None if not in a dev environment
    """
    # Get the directory of the main script (bin/urpm or bin/urpmd)
    if sys.argv and sys.argv[0]:
        script_path = Path(sys.argv[0]).resolve()
        # If script is in a bin/ directory, project root is parent of bin/
        if script_path.parent.name == 'bin':
            return script_path.parent.parent
    return None


def _read_local_config(project_root: Path) -> Optional[dict]:
    """Read .urpm.local config file if it exists in project root.

    Returns:
        Dict with config values, or None if file doesn't exist
    """
    config_path = project_root / LOCAL_CONFIG_FILE
    if not config_path.exists():
        return None

    config = {}
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()
    except (OSError, IOError):
        return None

    return config


def _is_system_install() -> bool:
    """Check if urpm is installed system-wide."""
    return Path("/usr/bin/urpm").exists()


def _detect_mode() -> dict:
    """Detect configuration based on environment.

    Returns:
        Dict with 'base_dir', 'db_path', 'pid_file', 'port', 'is_dev'
    """
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    # 1. Check for .urpm.local in project root (if running from dev tree)
    project_root = _get_project_root()
    if project_root:
        local_config = _read_local_config(project_root)
        if local_config is not None:
            # .urpm.local found - use custom base_dir or default DEV paths
            if 'base_dir' in local_config:
                base_dir = Path(local_config['base_dir']).expanduser()
            else:
                base_dir = DEV_BASE_DIR
            _cached_config = {
                'base_dir': base_dir,
                'db_path': base_dir / "packages.db",
                'pid_file': base_dir / "urpmd.pid",
                'port': DEV_PORT,
                'is_dev': True,
            }
            return _cached_config

    # 2. Check if system installation
    if _is_system_install():
        _cached_config = {
            'base_dir': PROD_BASE_DIR,
            'db_path': PROD_DB_PATH,
            'pid_file': PROD_PID_FILE,
            'port': PROD_PORT,
            'is_dev': False,
        }
        return _cached_config

    # 3. Default to DEV mode (running from dev tree without .urpm.local)
    _cached_config = {
        'base_dir': DEV_BASE_DIR,
        'db_path': DEV_DB_PATH,
        'pid_file': DEV_PID_FILE,
        'port': DEV_PORT,
        'is_dev': True,
    }
    return _cached_config


def is_dev_mode() -> bool:
    """Check if running in dev mode."""
    return _detect_mode()['is_dev']


def get_base_dir(dev_mode: bool = None) -> Path:
    """Get base directory.

    Args:
        dev_mode: Force DEV mode if True, PROD if False, auto-detect if None

    Returns:
        Base directory path
    """
    if dev_mode is True:
        return DEV_BASE_DIR
    if dev_mode is False:
        return PROD_BASE_DIR
    return _detect_mode()['base_dir']


def get_db_path(dev_mode: bool = None) -> Path:
    """Get database path.

    Args:
        dev_mode: Force DEV mode if True, PROD if False, auto-detect if None
    """
    if dev_mode is True:
        return DEV_DB_PATH
    if dev_mode is False:
        return PROD_DB_PATH
    return _detect_mode()['db_path']


def get_media_dir(base_dir: Path, hostname: str, media_name: str) -> Path:
    """Get media cache directory.

    Args:
        base_dir: Base urpm directory
        hostname: Server hostname
        media_name: Media name

    Returns:
        Path: <base_dir>/medias/<hostname>/<media_name>/
    """
    return base_dir / "medias" / hostname / media_name


def get_hostname_from_url(url: str) -> str:
    """Extract hostname from a URL for cache organization."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or "local"
