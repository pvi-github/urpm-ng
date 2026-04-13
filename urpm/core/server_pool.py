"""Server pool management — ensure enough mirrors for parallel downloads.

When parallel download count increases, the server pool must be large enough
to avoid multiple slots hitting the same mirror.  Rule of thumb:
    min_servers >= ceil(parallel * 1.5)

If the pool is too small, servers are auto-added from the Mageia mirrorlist
using the same logic as ``urpm server autoconf``.
"""

import logging
import math
import platform
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from .database import PackageDatabase

logger = logging.getLogger(__name__)

_MIRRORLIST_URL = (
    "https://www.mageia.org/mirrorlist/"
    "?release={version}&arch={arch}&section=core&repo=release"
)
"""Legacy mirrorlist endpoint (plain URLs, no geo metadata).

Kept as fallback constant but :func:`_fetch_and_filter` now uses
:func:`~urpm.core.mirrorlist.fetch_mirrors` (the API endpoint with
continent/country metadata) so that geo filtering works.
"""


@dataclass
class PoolCheckResult:
    """Result of a server pool check.

    Attributes:
        sufficient: True if the pool was already large enough.
        had: Number of enabled servers before the check.
        needed: Minimum number required for the configured parallelism.
        added: List of (name, latency_ms) for each server added.
    """
    sufficient: bool
    had: int
    needed: int
    added: List[tuple] = field(default_factory=list)


def minimum_servers_for(parallel: int) -> int:
    """Minimum number of enabled servers for *parallel* download slots."""
    return math.ceil(parallel * 1.5)


def ensure_minimum_servers(db: 'PackageDatabase',
                           parallel: int) -> PoolCheckResult:
    """Add mirrors from the Mageia mirrorlist if the pool is too small.

    Args:
        db: Package database (for server queries and inserts).
        parallel: Configured number of parallel download slots.

    Returns:
        PoolCheckResult describing what happened.
    """
    min_needed = minimum_servers_for(parallel)

    # Detect version/arch early — needed for both backfill and pool expansion.
    version = _detect_version()
    arch = platform.machine() if version else ''

    # Backfill country data for servers added before geo filtering existed.
    # Must run before the pool-size check: backfill may disable servers
    # that fail the geo filter, changing the enabled count.
    if version:
        from .mirrorlist import backfill_server_countries
        backfill_server_countries(db, version, arch)

    existing = db.list_servers(enabled_only=True)

    if len(existing) >= min_needed:
        return PoolCheckResult(sufficient=True, had=len(existing),
                               needed=min_needed)

    # Respect [server] auto_add = false
    from .settings import get_settings
    if not get_settings().server.auto_add:
        logger.info("Server pool too small (%d/%d) but auto_add is disabled",
                     len(existing), min_needed)
        return PoolCheckResult(sufficient=False, had=len(existing),
                               needed=min_needed)

    to_add = min_needed - len(existing)
    logger.info("Server pool too small (%d/%d) for %d parallel downloads, "
                "adding %d mirrors", len(existing), min_needed, parallel, to_add)

    if not version:
        logger.warning("Cannot detect Mageia version, skipping server auto-add")
        return PoolCheckResult(sufficient=False, had=len(existing),
                               needed=min_needed)

    # Build duplicate sets — keyed by (host, base_path) so that the same
    # hostname with different base paths (e.g. corporate mirror hosting
    # both official and community repos) is treated as distinct servers.
    all_servers = db.list_servers()
    existing_host_paths = set()
    existing_names = set()
    for s in all_servers:
        existing_host_paths.add((s['host'], s.get('base_path', '')))
        existing_names.add(s['name'])

    # Fetch mirrorlist
    candidates = _fetch_and_filter(version, arch, existing_host_paths)
    if not candidates:
        logger.warning("No new mirror candidates found")
        return PoolCheckResult(sufficient=False, had=len(existing),
                               needed=min_needed)

    # Test latency
    reachable = _test_latency(candidates)
    if not reachable:
        logger.warning("No reachable mirrors found")
        return PoolCheckResult(sufficient=False, had=len(existing),
                               needed=min_needed)

    # Sort by latency, take best N
    reachable.sort(key=lambda x: x[1])
    best = reachable[:to_add]

    # Seed new servers with the average bandwidth of existing ones so the
    # download planner gives them a fair share immediately.  The EWMA will
    # converge to real performance after the first few downloads.
    avg_kbps = _average_bandwidth(all_servers)

    # Add servers
    added = []
    for candidate, latency in best:
        name = _unique_name(candidate['host'], existing_names)
        try:
            server_id = db.add_server(
                name, candidate['scheme'], candidate['host'],
                candidate['base_path'],
                country=candidate.get('country'))
            db.update_server_stats(server_id, latency_ms=int(latency),
                                   bandwidth_kbps=avg_kbps)
            existing_names.add(name)
            added.append((name, int(latency)))
            logger.info("Auto-added server: %s (%dms, seeded %d KB/s)",
                        name, int(latency), avg_kbps)
        except Exception as e:
            logger.warning("Failed to add server %s: %s", name, e)

    # Link new servers to enabled media
    if added:
        server_ids = []
        for name, _latency in added:
            s = db.get_server(name)
            if s:
                server_ids.append((s['id'], name))
        _link_servers_to_media(db, server_ids)

    # Probe ALL enabled servers that lack bandwidth data — not just
    # newly added ones.  This covers servers imported from urpmi.cfg
    # or added manually that were never probed.
    servers_to_probe = []
    for s in db.list_servers(enabled_only=True):
        if not s.get('bandwidth_kbps'):
            servers_to_probe.append((s['id'], s['name']))
    if servers_to_probe:
        logger.info("Probing %d servers without bandwidth data",
                    len(servers_to_probe))
        _probe_bandwidth(db, servers_to_probe)

    return PoolCheckResult(sufficient=len(existing) + len(added) >= min_needed,
                           had=len(existing), needed=min_needed, added=added)


