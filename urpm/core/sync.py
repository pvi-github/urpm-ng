"""
Media synchronization for urpm

Downloads synthesis/hdlist files from media sources and imports into cache.
"""

import hashlib
import shutil
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Callable, Tuple, List
from dataclasses import dataclass

from .compression import decompress, decompress_stream
from .synthesis import parse_synthesis
from .hdlist import parse_hdlist
from .database import PackageDatabase


# Default paths for media metadata
SYNTHESIS_PATH = "media_info/synthesis.hdlist.cz"
HDLIST_PATH = "media_info/hdlist.cz"
MD5SUM_PATH = "media_info/MD5SUM"


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
                  timeout: int = 30) -> DownloadResult:
    """Download a file from URL to destination.

    Args:
        url: URL to download
        dest: Destination path
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        timeout: Connection timeout in seconds

    Returns:
        DownloadResult with success status and metadata
    """
    try:
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
        # HTTP(S) download
        return download_file(url, dest, progress_callback, timeout)


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


def check_media_update_needed(db: PackageDatabase, media_id: int,
                              media_url: str,
                              server: dict = None,
                              media: dict = None) -> Tuple[bool, Optional[str]]:
    """Check if a media needs to be updated by comparing MD5.

    Args:
        db: Database instance
        media_id: Media ID
        media_url: Full URL to the media (for legacy compatibility)
        server: Optional server dict (v8 schema, used for file:// handling)
        media: Optional media dict (v8 schema)

    Returns:
        Tuple of (needs_update, new_md5)
    """
    try:
        # Build MD5SUM URL using new or legacy method
        if server and media:
            md5_url = build_md5sum_url_v8(server, media)
        else:
            md5_url = build_md5sum_url(media_url)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)

        # Download using appropriate method
        if server:
            result = download_from_server(md5_url, tmp_path, server)
        else:
            result = download_file(md5_url, tmp_path)

        if not result.success:
            tmp_path.unlink(missing_ok=True)
            return True, None  # Can't check, assume update needed

        content = tmp_path.read_text()
        tmp_path.unlink()

        md5sums = parse_md5sum_file(content)
        synthesis_md5 = md5sums.get('synthesis.hdlist.cz')

        if not synthesis_md5:
            return True, None

        # Check against stored MD5
        media_row = db.conn.execute(
            "SELECT synthesis_md5 FROM media WHERE id = ?", (media_id,)
        ).fetchone()

        if media_row and media_row['synthesis_md5'] == synthesis_md5:
            return False, synthesis_md5  # No update needed

        return True, synthesis_md5

    except Exception:
        return True, None


def sync_media(db: PackageDatabase, media_name: str,
               progress_callback: Callable[[str, int, int], None] = None,
               force: bool = False,
               download_hdlist: bool = False) -> SyncResult:
    """Synchronize a media source.

    Downloads synthesis (and optionally hdlist), parses and imports into DB.
    Supports both v8 schema (server/media) and legacy (media.url) models.

    Args:
        db: Database instance
        media_name: Name of media to sync
        progress_callback: Optional callback(stage, current, total)
        force: Force update even if MD5 matches
        download_hdlist: Also download hdlist for full metadata

    Returns:
        SyncResult with status
    """
    media = db.get_media(media_name)
    if not media:
        return SyncResult(success=False, error=f"Media '{media_name}' not found")

    if not media['enabled']:
        return SyncResult(success=False, error=f"Media '{media_name}' is disabled")

    media_id = media['id']

    # Get server and URL (v8 schema or legacy fallback)
    server, media_url = get_effective_media_url(db, media)

    if not media_url:
        return SyncResult(success=False, error=f"No server available for '{media_name}'")

    # Check if update needed
    if not force:
        needs_update, new_md5 = check_media_update_needed(
            db, media_id, media_url, server=server, media=media
        )
        if not needs_update:
            if progress_callback:
                progress_callback("up-to-date", 0, 0)
            return SyncResult(success=True, packages_count=0)

    # Create temp directory for downloads
    with tempfile.TemporaryDirectory(prefix='urpm_sync_') as tmpdir:
        tmpdir = Path(tmpdir)

        # Download synthesis
        if progress_callback:
            progress_callback("downloading synthesis", 0, 0)

        # Build synthesis URL (v8 or legacy)
        if server:
            synthesis_url = build_synthesis_url_v8(server, media)
        else:
            synthesis_url = build_synthesis_url(media_url)

        synthesis_path = tmpdir / "synthesis.hdlist.cz"

        def dl_progress(current, total):
            if progress_callback:
                progress_callback("downloading synthesis", current, total)

        # Download using appropriate method (http/https or file://)
        if server:
            result = download_from_server(synthesis_url, synthesis_path, server, dl_progress)
        else:
            result = download_file(synthesis_url, synthesis_path, dl_progress)

        if not result.success:
            return SyncResult(
                success=False,
                error=f"Failed to download synthesis: {result.error}"
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
            cache_media_dir = get_media_local_path(media)
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

        if md5_result.success:
            shutil.copy2(md5sum_path, cache_media_info / "MD5SUM")

        # Update media sync info
        import time
        db.conn.execute("""
            UPDATE media SET last_sync = ?, synthesis_md5 = ?
            WHERE id = ?
        """, (int(time.time()), result.md5, media_id))
        db.conn.commit()

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
                   force: bool = False) -> List[Tuple[str, SyncResult]]:
    """Synchronize all enabled media.

    Args:
        db: Database instance
        progress_callback: Optional callback(media_name, stage, current, total)
        force: Force update even if MD5 matches

    Returns:
        List of (media_name, SyncResult) tuples
    """
    results = []
    media_list = db.list_media()

    for media in media_list:
        if not media['enabled']:
            continue

        name = media['name']

        def media_progress(stage, current, total):
            if progress_callback:
                progress_callback(name, stage, current, total)

        result = sync_media(db, name, media_progress, force=force)
        results.append((name, result))

    return results
