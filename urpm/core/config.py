"""
Central configuration for urpm paths.

Mode detection:
    1. Find project root (parent of bin/ where urpm/urpmd are located)
    2. If .urpm.local exists in project root → read config from it (DEV mode)
    3. If /usr/bin/urpm exists → PROD mode (system installation)
    4. Otherwise → DEV mode (development)

PROD mode: /var/lib/urpm/
DEV mode:  /var/lib/urpm-dev/

Directory structure:
    <base_dir>/packages.db                                              - Package database
    <base_dir>/medias/official/<version>/<arch>/media/<type>/<release>/ - Official media
    <base_dir>/medias/custom/<short_name>/                              - Custom/third-party media

    Example: <base_dir>/medias/official/10/x86_64/media/core/release/
             <base_dir>/medias/official/10/x86_64/media/core/release/media_info/synthesis.hdlist.cz
             <base_dir>/medias/official/10/x86_64/media/core/release/*.rpm

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
PROD_DISCOVERY_PORT = 9878  # UDP port for peer discovery

# DEV paths (separate directory, also requires root but isolated from prod)
DEV_BASE_DIR = Path("/var/lib/urpm-dev")
DEV_DB_PATH = DEV_BASE_DIR / "packages.db"
DEV_PID_FILE = DEV_BASE_DIR / "urpmd.pid"
DEV_PORT = 9877  # Different port so both daemons can coexist
DEV_DISCOVERY_PORT = 9879  # UDP port for peer discovery (dev mode)

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


def get_base_dir(dev_mode: bool = None, urpm_root: str = None) -> Path:
    """Get base directory.

    Args:
        dev_mode: Force DEV mode if True, PROD if False, auto-detect if None
        urpm_root: If set, return <urpm_root>/var/lib/urpm instead

    Returns:
        Base directory path
    """
    if urpm_root:
        # When using --urpm-root, base_dir is inside the chroot
        return Path(urpm_root) / "var/lib/urpm"
    if dev_mode is True:
        return DEV_BASE_DIR
    if dev_mode is False:
        return PROD_BASE_DIR
    return _detect_mode()['base_dir']


def get_db_path(dev_mode: bool = None, urpm_root: str = None) -> Path:
    """Get database path.

    Args:
        dev_mode: Force DEV mode if True, PROD if False, auto-detect if None
        urpm_root: If set, return <urpm_root>/var/lib/urpm/packages.db
    """
    if urpm_root:
        return Path(urpm_root) / "var/lib/urpm/packages.db"
    if dev_mode is True:
        return DEV_DB_PATH
    if dev_mode is False:
        return PROD_DB_PATH
    return _detect_mode()['db_path']


def get_rpm_root(root: str = None, urpm_root: str = None) -> Optional[str]:
    """Get RPM root directory for chroot installation.

    Args:
        root: --root option (RPM installs here, urpm config from host)
        urpm_root: --urpm-root option (both RPM and urpm config here)

    Returns:
        Root path for RPM, or None for normal system
    """
    # --urpm-root implies --root to the same location
    if urpm_root:
        return urpm_root
    return root


# =============================================================================
# DEPRECATED - Old hostname-based path functions (kept for migration/compat)
# =============================================================================

def get_media_dir(base_dir: Path, hostname: str, media_name: str) -> Path:
    """DEPRECATED: Use get_media_local_path() instead.

    Old structure: <base_dir>/medias/<hostname>/<media_name>/
    New structure: <base_dir>/medias/official/<relative_path>/
    """
    import warnings
    warnings.warn("get_media_dir() is deprecated, use get_media_local_path()", DeprecationWarning)
    return base_dir / "medias" / hostname / media_name


def get_hostname_from_url(url: str) -> str:
    """DEPRECATED: No longer needed with new media structure."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or "local"


# =============================================================================
# Media/server path functions
# =============================================================================

def get_media_local_path(media: dict, base_dir: Path = None) -> Path:
    """Get local cache path for a media.

    Official media: <base_dir>/medias/official/<relative_path>/
    Custom media:   <base_dir>/medias/custom/<short_name>/

    Args:
        media: Media dict with 'is_official', 'relative_path', 'short_name'
        base_dir: Base urpm directory (auto-detected if None)

    Returns:
        Full local path for the media cache
    """
    if base_dir is None:
        base_dir = get_base_dir()

    if media.get('is_official', 1):
        # Official: use relative_path
        return base_dir / "medias" / "official" / media['relative_path']
    else:
        # Custom: use short_name (isolated for security)
        return base_dir / "medias" / "custom" / media['short_name']


def build_server_url(server: dict) -> str:
    """Build base URL for a server.

    Args:
        server: Server dict with 'protocol', 'host', 'base_path'

    Returns:
        Base URL string (without trailing slash)
    """
    protocol = server['protocol']
    host = server['host']
    base_path = server.get('base_path', '').rstrip('/')

    if protocol == 'file':
        # Local filesystem - return file:// URL for urllib compatibility
        return f"file://{base_path}"
    else:
        # Remote URL
        return f"{protocol}://{host}{base_path}"


def build_media_url(server: dict, media: dict) -> str:
    """Build full URL/path to access a media on a server.

    Args:
        server: Server dict with 'protocol', 'host', 'base_path'
        media: Media dict with 'relative_path'

    Returns:
        Full URL or local path to the media
    """
    base_url = build_server_url(server)
    relative_path = media['relative_path'].strip('/')

    if server['protocol'] == 'file':
        # Local filesystem path
        return f"{base_url}/{relative_path}"
    else:
        # Remote URL
        return f"{base_url}/{relative_path}"