# ── Internal helpers ──────────────────────────────────────────────────────


def _average_bandwidth(servers: list) -> int:
    """Return the average bandwidth_kbps of servers that have a measurement.

    Falls back to 5000 KB/s (~5 MB/s) if no server has bandwidth data.
    """
    values = [s.get('bandwidth_kbps') for s in servers
              if s.get('bandwidth_kbps')]
    if not values:
        return 5000
    return int(sum(values) / len(values))


def _detect_version() -> str:
    """Read VERSION_ID from /etc/os-release."""
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if line.startswith('VERSION_ID='):
                    return line.strip().split('=')[1].strip('"')
    except OSError:
        pass
    return ''


def dedup_mirror_urls(mirror_urls, suffix_pattern, existing_host_paths=None):
    """Parse and deduplicate mirror URLs by (host, base_path).

    Filters non-HTTP(S) URLs, strips *suffix_pattern* from each URL path
    to obtain the server ``base_path``, then deduplicates:

    * Same host + same base_path + different scheme → keep https.
    * Same host + different base_path → distinct servers (e.g. corporate
      mirror hosting both official and community repos).
    * Entries whose (host, base_path) is in *existing_host_paths* are
      skipped entirely.

    Args:
        mirror_urls: Iterable of raw mirror URL strings.
        suffix_pattern: Compiled regex stripped from each URL path to
            derive the server base_path.
        existing_host_paths: Optional set of ``(host, base_path)`` tuples
            to exclude (typically servers already in the database).

    Returns:
        List of candidate dicts, each with keys
        ``scheme``, ``host``, ``base_path``, ``full_url``.
    """
    if existing_host_paths is None:
        existing_host_paths = set()

    by_host_path = {}  # (host, base_path) → candidate dict
    for mirror_url in mirror_urls:
        mirror_url = mirror_url.strip()
        if not mirror_url:
            continue
        parsed = urlparse(mirror_url)
        if parsed.scheme not in ('http', 'https'):
            continue

        host = parsed.hostname
        base_path = suffix_pattern.sub('', parsed.path).rstrip('/')
        key = (host, base_path)

        if key in existing_host_paths:
            continue

        prev = by_host_path.get(key)
        if prev is None or parsed.scheme == 'https':
            by_host_path[key] = {
                'scheme': parsed.scheme,
                'host': host,
                'base_path': base_path,
                'full_url': mirror_url,
            }

    return list(by_host_path.values())


