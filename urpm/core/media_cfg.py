"""Parser for media.cfg files — the standard Mageia media descriptor.

Every Mageia repository (official mirrors, community repos like MLO, local
builds) publishes a ``media_info/media.cfg`` at the media root.  This module
fetches and parses that file, returning structured data that ``urpm media
discover`` and ``autoconfig`` use to create media entries automatically.

Format (INI-like, parsed with configparser)::

    [media_info]
    version=10
    arch=x86_64
    branch=Devel

    [core/release]
    hdlist=hdlist_core_release.cz
    name=Core Release
    media_type=official:free:release
    noauto=1

Section names are relative paths from the media root.  Cross-architecture
media use ``../../`` prefixes (e.g. ``../../i686/media/core/release``).
"""

import configparser
import io
import logging
import posixpath
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────


@dataclass
class MediaCfgInfo:
    """Metadata from the ``[media_info]`` section."""

    version: str    # e.g. "9", "10", "cauldron"
    arch: str       # e.g. "x86_64" — may be empty on community repos
    branch: str     # e.g. "Devel", "MLO"


@dataclass
class DiscoveredMedia:
    """A single media discovered from a ``media.cfg`` file."""

    section: str          # raw section name (= relative path in media.cfg)
    name: str             # human-readable name (from ``name=`` or generated)
    relative_path: str    # full path relative to server base_path
    version: str          # from [media_info]
    architecture: str     # detected (x86_64, i686, noarch, …)
    short_name: str       # filesystem-safe identifier
    is_update: bool       # has ``updates_for=``
    is_srpms: bool        # source media
    is_debug: bool        # debug media
    is_testing: bool      # testing or backports_testing
    is_nonfree: bool      # nonfree section
    is_tainted: bool      # tainted section
    is_32bit: bool        # cross-arch 32-bit media (i586/i686)
    is_backports: bool    # backports media
    noauto: bool          # ``noauto=1`` in media.cfg
    media_type: str       # raw ``media_type=`` value
    is_official: bool     # inferred from media_type (``official:`` prefix)


# ── Known architectures for cross-arch detection ─────────────────────────

_KNOWN_ARCHES = {'x86_64', 'i586', 'i686', 'aarch64', 'armv7hl', 'noarch'}


# ── Public API ───────────────────────────────────────────────────────────


def fetch_media_cfg(base_url: str, timeout: int = 10) -> str:
    """Fetch ``media_info/media.cfg`` from *base_url*.

    Args:
        base_url: Media root URL (e.g. ``https://host/path/10/x86_64/media/``).
        timeout: Connection timeout in seconds.

    Returns:
        Raw content of the media.cfg file.

    Raises:
        RuntimeError: If the fetch fails.
    """
    import pycurl

    url = base_url.rstrip('/') + '/media_info/media.cfg'
    buf = io.BytesIO()

    c = pycurl.Curl()
    try:
        c.setopt(pycurl.URL, url)
        c.setopt(pycurl.WRITEFUNCTION, buf.write)
        c.setopt(pycurl.FOLLOWLOCATION, 1)
        c.setopt(pycurl.CONNECTTIMEOUT, timeout)
        c.setopt(pycurl.TIMEOUT, timeout * 3)
        c.setopt(pycurl.USERAGENT, 'urpm/0.7')
        c.setopt(pycurl.NOSIGNAL, 1)
        c.perform()

        http_code = c.getinfo(pycurl.HTTP_CODE)
        if http_code >= 400:
            raise RuntimeError(
                f"Failed to fetch {url}: HTTP {http_code}")

        return buf.getvalue().decode('utf-8')
    except pycurl.error as e:
        raise RuntimeError(f"Failed to fetch {url}: {e}") from e
    finally:
        c.close()


