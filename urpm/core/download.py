"""
Package download manager

Downloads RPM packages from media sources with progress reporting.
Inspired by apt's download handling - simple and efficient.
"""

import hashlib
import os
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Callable, Tuple

from .database import PackageDatabase
from .peer_client import (
    PeerClient, Peer, create_download_plan, summarize_download_plan,
    DownloadAssignment
)


# Default cache directory (mirrors structure)
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "urpm"


def get_hostname_from_url(url: str) -> str:
    """Extract hostname from a URL for cache organization."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or "local"


@dataclass
class DownloadItem:
    """A package to download."""
    name: str
    version: str
    release: str
    arch: str
    media_url: str
    media_name: str = ""
    size: int = 0

    @property
    def hostname(self) -> str:
        """Hostname from media URL for cache organization."""
        return get_hostname_from_url(self.media_url)

    @property
    def filename(self) -> str:
        """RPM filename (without epoch)."""
        return f"{self.name}-{self.version}-{self.release}.{self.arch}.rpm"

    @property
    def url(self) -> str:
        """Full download URL."""
        return f"{self.media_url.rstrip('/')}/{self.filename}"


@dataclass
class DownloadResult:
    """Result of a download operation."""
    item: DownloadItem
    success: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    cached: bool = False


class Downloader:
    """Download manager for RPM packages."""

    def __init__(self, cache_dir: Path = None, max_workers: int = 4,
                 use_peers: bool = True):
        """Initialize downloader.

        Args:
            cache_dir: Directory to store downloaded RPMs
            max_workers: Max parallel downloads
            use_peers: Whether to use P2P peer discovery for downloads
        """
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self.use_peers = use_peers
        self._peer_client: Optional[PeerClient] = None

    def get_cache_path(self, item: DownloadItem) -> Path:
        """Get cache path for a download item.

        Structure: ~/.cache/urpm/medias/<hostname>/<media_name>/*.rpm
        """
        if item.media_name and item.media_url:
            media_dir = self.cache_dir / "medias" / item.hostname / item.media_name
            media_dir.mkdir(parents=True, exist_ok=True)
            return media_dir / item.filename
        return self.cache_dir / item.filename

    def is_cached(self, item: DownloadItem) -> bool:
        """Check if package is already in cache."""
        path = self.get_cache_path(item)
        if not path.exists():
            return False
        # File exists and has content - consider it cached
        # Note: item.size is installed size, not RPM file size
        return path.stat().st_size > 0

    def download_one(self, item: DownloadItem,
                     progress_callback: Callable[[int, int], None] = None,
                     timeout: int = 30,
                     max_retries: int = 3) -> DownloadResult:
        """Download a single package with retry on transient errors.

        Args:
            item: Package to download
            progress_callback: Optional callback(downloaded, total)
            timeout: Connection timeout in seconds
            max_retries: Max retry attempts for transient errors

        Returns:
            DownloadResult with status
        """
        import time
        import socket

        # Check cache first
        cache_path = self.get_cache_path(item)
        if self.is_cached(item):
            return DownloadResult(
                item=item,
                success=True,
                path=cache_path,
                cached=True
            )

        last_error = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(item.url)
                req.add_header('User-Agent', 'urpm/0.1')

                with urllib.request.urlopen(req, timeout=timeout) as response:
                    total_size = int(response.headers.get('Content-Length', 0))
                    downloaded = 0

                    # Download to temp file first
                    temp_path = cache_path.with_suffix('.tmp')

                    with open(temp_path, 'wb') as f:
                        while True:
                            chunk = response.read(65536)  # 64KB chunks
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)

                            if progress_callback:
                                progress_callback(downloaded, total_size)

                    # Move to final path
                    temp_path.rename(cache_path)

                    return DownloadResult(
                        item=item,
                        success=True,
                        path=cache_path
                    )

            except urllib.error.HTTPError as e:
                # HTTP errors (404, 500, etc.) - don't retry
                return DownloadResult(
                    item=item,
                    success=False,
                    error=f"HTTP {e.code}: {e.reason}"
                )
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                # Transient errors - retry with backoff
                last_error = str(e.reason) if hasattr(e, 'reason') else str(e)
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))  # 1s, 2s, 3s backoff
                    continue
            except Exception as e:
                last_error = str(e)
                break

        return DownloadResult(
            item=item,
            success=False,
            error=f"After {max_retries} attempts: {last_error}"
        )

    def download_from_peer(self, item: DownloadItem, peer: Peer, peer_path: str,
                           progress_callback: Callable[[int, int], None] = None,
                           timeout: int = 30) -> DownloadResult:
        """Download a package from a peer.

        Args:
            item: Package to download
            peer: Peer to download from
            peer_path: Path on peer (from /api/have response)
            progress_callback: Optional callback(downloaded, total)
            timeout: Connection timeout in seconds

        Returns:
            DownloadResult with status
        """
        from urllib.parse import quote

        cache_path = self.get_cache_path(item)
        # URL-encode the path (but keep slashes)
        encoded_path = quote(peer_path, safe='/')
        url = f"{peer.base_url}/media/{encoded_path}"

        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'urpm/0.1')

            with urllib.request.urlopen(req, timeout=timeout) as response:
                total_size = int(response.headers.get('Content-Length', 0))
                downloaded = 0

                temp_path = cache_path.with_suffix('.tmp')

                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback:
                            progress_callback(downloaded, total_size)

                temp_path.rename(cache_path)

                return DownloadResult(
                    item=item,
                    success=True,
                    path=cache_path
                )

        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            # Clean up temp file
            temp_path = cache_path.with_suffix('.tmp')
            if temp_path.exists():
                temp_path.unlink()
            return DownloadResult(
                item=item,
                success=False,
                error=f"Peer download failed: {e}"
            )

    def download_all(self, items: List[DownloadItem],
                     progress_callback: Callable[[str, int, int, int, int], None] = None
                     ) -> Tuple[List[DownloadResult], int, int, dict]:
        """Download multiple packages in parallel, using P2P when available.

        Args:
            items: List of packages to download
            progress_callback: Optional callback(name, pkg_num, pkg_total, bytes, bytes_total)

        Returns:
            Tuple of (results, total_downloaded, total_cached, peer_stats)
            peer_stats contains P2P download statistics
        """
        import logging
        logger = logging.getLogger(__name__)

        results = []
        cached_count = 0
        downloaded_count = 0
        from_peer_count = 0
        from_upstream_count = 0
        total_bytes = sum(item.size for item in items)
        downloaded_bytes = 0

        # Check what's already cached
        to_download = []
        items_by_filename = {}  # Map filename -> DownloadItem
        for item in items:
            if self.is_cached(item):
                results.append(DownloadResult(
                    item=item,
                    success=True,
                    path=self.get_cache_path(item),
                    cached=True
                ))
                cached_count += 1
                downloaded_bytes += item.size
            else:
                to_download.append(item)
                items_by_filename[item.filename] = item

        if progress_callback and cached_count > 0:
            progress_callback("(cache)", cached_count, len(items), downloaded_bytes, total_bytes)

        if not to_download:
            return results, downloaded_count, cached_count, {'from_peers': 0, 'from_upstream': 0}

        # P2P: Discover peers and create download plan
        download_plan = {}  # filename -> DownloadAssignment
        if self.use_peers:
            import time as _time
            try:
                if self._peer_client is None:
                    self._peer_client = PeerClient()

                t0 = _time.time()
                peers = self._peer_client.discover_peers()
                t1 = _time.time()
                logger.debug(f"P2P: peer discovery took {t1-t0:.2f}s, found {len(peers)} peers")

                if peers:
                    filenames = [item.filename for item in to_download]
                    availability = self._peer_client.query_peers_have(peers, filenames)
                    t2 = _time.time()
                    logger.debug(f"P2P: availability query took {t2-t1:.2f}s")

                    assignments = create_download_plan(filenames, availability)

                    for assignment in assignments:
                        download_plan[assignment.filename] = assignment

                    summary = summarize_download_plan(assignments)
                    if summary['from_peers_count'] > 0:
                        logger.info(f"P2P: {summary['from_peers_count']} packages from peers, "
                                    f"{summary['from_upstream_count']} from upstream")
            except Exception as e:
                logger.debug(f"P2P discovery failed: {e}")

        # Download in parallel (peers + upstream)
        import time as _time
        t_dl_start = _time.time()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}

            for item in to_download:
                assignment = download_plan.get(item.filename)

                if assignment and assignment.source == 'peer' and assignment.peer:
                    # Download from peer
                    future = executor.submit(
                        self._download_with_fallback,
                        item, assignment.peer, assignment.peer_path
                    )
                else:
                    # Download from upstream
                    future = executor.submit(self.download_one, item)

                futures[future] = (item, assignment)

            t_submitted = _time.time()
            logger.debug(f"Submitted {len(futures)} download tasks in {t_submitted - t_dl_start:.2f}s")

            for future in as_completed(futures):
                item, assignment = futures[future]
                future_result = future.result()

                # Handle both tuple (from _download_with_fallback) and DownloadResult (from download_one)
                if isinstance(future_result, tuple):
                    result, from_peer = future_result
                else:
                    result = future_result
                    from_peer = False

                results.append(result)

                if result.success:
                    downloaded_count += 1
                    downloaded_bytes += item.size
                    if from_peer:
                        from_peer_count += 1
                    else:
                        from_upstream_count += 1

                if progress_callback:
                    progress_callback(
                        item.name,
                        cached_count + downloaded_count,
                        len(items),
                        downloaded_bytes,
                        total_bytes
                    )

            t_all_done = _time.time()
            logger.debug(f"All downloads completed in {t_all_done - t_submitted:.2f}s")

        t_executor_exit = _time.time()
        logger.debug(f"Executor cleanup took {t_executor_exit - t_all_done:.2f}s")

        peer_stats = {
            'from_peers': from_peer_count,
            'from_upstream': from_upstream_count,
        }
        return results, downloaded_count, cached_count, peer_stats

    def _download_with_fallback(self, item: DownloadItem, peer: Peer, peer_path: str
                                ) -> Tuple[DownloadResult, bool]:
        """Try peer download, fallback to upstream on failure.

        Returns:
            Tuple of (DownloadResult, from_peer_bool)
        """
        # Try peer first
        result = self.download_from_peer(item, peer, peer_path)
        if result.success:
            return result, True

        # Fallback to upstream
        result = self.download_one(item)
        return result, False


def get_download_items(db: PackageDatabase, packages: List[dict]) -> List[DownloadItem]:
    """Convert package actions to download items.

    Args:
        db: Database instance
        packages: List of package dicts with name, version, release, arch, media_name

    Returns:
        List of DownloadItem ready for download
    """
    items = []
    media_cache = {}  # Cache media URL lookups

    for pkg in packages:
        media_name = pkg.get('media_name', '')
        if media_name not in media_cache:
            media = db.get_media(media_name)
            media_cache[media_name] = media['url'] if media else ''

        media_url = media_cache[media_name]
        if not media_url:
            continue

        # Parse EVR - remove epoch if present
        evr = pkg.get('evr', '')
        if ':' in evr:
            evr = evr.split(':', 1)[1]  # Remove epoch

        if '-' in evr:
            version, release = evr.rsplit('-', 1)
        else:
            version = evr
            release = '1'

        items.append(DownloadItem(
            name=pkg['name'],
            version=version,
            release=release,
            arch=pkg['arch'],
            media_url=media_url,
            media_name=media_name,
            size=pkg.get('size', 0)
        ))

    return items


def format_speed(bytes_per_sec: float) -> str:
    """Format download speed."""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec / 1024 / 1024:.1f} MB/s"
