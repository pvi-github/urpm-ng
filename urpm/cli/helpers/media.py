"""Media and URL helper functions."""

import os
import subprocess
import tempfile
from urllib.parse import urlparse

# Known Mageia release versions
KNOWN_VERSIONS = {'7', '8', '9', '10', 'cauldron'}

# Known architectures
KNOWN_ARCHES = {'x86_64', 'aarch64', 'armv7hl', 'i586', 'i686'}

# Known media classes
KNOWN_CLASSES = {'core', 'nonfree', 'tainted', 'debug'}

# Known media types
KNOWN_TYPES = {'release', 'updates', 'backports', 'backports_testing', 'updates_testing', 'testing'}


def generate_media_name(class_name: str, type_name: str) -> str:
    """Generate display name from class and type.

    Examples:
        core, release -> Core Release
        nonfree, updates -> Nonfree Updates
        tainted, backports_testing -> Tainted Backports Testing
    """
    class_title = class_name.capitalize()
    type_title = type_name.replace('_', ' ').title()
    return f"{class_title} {type_title}"


def generate_short_name(class_name: str, type_name: str) -> str:
    """Generate short_name from class and type.

    Examples:
        core, release -> core_release
        nonfree, updates -> nonfree_updates
    """
    return f"{class_name}_{type_name}"


def generate_server_name(protocol: str, host: str) -> str:
    """Generate a server name from protocol and host.

    Examples:
        https, mirrors.mageia.org -> mageia-official
        https, distrib-coffee.ipsl.jussieu.fr -> distrib-coffee
        file, '' -> local-mirror
    """
    if protocol == 'file':
        return 'local-mirror'

    # Use first part of hostname
    if '.' in host:
        first_part = host.split('.')[0]
        # Special case for common mirror names
        if first_part in ('mirrors', 'mirror', 'ftp', 'www'):
            # Use second part instead
            parts = host.split('.')
            if len(parts) > 1:
                first_part = parts[1]
        return first_part
    return host


def parse_mageia_media_url(url: str) -> dict | None:
    """Parse an official Mageia media URL.

    Detects pattern: .../version/arch/media/class/type/
    Also handles file:// URLs for local mirrors.

    Args:
        url: Full URL to a media

    Returns:
        Dict with parsed components, or None if not a recognized Mageia URL.
        Keys: protocol, host, base_path, relative_path, version, arch,
              class_name, type_name, name, short_name, is_official
    """
    # Parse URL
    parsed = urlparse(url.rstrip('/'))

    if parsed.scheme == 'file':
        protocol = 'file'
        host = ''  # No host for file:// URLs
        path = parsed.path
    elif parsed.scheme in ('http', 'https'):
        protocol = parsed.scheme
        host = parsed.netloc
        path = parsed.path
    else:
        return None  # Unknown protocol

    # Split path into components
    parts = [p for p in path.split('/') if p]

    # Look for the pattern: version/arch/media/class/type
    # Or for debug: version/arch/media/debug/class/type
    # Search for 'media' keyword
    try:
        media_idx = parts.index('media')
    except ValueError:
        return None  # No 'media' in path

    # Need at least: something before media, and class/type after
    if media_idx < 2 or len(parts) < media_idx + 3:
        return None

    # Check for debug media: .../media/debug/{class}/{type}
    is_debug = False
    if parts[media_idx + 1] == 'debug':
        is_debug = True
        if len(parts) < media_idx + 4:
            return None
        class_name = parts[media_idx + 2]
        type_name = parts[media_idx + 3]
    else:
        class_name = parts[media_idx + 1]
        type_name = parts[media_idx + 2]

    # Validate class and type
    if class_name not in KNOWN_CLASSES:
        return None
    if type_name not in KNOWN_TYPES:
        return None

    # Look backwards from 'media' for version and arch
    # Pattern should be: version/arch/media
    arch = parts[media_idx - 1]
    version = parts[media_idx - 2]

    # Validate version and arch
    if arch not in KNOWN_ARCHES:
        return None
    if version not in KNOWN_VERSIONS:
        return None

    # Calculate base_path (everything before version)
    # e.g., /mageia or /pub/linux/Mageia
    version_idx = media_idx - 2
    base_path_parts = parts[:version_idx]
    if base_path_parts:
        base_path = '/' + '/'.join(base_path_parts)
    else:
        base_path = ''

    # Calculate relative_path (version onwards)
    # e.g., 9/x86_64/media/core/release
    relative_path = '/'.join(parts[version_idx:])

    # Generate names
    if is_debug:
        name = generate_media_name(class_name, type_name) + " Debug"
        short_name = "debug_" + generate_short_name(class_name, type_name)
    else:
        name = generate_media_name(class_name, type_name)
        short_name = generate_short_name(class_name, type_name)

    return {
        'protocol': protocol,
        'host': host,
        'base_path': base_path,
        'relative_path': relative_path,
        'version': version,
        'arch': arch,
        'class_name': class_name,
        'is_debug': is_debug,
        'type_name': type_name,
        'name': name,
        'short_name': short_name,
        'is_official': True,
    }