def parse_media_cfg(
    content: str,
    media_root: str,
) -> Tuple[MediaCfgInfo, List[DiscoveredMedia]]:
    """Parse a ``media.cfg`` file and return structured media descriptors.

    Args:
        content: Raw text of the media.cfg file.
        media_root: Path prefix for relative_path construction, typically
            ``"{version}/{arch}/media"`` (derived from the URL).

    Returns:
        A tuple of (info, media_list) where *info* is the ``[media_info]``
        metadata and *media_list* contains one :class:`DiscoveredMedia` per
        non-metadata section.
    """
    cfg = configparser.ConfigParser()
    # media.cfg uses leading spaces in some repos — strip them
    cfg.read_string(content)

    # ── [media_info] section ─────────────────────────────────────────
    info = MediaCfgInfo(
        version=cfg.get('media_info', 'version', fallback=''),
        arch=cfg.get('media_info', 'arch', fallback=''),
        branch=cfg.get('media_info', 'branch', fallback=''),
    )

    # ── Per-media sections ───────────────────────────────────────────
    media_root = media_root.strip('/')
    media_list: List[DiscoveredMedia] = []

    for section in cfg.sections():
        if section == 'media_info':
            continue

        opts = dict(cfg.items(section))
        raw_name = opts.get('name', '')
        media_type = opts.get('media_type', '')
        noauto = opts.get('noauto', '0').strip() == '1'
        has_updates_for = 'updates_for' in opts

        # ── Classify ─────────────────────────────────────────────
        is_srpms = (section.startswith('../../SRPMS')
                    or ':source' in media_type)
        is_debug = (section.startswith('debug/')
                    or ':debug' in media_type)
        is_testing = (':testing' in media_type
                      or 'testing' in section.split('/')[-1])

        # Detect section category (nonfree, tainted, 32-bit, backports)
        section_lower = section.lower()
        is_nonfree = 'nonfree' in section_lower
        is_tainted = 'tainted' in section_lower
        is_backports = (':backports' in media_type
                        or 'backports' in section.split('/')[-1])

        # ── Compute relative_path ────────────────────────────────
        raw_path = posixpath.normpath(media_root + '/' + section)
        # normpath handles ../../ correctly:
        #   "10/x86_64/media/../../i686/media/core/release"
        #   → "10/i686/media/core/release"
        relative_path = raw_path

        # ── Detect architecture ──────────────────────────────────
        architecture = _detect_arch(section, info.arch)
        is_32bit = architecture in ('i586', 'i686')

        # ── Generate short_name ──────────────────────────────────
        short_name = _make_short_name(section, architecture, info.arch)

        # ── Generate name if missing ─────────────────────────────
        name = raw_name or _make_display_name(section)

        # ── Detect is_official ───────────────────────────────────
        is_official = media_type.startswith('official:')

        media_list.append(DiscoveredMedia(
            section=section,
            name=name,
            relative_path=relative_path,
            version=info.version,
            architecture=architecture,
            short_name=short_name,
            is_update=has_updates_for,
            is_srpms=is_srpms,
            is_debug=is_debug,
            is_testing=is_testing,
            is_nonfree=is_nonfree,
            is_tainted=is_tainted,
            is_32bit=is_32bit,
            is_backports=is_backports,
            noauto=noauto,
            media_type=media_type,
            is_official=is_official,
        ))

    return info, media_list


def filter_media(
    media: List[DiscoveredMedia],
    *,
    include_srpms: bool = False,
    include_debug: bool = False,
) -> List[DiscoveredMedia]:
    """Filter discovered media, dropping SRPMS and debug by default.

    Args:
        media: List from :func:`parse_media_cfg`.
        include_srpms: If True, keep source media.
        include_debug: If True, keep debug media.

    Returns:
        Filtered list (new list, original unchanged).
    """
    result = []
    for m in media:
        if m.is_srpms and not include_srpms:
            continue
        if m.is_debug and not include_debug:
            continue
        result.append(m)
    return result