def _fetch_and_filter(version, arch, existing_host_paths):
    """Fetch mirrors from the Mageia API and return deduplicated candidates.

    Uses :func:`~urpm.core.mirrorlist.fetch_mirrors` (the API endpoint
    with geo metadata) so that ``[server]`` country/continent filters
    are applied automatically.

    Args:
        version: Mageia version string (e.g. "9", "10").
        arch: Architecture string (e.g. "x86_64").
        existing_host_paths: Set of ``(host, base_path)`` tuples already
            in the database.

    Returns:
        List of candidate dicts with keys ``scheme``, ``host``,
        ``base_path``, ``full_url``, ``country``.
    """
    from .mirrorlist import fetch_mirrors, dedup_mirrors

    try:
        mirrors = fetch_mirrors(version, arch, timeout=10)
    except Exception as e:
        logger.warning("Failed to fetch mirrorlist: %s", e)
        return []

    if not mirrors:
        return []

    # Dedup by (host, base_path), preferring HTTPS
    suffix = f"/{version}/{arch}"
    mirrors = dedup_mirrors(mirrors, strip_suffix=suffix)

    # Convert to candidate dicts (expected by _test_latency et al.)
    # Filter by host — same host with different paths still shares
    # the same bandwidth, so adding both wouldn't help parallelism.
    existing_hosts = {hp[0] for hp in existing_host_paths}
    candidates = []
    seen_hosts = set()
    for m in mirrors:
        if m.host in existing_hosts or m.host in seen_hosts:
            continue
        seen_hosts.add(m.host)

        base_path = m.base_path
        if base_path.endswith(suffix):
            base_path = base_path[: -len(suffix)]
        base_path = base_path.rstrip("/")

        candidates.append({
            "scheme": m.scheme,
            "host": m.host,
            "base_path": base_path,
            "full_url": m.url.rstrip("/") + "/media/core/release/",
            "country": m.country or None,
        })

    return candidates


def _test_latency(candidates, max_workers=10, timeout=5):
    """Test latency to candidates in parallel, return [(candidate, ms)]."""
    def _test(c):
        try:
            start = time.time()
            req = Request(c['full_url'], method='HEAD')
            with urlopen(req, timeout=timeout):
                return (c, (time.time() - start) * 1000)
        except Exception:
            return (c, None)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_test, c) for c in candidates]
        for future in as_completed(futures):
            candidate, latency = future.result()
            if latency is not None:
                results.append((candidate, latency))
    return results


def _unique_name(host, existing_names):
    """Generate a unique server name from hostname."""
    name = host
    original = name
    counter = 1
    while name in existing_names:
        name = f"{original}-{counter}"
        counter += 1
    return name


def _fetch_synthesis_md5(base_url: str, relative_path: str,
                         timeout: int = 5) -> Optional[str]:
    """Fetch MD5SUM file and extract the synthesis.hdlist.cz hash.

    Args:
        base_url: Server base URL (scheme + host + base_path).
        relative_path: Media relative path (e.g. ``10/x86_64/media/core/release``).
        timeout: HTTP timeout in seconds.

    Returns:
        MD5 hex digest of ``synthesis.hdlist.cz``, or None on failure.
    """
    from .sync import parse_md5sum_file
    url = f"{base_url}/{relative_path}/media_info/MD5SUM"
    try:
        req = Request(url)
        req.add_header('User-Agent', 'urpm/0.7')
        with urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode('utf-8', errors='replace')
        checksums = parse_md5sum_file(content)
        return checksums.get('synthesis.hdlist.cz')
    except Exception:
        return None


def verify_media_match(candidate_url: str, media: dict,
                       db: 'PackageDatabase') -> bool:
    """Check that a candidate server hosts the same media content.

    Compares the MD5 of ``synthesis.hdlist.cz`` between the candidate
    server and servers already linked to this media.  Tries up to 3
    reference servers; a single match is enough to confirm.

    Args:
        candidate_url: Base URL of the candidate server.
        media: Media dict (must have ``relative_path`` and ``id``).
        db: Database instance.

    Returns:
        True if the candidate's synthesis matches a reference server's,
        False if no match after 3 attempts or no reference available.
    """
    from .config import build_server_url

    rpath = media.get('relative_path', '')
    if not rpath:
        return False

    # Fetch candidate's synthesis MD5
    candidate_md5 = _fetch_synthesis_md5(candidate_url, rpath)
    if not candidate_md5:
        return False

    # Get servers already linked to this media
    linked_servers = db.get_servers_for_media(media['id'])
    if not linked_servers:
        # No reference — first server for this media, accept it
        return True

    max_attempts = 3
    attempts = 0
    for srv in linked_servers:
        if attempts >= max_attempts:
            break
        ref_url = build_server_url(srv)
        ref_md5 = _fetch_synthesis_md5(ref_url, rpath)
        if ref_md5 is None:
            # Reference unreachable, try next
            continue
        attempts += 1
        if ref_md5 == candidate_md5:
            return True

    # No match after trying references
    return False


