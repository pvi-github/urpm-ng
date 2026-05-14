"""
Media synchronization for urpm

Downloads synthesis/hdlist files from media sources and imports into cache.
"""

import hashlib
import logging
import shutil
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Callable, Tuple, List, Dict
from dataclasses import dataclass

from .compression import decompress, decompress_stream
from .synthesis import parse_synthesis
from .hdlist import parse_hdlist
from .database import PackageDatabase


logger = logging.getLogger(__name__)

# Default paths for media metadata
SYNTHESIS_PATH = "media_info/synthesis.hdlist.cz"
HDLIST_PATH = "media_info/hdlist.cz"
MD5SUM_PATH = "media_info/MD5SUM"
FILES_XML_PATH = "media_info/files.xml.lzma"
APPSTREAM_PATH = "media_info/appstream.xml.lzma"


# Import from config
from .config import get_base_dir, get_media_local_path, build_server_url, build_media_url, is_local_server
# Deprecated imports (kept for migration)
from .config import get_hostname_from_url, get_media_dir


def get_media_cache_dir(media_name: str, media_url: str, base_dir: Path = None) -> Path:
    """DEPRECATED: Use get_media_local_path() instead.

    This function uses the old hostname-based structure.
    New code should use get_media_local_path(media_dict).
    """
    import warnings
    warnings.warn("get_media_cache_dir() is deprecated, use get_media_local_path()", DeprecationWarning)
    if base_dir is None:
        base_dir = get_base_dir()
    hostname = get_hostname_from_url(media_url)
    return get_media_dir(base_dir, hostname, media_name)


@dataclass
class DownloadResult:
    """Result of a download operation."""
    success: bool
    path: Optional[Path] = None
    size: int = 0
    md5: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SyncResult:
    """Result of a media sync operation."""
    success: bool
    packages_count: int = 0
    synthesis_downloaded: bool = False
    hdlist_downloaded: bool = False
    error: Optional[str] = None


