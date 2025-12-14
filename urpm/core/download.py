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

    def __init__(self, cache_dir: Path = None, max_workers: int = 4):
        """Initialize downloader.

        Args:
            cache_dir: Directory to store downloaded RPMs
            max_workers: Max parallel downloads
        """
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers

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
                     timeout: int = 30) -> DownloadResult:
        """Download a single package.

        Args:
            item: Package to download
            progress_callback: Optional callback(downloaded, total)
            timeout: Connection timeout in seconds

        Returns:
            DownloadResult with status
        """
        # Check cache first
        cache_path = self.get_cache_path(item)
        if self.is_cached(item):
            return DownloadResult(
                item=item,
                success=True,
                path=cache_path,
                cached=True
            )

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
            return DownloadResult(
                item=item,
                success=False,
                error=f"HTTP {e.code}: {e.reason}"
            )
        except urllib.error.URLError as e:
            return DownloadResult(
                item=item,
                success=False,
                error=f"URL error: {e.reason}"
            )
        except Exception as e:
            return DownloadResult(
                item=item,
                success=False,
                error=str(e)
            )

    def download_all(self, items: List[DownloadItem],
                     progress_callback: Callable[[str, int, int, int, int], None] = None
                     ) -> Tuple[List[DownloadResult], int, int]:
        """Download multiple packages in parallel.

        Args:
            items: List of packages to download
            progress_callback: Optional callback(name, pkg_num, pkg_total, bytes, bytes_total)

        Returns:
            Tuple of (results, total_downloaded, total_cached)
        """
        results = []
        cached_count = 0
        downloaded_count = 0
        total_bytes = sum(item.size for item in items)
        downloaded_bytes = 0

        # Check what's already cached
        to_download = []
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

        if progress_callback and cached_count > 0:
            progress_callback("(cache)", cached_count, len(items), downloaded_bytes, total_bytes)

        if not to_download:
            return results, downloaded_count, cached_count

        # Download remaining in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for item in to_download:
                future = executor.submit(self.download_one, item)
                futures[future] = item

            for future in as_completed(futures):
                item = futures[future]
                result = future.result()
                results.append(result)

                if result.success:
                    downloaded_count += 1
                    downloaded_bytes += item.size

                if progress_callback:
                    progress_callback(
                        item.name,
                        cached_count + downloaded_count,
                        len(items),
                        downloaded_bytes,
                        total_bytes
                    )

        return results, downloaded_count, cached_count


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