def is_local_server(server: dict) -> bool:
    """Check if a server is a local filesystem (file://)."""
    return server.get('protocol') == 'file'


# =============================================================================
# IPv4/IPv6 connectivity testing
# =============================================================================

def test_server_ip_connectivity(host: str, port: int = 80, timeout: float = 5.0) -> str:
    """Test IPv4 and IPv6 connectivity to a server.

    Args:
        host: Hostname to test (e.g., 'ftp.belnet.be')
        port: Port to connect to (default 80 for HTTP)
        timeout: Connection timeout in seconds

    Returns:
        ip_mode string:
        - 'dual': Both IPv4 and IPv6 work (prefer IPv4 in downloads)
        - 'ipv4': Only IPv4 works
        - 'ipv6': Only IPv6 works
        - 'auto': Could not test (DNS failure, etc.) - use system default
    """
    import socket

    ipv4_works = False
    ipv6_works = False

    # Test IPv4
    try:
        # Get IPv4 addresses only
        addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if addrs:
            addr = addrs[0][4]  # (ip, port)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(addr)
                ipv4_works = True
            finally:
                sock.close()
    except (socket.gaierror, socket.timeout, OSError):
        pass

    # Test IPv6
    try:
        # Get IPv6 addresses only
        addrs = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
        if addrs:
            addr = addrs[0][4]  # (ip, port, flowinfo, scopeid)
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(addr)
                ipv6_works = True
            finally:
                sock.close()
    except (socket.gaierror, socket.timeout, OSError):
        pass

    # Determine ip_mode
    if ipv4_works and ipv6_works:
        return 'dual'
    elif ipv4_works:
        return 'ipv4'
    elif ipv6_works:
        return 'ipv6'
    else:
        return 'auto'  # Neither worked, let system decide


def get_socket_family_for_ip_mode(ip_mode: str) -> int:
    """Get the socket address family to use for a given ip_mode.

    Args:
        ip_mode: 'auto', 'ipv4', 'ipv6', or 'dual'

    Returns:
        socket.AF_INET or socket.AF_INET6
    """
    import socket

    if ip_mode == 'ipv6':
        return socket.AF_INET6
    else:
        # 'auto', 'dual', 'ipv4' - prefer IPv4 (faster, more reliable)
        return socket.AF_INET


# Cache for system version
_system_version_cache: Optional[str] = None


def get_system_version(root: str = None) -> Optional[str]:
    """Get the Mageia version of the system.

    Reads VERSION_ID from /etc/os-release (or <root>/etc/os-release).

    Args:
        root: Optional chroot path (for --root or --urpm-root)

    Returns:
        Version string (e.g., '9', '10', 'cauldron') or None if not detected.
    """
    global _system_version_cache

    # Use cache only for non-chroot case
    if root is None and _system_version_cache is not None:
        return _system_version_cache

    os_release = Path(root or '/') / 'etc' / 'os-release'

    version = None
    try:
        with open(os_release) as f:
            for line in f:
                if line.startswith('VERSION_ID='):
                    version = line.strip().split('=')[1].strip('"')
                    break
    except (IOError, OSError):
        pass

    # Cache for non-chroot case
    if root is None:
        _system_version_cache = version

    return version


def get_accepted_versions(db, system_version: str = None) -> tuple:
    """Determine which media versions to accept based on configured media.

    Implements smart version filtering:
    1. Only cauldron media enabled → accept only 'cauldron'
    2. Only numeric media enabled → accept only system version
    3. Mix of system version + cauldron → returns None (needs user choice)

    Args:
        db: PackageDatabase instance
        system_version: System version (from get_system_version), or None to auto-detect

    Returns:
        Tuple of (accepted_versions: set or None, needs_user_choice: bool, conflict_info: dict)
        - accepted_versions: set of version strings to accept, or None if ambiguous
        - needs_user_choice: True if user must choose between versions
        - conflict_info: dict with 'system_version', 'cauldron_media', 'numeric_media' for UI
    """
    if system_version is None:
        system_version = get_system_version()

    # Get all enabled media with their versions
    media_list = db.list_media()

    cauldron_media = []
    system_version_media = []
    other_numeric_media = []

    for m in media_list:
        if not m.get('enabled'):
            continue
        media_version = m.get('mageia_version', '')
        if not media_version:
            continue  # No version info, will be accepted anyway

        if media_version == 'cauldron':
            cauldron_media.append(m['name'])
        elif media_version == system_version:
            system_version_media.append(m['name'])
        else:
            # Other numeric version (e.g., mga9 on mga10 system)
            other_numeric_media.append((m['name'], media_version))

    conflict_info = {
        'system_version': system_version,
        'cauldron_media': cauldron_media,
        'system_version_media': system_version_media,
        'other_numeric_media': other_numeric_media,
    }

    # First: check if user has set an explicit preference
    version_mode = db.get_config('version-mode')
    if version_mode == 'cauldron':
        return {'cauldron'}, False, conflict_info
    elif version_mode == 'system':
        return {system_version}, False, conflict_info

    # No explicit preference - auto-detect based on enabled media

    # Case 1: Only cauldron media
    if cauldron_media and not system_version_media:
        return {'cauldron'}, False, conflict_info

    # Case 2: Only numeric media (system version)
    if system_version_media and not cauldron_media:
        return {system_version}, False, conflict_info

    # Case 3: Mix of system version + cauldron - needs user choice
    if system_version_media and cauldron_media:
        return None, True, conflict_info

    # No versioned media at all → accept system version by default
    return {system_version} if system_version else set(), False, conflict_info
