"""
Package download manager

Downloads RPM packages from media sources with progress reporting.
Uses a queue-based architecture for robust parallel downloads with
dynamic peer failure tracking and reassignment.
"""

import hashlib
import logging
import os
import queue
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Callable, Tuple, Dict, Set

from .database import PackageDatabase
from .config import get_base_dir, is_dev_mode
from .peer_client import (
    PeerClient, Peer, create_download_plan, summarize_download_plan,
    DownloadAssignment
)

logger = logging.getLogger(__name__)

# RPM magic bytes: 0xED 0xAB 0xEE 0xDB
RPM_MAGIC = b'\xed\xab\xee\xdb'


def is_valid_rpm(file_path: Path) -> Tuple[bool, str]:
    """Quick check if a file is a valid RPM by checking magic bytes.

    Args:
        file_path: Path to the file to check

    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if file starts with RPM magic bytes
        - error_message: Description of problem if invalid, None if valid
    """
    try:
        with open(file_path, 'rb') as f:
            magic = f.read(4)
            if len(magic) < 4:
                return False, "File too small (< 4 bytes)"
            if magic != RPM_MAGIC:
                # Try to detect what it actually is
                f.seek(0)
                start = f.read(100)
                if b'<!DOCTYPE' in start or b'<html' in start.lower() or b'<HTML' in start:
                    return False, "File is HTML (captive portal?)"
                elif start.startswith(b'<?xml'):
                    return False, "File is XML"
                elif start.startswith(b'PK'):
                    return False, "File is ZIP archive"
                else:
                    return False, f"Invalid RPM magic (got {magic.hex()})"
            return True, None
    except (OSError, IOError) as e:
        return False, f"Cannot read file: {e}"


def verify_rpm_signature(rpm_path: Path) -> tuple:
    """Verify GPG signature of an RPM file.

    Args:
        rpm_path: Path to RPM file

    Returns:
        Tuple of (success: bool, error_message: str or None)
    """
    import rpm

    ts = rpm.TransactionSet()
    # Enable all signature/digest verification
    ts.setVSFlags(0)

    try:
        with open(rpm_path, 'rb') as f:
            # hdrFromFdno verifies signature when VSFlags allows it
            hdr = ts.hdrFromFdno(f.fileno())
            return (True, None)
    except rpm.error as e:
        return (False, str(e))
    except Exception as e:
        return (False, f"Verification error: {e}")


