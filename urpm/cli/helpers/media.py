"""Media and URL helper functions."""

import os
import re
import subprocess
import tempfile
from urllib.parse import urlparse

# Known Mageia release versions
KNOWN_VERSIONS = {'7', '8', '9', '10', 'cauldron'}

# Known architectures
KNOWN_ARCHES = {'x86_64', 'aarch64', 'armv7hl', 'i586', 'i686'}

# Version segment with optional ``mageia`` / ``mga`` prefix — covers
# community layouts like blogdrake (``mageia10``, ``mga10``) in
# addition to the officiels' bare ``10``.  Captures the version
# number so the caller can compare it to :data:`KNOWN_VERSIONS`.
# Kept in sync with :data:`urpm.core.media_pipeline._VERSION_TOKEN_RE`.
_VERSION_TOKEN_RE = re.compile(
    r"^(?:mageia|mga)?(\d+(?:\.\d+)?|cauldron)$", re.IGNORECASE,
)


def _match_version_token(segment: str) -> str | None:
    """Return the version number if *segment* carries one, else None.

    Recognises ``10``, ``cauldron``, ``mageia10``, ``mga10`` and
    returns the numeric portion (or ``'cauldron'``) as a lowercase
    string.  Only versions in :data:`KNOWN_VERSIONS` are accepted —
    a random ``mageia42`` is rejected as noise.
    """
    if not segment:
        return None
    m = _VERSION_TOKEN_RE.match(segment)
    if not m:
        return None
    version = m.group(1).lower()
    return version if version in KNOWN_VERSIONS else None

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


class MediaNameCollision(Exception):
    """A media display name cannot be safely placed in the DB.

    Raised by :func:`disambiguate_media_name` when the requested
    ``base_name`` is already taken AND the situation cannot be
    salvaged by appending the arch suffix (because the candidate is
    on the native arch, or the suffixed name is also taken).

    Carries enough context for the CLI to print a helpful message
    (existing media id, hint to pass ``--name`` explicitly).
    """

    def __init__(self, base_name: str, existing: dict):
        super().__init__(f"Media name {base_name!r} already taken")
        self.base_name = base_name
        self.existing = existing


def disambiguate_media_name(db, base_name: str, arch: str) -> str:
    """Return a display name guaranteed not to collide in DB right now.

    ``UNIQUE(media.name)`` would otherwise abort an insert when two
    media share the same canonical display name across architectures
    (a typical cross-arch sibling case).  Resolution rules:

    * No existing row owns ``base_name`` → return ``base_name``.
    * Existing row owns ``base_name`` and the new media is on a
      **foreign** arch → return ``f"{base_name} ({arch})"`` after
      checking that suffixed form is also free.  This matches the
      urpmi convention where the native arch wears the canonical
      name and cross-arch siblings carry the disambiguator.
    * Existing row owns ``base_name`` and the new media is on the
      **native** arch (or the suffixed form is also taken) →
      raise :class:`MediaNameCollision`.  The caller is expected
      to surface a clear error rather than invent a name silently.

    The returned value is safe to feed into ``db.add_media`` only
    immediately afterwards — there is no TOCTOU guard, but urpm-ng
    CLI is single-process so the window is negligible.

    Args:
        db: Database accessor.
        base_name: Desired display name.
        arch: Architecture of the media being added.

    Returns:
        A display name that does not currently collide.

    Raises:
        MediaNameCollision: When no safe disambiguation is available.
    """
    existing = db.get_media(base_name)
    if existing is None:
        return base_name

    from .package import system_arch
    if arch == system_arch():
        raise MediaNameCollision(base_name, existing)

    suffixed = f"{base_name} ({arch})"
    if db.get_media(suffixed) is None:
        return suffixed
    raise MediaNameCollision(base_name, existing)


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

    Attempts best-effort detection of version/arch from the URL path
    using the same pattern as official Mageia URLs (version/arch/media/).
    If not detected, version and arch are set to None.

    Args:
        url: Full URL to a custom media

    Returns:
        Dict with parsed components, or None if invalid.
        Keys: protocol, host, base_path, relative_path, version, arch
    """
    parsed = urlparse(url.rstrip('/'))

    if parsed.scheme == 'file':
        protocol = 'file'
        host = ''
        path = parsed.path
    elif parsed.scheme in ('http', 'https'):
        protocol = parsed.scheme
        host = parsed.netloc
        path = parsed.path
    else:
        return None

    # Version and arch detection.  Scan the path independently for
    # each — officiels place them consecutive (``.../10/x86_64/``),
    # blogdrake plate separates them by the channel
    # (``.../mageia10/free/x86_64/``), and this decoupled scan handles
    # both.  Version tokens accept the ``mageia<N>`` / ``mga<N>``
    # prefix in addition to the bare form.
    parts = [p for p in path.split('/') if p]
    version_hits = [
        (i, _match_version_token(p)) for i, p in enumerate(parts)
        if _match_version_token(p) is not None
    ]
    arch_hits = [(i, p) for i, p in enumerate(parts) if p in KNOWN_ARCHES]

    # Guard : require exactly one hit on each dimension.  Multiple
    # candidates on either side means the URL is either weird or
    # third-party in a way we shouldn't guess ; the user can force
    # things via ``--version`` / ``--arch`` at that point.
    if len(version_hits) == 1 and len(arch_hits) == 1:
        version = version_hits[0][1]
        arch = arch_hits[0][1]
        # ── base_path / relative_path split ─────────────────────────
        # Keep in lockstep with :func:`urpm.core.media_pipeline._split_url`.
        # Pivot is whichever of the two segments comes first — for
        # official URLs that's the version, for blogdrake plate too
        # (mageia<N> before <arch>).  Everything before the pivot
        # belongs to base_path, everything at/after to relative_path.
        pivot = min(version_hits[0][0], arch_hits[0][0])
        base_path = '/' + '/'.join(parts[:pivot]) if pivot > 0 else ''
        relative_path = '/'.join(parts[pivot:])
    else:
        version = None
        arch = None
        base_path = ''
        relative_path = path.lstrip('/')

    return {
        'protocol': protocol,
        'host': host,
        'base_path': base_path,
        'relative_path': relative_path,
        'version': version,
        'arch': arch,
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
    # rpmdb access via urpm.core.rpmdb — never open a librpm handle
    # in the parent (module contract).  gpg-pubkey pkg VERSION field
    # holds the short key id, compared case-insensitively.
    from ...core import rpmdb
    keyid_lower = keyid.lower()
    for pkg in rpmdb.query_by_name('gpg-pubkey'):
        if pkg.version.lower() == keyid_lower:
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