@dataclass
class InstalledCategories:
    """Which non-default package categories are present on the system.

    Used to decide whether ``noauto`` media should be re-enabled, following
    the same logic as urpmi's ``needed_extra_media()``.
    """

    nonfree: bool = False   # packages with release ending in "nonfree"
    tainted: bool = False   # packages with release ending in "tainted"
    has_32bit: bool = False  # i?86 packages installed on a 64-bit system


def detect_installed_categories() -> InstalledCategories:
    """Scan the RPM database for nonfree, tainted and 32-bit packages.

    Mirrors urpmi's ``needed_extra_media()`` logic: checks the ``release``
    tag of installed packages for ``nonfree``/``tainted`` suffixes, and the
    ``arch`` tag for ``i?86`` on 64-bit systems.
    """
    import platform
    import re
    import rpm

    result = InstalledCategories()
    is_64bit = platform.machine() in ('x86_64', 'aarch64')

    ts = rpm.TransactionSet()
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)
    mi = ts.dbMatch()

    for hdr in mi:
        if result.nonfree and result.tainted and result.has_32bit:
            break  # all detected, stop early
        rel = hdr[rpm.RPMTAG_RELEASE]
        if not rel:
            continue
        if isinstance(rel, bytes):
            rel = rel.decode('utf-8', errors='replace')
        if not result.nonfree and rel.endswith('nonfree'):
            result.nonfree = True
        if not result.tainted and rel.endswith('tainted'):
            result.tainted = True
        if is_64bit and not result.has_32bit:
            arch = hdr[rpm.RPMTAG_ARCH]
            if not arch:
                continue
            if isinstance(arch, bytes):
                arch = arch.decode('utf-8', errors='replace')
            if re.match(r'i[3-6]86', arch):
                result.has_32bit = True

    return result


def should_enable(
    media: DiscoveredMedia,
    installed: InstalledCategories,
    *,
    force_nonfree: Optional[bool] = None,
    force_tainted: Optional[bool] = None,
    force_32bit: Optional[bool] = None,
    force_enable_all: bool = False,
) -> bool:
    """Decide whether a discovered media should be enabled.

    Priority order:
        1. ``force_enable_all`` overrides everything (except SRPMS/debug/testing)
        2. Explicit ``force_nonfree`` / ``force_tainted`` / ``force_32bit``
           override auto-detection
        3. Auto-detection from installed packages (like urpmi)
        4. ``noauto`` flag from media.cfg

    Backports, testing, debug and SRPMS are never auto-enabled
    (matching urpmi's ``$non_regular_medium`` check).
    """
    # "Non-regular" media are never auto-enabled (urpmi media.pm:790)
    if media.is_testing or media.is_srpms or media.is_debug or media.is_backports:
        return False

    if force_enable_all:
        return True

    if not media.noauto:
        return True

    # noauto=1 — check overrides and auto-detection
    # A media can belong to multiple categories (e.g. 32-bit nonfree).
    # ALL applicable conditions must be satisfied.

    # 32-bit gate: if the media is cross-arch, the system must have
    # 32-bit packages (or --with 32bit) to enable it.
    if media.is_32bit:
        bit32_ok = force_32bit if force_32bit is not None else installed.has_32bit
        if not bit32_ok:
            return False

    # Category gate: nonfree/tainted must be detected or forced.
    if media.is_nonfree:
        nf_ok = force_nonfree if force_nonfree is not None else installed.nonfree
        return nf_ok

    if media.is_tainted:
        t_ok = force_tainted if force_tainted is not None else installed.tainted
        return t_ok

    # 32-bit core (not nonfree/tainted): 32-bit gate already passed
    if media.is_32bit:
        return True

    return False


