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

# Local cache directory (mirrors structure)
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "urpm"


def get_hostname_from_url(url: str) -> str:
    """Extract hostname from a URL for cache organization."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or "local"


def get_media_cache_dir(media_name: str, media_url: str) -> Path:
    """Get cache directory for a media.

    Structure: ~/.cache/urpm/medias/<hostname>/<media_name>/
    """
    hostname = get_hostname_from_url(media_url)
    return DEFAULT_CACHE_DIR / "medias" / hostname / media_name


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
                              media_url: str) -> Tuple[bool, Optional[str]]:
    """Check if a media needs to be updated by comparing MD5.

    Returns:
        Tuple of (needs_update, new_md5)
    """
    try:
        md5_url = build_md5sum_url(media_url)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)

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
        media = db.conn.execute(
            "SELECT synthesis_md5 FROM media WHERE id = ?", (media_id,)
        ).fetchone()

        if media and media['synthesis_md5'] == synthesis_md5:
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

    media_url = media['url']
    if not media_url:
        # TODO: Handle mirrorlist
        return SyncResult(success=False, error="Mirrorlist not yet supported")

    media_id = media['id']

    # Check if update needed
    if not force:
        needs_update, new_md5 = check_media_update_needed(db, media_id, media_url)
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

        synthesis_url = build_synthesis_url(media_url)
        synthesis_path = tmpdir / "synthesis.hdlist.cz"

        def dl_progress(current, total):
            if progress_callback:
                progress_callback("downloading synthesis", current, total)

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

            hdlist_url = build_hdlist_url(media_url)
            hdlist_path = tmpdir / "hdlist.cz"

            def hdl_progress(current, total):
                if progress_callback:
                    progress_callback("downloading hdlist", current, total)

            hdl_result = download_file(hdlist_url, hdlist_path, hdl_progress)
            hdlist_downloaded = hdl_result.success

        # Clear old packages from this media
        if progress_callback:
            progress_callback("clearing old data", 0, 0)

        db.clear_media_packages(media_id)

        # Parse and import synthesis
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

        # Copy files to permanent cache (mirrors structure)
        # Structure: ~/.cache/urpm/medias/<hostname>/<media_name>/media_info/
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
        md5sum_url = build_md5sum_url(media_url)
        md5sum_path = tmpdir / "MD5SUM"
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
