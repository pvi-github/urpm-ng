"""Shared mirror list fetching and geographic filtering.

All mirror discovery (``urpm init``, ``urpm media autoconfig``,
``urpm server autoconf``, and background pool expansion) should go
through :func:`fetch_mirrors` to ensure consistent geo filtering
and a single source of mirror metadata.

The Mageia mirror API returns key=value lines::

    continent=EU,zone=DE,country=DE,city=Falkenstein,…,url=https://…

This module parses all fields, applies the ``[server]`` geo settings
(see :mod:`urpm.core.settings`), and returns typed :class:`MirrorInfo`
objects.
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from .database import PackageDatabase

log = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────

_API_URL = "https://mirrors.mageia.org/api/mageia.{release}.{arch}.list"
"""Mageia mirror API endpoint — returns key=value CSV with geo metadata."""

_KV_PATTERN = re.compile(r'(\w+)=([^,]*)')
"""Matches ``key=value`` pairs separated by commas.

Works for all known fields (continent, zone, country, city, latitude,
longitude, version, arch, type, url).  The ``url=`` value is always last
and may contain ``/`` and ``:`` which are fine since there is no comma
after it.
"""


# ─── Data model ────────────────────────────────────────────────────────

@dataclass
class MirrorInfo:
    """Parsed mirror entry from the Mageia mirror API."""

    url: str
    """Full URL as returned by the API (e.g. ``https://…/distrib/9/x86_64``)."""

    scheme: str
    """``http`` or ``https``."""

    host: str
    """Hostname extracted from the URL."""

    base_path: str
    """Full path component of the URL (caller strips version/arch suffix)."""

    continent: str
    """Two-letter continent code (``EU``, ``NA``, ``SA``, ``AS``, ``AF``, ``OC``)."""

    country: str
    """Two-letter ISO 3166 country code (``FR``, ``DE``, ``UA``, …)."""

    city: str
    """City name (informational, may be empty)."""


# ─── Parsing ───────────────────────────────────────────────────────────

def _parse_mirror_line(line: str) -> Optional[MirrorInfo]:
    """Parse a single ``key=value,...`` line into a :class:`MirrorInfo`.

    Returns ``None`` for non-HTTP(S) entries (rsync, ftp) or malformed
    lines.
    """
    fields = dict(_KV_PATTERN.findall(line))
    url = fields.get("url", "")
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return None
    parsed = urlparse(url)
    return MirrorInfo(
        url=url,
        scheme=parsed.scheme,
        host=parsed.hostname or "",
        base_path=parsed.path,
        continent=fields.get("continent", ""),
        country=fields.get("country", fields.get("zone", "")),
        city=fields.get("city", ""),
    )


# ─── Geo filtering ────────────────────────────────────────────────────

def _passes_geo_filter(
    mirror: MirrorInfo,
    continent_whitelist: list,
    continent_blacklist: list,
    country_whitelist: list,
    country_blacklist: list,
) -> bool:
    """Return True if *mirror* passes all configured geo filters.

    Rules (applied in order):

    1. Continent: whitelist wins over blacklist.
    2. Country: whitelist wins over blacklist.
    3. A mirror must pass **both** filters (continent AND country).
    """
    # — Continent —
    if continent_whitelist:
        if mirror.continent not in continent_whitelist:
            return False
    elif continent_blacklist:
        if mirror.continent in continent_blacklist:
            return False

    # — Country —
    if country_whitelist:
        if mirror.country not in country_whitelist:
            return False
    elif country_blacklist:
        if mirror.country in country_blacklist:
            return False

    return True


# ─── Fetch ─────────────────────────────────────────────────────────────

def fetch_mirrors(
    release: str,
    arch: str,
    apply_geo_filter: bool = True,
    timeout: int = 30,
) -> List[MirrorInfo]:
    """Fetch the Mageia mirror list with optional geo filtering.

    Args:
        release: Mageia release number (e.g. ``"9"``, ``"cauldron"``).
        arch: Architecture (e.g. ``"x86_64"``).
        apply_geo_filter: When True (default), apply the ``[server]``
            geo settings from :func:`~urpm.core.settings.get_settings`.
        timeout: HTTP timeout in seconds.

    Returns:
        List of :class:`MirrorInfo` (HTTP/HTTPS only), filtered by geo
        settings when *apply_geo_filter* is True.

    Raises:
        URLError, HTTPError: On network failure.
    """
    url = _API_URL.format(release=release, arch=arch)
    req = Request(url)
    req.add_header("User-Agent", "urpm-ng")

    log.debug("Fetching mirror list from %s", url)
    with urlopen(req, timeout=timeout) as response:
        content = response.read().decode("utf-8").strip()

    mirrors: List[MirrorInfo] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        info = _parse_mirror_line(line)
        if info is not None:
            mirrors.append(info)

    log.debug("Parsed %d HTTP(S) mirrors from API", len(mirrors))

    if apply_geo_filter:
        from .settings import get_settings

        s = get_settings().server
        before = len(mirrors)
        mirrors = [
            m for m in mirrors
            if _passes_geo_filter(
                m,
                continent_whitelist=s.continent_whitelist,
                continent_blacklist=s.continent_blacklist,
                country_whitelist=s.country_whitelist,
                country_blacklist=s.country_blacklist,
            )
        ]
        filtered = before - len(mirrors)
        if filtered:
            log.info(
                "Geo filter: %d/%d mirrors excluded", filtered, before
            )

    return mirrors


def parse_mirrorlist_content(content: str) -> List[MirrorInfo]:
    """Parse raw mirrorlist text (key=value CSV lines) into MirrorInfo objects.

    This handles the same ``key=value,...`` format used by the Mageia mirror
    API.  Non-HTTP(S) entries and blank lines are silently ignored.

    Useful for parsing the body of a custom mirrorlist URL that follows
    the same format as the official API.

    Args:
        content: Raw text with one mirror per line, each line being
            comma-separated ``key=value`` pairs (at minimum ``url=…``).

    Returns:
        List of :class:`MirrorInfo` for valid HTTP/HTTPS entries.
    """
    mirrors: List[MirrorInfo] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        info = _parse_mirror_line(line)
        if info is not None:
            mirrors.append(info)
    return mirrors


def backfill_server_countries(
    db: 'PackageDatabase',
    release: str,
    arch: str,
) -> int:
    """Populate the ``country`` column for servers added before geo filtering.

    Fetches the **full** mirror list (unfiltered) from the Mageia API,
    matches existing database servers by hostname, and fills in their
    ``country`` value.  Servers whose host is not in the API (private or
    corporate mirrors) keep ``country = NULL`` and always pass the geo
    filter.

    After backfill, any server whose newly-populated country fails the
    current ``[server]`` geo settings is **disabled** (not deleted) so it
    stops being selected for downloads.

    This function is cheap when there is nothing to do: one
    ``SELECT COUNT(*)`` equivalent on the server table, then early return.

    Args:
        db: Package database.
        release: Mageia version (e.g. ``"9"``, ``"10"``).
        arch: Architecture (e.g. ``"x86_64"``).

    Returns:
        Number of servers whose country was updated.
    """
    servers = db.list_servers()
    need_backfill = [s for s in servers if s.get('country') is None]
    if not need_backfill:
        return 0

    # Fetch ALL mirrors (no geo filter) — we need the complete list so we
    # can match hosts even if those hosts are blacklisted.
    try:
        all_mirrors = fetch_mirrors(
            release, arch, apply_geo_filter=False, timeout=15,
        )
    except Exception as e:
        log.debug("Cannot fetch mirror list for country backfill: %s", e)
        return 0

    # Build host → MirrorInfo lookup.  First match per host is enough:
    # all entries for the same host share the same country/continent.
    host_to_mirror: dict = {}
    for m in all_mirrors:
        if m.host not in host_to_mirror:
            host_to_mirror[m.host] = m

    # Load geo settings once for the disable check.
    from .settings import get_settings
    geo = get_settings().server

    updated = 0
    disabled = 0
    for srv in need_backfill:
        mirror = host_to_mirror.get(srv['host'])
        if not mirror or not mirror.country:
            continue

        db.set_server_country_by_id(srv['id'], mirror.country)
        updated += 1
        log.debug("Backfilled country=%s for server %s",
                  mirror.country, srv['name'])

        # If this server now fails the geo filter, disable it.
        if not _passes_geo_filter(
            mirror,
            continent_whitelist=geo.continent_whitelist,
            continent_blacklist=geo.continent_blacklist,
            country_whitelist=geo.country_whitelist,
            country_blacklist=geo.country_blacklist,
        ):
            if srv.get('enabled', 1):
                db.enable_server(srv['name'], enabled=False)
                disabled += 1
                log.warning(
                    "Disabled server %s: %s/%s blocked by geo filter",
                    srv['name'], mirror.continent, mirror.country,
                )

    if updated:
        log.info("Backfilled country for %d/%d servers",
                 updated, len(need_backfill))
    if disabled:
        log.info("Disabled %d server(s) blocked by geo filter", disabled)

    return updated


def dedup_mirrors(
    mirrors: List[MirrorInfo],
    strip_suffix: str = "",
) -> List[MirrorInfo]:
    """Deduplicate mirrors by ``(host, base_path)``, preferring HTTPS.

    Args:
        mirrors: Input mirror list.
        strip_suffix: Suffix to strip from *base_path* before comparing
            (e.g. ``"/9/x86_64"`` to normalize all mirrors to their
            root path).

    Returns:
        Deduplicated list preserving input order.  When the same
        ``(host, base_path)`` appears with both HTTP and HTTPS, the
        HTTPS entry is kept.
    """
    seen: dict = {}  # (host, norm_path) → index in result
    result: List[MirrorInfo] = []

    for m in mirrors:
        path = m.base_path
        if strip_suffix and path.endswith(strip_suffix):
            path = path[: -len(strip_suffix)]
        path = path.rstrip("/")

        key = (m.host, path)
        if key in seen:
            idx = seen[key]
            if result[idx].scheme == "http" and m.scheme == "https":
                result[idx] = m  # Upgrade to HTTPS
        else:
            seen[key] = len(result)
            result.append(m)

    return result
