"""
Media synchronization for urpm

Downloads synthesis/hdlist files from media sources and imports into cache.
"""

import hashlib
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


# Default paths for media metadata
SYNTHESIS_PATH = "media_info/synthesis.hdlist.cz"
HDLIST_PATH = "media_info/hdlist.cz"
MD5SUM_PATH = "media_info/MD5SUM"
FILES_XML_PATH = "media_info/files.xml.lzma"


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
        media_info = db.get_media_by_id(media_id)
        if media_info and media_info.get('synthesis_md5') == synthesis_md5:
            return False, synthesis_md5  # No update needed

        return True, synthesis_md5

    except Exception:
        return True, None


def sync_media(db: PackageDatabase, media_name: str,
               progress_callback: Callable[[str, int, int], None] = None,
               force: bool = False,
               download_hdlist: bool = False,
               urpm_root: str = None) -> SyncResult:
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

        if md5_result.success:
            shutil.copy2(md5sum_path, cache_media_info / "MD5SUM")

        # Update media sync info (thread-safe)
        db.update_media_sync_info(media_id, result.md5)

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
                   urpm_root: str = None) -> List[Tuple[str, SyncResult]]:
    """Synchronize all enabled media in parallel.

    Args:
        db: Database instance
        progress_callback: Optional callback(media_name, stage, current, total)
        force: Force update even if MD5 matches
        max_workers: Maximum parallel downloads (default: 4)
        urpm_root: If set, store files in <urpm_root>/var/lib/urpm/

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

        result = sync_media(db, media_name, media_progress, force=force, urpm_root=urpm_root)
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

    return results


@dataclass
class FilesXmlResult:
    """Result of a files.xml sync operation."""
    success: bool
    file_count: int = 0
    pkg_count: int = 0
    error: Optional[str] = None
    skipped: bool = False  # True if already up-to-date


def sync_files_xml(
    db: PackageDatabase,
    media_name: str,
    progress_callback: Callable[[str, int, int], None] = None,
    force: bool = False
) -> FilesXmlResult:
    """Download and import files.xml.lzma for a media.

    Args:
        db: Package database
        media_name: Name of the media to sync files for
        progress_callback: Called with (stage, current, total)
        force: If True, re-download even if files.xml hasn't changed

    Returns:
        FilesXmlResult with success status and counts
    """
    from .files_xml import parse_files_xml

    media = db.get_media(media_name)
    if not media:
        return FilesXmlResult(success=False, error=f"Media '{media_name}' not found")

    media_id = media['id']

    # Get cache directory for this media
    cache_dir = get_media_local_path(media)
    cache_media_info = cache_dir / "media_info"
    cache_media_info.mkdir(parents=True, exist_ok=True)

    # Get server URLs for this media
    servers = db.get_servers_for_media(media_id, enabled_only=True)
    if not servers:
        return FilesXmlResult(success=False, error="No servers configured for this media")

    # Sort by priority
    servers.sort(key=lambda s: s.get('priority', 50))

    # Try to download from servers
    files_xml_downloaded = False
    files_xml_path = None
    files_xml_md5 = None

    for server in servers:
        if progress_callback:
            progress_callback("checking server", 0, 0)

        base_url = build_media_url(server, media)
        files_xml_url = f"{base_url}/{FILES_XML_PATH}"

        # Check MD5SUM first if available (to skip unchanged files)
        md5sum_url = f"{base_url}/{MD5SUM_PATH}"
        current_md5 = None

        try:
            req = urllib.request.Request(md5sum_url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                md5_content = resp.read().decode('utf-8', errors='ignore')
                # Parse MD5SUM to find files.xml.lzma entry
                for line in md5_content.strip().split('\n'):
                    parts = line.split()
                    if len(parts) >= 2 and 'files.xml.lzma' in parts[1]:
                        current_md5 = parts[0]
                        break
        except:
            pass  # MD5SUM check is optional

        # Check if we already have this version
        if current_md5 and not force:
            state = db.get_files_xml_state(media_id)
            if state and state.get('files_md5') == current_md5:
                return FilesXmlResult(
                    success=True,
                    file_count=state.get('file_count', 0),
                    pkg_count=state.get('pkg_count', 0),
                    skipped=True
                )

        # Download files.xml.lzma
        if progress_callback:
            progress_callback("downloading files.xml", 0, 0)

        try:
            req = urllib.request.Request(files_xml_url)
            with urllib.request.urlopen(req, timeout=120) as resp:
                # Save to cache
                files_xml_path = cache_media_info / "files.xml.lzma"
                with open(files_xml_path, 'wb') as f:
                    f.write(resp.read())

                # Calculate MD5 if not known
                if not current_md5:
                    with open(files_xml_path, 'rb') as f:
                        current_md5 = hashlib.md5(f.read()).hexdigest()

                files_xml_md5 = current_md5
                files_xml_downloaded = True
                break

        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # Try next server
            return FilesXmlResult(success=False, error=f"HTTP error: {e.code}")
        except Exception as e:
            continue  # Try next server

    if not files_xml_downloaded:
        return FilesXmlResult(success=False, error="files.xml.lzma not found on any server")

    # Get file size for progress estimation
    compressed_size = files_xml_path.stat().st_size

    # Estimate total files based on ratio from previous imports
    estimated_total = None
    ratio = db.get_files_xml_ratio()
    if ratio:
        estimated_total = int(compressed_size * ratio)

    # Import into database - format estimated total as human-readable approximation
    def format_estimate(n):
        """Format number as rounded approximation: ~7M, ~250K, etc."""
        if n >= 1_000_000:
            return f"~{round(n / 1_000_000)}M"
        elif n >= 1_000:
            return f"~{round(n / 1_000)}K"
        else:
            return f"~{n}"

    estimate_str = format_estimate(estimated_total) if estimated_total else None

    if progress_callback:
        if estimate_str:
            progress_callback(f"importing: 0/{estimate_str}", 0, 0)
        else:
            progress_callback("importing...", 0, 0)

    def import_progress(files_imported, pkgs_imported):
        if progress_callback:
            if estimated_total:
                pct = min(99, int(100 * files_imported / estimated_total))
                progress_callback(f"importing: {pct}% ({files_imported:,}/{estimate_str})", 0, 0)
            else:
                progress_callback(f"importing: {files_imported:,} files", 0, 0)

    try:
        file_count, pkg_count = db.import_files_xml(
            media_id,
            parse_files_xml(files_xml_path),
            files_md5=files_xml_md5,
            compressed_size=compressed_size,
            progress_callback=import_progress
        )

        return FilesXmlResult(
            success=True,
            file_count=file_count,
            pkg_count=pkg_count
        )

    except Exception as e:
        return FilesXmlResult(success=False, error=f"Import error: {e}")


def sync_files_xml_incremental(
    db: PackageDatabase,
    media_id: int,
    files_xml_path: Path,
    progress_callback: Callable[[str, int, int], None] = None
) -> Tuple[int, int, int]:
    """Perform incremental sync of files.xml - only add/remove changed packages.

    Much faster than full import since indexes are not recreated.

    Args:
        db: Package database
        media_id: Media ID to sync
        files_xml_path: Path to downloaded files.xml.lzma
        progress_callback: Called with (stage, current, total)

    Returns:
        Tuple of (added_count, removed_count, total_files)
    """
    from .files_xml import parse_files_xml, extract_nevras_from_files_xml

    if progress_callback:
        progress_callback("analyzing", 0, 0)

    # 1. Get existing NEVRAs from DB
    existing_nevras = db.get_package_nevras_for_media(media_id)

    # 2. Extract NEVRAs from new files.xml (fast regex scan)
    new_nevras = extract_nevras_from_files_xml(files_xml_path)

    # 3. Compute diff
    to_remove = existing_nevras - new_nevras
    to_add = new_nevras - existing_nevras

    if progress_callback:
        progress_callback("diff", len(to_remove), len(to_add))

    # 4. Delete removed packages
    if to_remove:
        if progress_callback:
            progress_callback("removing", 0, len(to_remove))
        db.delete_package_files_by_nevra(media_id, to_remove)

    # 5. Add new packages (parse XML but only insert for new ones)
    added_files = 0
    if to_add:
        add_count = 0
        for nevra, files in parse_files_xml(files_xml_path):
            if nevra in to_add:
                db.insert_package_files_batch(media_id, nevra, files)
                added_files += len(files)
                add_count += 1
                if progress_callback:
                    progress_callback("adding", add_count, len(to_add))

    # Get final count
    state = db.get_files_xml_state(media_id)
    total_files = state.get('file_count', 0) if state else added_files

    return len(to_add), len(to_remove), total_files


@dataclass
class FilesXmlMediaState:
    """State of files.xml download/import for a single media."""
    media_id: int
    media_name: str
    media_version: str
    # Pre-fetched data (avoid DB access in threads)
    media_dict: Optional[Dict] = None
    servers: Optional[List[Dict]] = None
    cache_dir: Optional[Path] = None
    # Runtime state
    server_url: Optional[str] = None
    compressed_size: int = 0
    current_md5: Optional[str] = None
    stored_md5: Optional[str] = None
    download_path: Optional[Path] = None
    download_complete: bool = False
    import_complete: bool = False
    file_count: int = 0
    pkg_count: int = 0
    error: Optional[str] = None
    skipped: bool = False  # True if MD5 unchanged


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


def sync_all_files_xml(
    db: PackageDatabase,
    progress_callback: Callable[[str, str, int, int, int, int], None] = None,
    force: bool = False,
    max_workers: int = 4,
    filter_version: bool = True,
    sync_files_only: bool = True
) -> List[Tuple[str, FilesXmlResult]]:
    """Download and import files.xml.lzma for all media in parallel.

    Uses optimized import strategy:
    1. Check MD5 for all media first (parallel)
    2. Skip unchanged media unless force=True
    3. Download changed files in parallel
    4. Import into staging table (no indexes)
    5. Atomic swap at the end

    Args:
        db: Package database
        progress_callback: Called with (media_name, stage, dl_current, dl_total, import_current, import_total)
                          stage is: 'checking'|'downloading'|'downloaded'|'importing'|'done'|'skipped'|'error'
        force: If True, re-download even if files.xml hasn't changed
        max_workers: Number of parallel download threads
        filter_version: If True, only sync media matching current Mageia version
        sync_files_only: If True, only sync media with sync_files=1 (default: True)

    Returns:
        List of (media_name, FilesXmlResult) tuples
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from queue import PriorityQueue
    from .files_xml import parse_files_xml
    import threading

    # Get all enabled media
    all_media = [m for m in db.list_media() if m.get('enabled', True)]
    if not all_media:
        return []

    # Filter by sync_files flag if requested
    if sync_files_only:
        all_media = [m for m in all_media if m.get('sync_files')]
        if not all_media:
            return []

    # Filter by version and architecture if requested
    if filter_version:
        from .config import get_accepted_versions
        import platform

        # Get accepted versions (respects version-mode config)
        accepted_versions, _, _ = get_accepted_versions(db)
        arch = platform.machine()

        filtered_media = []
        for m in all_media:
            media_version = m.get('mageia_version', '')
            media_arch = m.get('architecture', '')
            # Check version match using accepted_versions set
            if accepted_versions:
                version_ok = not media_version or media_version in accepted_versions
            else:
                version_ok = True  # No filtering if accepted_versions is None
            # Check arch match (accept if media arch is empty or matches)
            arch_ok = not media_arch or not arch or media_arch == arch
            if version_ok and arch_ok:
                filtered_media.append(m)
        all_media = filtered_media

    if not all_media:
        return []

    # Initialize state for each media - PRE-FETCH all DB data to avoid locks in threads
    media_states: List[FilesXmlMediaState] = []
    for m in all_media:
        media_id = m['id']
        servers = db.get_servers_for_media(media_id, enabled_only=True)
        servers.sort(key=lambda s: s.get('priority', 50))

        state = FilesXmlMediaState(
            media_id=media_id,
            media_name=m['name'],
            media_version=m.get('version', ''),
            media_dict=m,
            servers=servers,
            cache_dir=get_media_local_path(m)
        )
        # Get stored MD5 for comparison
        stored_state = db.get_files_xml_state(media_id)
        if stored_state:
            state.stored_md5 = stored_state.get('files_md5')
            state.file_count = stored_state.get('file_count', 0)
            state.pkg_count = stored_state.get('pkg_count', 0)
        media_states.append(state)

    # =========================================================================
    # Phase 1: Check MD5 for all media (parallel)
    # =========================================================================
    def check_md5(state: FilesXmlMediaState) -> FilesXmlMediaState:
        """Fetch MD5SUM and check if files.xml has changed. NO DB ACCESS - uses pre-fetched data."""
        if progress_callback:
            progress_callback(state.media_name, 'checking', 0, 0, 0, 0)

        if not state.servers:
            state.error = "No servers"
            return state

        for server in state.servers:
            base_url = build_media_url(server, state.media_dict)
            md5sum_url = f"{base_url}/{MD5SUM_PATH}"

            try:
                req = urllib.request.Request(md5sum_url)
                req.add_header('User-Agent', 'urpm/0.1')
                with urllib.request.urlopen(req, timeout=30) as resp:
                    md5_content = resp.read().decode('utf-8', errors='ignore')
                    for line in md5_content.strip().split('\n'):
                        parts = line.split()
                        if len(parts) >= 2 and 'files.xml.lzma' in parts[1]:
                            state.current_md5 = parts[0]
                            state.server_url = base_url
                            break

                if state.current_md5:
                    break
            except Exception:
                continue

        # Check if unchanged (file_count/pkg_count already pre-fetched)
        if state.current_md5 and state.stored_md5 == state.current_md5 and not force:
            state.skipped = True
            if progress_callback:
                progress_callback(state.media_name, 'skipped', 0, 0, state.file_count, state.file_count)

        return state

    # Check MD5 in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_md5, state): state for state in media_states}
        for future in as_completed(futures):
            state = future.result()
            # State is updated in place

    # Early exit if all unchanged
    changed_states = [s for s in media_states if not s.skipped and not s.error]
    if not changed_states:
        results = []
        for state in media_states:
            if state.skipped:
                results.append((state.media_name, FilesXmlResult(
                    success=True, file_count=state.file_count, pkg_count=state.pkg_count, skipped=True)))
            else:
                results.append((state.media_name, FilesXmlResult(
                    success=False, error=state.error or "No files.xml found")))
        return results

    # =========================================================================
    # Phase 2: Download files.xml in parallel
    # =========================================================================
    def download_files_xml(state: FilesXmlMediaState) -> FilesXmlMediaState:
        """Download files.xml.lzma for a media. NO DB ACCESS - uses pre-fetched data."""
        if state.skipped or state.error:
            return state

        if progress_callback:
            progress_callback(state.media_name, 'downloading', 0, 0, 0, 0)

        # Use pre-fetched cache directory
        cache_media_info = state.cache_dir / "media_info"
        cache_media_info.mkdir(parents=True, exist_ok=True)

        # Use server URL from MD5 check, or find one from pre-fetched servers
        if not state.server_url and state.servers:
            state.server_url = build_media_url(state.servers[0], state.media_dict)

        if not state.server_url:
            state.error = "No server available"
            return state

        files_xml_url = f"{state.server_url}/{FILES_XML_PATH}"
        dest_path = cache_media_info / "files.xml.lzma"

        try:
            req = urllib.request.Request(files_xml_url)
            req.add_header('User-Agent', 'urpm/0.1')

            with urllib.request.urlopen(req, timeout=300) as resp:
                total_size = int(resp.headers.get('Content-Length', 0))
                downloaded = 0
                md5_hash = hashlib.md5()

                with open(dest_path, 'wb') as f:
                    while True:
                        chunk = resp.read(65536)  # 64KB chunks
                        if not chunk:
                            break
                        f.write(chunk)
                        md5_hash.update(chunk)
                        downloaded += len(chunk)

                        if progress_callback and total_size > 0:
                            progress_callback(state.media_name, 'downloading',
                                            downloaded, total_size, 0, 0)

                state.download_path = dest_path
                state.compressed_size = dest_path.stat().st_size
                state.current_md5 = md5_hash.hexdigest()
                state.download_complete = True

                if progress_callback:
                    progress_callback(state.media_name, 'downloaded',
                                    state.compressed_size, state.compressed_size, 0, 0)

        except urllib.error.HTTPError as e:
            if e.code == 404:
                state.error = "files.xml not found (404)"
            else:
                state.error = f"HTTP {e.code}"
        except Exception as e:
            state.error = str(e)

        return state

    # Download in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_files_xml, state): state for state in changed_states}
        for future in as_completed(futures):
            state = future.result()

    # =========================================================================
    # Phase 3: Import to staging table (sequential, small files first)
    # =========================================================================
    # Filter to successfully downloaded
    downloaded_states = [s for s in changed_states if s.download_complete and s.download_path]

    if not downloaded_states:
        # No successful downloads
        results = []
        for state in media_states:
            if state.skipped:
                results.append((state.media_name, FilesXmlResult(
                    success=True, file_count=state.file_count, pkg_count=state.pkg_count, skipped=True)))
            else:
                results.append((state.media_name, FilesXmlResult(
                    success=False, error=state.error or "Download failed")))
        return results

    # Sort by compressed size (small first for faster initial feedback)
    downloaded_states.sort(key=lambda s: s.compressed_size)

    # Check if we have existing data (for incremental vs full import decision)
    conn = db._get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM package_files")
    existing_file_count = cursor.fetchone()[0]
    use_incremental = existing_file_count > 0

    # =========================================================================
    # Phase 3: Import - INCREMENTAL or FULL depending on existing data
    # =========================================================================
    if use_incremental:
        # INCREMENTAL MODE: faster, no index recreation
        for state in downloaded_states:
            if progress_callback:
                progress_callback(state.media_name, 'syncing', 0, 0, 0, 0)

            try:
                def incr_progress(stage, current, total):
                    if progress_callback:
                        progress_callback(state.media_name, stage, 0, 0, current, total)

                added, removed, total_files = sync_files_xml_incremental(
                    db, state.media_id, state.download_path, incr_progress
                )
                state.file_count = total_files
                state.pkg_count = added  # Approximate
                state.import_complete = True

                if progress_callback:
                    progress_callback(state.media_name, 'done', 0, 0, total_files, total_files)

            except Exception as e:
                state.error = f"Sync error: {e}"
                if progress_callback:
                    progress_callback(state.media_name, 'error', 0, 0, 0, 0)

        # Update state for successful imports
        for state in downloaded_states:
            if state.import_complete:
                # Update files_xml_state
                cursor.execute("""
                    INSERT OR REPLACE INTO files_xml_state
                    (media_id, last_sync, files_md5, file_count, pkg_count, compressed_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    state.media_id,
                    int(time.time()),
                    state.current_md5,
                    state.file_count,
                    state.pkg_count,
                    state.compressed_size
                ))
        conn.commit()

    else:
        # FULL IMPORT MODE: staging table + atomic swap (first import only)
        # Estimate total files based on historical ratio
        ratio = db.get_files_xml_ratio()
        if ratio:
            for state in downloaded_states:
                state.file_count = int(state.compressed_size * ratio)  # Estimate

        original_pragmas = None
        import_success = False

        try:
            # Force WAL checkpoint to clear any pending transactions
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()

            db.create_package_files_staging()
            original_pragmas = db.set_fast_import_pragmas()

            # Import each media
            for state in downloaded_states:
                if progress_callback:
                    progress_callback(state.media_name, 'importing', 0, 0, 0, state.file_count)

                estimated_total = state.file_count

                def import_progress(files_imported, pkgs_imported):
                    if progress_callback:
                        progress_callback(state.media_name, 'importing',
                                        0, 0, files_imported, estimated_total or files_imported)

                try:
                    file_count, pkg_count = db.import_files_to_staging(
                        state.media_id,
                        parse_files_xml(state.download_path),
                        batch_size=1000,
                        progress_callback=import_progress
                    )
                    state.file_count = file_count
                    state.pkg_count = pkg_count
                    state.import_complete = True

                    if progress_callback:
                        progress_callback(state.media_name, 'done', 0, 0, file_count, file_count)

                except Exception as e:
                    state.error = f"Import error: {e}"
                    if progress_callback:
                        progress_callback(state.media_name, 'error', 0, 0, 0, 0)

            import_success = any(s.import_complete for s in downloaded_states)

        finally:
            if original_pragmas:
                db.restore_pragmas(original_pragmas)

        # Atomic swap for full import
        if import_success:
            try:
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                # Signal indexing phase (can take ~30s for 7M files)
                if progress_callback:
                    for state in downloaded_states:
                        if state.import_complete:
                            progress_callback(state.media_name, 'indexing', 0, 0, 0, 0)

                db.finalize_package_files_atomic()

                # Update files_xml_state for all successfully imported media
                states_to_update = []
                for state in downloaded_states:
                    if state.import_complete:
                        states_to_update.append({
                            'media_id': state.media_id,
                            'md5': state.current_md5,
                            'file_count': state.file_count,
                            'pkg_count': state.pkg_count,
                            'compressed_size': state.compressed_size
                        })
                if states_to_update:
                    db.update_files_xml_state_batch(states_to_update)

                # Rebuild FTS index after full import (atomic swap cleared it)
                if db.is_fts_available():
                    if progress_callback:
                        # Use first media for progress reporting
                        progress_callback("FTS index", 'indexing', 0, 0, 0, 0)

                    def fts_progress(current, total):
                        if progress_callback:
                            progress_callback("FTS index", 'indexing', 0, 0, current, total)

                    db.rebuild_fts_index(progress_callback=fts_progress)

                    if progress_callback:
                        progress_callback("FTS index", 'done', 0, 0, 0, 0)

            except Exception as e:
                # Atomic swap failed - abort
                db.abort_package_files_atomic()
                for state in downloaded_states:
                    if state.import_complete:
                        state.error = f"Atomic swap failed: {e}"
                        state.import_complete = False
        else:
            # All imports failed - cleanup staging
            db.abort_package_files_atomic()

    # =========================================================================
    # Check if FTS needs rebuild (migration case: data exists but FTS empty)
    # =========================================================================
    if use_incremental and db.is_fts_available() and not db.is_fts_index_current():
        if progress_callback:
            progress_callback("FTS index", 'indexing', 0, 0, 0, 0)

        def fts_progress(current, total):
            if progress_callback:
                progress_callback("FTS index", 'indexing', 0, 0, current, total)

        db.rebuild_fts_index(progress_callback=fts_progress)

        if progress_callback:
            progress_callback("FTS index", 'done', 0, 0, 0, 0)

    # =========================================================================
    # Build results
    # =========================================================================
    results = []
    for state in media_states:
        if state.skipped:
            results.append((state.media_name, FilesXmlResult(
                success=True,
                file_count=state.file_count,
                pkg_count=state.pkg_count,
                skipped=True
            )))
        elif state.import_complete:
            results.append((state.media_name, FilesXmlResult(
                success=True,
                file_count=state.file_count,
                pkg_count=state.pkg_count
            )))
        else:
            results.append((state.media_name, FilesXmlResult(
                success=False,
                error=state.error or "Unknown error"
            )))

    return results
