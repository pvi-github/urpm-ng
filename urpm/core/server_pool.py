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
from typing import List, TYPE_CHECKING
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
    existing = db.list_servers(enabled_only=True)

    if len(existing) >= min_needed:
        return PoolCheckResult(sufficient=True, had=len(existing),
                               needed=min_needed)

    to_add = min_needed - len(existing)
    logger.info("Server pool too small (%d/%d) for %d parallel downloads, "
                "adding %d mirrors", len(existing), min_needed, parallel, to_add)

    # Detect version and arch
    version = _detect_version()
    if not version:
        logger.warning("Cannot detect Mageia version, skipping server auto-add")
        return PoolCheckResult(sufficient=False, had=len(existing),
                               needed=min_needed)
    arch = platform.machine()

    # Build duplicate sets
    all_servers = db.list_servers()
    existing_urls = set()
    existing_names = set()
    for s in all_servers:
        url = f"{s['protocol']}://{s['host']}{s.get('base_path', '')}".rstrip('/')
        existing_urls.add(url)
        existing_names.add(s['name'])

    # Fetch mirrorlist
    candidates = _fetch_and_filter(version, arch, existing_urls)
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
                candidate['base_path'])
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
        for name, _ in added:
            s = db.get_server(name)
            if s:
                server_ids.append((s['id'], name))
        _link_servers_to_media(db, server_ids)

        # Probe real bandwidth by downloading a synthesis.hdlist.cz from
        # one of the linked media.  Runs in parallel across all new
        # servers so user wait is bounded by the slowest, not the sum.
        _probe_bandwidth(db, server_ids)

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


def _fetch_and_filter(version, arch, existing_urls):
    """Fetch mirrorlist, parse URLs, filter duplicates and non-HTTP."""
    url = _MIRRORLIST_URL.format(version=version, arch=arch)
    try:
        with urlopen(url, timeout=10) as resp:
            content = resp.read().decode('utf-8').strip()
    except (URLError, HTTPError) as e:
        logger.warning("Failed to fetch mirrorlist: %s", e)
        return []

    if not content:
        return []

    suffix_re = re.compile(
        rf'{re.escape(version)}/{re.escape(arch)}/media/core/release/?$')

    candidates = []
    for mirror_url in content.split('\n'):
        mirror_url = mirror_url.strip()
        if not mirror_url:
            continue
        parsed = urlparse(mirror_url)
        if parsed.scheme not in ('http', 'https'):
            continue
        base_path = suffix_re.sub('', parsed.path).rstrip('/')
        full_base = f"{parsed.scheme}://{parsed.hostname}{base_path}"
        if full_base in existing_urls:
            continue
        candidates.append({
            'scheme': parsed.scheme,
            'host': parsed.hostname,
            'base_path': base_path,
            'full_url': mirror_url,
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


def _link_servers_to_media(db, added_servers):
    """Link newly added servers to all enabled media."""
    from .config import build_server_url

    all_media = db.list_media()
    media_to_scan = [(m['id'], m.get('relative_path', ''))
                     for m in all_media
                     if m.get('enabled', 1) and m.get('relative_path')]
    if not media_to_scan:
        return

    for server_id, server_name in added_servers:
        server = db.get_server(server_name)
        base_url = build_server_url(server)

        def check_media(mid, rpath):
            try:
                req = Request(f"{base_url}/{rpath}/media_info/MD5SUM",
                              method='HEAD')
                urlopen(req, timeout=3)
                return mid
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_media, mid, rp): mid
                       for mid, rp in media_to_scan}
            for future in as_completed(futures):
                media_id = future.result()
                if media_id:
                    db.link_server_media(server_id, media_id)


def _probe_bandwidth(db, added_servers):
    """Measure real bandwidth of new servers by downloading synthesis.hdlist.cz.

    Picks one enabled media with a relative_path and downloads its
    ``synthesis.hdlist.cz`` from each new server in parallel.  The
    measured KB/s replaces the seeded average, giving the proportional
    planner accurate data from the start.
    """
    import pycurl
    from .config import build_server_url

    # Find a media to probe against
    all_media = db.list_media()
    probe_media = None
    for m in all_media:
        if m.get('enabled', 1) and m.get('relative_path'):
            probe_media = m
            break
    if not probe_media:
        return

    rpath = probe_media['relative_path']
    probe_file = f"{rpath}/media_info/synthesis.hdlist.cz"

    def _probe_one(server_id, server_name):
        """Download synthesis.hdlist.cz and return (server_id, kbps)."""
        server = db.get_server(server_name)
        if not server:
            return server_id, None
        base_url = build_server_url(server)
        url = f"{base_url}/{probe_file}"

        c = pycurl.Curl()
        try:
            # Discard data — we only care about speed
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
                return server_id, None

            dl_bytes = c.getinfo(pycurl.SIZE_DOWNLOAD)
            dl_time = c.getinfo(pycurl.TOTAL_TIME)
            if dl_time > 0 and dl_bytes > 1024:
                kbps = int(dl_bytes / dl_time / 1024)
                return server_id, kbps
            return server_id, None

        except pycurl.error:
            return server_id, None
        finally:
            c.close()

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