def decompose_url(
    url: str,
    version: str,
    arch: str,
) -> Tuple[str, str, str, str]:
    """Split a media root URL into server and media components.

    Given a URL like ``https://host/pub/Mageia/distrib/10/x86_64/media/``
    and version/arch from ``[media_info]``, returns:

    - **scheme**: ``https``
    - **host**: ``host``
    - **base_path**: ``/pub/Mageia/distrib`` (before version/arch/media)
    - **media_root**: ``10/x86_64/media`` (used to build relative_path)

    If the ``{version}/{arch}/media`` pattern is not found in the URL path
    (e.g. non-standard repos), *base_path* is empty and *media_root* is the
    full URL path.

    Args:
        url: The media root URL provided by the user.
        version: Version string from ``[media_info]``.
        arch: Architecture string from ``[media_info]``.

    Returns:
        Tuple of (scheme, host, base_path, media_root).
    """
    parsed = urlparse(url.rstrip('/'))
    path = parsed.path.strip('/')

    # Try to find {version}/{arch}/media (or just {version}/media if no arch)
    if version and arch:
        needle = f"{version}/{arch}/media"
    elif version:
        needle = f"{version}/media"
    else:
        needle = None

    if needle and needle in path:
        idx = path.index(needle)
        base_path = '/' + path[:idx].rstrip('/') if idx > 0 else ''
        media_root = needle
    else:
        # Fallback: entire path is the media root
        base_path = ''
        media_root = path

    return parsed.scheme, parsed.hostname or '', base_path, media_root


# ── Internal helpers ─────────────────────────────────────────────────────


def _detect_arch(section: str, default_arch: str) -> str:
    """Detect architecture from a media.cfg section name.

    Cross-architecture sections use paths like ``../../i686/media/core/release``
    where the architecture appears right before ``media``.  Native sections
    (e.g. ``core/release``) use the default arch from ``[media_info]``.
    """
    parts = section.replace('\\', '/').split('/')

    # Look for a known arch followed by 'media' in the path
    for i, part in enumerate(parts):
        if part in _KNOWN_ARCHES and i + 1 < len(parts) and parts[i + 1] == 'media':
            return part

    return default_arch or 'x86_64'


def _make_short_name(section: str, arch: str, default_arch: str) -> str:
    """Generate a filesystem-safe short name from a section path.

    Examples:
        - ``core/release`` → ``core_release``
        - ``../../i686/media/core/release`` → ``i686_core_release``
        - ``debug/core/release`` → ``debug_core_release``
        - ``core`` (MLO) → ``core``
    """
    # Strip ../../ prefixes and /media/ segments
    clean = section
    while clean.startswith('../'):
        clean = clean[3:]

    parts = clean.split('/')
    # Remove architecture and 'media' segments
    filtered = [p for p in parts if p not in ('media',) and p not in _KNOWN_ARCHES]

    name = '_'.join(filtered) if filtered else section.replace('/', '_')

    # Prefix with arch if it's a cross-arch section
    if arch != default_arch and arch:
        name = f"{arch}_{name}"

    return name.lower()


def _make_display_name(section: str) -> str:
    """Generate a human-readable display name from a section path.

    Examples:
        - ``core/release`` → ``Core Release``
        - ``../../i686/media/core/release`` → ``Core 32bit Release``
    """
    # Detect cross-arch
    is_cross = section.startswith('../../')
    arch_label = ''
    parts = section.split('/')

    if is_cross:
        for part in parts:
            if part in _KNOWN_ARCHES:
                if part in ('i586', 'i686'):
                    arch_label = '32bit '
                else:
                    arch_label = f'{part} '
                break

    # Extract the meaningful path segments (after 'media' if present)
    if 'media' in parts:
        idx = parts.index('media')
        meaningful = parts[idx + 1:]
    else:
        # Strip leading ../
        meaningful = [p for p in parts if p != '..']

    # Capitalize each part
    words = []
    for part in meaningful:
        words.append(part.replace('_', ' ').title())

    name = ' '.join(words)

    if arch_label and name:
        # Insert arch after first word: "Core 32bit Release"
        first, *rest = name.split(' ', 1)
        name = f"{first} {arch_label}" + (rest[0] if rest else '')

    return name or section