def parse_custom_media_url(url: str) -> dict | None:
    """Parse a custom (non-Mageia) media URL.

    For custom URLs, we can't auto-detect version/arch.
    We use the hostname as base and the path as relative_path.

    Args:
        url: Full URL to a custom media

    Returns:
        Dict with parsed components, or None if invalid.
        Keys: protocol, host, base_path, relative_path
    """
    parsed = urlparse(url.rstrip('/'))

    if parsed.scheme == 'file':
        protocol = 'file'
        host = ''
        # For file://, everything is the path
        relative_path = parsed.path.lstrip('/')
        base_path = ''
    elif parsed.scheme in ('http', 'https'):
        protocol = parsed.scheme
        host = parsed.netloc
        # For http(s), base_path is empty, relative_path is the full path
        relative_path = parsed.path.lstrip('/')
        base_path = ''
    else:
        return None

    return {
        'protocol': protocol,
        'host': host,
        'base_path': base_path,
        'relative_path': relative_path,
        'is_official': False,
    }


def fetch_media_pubkey(url: str) -> bytes | None:
    """Fetch pubkey from media_info/pubkey.

    Args:
        url: Media base URL

    Returns:
        Key data as bytes, or None if not found
    """
    import urllib.request
    import urllib.error

    pubkey_url = url.rstrip('/') + '/media_info/pubkey'
    try:
        with urllib.request.urlopen(pubkey_url, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # No pubkey, that's OK
        raise
    except urllib.error.URLError:
        return None


def get_gpg_key_info(key_data: bytes) -> dict | None:
    """Parse GPG key info using gpg command.

    Args:
        key_data: Raw GPG key data

    Returns:
        Dict with 'keyid', 'fingerprint', 'uid', 'created' or None on error
    """
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.gpg', delete=False) as tmp:
        tmp.write(key_data)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ['gpg', '--show-keys', '--keyid-format', 'long', '--with-colons', tmp_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None

        info = {}
        for line in result.stdout.split('\n'):
            fields = line.split(':')
            if fields[0] == 'pub':
                # pub:...:keyid:created:...
                info['keyid'] = fields[4][-8:].lower()  # Last 8 chars
                info['keyid_long'] = fields[4].lower()
                if fields[5]:
                    info['created'] = fields[5]
            elif fields[0] == 'fpr':
                info['fingerprint'] = fields[9]
            elif fields[0] == 'uid' and 'uid' not in info:
                info['uid'] = fields[9]

        return info if info.get('keyid') else None
    finally:
        os.unlink(tmp_path)


def is_key_in_rpm_keyring(keyid: str) -> bool:
    """Check if a GPG key is already in the RPM keyring.

    Args:
        keyid: Key ID (8 hex chars, lowercase)

    Returns:
        True if key is installed
    """
    import rpm

    ts = rpm.TransactionSet()
    for hdr in ts.dbMatch('name', 'gpg-pubkey'):
        version = hdr[rpm.RPMTAG_VERSION].lower()
        if version == keyid:
            return True
    return False


def import_gpg_key(key_data: bytes) -> bool:
    """Import GPG key into RPM keyring.

    Args:
        key_data: Raw GPG key data

    Returns:
        True on success
    """
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.gpg', delete=False) as tmp:
        tmp.write(key_data)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ['rpm', '--import', tmp_path],
            capture_output=True, text=True
        )
        return result.returncode == 0
    finally:
        os.unlink(tmp_path)


# Backwards compatibility aliases (with underscore prefix)
_generate_media_name = generate_media_name
_generate_short_name = generate_short_name
_generate_server_name = generate_server_name
_fetch_media_pubkey = fetch_media_pubkey
_get_gpg_key_info = get_gpg_key_info
_is_key_in_rpm_keyring = is_key_in_rpm_keyring
_import_gpg_key = import_gpg_key