def download_file(url: str, dest: Path,
                  progress_callback: Callable[[int, int], None] = None,
                  timeout: int = 30,
                  ip_mode: str = 'auto') -> DownloadResult:
    """Download a file from URL to destination.

    Args:
        url: URL to download
        dest: Destination path
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        timeout: Connection timeout in seconds
        ip_mode: IP version preference ('auto', 'ipv4', 'ipv6', 'dual')

    Returns:
        DownloadResult with success status and metadata
    """
    import socket
    from .config import get_socket_family_for_ip_mode

    # Force IPv4/IPv6 by patching getaddrinfo temporarily
    family = get_socket_family_for_ip_mode(ip_mode)
    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, fam=0, *args, **kwargs):
        # Force the socket family for hostname resolution
        return original_getaddrinfo(host, port, family, *args, **kwargs)

    try:
        socket.getaddrinfo = patched_getaddrinfo

        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'urpm/0.1')

        with urllib.request.urlopen(req, timeout=timeout) as response:
            total_size = int(response.headers.get('Content-Length', 0))
            downloaded = 0
            md5_hash = hashlib.md5()

            dest.parent.mkdir(parents=True, exist_ok=True)

            with open(dest, 'wb') as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    md5_hash.update(chunk)
                    downloaded += len(chunk)

                    if progress_callback:
                        progress_callback(downloaded, total_size)

            return DownloadResult(
                success=True,
                path=dest,
                size=downloaded,
                md5=md5_hash.hexdigest()
            )

    except urllib.error.HTTPError as e:
        return DownloadResult(success=False, error=f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return DownloadResult(success=False, error=f"URL error: {e.reason}")
    except Exception as e:
        return DownloadResult(success=False, error=str(e))
    finally:
        socket.getaddrinfo = original_getaddrinfo


def get_media_base_url(media_url: str) -> str:
    """Normalize media URL to base path.

    Handles URLs that may or may not end with /
    """
    url = media_url.rstrip('/')
    return url


def build_synthesis_url(media_url: str) -> str:
    """Build full URL for synthesis file."""
    base = get_media_base_url(media_url)
    return f"{base}/{SYNTHESIS_PATH}"


def build_hdlist_url(media_url: str) -> str:
    """Build full URL for hdlist file."""
    base = get_media_base_url(media_url)
    return f"{base}/{HDLIST_PATH}"


def build_md5sum_url(media_url: str) -> str:
    """Build full URL for MD5SUM file."""
    base = get_media_base_url(media_url)
    return f"{base}/{MD5SUM_PATH}"


# =============================================================================
# New v8 schema functions (server/media dicts)
# =============================================================================

def build_synthesis_url_v8(server: dict, media: dict) -> str:
    """Build full URL for synthesis file using server/media model."""
    base_url = build_media_url(server, media)
    return f"{base_url}/{SYNTHESIS_PATH}"


def build_hdlist_url_v8(server: dict, media: dict) -> str:
    """Build full URL for hdlist file using server/media model."""
    base_url = build_media_url(server, media)
    return f"{base_url}/{HDLIST_PATH}"


def build_md5sum_url_v8(server: dict, media: dict) -> str:
    """Build full URL for MD5SUM file using server/media model."""
    base_url = build_media_url(server, media)
    return f"{base_url}/{MD5SUM_PATH}"


def download_from_server(url: str, dest: Path, server: dict,
                         progress_callback: Callable[[int, int], None] = None,
                         timeout: int = 30) -> DownloadResult:
    """Download a file from a server (handles both http(s) and file://).

    Args:
        url: Full URL or local path
        dest: Destination path
        server: Server dict (used to check if local)
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        timeout: Connection timeout in seconds

    Returns:
        DownloadResult with success status and metadata
    """
    if is_local_server(server):
        # Local file copy
        try:
            # Extract path from file:// URL
            if url.startswith('file://'):
                source_path = Path(url[7:])  # Remove 'file://'
            else:
                source_path = Path(url)
            if not source_path.exists():
                return DownloadResult(success=False, error=f"File not found: {url}")

            dest.parent.mkdir(parents=True, exist_ok=True)

            # Calculate MD5 while copying
            md5_hash = hashlib.md5()
            size = 0

            with open(source_path, 'rb') as src, open(dest, 'wb') as dst:
                while True:
                    chunk = src.read(8192)
                    if not chunk:
                        break
                    dst.write(chunk)
                    md5_hash.update(chunk)
                    size += len(chunk)
                    if progress_callback:
                        progress_callback(size, source_path.stat().st_size)

            return DownloadResult(
                success=True,
                path=dest,
                size=size,
                md5=md5_hash.hexdigest()
            )

        except Exception as e:
            return DownloadResult(success=False, error=str(e))
    else:
        # HTTP(S) download - use server's IP mode preference
        ip_mode = server.get('ip_mode', 'auto') if server else 'auto'
        return download_file(url, dest, progress_callback, timeout, ip_mode=ip_mode)


def get_effective_media_url(db, media: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Get the best server and full media URL for a media.

    Tries servers in priority order. Falls back to legacy media['url'] if no
    servers are linked.

    Args:
        db: Database instance
        media: Media dict

    Returns:
        Tuple of (server_dict or None, full_media_url or None)
        If using legacy URL, server_dict is None
    """
    media_id = media['id']

    # Try to get the best linked server
    server = db.get_best_server_for_media(media_id)

    if server:
        # Use new model: server + media.relative_path
        url = build_media_url(server, media)
        return server, url

    # Fallback to legacy URL in media table
    if media.get('url'):
        return None, media['url']

    # No way to access this media
    return None, None


def parse_md5sum_file(content: str) -> dict:
    """Parse MD5SUM file content.

    Format: <md5>  <filename>

    Returns:
        Dict mapping filename to md5
    """
    result = {}
    for line in content.strip().split('\n'):
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            md5, filename = parts
            # Remove leading ./ if present
            filename = filename.lstrip('./')
            result[filename] = md5
    return result


def head_synthesis(synthesis_url: str, server: dict = None,
                   timeout: int = 10) -> Optional[str]:
    """HTTP HEAD on synthesis to retrieve Last-Modified header.

    This is the lightest possible freshness check — no body is downloaded.
    For local file:// servers, falls back to filesystem mtime.

    Args:
        synthesis_url: Full URL to synthesis.hdlist.cz.
        server: Optional server dict (to detect file:// servers).
        timeout: Connection timeout in seconds.

    Returns:
        Last-Modified header value as string, or None if HEAD fails
        or the server doesn't provide the header.
    """
    if server and is_local_server(server):
        # Local file — use mtime directly
        from email.utils import formatdate
        local_path = synthesis_url.replace('file://', '')
        try:
            mtime = Path(local_path).stat().st_mtime
            return formatdate(mtime, usegmt=True)
        except OSError:
            return None

    try:
        req = urllib.request.Request(synthesis_url, method='HEAD')
        req.add_header('User-Agent', 'urpm/0.1')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.headers.get('Last-Modified')
    except Exception:
        return None


def _fetch_files_xml_if_changed(db: PackageDatabase, media_id: int,
                                media: dict, server: Optional[dict],
                                media_url: str, cache_media_info: Path,
                                md5sums: dict) -> None:
    """Refresh ``files.xml.lzma`` in ``cache_media_info`` when it changed.

    Conditional download keyed on the MD5 reported in the just-parsed
    ``MD5SUM`` file (same source of truth as for ``synthesis.hdlist.cz``).
    If the remote MD5 matches what we stored last time, nothing is
    downloaded and we exit silently.  When MD5SUM doesn't list
    ``files.xml.lzma`` at all, we leave whatever is on disk in place.

    The freshly downloaded file is not parsed nor imported anywhere —
    ``urpm f`` streams it directly via
    :func:`urpm.core.files_xml.iter_file_matches`.

    Args:
        db: Database instance — used to read and update
            ``media.files_xml_md5``.
        media_id: Numeric media id.
        media: Media dict (v8 schema info).
        server: Server dict the synthesis was just fetched from, or
            ``None`` for the legacy single-URL flow.
        media_url: Effective media URL, used by the legacy URL
            builders when ``server`` is ``None``.
        cache_media_info: ``<media>/media_info`` destination directory.
        md5sums: Parsed contents of the ``MD5SUM`` file already
            downloaded for the synthesis check.
    """
    remote_md5 = md5sums.get('files.xml.lzma')
    if not remote_md5:
        # No entry in MD5SUM — either the medium does not publish a
        # files.xml.lzma (some mirrors / dev media) or MD5SUM was
        # unavailable.  Leave on-disk state untouched.
        return

    media_info = db.get_media_by_id(media_id)
    stored_md5 = (media_info or {}).get('files_xml_md5')
    dest = cache_media_info / FILES_XML_PATH.split('/')[-1]

    if stored_md5 == remote_md5 and dest.exists():
        return  # Up to date, nothing to fetch.

    files_xml_url = (
        f"{build_media_url(server, media)}/{FILES_XML_PATH}"
        if server
        else f"{media_url}/{FILES_XML_PATH}"
    )

    with tempfile.NamedTemporaryFile(
        delete=False, suffix='.files.xml.lzma',
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        if server:
            result = download_from_server(files_xml_url, tmp_path, server)
        else:
            result = download_file(files_xml_url, tmp_path)
        if not result.success:
            logger.debug(
                "files.xml.lzma download failed for media %d: %s",
                media_id, getattr(result, 'error', 'unknown'),
            )
            return
        shutil.move(str(tmp_path), str(dest))
        db.update_media_files_xml_md5(media_id, remote_md5)
    finally:
        tmp_path.unlink(missing_ok=True)


def check_media_update_needed(db: PackageDatabase, media_id: int,
                              media_url: str,
                              server: dict = None,
                              media: dict = None) -> Tuple[bool, Optional[str]]:
    """Check if a media needs to be updated.

    Two-tier freshness check, lightest first:

    1. **HTTP HEAD** on synthesis.hdlist.cz → compare ``Last-Modified``
       with stored value.  If unchanged, return immediately (no download).
    2. **MD5SUM fallback** — download the small MD5SUM file and compare
       the synthesis hash.  Used when HEAD fails (405, no header, etc.)
       or when Last-Modified changed (to confirm with content hash).

    Args:
        db: Database instance
        media_id: Media ID
        media_url: Full URL to the media (for legacy compatibility)
        server: Optional server dict (v8 schema, used for file:// handling)
        media: Optional media dict (v8 schema)

    Returns:
        Tuple of (needs_update, new_md5)
    """
    media_info = db.get_media_by_id(media_id)

    # --- Tier 1: HTTP HEAD (cheapest) ---
    if server and media:
        synthesis_url = build_synthesis_url_v8(server, media)
    else:
        synthesis_url = build_synthesis_url(media_url)

    remote_last_modified = head_synthesis(synthesis_url, server)

    if remote_last_modified and media_info:
        stored_last_modified = media_info.get('synthesis_last_modified')
        if stored_last_modified and stored_last_modified == remote_last_modified:
            logger.debug("Media %d: HEAD Last-Modified unchanged, skipping",
                         media_id)
            return False, media_info.get('synthesis_md5')

    # --- Tier 2: MD5SUM download (fallback / confirmation) ---
    try:
        if server and media:
            md5_url = build_md5sum_url_v8(server, media)
        else:
            md5_url = build_md5sum_url(media_url)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)

        if server:
            result = download_from_server(md5_url, tmp_path, server)
        else:
            result = download_file(md5_url, tmp_path)

        if not result.success:
            tmp_path.unlink(missing_ok=True)
            return True, None

        content = tmp_path.read_text()
        tmp_path.unlink()

        md5sums = parse_md5sum_file(content)
        synthesis_md5 = md5sums.get('synthesis.hdlist.cz')

        if not synthesis_md5:
            return True, None

        if media_info and media_info.get('synthesis_md5') == synthesis_md5:
            # MD5 identical — HEAD gave a false positive (or wasn't available).
            # Update stored Last-Modified so next check skips via HEAD.
            if remote_last_modified:
                db.update_media_sync_info(media_id, synthesis_md5,
                                          remote_last_modified)
            return False, synthesis_md5

        return True, synthesis_md5

    except Exception:
        return True, None


def sync_media(db: PackageDatabase, media_name: str,
               progress_callback: Callable[[str, int, int], None] = None,
               force: bool = False,
               download_hdlist: bool = False,
               urpm_root: str = None,
               skip_appstream: bool = False) -> SyncResult:
    """Synchronize a media source.

    Downloads synthesis (and optionally hdlist), parses and imports into DB.
    Supports both v8 schema (server/media) and legacy (media.url) models.

    Args:
        db: Database instance
        media_name: Name of media to sync
        progress_callback: Optional callback(stage, current, total)
        force: Force update even if MD5 matches
        download_hdlist: Also download hdlist for full metadata
        urpm_root: If set, store files in <urpm_root>/var/lib/urpm/
        skip_appstream: Skip AppStream sync (default: False)

    Returns:
        SyncResult with status
    """
    # Determine base directory (normal or urpm_root)
    base_dir = get_base_dir(urpm_root=urpm_root)
    media = db.get_media(media_name)
    if not media:
        return SyncResult(success=False, error=f"Media '{media_name}' not found")

    if not media['enabled']:
        return SyncResult(success=False, error=f"Media '{media_name}' is disabled")

    media_id = media['id']

    # Build an ordered list of (server_dict, media_url) candidates to try.
    # get_servers_for_media() already orders by priority DESC, bandwidth_kbps DESC
    # so we try the fastest/most reliable servers first.
    # Falls back to a single legacy URL entry if no servers are linked.
    linked_servers = db.get_servers_for_media(media_id, enabled_only=True)
    if linked_servers:
        server_candidates = [
            (s, build_media_url(s, media)) for s in linked_servers
        ]
    elif media.get('url'):
        server_candidates = [(None, media['url'])]
    else:
        return SyncResult(success=False, error=f"No server available for '{media_name}'")

    # Check if update needed using the best available server.
    # On failure check_media_update_needed already returns (True, None),
    # meaning we'll proceed with the download — acceptable behaviour.
    first_server, first_url = server_candidates[0]
    if not force:
        needs_update, new_md5 = check_media_update_needed(
            db, media_id, first_url, server=first_server, media=media
        )
        if not needs_update:
            if progress_callback:
                progress_callback("up-to-date", 0, 0)
            return SyncResult(success=True, packages_count=0)

    # Create temp directory for downloads
    with tempfile.TemporaryDirectory(prefix='urpm_sync_') as tmpdir:
        tmpdir = Path(tmpdir)

        if progress_callback:
            progress_callback("downloading synthesis", 0, 0)

        synthesis_path = tmpdir / "synthesis.hdlist.cz"

        def dl_progress(current, total):
            if progress_callback:
                progress_callback("downloading synthesis", current, total)

        # Try servers in order until one succeeds (failover).
        # We update server stats on each attempt so slow/broken servers
        # are gradually deprioritised for future syncs.
        server = None
        media_url = None
        result = None
        all_errors = []

        for candidate, candidate_url in server_candidates:
            synthesis_url = (
                build_synthesis_url_v8(candidate, media) if candidate
                else build_synthesis_url(candidate_url)
            )
            t_start = time.time()
            attempt = (
                download_from_server(synthesis_url, synthesis_path, candidate, dl_progress)
                if candidate
                else download_file(synthesis_url, synthesis_path, dl_progress)
            )
            elapsed = time.time() - t_start

            if candidate and candidate.get('id'):
                sid = candidate['id']
                if attempt.success and elapsed >= 0.5 and attempt.size:
                    kbps = int(attempt.size / elapsed / 1024)
                    db.update_server_stats(sid, success=True, bandwidth_kbps=kbps)
                elif attempt.success:
                    db.update_server_stats(sid, success=True)
                else:
                    db.update_server_stats(sid, success=False)

            if attempt.success:
                server = candidate
                media_url = candidate_url
                result = attempt
                break

            name = candidate['name'] if candidate else candidate_url
            all_errors.append(f"{name}: {attempt.error}")
            # logger.warning(f"Sync failed on {name}: {attempt.error}")
            print((f"Sync failed on {name}: {attempt.error}"))

        if result is None or not result.success:
            return SyncResult(
                success=False,
                error=f"Failed to download synthesis (tried {len(server_candidates)} server(s)): "
                      + "; ".join(all_errors[-3:])
            )

        synthesis_downloaded = True
        hdlist_downloaded = False

        # Optionally download hdlist
        if download_hdlist:
            if progress_callback:
                progress_callback("downloading hdlist", 0, 0)

            if server:
                hdlist_url = build_hdlist_url_v8(server, media)
            else:
                hdlist_url = build_hdlist_url(media_url)

            hdlist_path = tmpdir / "hdlist.cz"

            def hdl_progress(current, total):
                if progress_callback:
                    progress_callback("downloading hdlist", current, total)

            if server:
                hdl_result = download_from_server(hdlist_url, hdlist_path, server, hdl_progress)
            else:
                hdl_result = download_file(hdlist_url, hdlist_path, hdl_progress)

            hdlist_downloaded = hdl_result.success

        # Parse and import synthesis (UPSERT handles obsolete packages)
        if progress_callback:
            progress_callback("parsing synthesis", 0, 0)

        try:
            packages = parse_synthesis(synthesis_path)

            def import_progress(count, name):
                if progress_callback:
                    progress_callback(f"importing: {name}", count, 0)

            count = db.import_packages(
                packages,
                media_id=media_id,
                source='synthesis',
                progress_callback=import_progress
            )

        except Exception as e:
            return SyncResult(
                success=False,
                error=f"Failed to parse synthesis: {e}"
            )

        # Copy files to permanent cache
        # v8 schema: official/<relative_path>/ or custom/<short_name>/
        # Legacy: <hostname>/<media_name>/
        if media.get('relative_path'):
            # v8 schema - use new path structure
            cache_media_dir = get_media_local_path(media, base_dir)
        else:
            # Legacy - use old hostname-based structure
            cache_media_dir = get_media_cache_dir(media_name, media_url)

        cache_media_info = cache_media_dir / "media_info"
        cache_media_info.mkdir(parents=True, exist_ok=True)

        # Copy synthesis
        cache_synthesis = cache_media_info / "synthesis.hdlist.cz"
        shutil.copy2(synthesis_path, cache_synthesis)

        # Copy hdlist if downloaded
        if hdlist_downloaded:
            cache_hdlist = cache_media_info / "hdlist.cz"
            shutil.copy2(tmpdir / "hdlist.cz", cache_hdlist)

        # Download and copy MD5SUM
        if server:
            md5sum_url = build_md5sum_url_v8(server, media)
        else:
            md5sum_url = build_md5sum_url(media_url)

        md5sum_path = tmpdir / "MD5SUM"

        if server:
            md5_result = download_from_server(md5sum_url, md5sum_path, server)
        else:
            md5_result = download_file(md5sum_url, md5sum_path)

        # Parse MD5SUM once — both synthesis state-update (above) and
        # the files.xml.lzma conditional fetch (below) rely on it.
        md5sums = {}
        if md5_result.success:
            shutil.copy2(md5sum_path, cache_media_info / "MD5SUM")
            try:
                md5sums = parse_md5sum_file(md5sum_path.read_text())
            except OSError:
                pass

        # Retrieve Last-Modified from the server we just synced from
        sync_synthesis_url = (
            build_synthesis_url_v8(server, media) if server
            else build_synthesis_url(media_url)
        )
        last_modified = head_synthesis(sync_synthesis_url, server)

        # Update media sync info (thread-safe)
        db.update_media_sync_info(media_id, result.md5, last_modified)

        # Conditionally fetch files.xml.lzma alongside the synthesis.
        # The file is consumed on demand by ``urpm f`` (no DB import);
        # we only refresh it when MD5SUM says it changed.  Errors are
        # logged but never fail the whole sync — having a slightly
        # stale (or missing) files.xml.lzma is much better than
        # blocking a synthesis update on a flaky files.xml mirror.
        try:
            _fetch_files_xml_if_changed(
                db, media_id, media, server, media_url,
                cache_media_info, md5sums,
            )
        except Exception as exc:
            logger.warning(
                "files.xml.lzma refresh failed for %s: %s",
                media_name, exc,
            )

        # Feed adaptive scheduling model with this content change
        try:
            from ..daemon.adaptive import record_content_change
            record_content_change(db, media_id)
        except Exception:
            pass  # Adaptive scheduling is optional

        # Sync AppStream metadata
        appstream_synced = False
        if not skip_appstream:
            if progress_callback:
                progress_callback("syncing appstream", 0, 0)
            try:
                from .appstream import AppStreamManager
                appstream_mgr = AppStreamManager(db, base_dir)
                appstream_result = appstream_mgr.sync_media_appstream(
                    media_id=media_id,
                    media_name=media_name,
                    media_url=media_url,
                    progress_callback=lambda msg: progress_callback("appstream", 0, 0) if progress_callback else None
                )
                appstream_synced = appstream_result.success
            except Exception as e:
                # AppStream sync failure is not fatal
                import logging
                logging.getLogger(__name__).warning(f"AppStream sync failed for {media_name}: {e}")

        if progress_callback:
            progress_callback("done", count, count)

        return SyncResult(
            success=True,
            packages_count=count,
            synthesis_downloaded=synthesis_downloaded,
            hdlist_downloaded=hdlist_downloaded
        )


def sync_all_media(db: PackageDatabase,
                   progress_callback: Callable[[str, str, int, int], None] = None,
                   force: bool = False,
                   max_workers: int = 4,
                   urpm_root: str = None,
                   skip_appstream: bool = False) -> List[Tuple[str, SyncResult]]:
    """Synchronize all enabled media in parallel.

    Args:
        db: Database instance
        progress_callback: Optional callback(media_name, stage, current, total)
        force: Force update even if MD5 matches
        max_workers: Maximum parallel downloads (default: 4)
        urpm_root: If set, store files in <urpm_root>/var/lib/urpm/
        skip_appstream: Skip AppStream sync and merge (default: False)

    Returns:
        List of (media_name, SyncResult) tuples
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    media_list = db.list_media()
    enabled_media = [m for m in media_list if m['enabled']]

    if not enabled_media:
        return []

    # Thread-safe progress callback wrapper
    progress_lock = threading.Lock()

    def thread_safe_progress(name, stage, current, total):
        if progress_callback:
            with progress_lock:
                progress_callback(name, stage, current, total)

    def sync_one(media_name: str) -> Tuple[str, SyncResult]:
        """Sync a single media (runs in thread)."""
        def media_progress(stage, current, total):
            thread_safe_progress(media_name, stage, current, total)

        result = sync_media(db, media_name, media_progress, force=force,
                           urpm_root=urpm_root, skip_appstream=skip_appstream)
        return (media_name, result)

    # Use parallel execution
    results = []
    num_workers = min(max_workers, len(enabled_media))

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(sync_one, m['name']): m['name']
            for m in enabled_media
        }

        for future in as_completed(futures):
            media_name = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append((media_name, SyncResult(success=False, error=str(e))))

    # Sort by original order
    name_order = {m['name']: i for i, m in enumerate(enabled_media)}
    results.sort(key=lambda x: name_order.get(x[0], 999))

    # Merge AppStream catalogs and refresh system cache
    if not skip_appstream:
        try:
            from .appstream import AppStreamManager
            base_dir = get_base_dir(urpm_root=urpm_root)
            appstream_mgr = AppStreamManager(db, base_dir)

            if progress_callback:
                progress_callback("__appstream__", "merging catalogs", 0, 0)

            total_components, media_count = appstream_mgr.merge_all_catalogs()

            if total_components > 0:
                if progress_callback:
                    progress_callback("__appstream__", "refreshing cache", 0, 0)
                appstream_mgr.refresh_system_cache()

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"AppStream merge failed: {e}")

    return results




def get_mageia_version_arch() -> Tuple[Optional[str], Optional[str]]:
    """Detect current Mageia version and architecture from /etc/mageia-release.

    Returns:
        Tuple of (version, arch) or (None, None) if detection fails
        version is like "9", "10", etc.
        arch is like "x86_64", "i586", etc.
    """
    import platform
    import re

    # Get architecture from platform
    arch = platform.machine()

    # Get version from /etc/mageia-release
    try:
        release_path = Path("/etc/mageia-release")
        if release_path.exists():
            content = release_path.read_text().strip()
            # Format: "Mageia release 10 (Cauldron) for x86_64"
            match = re.search(r'Mageia release (\d+)', content)
            if match:
                version = match.group(1)
                return version, arch
    except Exception:
        pass

    return None, arch