def _link_servers_to_media(db, added_servers):
    """Link newly added servers to enabled media after MD5 verification.

    For each candidate server and each enabled media, verifies that the
    server hosts the same ``synthesis.hdlist.cz`` (by MD5 comparison with
    existing reference servers) before creating the link.
    """
    from .config import build_server_url

    all_media = db.list_media()
    media_to_scan = [m for m in all_media
                     if m.get('enabled', 1) and m.get('relative_path')]
    if not media_to_scan:
        return

    for server_id, server_name in added_servers:
        server = db.get_server(server_name)
        base_url = build_server_url(server)

        def check_media(media_entry):
            if verify_media_match(base_url, media_entry, db):
                return media_entry['id']
            return None

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_media, m): m['id']
                       for m in media_to_scan}
            for future in as_completed(futures):
                media_id = future.result()
                if media_id:
                    db.link_server_media(server_id, media_id)


def _probe_bandwidth(db, added_servers):
    """Measure real bandwidth of servers by downloading files.xml.lzma.

    Uses ``files.xml.lzma`` (~25 MB on core/release) rather than
    ``synthesis.hdlist.cz`` (a few KB) to get past TCP slow-start and
    measure real sustained throughput.  Falls back to synthesis.hdlist.cz
    if files.xml.lzma is not available.

    Picks one enabled media with a relative_path and downloads from each
    server in parallel.  The measured KB/s replaces any seeded average,
    giving the proportional planner accurate data.
    """
    import pycurl
    from .config import build_server_url

    # Find a media to probe against — prefer core/release (largest files)
    all_media = db.list_media()
    probe_media = None
    for m in all_media:
        if m.get('enabled', 1) and m.get('relative_path'):
            if probe_media is None:
                probe_media = m
            # Prefer core/release for bigger files
            rp = m.get('relative_path', '')
            if 'core' in rp and 'release' in rp and 'debug' not in rp:
                probe_media = m
                break
    if not probe_media:
        return

    rpath = probe_media['relative_path']
    # files.xml.lzma is ~25 MB, giving a real bandwidth measurement.
    # Fall back to synthesis.hdlist.cz if unavailable (HTTP 404).
    probe_files = [
        f"{rpath}/media_info/files.xml.lzma",
        f"{rpath}/media_info/synthesis.hdlist.cz",
    ]

    def _probe_one(server_id, server_name):
        """Download a probe file and return (server_id, kbps).

        Tries files.xml.lzma first (large, accurate), falls back to
        synthesis.hdlist.cz (small, rough estimate).
        """
        server = db.get_server(server_name)
        if not server:
            return server_id, None
        base_url = build_server_url(server)

        for probe_file in probe_files:
            url = f"{base_url}/{probe_file}"
            c = pycurl.Curl()
            try:
                c.setopt(pycurl.URL, url)
                c.setopt(pycurl.WRITEFUNCTION, lambda _data: None)
                c.setopt(pycurl.FOLLOWLOCATION, 1)
                c.setopt(pycurl.CONNECTTIMEOUT, 5)
                c.setopt(pycurl.LOW_SPEED_LIMIT, 512)
                c.setopt(pycurl.LOW_SPEED_TIME, 10)
                c.setopt(pycurl.USERAGENT, 'urpm/0.7')
                c.setopt(pycurl.NOSIGNAL, 1)
                c.setopt(pycurl.NOPROGRESS, 1)

                c.perform()

                http_code = c.getinfo(pycurl.HTTP_CODE)
                if http_code >= 400:
                    continue  # try next probe file

                dl_bytes = c.getinfo(pycurl.SIZE_DOWNLOAD)
                dl_time = c.getinfo(pycurl.TOTAL_TIME)
                if dl_time > 0 and dl_bytes > 1024:
                    kbps = int(dl_bytes / dl_time / 1024)
                    return server_id, kbps

            except pycurl.error:
                continue  # try next probe file
            finally:
                c.close()

        return server_id, None

    with ThreadPoolExecutor(max_workers=len(added_servers)) as executor:
        futures = [
            executor.submit(_probe_one, sid, name)
            for sid, name in added_servers
        ]
        for future in as_completed(futures):
            server_id, kbps = future.result()
            if kbps and kbps > 0:
                db.update_server_stats(server_id, bandwidth_kbps=kbps)
                logger.info("Probed server %d: %d KB/s", server_id, kbps)