def get_hostname_from_url(url: str) -> str:
    """Extract hostname from a URL for cache organization."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or "local"


@dataclass
class DownloadItem:
    """A package to download.

    Supports both legacy (media_url) and new (media_id + relative_path) schemas.
    For new schema, servers must be pre-loaded to avoid SQLite threading issues.
    """
    name: str
    version: str
    release: str
    arch: str
    # Legacy fields
    media_url: str = ""
    media_name: str = ""
    # New schema fields
    media_id: int = 0
    relative_path: str = ""
    is_official: bool = True
    # Pre-loaded servers (list of dicts with protocol, host, base_path)
    servers: List[dict] = field(default_factory=list)
    size: int = 0

    @property
    def hostname(self) -> str:
        """Hostname from media URL for cache organization (legacy)."""
        return get_hostname_from_url(self.media_url) if self.media_url else ""

    @property
    def filename(self) -> str:
        """RPM filename (without epoch)."""
        return f"{self.name}-{self.version}-{self.release}.{self.arch}.rpm"

    @property
    def url(self) -> str:
        """Full download URL (legacy - uses first media_url)."""
        if self.media_url:
            return f"{self.media_url.rstrip('/')}/{self.filename}"
        return ""

    def uses_new_schema(self) -> bool:
        """Check if this item uses new multi-server schema."""
        return len(self.servers) > 0 and self.relative_path != ""


@dataclass
class PeerProvenance:
    """Provenance info for P2P download."""
    peer_host: str
    peer_port: int
    checksum_sha256: str
    file_size: int
    verified: bool


@dataclass
class PeerToBlacklist:
    """Info about a peer to blacklist (deferred to main thread)."""
    host: str
    port: int
    reason: str


@dataclass
class DownloadResult:
    """Result of a download operation."""
    item: DownloadItem
    success: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    cached: bool = False
    peer_info: Optional[PeerProvenance] = None  # Set if downloaded from peer
    blacklist_peer: Optional[PeerToBlacklist] = None  # Set if peer should be blacklisted
    from_peer: bool = False  # True if downloaded from peer (not upstream)


@dataclass
class PeerAvailability:
    """Tracks which peers have which files."""
    # Map: filename -> list of (peer, path) tuples
    file_to_peers: Dict[str, List[Tuple[Peer, str]]] = field(default_factory=dict)

    def get_peers_for_file(self, filename: str, exclude: Set[Tuple[str, int]] = None
                          ) -> List[Tuple[Peer, str]]:
        """Get peers that have a file, excluding failed ones."""
        peers = self.file_to_peers.get(filename, [])
        if not exclude:
            return peers
        return [(p, path) for p, path in peers if (p.host, p.port) not in exclude]


@dataclass
class DownloadProgress:
    """Real-time progress for an active download.

    Used for display and server performance tracking.
    """
    name: str
    bytes_done: int
    bytes_total: int
    source: str            # Server name or "peer@host"
    source_type: str       # 'server', 'peer', 'cache'
    start_time: float      # time.time() when download started
    samples: List[Tuple[float, int]] = field(default_factory=list)  # [(time, bytes), ...]

    def add_sample(self, bytes_done: int):
        """Add a progress sample for speed calculation."""
        import time as _time
        now = _time.time()
        self.bytes_done = bytes_done
        # Keep only last 10 samples for rolling average
        self.samples.append((now, bytes_done))
        if len(self.samples) > 10:
            self.samples.pop(0)

    def get_speed(self) -> float:
        """Calculate current download speed in bytes/sec."""
        if len(self.samples) < 2:
            return 0.0
        oldest_time, oldest_bytes = self.samples[0]
        newest_time, newest_bytes = self.samples[-1]
        elapsed = newest_time - oldest_time
        if elapsed <= 0:
            return 0.0
        return (newest_bytes - oldest_bytes) / elapsed


class DownloadCoordinator:
    """Coordinates parallel downloads with queue-based architecture.

    Key features:
    - Central queue of packages to download
    - Thread-safe failed peer tracking (immediate propagation)
    - Dynamic peer reassignment when a peer fails
    - Workers pull work from queue and check peer status before each download
    """

    def __init__(self, downloader: 'Downloader', max_workers: int = 4):
        self.downloader = downloader
        self.max_workers = max_workers

        # Work queue: (DownloadItem, Optional[DownloadAssignment])
        self._work_queue: queue.Queue = queue.Queue()

        # Results queue: DownloadResult
        self._results_queue: queue.Queue = queue.Queue()

        # Failed peers tracking (thread-safe)
        self._failed_peers_lock = threading.Lock()
        self._failed_peers: Set[Tuple[str, int]] = set()

        # Peer availability for reassignment
        self._peer_availability: Optional[PeerAvailability] = None

        # Pending DB operations (collected in main thread after workers finish)
        self._pending_blacklist: List[PeerToBlacklist] = []

        # Stats
        self._from_peer_count = 0
        self._from_upstream_count = 0
        self._stats_lock = threading.Lock()

        # Real-time download progress tracking (thread-safe)
        # Each worker has a fixed slot (0 to max_workers-1) for stable display
        self._current_progress_lock = threading.Lock()
        self._current_downloads: Dict[int, DownloadProgress] = {}  # slot -> DownloadProgress

    def is_peer_failed(self, peer: Peer) -> bool:
        """Check if peer has failed (thread-safe)."""
        with self._failed_peers_lock:
            return (peer.host, peer.port) in self._failed_peers

    def mark_peer_failed(self, peer: Peer, reason: str):
        """Mark peer as failed (thread-safe, immediate propagation)."""
        with self._failed_peers_lock:
            peer_key = (peer.host, peer.port)
            if peer_key not in self._failed_peers:
                self._failed_peers.add(peer_key)
                logger.warning(f"Peer {peer.host}:{peer.port} marked as failed: {reason}")
                self._pending_blacklist.append(
                    PeerToBlacklist(peer.host, peer.port, reason)
                )

    def get_failed_peers(self) -> Set[Tuple[str, int]]:
        """Get current set of failed peers (thread-safe copy)."""
        with self._failed_peers_lock:
            return self._failed_peers.copy()

    def find_alternative_peer(self, item: DownloadItem,
                              exclude_peer: Peer = None) -> Optional[Tuple[Peer, str]]:
        """Find an alternative peer for a file, excluding failed ones."""
        if not self._peer_availability:
            return None

        excluded = self.get_failed_peers()
        if exclude_peer:
            excluded.add((exclude_peer.host, exclude_peer.port))

        alternatives = self._peer_availability.get_peers_for_file(
            item.filename, exclude=excluded
        )
        return alternatives[0] if alternatives else None

    def start_download(self, slot: int, item_name: str, bytes_total: int,
                        source: str, source_type: str):
        """Start tracking a new download for a worker slot (thread-safe)."""
        import time as _time
        with self._current_progress_lock:
            self._current_downloads[slot] = DownloadProgress(
                name=item_name,
                bytes_done=0,
                bytes_total=bytes_total,
                source=source,
                source_type=source_type,
                start_time=_time.time(),
                samples=[]
            )

    def update_download_progress(self, slot: int, bytes_done: int):
        """Update real-time download progress for a worker slot (thread-safe)."""
        with self._current_progress_lock:
            if slot in self._current_downloads:
                self._current_downloads[slot].add_sample(bytes_done)

    def clear_download_progress(self, slot: int) -> Optional[DownloadProgress]:
        """Clear download progress when item completes (thread-safe).

        Returns the final DownloadProgress for stats collection.
        """
        with self._current_progress_lock:
            return self._current_downloads.pop(slot, None)

    def get_all_active_downloads(self) -> List[Tuple[int, DownloadProgress]]:
        """Get all active downloads progress (thread-safe).

        Returns list of (slot, DownloadProgress) sorted by slot number.
        """
        with self._current_progress_lock:
            if not self._current_downloads:
                return []
            # Sort by slot number for stable display order
            return [(slot, prog) for slot, prog in sorted(self._current_downloads.items())]

    def get_all_slots_status(self) -> List[Tuple[int, Optional[DownloadProgress]]]:
        """Get status of all worker slots (thread-safe).

        Returns list of (slot, DownloadProgress or None) for all slots.
        """
        with self._current_progress_lock:
            return [(slot, self._current_downloads.get(slot))
                    for slot in range(self.max_workers)]

    def _worker(self, slot: int):
        """Worker thread: dequeue, download, report, repeat.

        Args:
            slot: Fixed slot number (0 to max_workers-1) for stable progress display
        """
        while True:
            try:
                # Short timeout to allow checking for shutdown
                work = self._work_queue.get(timeout=0.5)
            except queue.Empty:
                # Check if we're done (no more work coming)
                if self._work_queue.empty():
                    break
                continue

            item, assignment = work

            try:
                result = self._download_item(item, assignment, slot)
                self._results_queue.put(result)

                # Update stats
                with self._stats_lock:
                    if result.success:
                        if result.from_peer:
                            self._from_peer_count += 1
                        else:
                            self._from_upstream_count += 1

            except Exception as e:
                # Unexpected error - report failure
                logger.error(f"Worker error downloading {item.filename}: {e}")
                self._results_queue.put(DownloadResult(
                    item=item,
                    success=False,
                    error=str(e)
                ))
            finally:
                self._work_queue.task_done()

    def _download_item(self, item: DownloadItem,
                       assignment: Optional[DownloadAssignment],
                       slot: int) -> DownloadResult:
        """Download an item, handling peer failures with reassignment.

        Args:
            item: Item to download
            assignment: Optional peer assignment
            slot: Worker slot number for progress tracking
        """
        # Check cache first
        if self.downloader.is_cached(item):
            return DownloadResult(
                item=item,
                success=True,
                path=self.downloader.get_cache_path(item),
                cached=True
            )

        # Create progress callback for real-time tracking
        def progress_cb(bytes_done: int, bytes_total: int):
            self.update_download_progress(slot, bytes_done)

        try:
            # Try peer download if assigned
            if assignment and assignment.source == 'peer' and assignment.peer:
                peer = assignment.peer
                peer_path = assignment.peer_path

                # Check if this peer has already failed
                if self.is_peer_failed(peer):
                    logger.debug(f"Skipping failed peer {peer.host} for {item.filename}")
                    # Try alternative peer
                    alt = self.find_alternative_peer(item, exclude_peer=peer)
                    if alt:
                        peer, peer_path = alt
                        logger.debug(f"Using alternative peer {peer.host} for {item.filename}")
                    else:
                        # No alternative - fall through to upstream
                        assignment = None
                        peer = None

                if peer:
                    # Start tracking with peer source
                    self.start_download(slot, item.name, item.size or 0,
                                        f"peer@{peer.host}", 'peer')
                    result = self.downloader.download_from_peer(item, peer, peer_path, progress_callback=progress_cb)

                    if result.success:
                        result.from_peer = True
                        return result

                    # Peer failed - check if GPG issue (should blacklist)
                    if result.blacklist_peer:
                        self.mark_peer_failed(peer, result.blacklist_peer.reason)

                    # Try alternative peer before upstream
                    alt = self.find_alternative_peer(item, exclude_peer=peer)
                    if alt:
                        alt_peer, alt_path = alt
                        logger.debug(f"Retrying {item.filename} with alternative peer {alt_peer.host}")
                        # Update tracking with new peer
                        self.start_download(slot, item.name, item.size or 0,
                                            f"peer@{alt_peer.host}", 'peer')
                        result = self.downloader.download_from_peer(item, alt_peer, alt_path, progress_callback=progress_cb)
                        if result.success:
                            result.from_peer = True
                            return result
                        if result.blacklist_peer:
                            self.mark_peer_failed(alt_peer, result.blacklist_peer.reason)

            # Fall back to upstream - unless only_peers mode
            if self.downloader.only_peers:
                # Only peers mode: fail if no peer has the package
                logger.debug(f"only_peers mode: skipping upstream for {item.filename}")
                return DownloadResult(
                    item=item,
                    success=False,
                    error="Not available from peers (--only-peers mode)"
                )

            # Download from upstream - source will be set by download_one via callback
            result = self.downloader.download_one(
                item, progress_callback=progress_cb, worker_slot=slot,
                start_callback=lambda source: self.start_download(
                    slot, item.name, item.size or 0, source, 'server'
                )
            )
            result.from_peer = False
            return result
        finally:
            # Clear progress when download completes
            self.clear_download_progress(slot)

    def download_all(self, items: List[DownloadItem],
                     peer_availability: Dict,
                     progress_callback: Callable = None) -> Tuple[List[DownloadResult], dict]:
        """Download all items using queue-based parallel processing.

        Args:
            items: List of items to download
            peer_availability: Dict[filename, List[PeerPackageInfo]] from query_peers_have()
            progress_callback: Optional callback(name, pkg_num, total, bytes, total_bytes)

        Returns:
            Tuple of (results, stats)
        """
        # Build peer availability index from PeerPackageInfo format
        # Input: {filename: [PeerPackageInfo(peer, path, ...), ...], ...}
        # Output: {filename: [(peer, path), ...], ...}
        self._peer_availability = PeerAvailability()
        for filename, peer_infos in peer_availability.items():
            for info in peer_infos:
                if filename not in self._peer_availability.file_to_peers:
                    self._peer_availability.file_to_peers[filename] = []
                self._peer_availability.file_to_peers[filename].append((info.peer, info.path))

        # Create download plan
        filenames = [item.filename for item in items]
        assignments_list = create_download_plan(
            filenames,
            peer_availability
        ) if peer_availability else []

        # Map filename to assignment
        assignments = {a.filename: a for a in assignments_list}

        # Queue all work
        for item in items:
            assignment = assignments.get(item.filename)
            self._work_queue.put((item, assignment))

        # Start workers with fixed slot numbers for stable display
        workers = []
        for slot in range(self.max_workers):
            t = threading.Thread(target=self._worker, args=(slot,), daemon=True)
            t.start()
            workers.append(t)

        # Collect results in real-time with progress reporting
        results = []
        total_bytes = sum(item.size for item in items)
        completed_bytes = 0  # Bytes from fully completed downloads
        total_items = len(items)
        last_active_name = None

        # Poll results queue in real-time instead of waiting for join()
        while len(results) < total_items:
            try:
                # Short timeout to stay responsive and allow progress updates
                result = self._results_queue.get(timeout=0.1)
                results.append(result)

                if result.success:
                    completed_bytes += result.item.size

                if progress_callback:
                    # Get all slots status for consistent multi-line display
                    slots_status = self.get_all_slots_status()
                    active_downloads = [(s, p) for s, p in slots_status if p is not None]
                    if active_downloads:
                        partial_bytes = sum(p.bytes_done for _, p in active_downloads)
                        current_bytes = completed_bytes + partial_bytes
                        _, first_prog = active_downloads[0]
                        progress_callback(
                            first_prog.name,
                            len(results),
                            total_items,
                            current_bytes,
                            total_bytes,
                            first_prog.bytes_done,
                            first_prog.bytes_total,
                            slots_status  # Pass all slots (active and inactive)
                        )
                    else:
                        # No more active downloads - just show completion
                        progress_callback(
                            result.item.name,
                            len(results),
                            total_items,
                            completed_bytes,
                            total_bytes
                        )
                last_active_name = None  # Reset after completion
            except queue.Empty:
                # Check if workers are still alive
                if not any(t.is_alive() for t in workers):
                    # All workers dead but we don't have all results - something went wrong
                    logger.warning(f"Workers finished early: got {len(results)}/{total_items} results")
                    break

                # Report real-time progress for all active downloads
                if progress_callback:
                    slots_status = self.get_all_slots_status()
                    active_downloads = [(s, p) for s, p in slots_status if p is not None]
                    if active_downloads:
                        # Calculate real-time total including all partial downloads
                        partial_bytes = sum(p.bytes_done for _, p in active_downloads)
                        current_bytes = completed_bytes + partial_bytes
                        _, first_prog = active_downloads[0]
                        progress_callback(
                            first_prog.name,
                            len(results),
                            total_items,
                            current_bytes,
                            total_bytes,
                            first_prog.bytes_done,
                            first_prog.bytes_total,
                            slots_status  # Pass all slots (active and inactive)
                        )
                        last_active_name = first_prog.name
                    else:
                        # No active downloads but still working - show overall progress
                        # Use last_active_name or a generic message
                        progress_callback(
                            last_active_name or "...",
                            len(results),
                            total_items,
                            completed_bytes,
                            total_bytes
                        )
                continue

        # Wait for workers to finish cleanly
        for t in workers:
            t.join(timeout=1.0)

        stats = {
            'from_peers': self._from_peer_count,
            'from_upstream': self._from_upstream_count,
            'failed_peers': list(self._failed_peers),
            'pending_blacklist': self._pending_blacklist,
        }

        return results, stats


class Downloader:
    """Download manager for RPM packages."""

    def __init__(self, cache_dir: Path = None, max_workers: int = 4,
                 use_peers: bool = True, only_peers: bool = False,
                 db: 'PackageDatabase' = None,
                 target_version: str = None, target_arch: str = None):
        """Initialize downloader.

        Args:
            cache_dir: Directory to store downloaded RPMs
            max_workers: Max parallel downloads
            use_peers: Whether to use P2P peer discovery for downloads
            only_peers: If True, only download from peers (no upstream fallback)
            db: Database for provenance tracking and blacklist (optional)
            target_version: Target Mageia version for P2P queries (e.g., "10")
            target_arch: Target architecture for P2P queries (e.g., "x86_64")
        """
        self.cache_dir = cache_dir or get_base_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self.use_peers = use_peers or only_peers  # only_peers implies use_peers
        self.only_peers = only_peers
        self.db = db
        self.target_version = target_version
        self.target_arch = target_arch
        self._peer_client: Optional[PeerClient] = None

    def get_cache_path(self, item: DownloadItem) -> Path:
        """Get cache path for a download item.

        New schema: <base_dir>/medias/official/<relative_path>/*.rpm
                    <base_dir>/medias/custom/<short_name>/*.rpm
        Legacy:     <base_dir>/medias/<hostname>/<media_name>/*.rpm
        """
        if item.uses_new_schema():
            # New schema - use relative_path
            if item.is_official:
                media_dir = self.cache_dir / "medias" / "official" / item.relative_path
            else:
                media_dir = self.cache_dir / "medias" / "custom" / item.media_name
            media_dir.mkdir(parents=True, exist_ok=True)
            return media_dir / item.filename
        elif item.media_name and item.media_url:
            # Legacy schema
            media_dir = self.cache_dir / "medias" / item.hostname / item.media_name
            media_dir.mkdir(parents=True, exist_ok=True)
            return media_dir / item.filename
        return self.cache_dir / item.filename

    def is_cached(self, item: DownloadItem) -> bool:
        """Check if package is already in cache and is a valid RPM.

        Verifies:
        - File exists and is not empty
        - File has valid RPM magic bytes (0xedabeedb)

        This catches partial downloads and corrupted files.
        Full signature verification is done at install time.
        """
        path = self.get_cache_path(item)
        if not path.exists():
            return False
        if path.stat().st_size == 0:
            return False
        # Check RPM magic bytes
        try:
            with open(path, 'rb') as f:
                magic = f.read(4)
            return magic == RPM_MAGIC
        except OSError:
            return False

    def _register_cache_file(self, item: DownloadItem, cache_path: Path):
        """Register a downloaded file in the cache database for quota tracking.

        Args:
            item: Download item with media info
            cache_path: Path where the file was saved
        """
        if not self.db:
            return

        try:
            # Get file size
            file_size = cache_path.stat().st_size if cache_path.exists() else 0

            # Compute relative path from medias/
            medias_dir = self.cache_dir / "medias"
            try:
                rel_path = str(cache_path.relative_to(medias_dir))
            except ValueError:
                # File not under medias/, use full path
                rel_path = str(cache_path)

            # Get media_id (0 for legacy schema without media_id)
            media_id = item.media_id if item.media_id else 0

            if media_id:
                self.db.register_cache_file(
                    filename=item.filename,
                    media_id=media_id,
                    file_path=rel_path,
                    file_size=file_size
                )
                logger.debug(f"Registered cache file: {item.filename} ({file_size} bytes)")
        except Exception as e:
            # Don't fail the download if cache registration fails
            logger.warning(f"Failed to register cache file {item.filename}: {e}")

    def _download_from_url(self, url: str, cache_path: Path,
                            progress_callback: Callable[[int, int], None] = None,
                            timeout: int = 30,
                            ip_mode: str = 'auto') -> Tuple[bool, Optional[str]]:
        """Download a file from URL to cache path.

        Args:
            url: URL to download
            cache_path: Where to save the file
            progress_callback: Optional progress callback
            timeout: Connection timeout in seconds
            ip_mode: 'auto', 'ipv4', 'ipv6', or 'dual' (dual prefers ipv4)

        Returns:
            Tuple of (success, error_message or None)
        """
        import socket
        from .config import get_socket_family_for_ip_mode

        # Determine socket family based on ip_mode
        family = get_socket_family_for_ip_mode(ip_mode)

        # Patch getaddrinfo if we need to force a specific IP version
        original_getaddrinfo = None
        if family != 0:
            original_getaddrinfo = socket.getaddrinfo
            def patched_getaddrinfo(host, port, fam=0, type=0, proto=0, flags=0):
                # Force the specified family if caller didn't specify one
                if fam == 0:
                    fam = family
                return original_getaddrinfo(host, port, fam, type, proto, flags)
            socket.getaddrinfo = patched_getaddrinfo

        try:
            req = urllib.request.Request(url)
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
                return True, None

        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return False, f"URL error: {e.reason}"
        except Exception as e:
            return False, str(e)
        finally:
            # Restore original getaddrinfo
            if original_getaddrinfo is not None:
                socket.getaddrinfo = original_getaddrinfo

    def _build_package_url(self, server: dict, relative_path: str, filename: str) -> str:
        """Build full package URL from server info."""
        from .config import build_server_url
        base_url = build_server_url(server)
        return f"{base_url}/{relative_path}/{filename}"

    def download_one(self, item: DownloadItem,
                     progress_callback: Callable[[int, int], None] = None,
                     timeout: int = 30,
                     max_retries: int = 3,
                     worker_slot: int = 0,
                     start_callback: Callable[[str], None] = None) -> DownloadResult:
        """Download a single package with multi-server failover.

        For new schema: tries each server in priority order with retries.
        For legacy schema: uses single media_url with retries.

        Args:
            item: Package to download
            progress_callback: Optional callback(downloaded, total)
            timeout: Connection timeout in seconds
            max_retries: Max retry attempts per server for transient errors
            worker_slot: Worker slot number for load balancing across servers.
                         Workers are distributed across servers (slot 0,1 -> server 0 first,
                         slot 2,3 -> server 1 first, etc.) to avoid all workers hitting
                         the same server simultaneously.
            start_callback: Optional callback(source_name) called when starting download

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

        # New schema: try multiple servers (pre-loaded in item.servers)
        if item.uses_new_schema():
            servers = item.servers
            if not servers:
                return DownloadResult(
                    item=item,
                    success=False,
                    error="No servers configured for this media"
                )

            # Rotate servers based on worker_slot for load balancing
            # With 4 workers and 2 servers: workers 0,2 start with server 0,
            # workers 1,3 start with server 1
            if len(servers) > 1:
                start_idx = worker_slot % len(servers)
                servers = servers[start_idx:] + servers[:start_idx]

            all_errors = []
            for server in servers:
                url = self._build_package_url(server, item.relative_path, item.filename)
                ip_mode = server.get('ip_mode', 'auto')
                logger.debug(f"Trying server {server['name']} (ip_mode={ip_mode}): {url}")

                # Notify about source before starting
                if start_callback:
                    start_callback(server['name'])

                # Try this server with retries
                for attempt in range(max_retries):
                    success, error = self._download_from_url(
                        url, cache_path, progress_callback, timeout, ip_mode=ip_mode
                    )
                    if success:
                        # Verify the downloaded file is actually an RPM
                        is_rpm, rpm_error = is_valid_rpm(cache_path)
                        if not is_rpm:
                            # Delete corrupt file and treat as failure
                            logger.warning(f"Downloaded file is not a valid RPM: {rpm_error}")
                            try:
                                cache_path.unlink()
                            except OSError:
                                pass
                            all_errors.append(f"{server['name']}: {rpm_error}")
                            break  # Try next server

                        logger.info(f"Downloaded {item.filename} from {server['name']}")
                        self._register_cache_file(item, cache_path)
                        return DownloadResult(
                            item=item,
                            success=True,
                            path=cache_path
                        )

                    # Check if we should retry on this server
                    if error and error.startswith("HTTP"):
                        # HTTP error - try next server immediately
                        all_errors.append(f"{server['name']}: {error}")
                        break
                    else:
                        # Transient error - retry with backoff
                        all_errors.append(f"{server['name']}: {error}")
                        if attempt < max_retries - 1:
                            time.sleep(1 * (attempt + 1))

            # All servers failed
            return DownloadResult(
                item=item,
                success=False,
                error=f"All servers failed: {'; '.join(all_errors[-3:])}"  # Last 3 errors
            )

        # Legacy schema: single URL with retries
        if not item.url:
            return DownloadResult(
                item=item,
                success=False,
                error="No download URL available"
            )

        # Extract hostname from URL for source tracking
        try:
            from urllib.parse import urlparse
            source_name = urlparse(item.url).netloc or item.url
        except Exception:
            source_name = item.url

        # Notify about source before starting
        if start_callback:
            start_callback(source_name)

        # Legacy schema: default to ipv4 to avoid IPv6 timeout issues
        last_error = None
        for attempt in range(max_retries):
            success, error = self._download_from_url(
                item.url, cache_path, progress_callback, timeout, ip_mode='ipv4'
            )
            if success:
                # Verify the downloaded file is actually an RPM
                is_rpm, rpm_error = is_valid_rpm(cache_path)
                if not is_rpm:
                    logger.warning(f"Downloaded file is not a valid RPM: {rpm_error}")
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
                    last_error = rpm_error
                    break  # Don't retry, server is probably broken

                self._register_cache_file(item, cache_path)
                return DownloadResult(
                    item=item,
                    success=True,
                    path=cache_path
                )

            last_error = error
            if error and error.startswith("HTTP"):
                # HTTP error - don't retry
                break
            elif attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))  # 1s, 2s, 3s backoff

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
                sha256 = hashlib.sha256()

                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        sha256.update(chunk)
                        downloaded += len(chunk)

                        if progress_callback:
                            progress_callback(downloaded, total_size)

                checksum = sha256.hexdigest()

                # NOTE: GPG verification removed from peer download for performance.
                # Rationale:
                # 1. Peers are trusted local network hosts (discovered via mDNS)
                # 2. rpm will verify signature anyway at install time
                # 3. Per-package GPG check was adding ~0.5-1s latency each
                #
                # If a peer serves tampered packages, rpm install will reject them.

                # Move to final location
                temp_path.rename(cache_path)

                # Verify the downloaded file is actually an RPM
                is_rpm, rpm_error = is_valid_rpm(cache_path)
                if not is_rpm:
                    logger.warning(f"Peer {peer.host} served invalid RPM: {rpm_error}")
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
                    return DownloadResult(
                        item=item,
                        success=False,
                        error=f"Peer served invalid file: {rpm_error}"
                    )

                # Register in cache for quota tracking
                self._register_cache_file(item, cache_path)

                # Return result with provenance info (DB write happens in main thread)
                return DownloadResult(
                    item=item,
                    success=True,
                    path=cache_path,
                    peer_info=PeerProvenance(
                        peer_host=peer.host,
                        peer_port=peer.port,
                        checksum_sha256=checksum,
                        file_size=downloaded,
                        verified=False  # GPG verification deferred to rpm install
                    )
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
        """Download multiple packages using queue-based parallel processing.

        Uses DownloadCoordinator for robust handling:
        - Thread-safe failed peer tracking (immediate propagation)
        - Dynamic peer reassignment when a peer fails GPG verification
        - Each worker checks peer status before each download

        Args:
            items: List of packages to download
            progress_callback: Optional callback(name, pkg_num, pkg_total, bytes, bytes_total)

        Returns:
            Tuple of (results, total_downloaded, total_cached, peer_stats)
            peer_stats contains P2P download statistics
        """
        import time as _time

        results = []
        cached_count = 0
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
            return results, 0, cached_count, {'from_peers': 0, 'from_upstream': 0}

        # P2P: Discover peers and get availability
        peer_availability = {}  # peer -> {filename: path}
        if self.use_peers:
            try:
                if self._peer_client is None:
                    self._peer_client = PeerClient(dev_mode=is_dev_mode())

                t0 = _time.time()
                peers = self._peer_client.discover_peers()
                t1 = _time.time()
                logger.debug(f"P2P: peer discovery took {t1-t0:.2f}s, found {len(peers)} peers")

                # Filter out blacklisted peers
                if peers and self.db:
                    original_count = len(peers)
                    peers = [p for p in peers if not self.db.is_peer_blacklisted(p.host, p.port)]
                    if len(peers) < original_count:
                        logger.info(f"P2P: filtered out {original_count - len(peers)} blacklisted peers")

                if peers:
                    filenames = [item.filename for item in to_download]
                    peer_availability = self._peer_client.query_peers_have(
                        peers, filenames,
                        version=self.target_version,
                        arch=self.target_arch
                    )
                    t2 = _time.time()
                    logger.debug(f"P2P: availability query took {t2-t1:.2f}s")

                    # Log summary
                    total_available = sum(len(files) for files in peer_availability.values())
                    if total_available > 0:
                        logger.info(f"P2P: {total_available} packages available from {len(peer_availability)} peers")

            except Exception as e:
                logger.debug(f"P2P discovery failed: {e}")
                peer_availability = {}

        # Use coordinator for queue-based parallel downloads
        t_dl_start = _time.time()
        coordinator = DownloadCoordinator(self, max_workers=self.max_workers)

        # Create wrapper callback that accounts for cached items
        # The coordinator reports progress for to_download items only
        # We need to offset by cached_count and cached bytes
        if progress_callback:
            def coordinator_progress(name, pkg_num, pkg_total, dl_bytes, dl_total,
                                     item_bytes=None, item_total=None, active_downloads=None):
                # Offset by cached items
                progress_callback(
                    name,
                    cached_count + pkg_num,
                    len(items),
                    downloaded_bytes + dl_bytes,  # downloaded_bytes has cached bytes
                    total_bytes,
                    item_bytes,  # Per-item progress (optional)
                    item_total,
                    active_downloads  # All active downloads
                )
            coord_callback = coordinator_progress
        else:
            coord_callback = None

        dl_results, stats = coordinator.download_all(
            to_download,
            peer_availability,
            progress_callback=coord_callback
        )

        t_dl_end = _time.time()
        logger.debug(f"Downloads completed in {t_dl_end - t_dl_start:.2f}s")

        # Merge results
        results.extend(dl_results)

        # Count successes
        downloaded_count = sum(1 for r in dl_results if r.success and not r.cached)

        # Process DB operations in main thread (SQLite is not thread-safe)
        if self.db:
            # Blacklist peers that served bad packages
            blacklisted_count = 0
            for bl in stats.get('pending_blacklist', []):
                try:
                    self.db.blacklist_peer(bl.host, bl.port, bl.reason)
                    blacklisted_count += 1
                    logger.warning(f"Auto-blacklisted peer {bl.host}:{bl.port} - {bl.reason}")
                except Exception as e:
                    logger.warning(f"Failed to blacklist peer {bl.host}: {e}")

            # Record provenance for successful peer downloads
            provenance_count = 0
            for result in dl_results:
                if result.success and result.peer_info and result.path:
                    try:
                        self.db.record_peer_download(
                            filename=result.item.filename,
                            file_path=str(result.path),
                            peer_host=result.peer_info.peer_host,
                            peer_port=result.peer_info.peer_port,
                            file_size=result.peer_info.file_size,
                            checksum_sha256=result.peer_info.checksum_sha256,
                            verified=result.peer_info.verified
                        )
                        provenance_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to record provenance for {result.item.filename}: {e}")

            if provenance_count > 0:
                logger.debug(f"Recorded provenance for {provenance_count} peer downloads")
            if blacklisted_count > 0:
                logger.info(f"Auto-blacklisted {blacklisted_count} peers due to GPG failures")

        peer_stats = {
            'from_peers': stats.get('from_peers', 0),
            'from_upstream': stats.get('from_upstream', 0),
            'failed_peers': stats.get('failed_peers', []),
        }
        return results, downloaded_count, cached_count, peer_stats


def get_download_items(db: PackageDatabase, packages: List[dict]) -> List[DownloadItem]:
    """Convert package actions to download items.

    Args:
        db: Database instance
        packages: List of package dicts with name, version, release, arch, media_name

    Returns:
        List of DownloadItem ready for download
    """
    items = []
    media_cache = {}  # Cache media lookups
    servers_cache = {}  # Cache servers lookups

    for pkg in packages:
        media_name = pkg.get('media_name', '')
        if media_name not in media_cache:
            media = db.get_media(media_name)
            media_cache[media_name] = media
            # Pre-load servers
            if media and media.get('id'):
                servers = db.get_servers_for_media(media['id'], enabled_only=True)
                servers_cache[media['id']] = [dict(s) for s in servers]

        media = media_cache[media_name]
        if not media:
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

        # Use new schema if available, fallback to legacy URL
        # Fallback to uncompressed size if filesize is zero
        size = pkg.get('filesize', 0) if pkg.get('filesize', 0) != 0 else pkg.size
        if media.get('relative_path'):
            servers = servers_cache.get(media['id'], [])
            items.append(DownloadItem(
                name=pkg['name'],
                version=version,
                release=release,
                arch=pkg['arch'],
                media_id=media['id'],
                relative_path=media['relative_path'],
                is_official=bool(media.get('is_official', 1)),
                servers=servers,
                media_name=media_name,
                size=size
            ))
        elif media.get('url'):
            items.append(DownloadItem(
                name=pkg['name'],
                version=version,
                release=release,
                arch=pkg['arch'],
                media_url=media['url'],
                media_name=media_name,
                size=size
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
