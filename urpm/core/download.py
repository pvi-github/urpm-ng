"""
Package download manager

Downloads RPM packages from media sources with progress reporting.
Uses a queue-based architecture for robust parallel downloads with
dynamic peer failure tracking and reassignment.
"""

import enum
import hashlib
import logging
import os
import queue
import threading
import urllib.request
import urllib.error
import time as _time_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Callable, Tuple, Dict, Set, Iterable

import pycurl

from .database import PackageDatabase
from .config import get_base_dir, is_dev_mode
from .peer_client import (
    PeerClient, Peer, create_download_plan, summarize_download_plan,
    DownloadAssignment
)

logger = logging.getLogger(__name__)


class InsufficientSpaceError(Exception):
    """Not enough room where the RPM payload would be written.

    Raised before the first byte is fetched.  Carries the figures and
    the directory : « no space left on device » a few GB into a
    distupgrade says nothing about *which* filesystem, and by then the
    partial payload is occupying the space that ran out.

    Mentions ``download.payload_dir`` because relocating the payload is
    the fix on a machine whose ``/var`` is simply too small — a lasting
    property that no amount of cleaning changes.
    """

    def __init__(self, directory, required: int, available: int):
        from ..i18n import _
        self.directory = directory
        self.required = required
        self.available = available

        def _gb(n: int) -> str:
            return f"{n / (1024 ** 3):.1f} GB"

        super().__init__(_(
            "Not enough space in {directory}: {required} needed, "
            "{available} available.\n"
            "Free some space, or point the download elsewhere with "
            "--download-dir, or set 'payload_dir' in the [download] "
            "section of /etc/urpm/conf.d/ to move it for good."
        ).format(
            directory=directory,
            required=_gb(required),
            available=_gb(available),
        ))


# ── Network error grammar ─────────────────────────────────────────────


class DownloadErrorKind(enum.Enum):
    """How to react to a download failure.

    ``HARD_HTTP`` is a server-side rejection that will not change by
    retrying the same URL (404, 410, 451…) — the caller should move on
    to a different mirror instead of spinning on this one.

    ``TRANSIENT_NETWORK`` is a recoverable failure (DNS, timeout,
    connection reset, partial transfer cut by a proxy…); the caller is
    expected to retry on the same mirror with a backoff.

    ``LOCAL_IO`` covers cache write failures (disk full, permissions);
    retrying is pointless until the local side is fixed.

    ``UNKNOWN`` is for anything else; callers default to a single retry
    and surface the message verbatim.
    """

    HARD_HTTP = "hard_http"
    TRANSIENT_NETWORK = "transient"
    LOCAL_IO = "local_io"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DownloadError:
    """Typed result of a failed download attempt.

    ``str(err)`` returns ``message`` so existing logging / aggregation
    sites that treated the error as a string keep working without
    changes.
    """

    kind: DownloadErrorKind
    message: str
    http_code: Optional[int] = None

    def __str__(self) -> str:  # noqa: D401 — short form
        return self.message

    @property
    def is_hard(self) -> bool:
        """True when retrying the same mirror is pointless."""
        return self.kind in (
            DownloadErrorKind.HARD_HTTP, DownloadErrorKind.LOCAL_IO,
        )





# 1 MB read chunks for large files — minimises syscall count and GIL
# re-acquisition overhead that otherwise throttle TCP throughput.
# Small files still finish in one or two reads.
_CHUNK_SIZE = 1048576

# RPM magic bytes: 0xED 0xAB 0xEE 0xDB
RPM_MAGIC = b'\xed\xab\xee\xdb'


def resolve_payload_dir(payload_dir: str = None,
                        *,
                        default: Path = None) -> Path:
    """Where ``.rpm`` payloads land.

    Defaults to the cache directory, so the on-disk layout is unchanged
    unless someone asks otherwise.  Only the payload moves : the
    database and the media metadata stay put, being small and
    permanently needed.

    Precedence : explicit argument (``--download-dir``) beats the
    ``download.payload_dir`` config key beats the default.

    Shared with :mod:`urpm.core.distupgrade.root_space`, which needs to
    know whether the payload will land on the same filesystem as
    ``/usr`` before any downloader exists.  Deriving it twice is how
    the two answers would end up disagreeing.
    """
    from .settings import get_settings

    configured = (payload_dir or get_settings().download.payload_dir or '')
    if configured:
        return Path(configured).expanduser()
    return default if default is not None else get_base_dir()


#: Subdirectory of the payload directory holding ``.rpm`` files fetched
#: straight from a URL.  A sibling of ``medias/``, never inside it: that
#: tree mirrors a remote one, medium by medium, and every file in it is
#: expected to correspond to something the mirror serves.  A package
#: named by a URL belongs to no medium, so dropping it in one would
#: make the cache disagree with its source for no benefit.
STANDALONE_DIR_NAME = 'downloads'


def standalone_rpm_dir(payload_dir: str = None) -> Path:
    """Where an ``.rpm`` fetched from a URL lands.

    Args:
        payload_dir: Explicit payload directory, as for
            :func:`resolve_payload_dir`.

    Returns:
        The directory, created if missing.
    """
    directory = resolve_payload_dir(payload_dir) / STANDALONE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def fetch_standalone_rpm(url: str, payload_dir: str = None,
                         progress_callback=None) -> Tuple[Optional[Path], str]:
    """Fetch the ``.rpm`` at *url* into :func:`standalone_rpm_dir`.

    The file is rewritten on every call rather than reused when
    present: a plain drop box cannot go stale, whereas a cache keyed on
    a basename would happily serve the ``foo-1.0.rpm`` of another
    server.  Callers get a path and are expected to put it through the
    same header read and signature check as any local RPM.

    Args:
        url: ``http``, ``https`` or ``ftp`` URL ending in ``.rpm``.
        payload_dir: Explicit payload directory, or ``None``.
        progress_callback: Optional ``callback(done, total)``.

    Returns:
        ``(path, "")`` on success, ``(None, reason)`` otherwise.  The
        partial file is removed on failure, so a retry never resumes
        onto a truncated body.
    """
    from urllib.parse import unquote, urlsplit

    from ..i18n import _
    from .sync import download_file

    name = Path(unquote(urlsplit(url).path)).name
    if not name.endswith('.rpm'):
        return None, _("the URL does not name an .rpm file")

    destination = standalone_rpm_dir(payload_dir) / name
    result = download_file(url, destination,
                           progress_callback=progress_callback)
    if not result.success:
        destination.unlink(missing_ok=True)
        return None, getattr(result, 'error', None) or _("download failed")

    valid, reason = is_valid_rpm(destination)
    if not valid:
        # A mirror answering 404 with an HTML page is the common case,
        # and rpm would report something far less obvious about it.
        destination.unlink(missing_ok=True)
        return None, reason

    return destination, ""


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

    # ``rpm.TransactionSet()`` opens rpmdb env even though we only
    # use it to route ``hdrFromFdno`` through librpm.  We close it
    # explicitly in ``finally`` so the SQLite connection is released
    # before we hand back control — see :mod:`urpm.core.rpmdb` for
    # the full rationale.  This function stays out of that central
    # module because signature verification is security-critical
    # territory that belongs with the download / install stack for
    # auditability.
    ts = rpm.TransactionSet()
    try:
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
    finally:
        try:
            ts.closeDB()
        except Exception:
            pass


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
    exclude_server_ids: List[int] = field(default_factory=list)  # Servers to skip on retry

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
    source_server_id: Optional[int] = None  # Server that served this file
    # True when the server that served it was a removable disc.  Not
    # derivable afterwards from ``source_server_id`` alone without a
    # second DB read, and the worker threads must not touch SQLite.
    from_disc: bool = False


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

        # Stats.  The three counters are disjoint and sum to the
        # number of successful transfers : a file comes from a peer, a
        # mounted disc, or a mirror.
        self._from_peer_count = 0
        self._from_disc_count = 0
        self._from_upstream_count = 0
        self._stats_lock = threading.Lock()

        # Real-time download progress tracking (thread-safe)
        # Each worker has a fixed slot (0 to max_workers-1) for stable display
        self._current_progress_lock = threading.Lock()
        self._current_downloads: Dict[int, DownloadProgress] = {}  # slot -> DownloadProgress

        # Global speed tracker: accumulates bytes across ALL downloads so that
        # a meaningful speed is always available even when individual packages
        # finish too fast (< 0.1s) to collect per-slot samples.
        self._global_bytes_total = 0  # total bytes downloaded so far (all slots)
        self._global_speed_samples: List[Tuple[float, int]] = []  # [(time, cumulative_bytes)]
        self._GLOBAL_SPEED_WINDOW = 3.0  # seconds of history for speed calc

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

    def update_global_speed(self, completed_bytes: int):
        """Record a global speed sample based on total completed + in-flight bytes.

        Call this from the poll loop in download_all() at each tick.
        The sample records *completed_bytes* (fully finished downloads)
        plus the sum of bytes_done across all active slots.

        Args:
            completed_bytes: Total bytes from fully completed downloads.
        """
        with self._current_progress_lock:
            partial = sum(p.bytes_done for p in self._current_downloads.values())
        total = completed_bytes + partial

        now = _time_mod.time()
        self._global_speed_samples.append((now, total))

        # Prune samples older than the window
        cutoff = now - self._GLOBAL_SPEED_WINDOW
        while (self._global_speed_samples
               and self._global_speed_samples[0][0] < cutoff):
            self._global_speed_samples.pop(0)

    def get_global_speed(self) -> float:
        """Calculate global download speed in bytes/sec over recent window.

        Returns 0.0 if fewer than 2 samples are available.
        """
        if len(self._global_speed_samples) < 2:
            return 0.0
        oldest_time, oldest_bytes = self._global_speed_samples[0]
        newest_time, newest_bytes = self._global_speed_samples[-1]
        elapsed = newest_time - oldest_time
        if elapsed <= 0:
            return 0.0
        return (newest_bytes - oldest_bytes) / elapsed

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
                    if result.success and not result.cached:
                        if result.from_peer:
                            self._from_peer_count += 1
                        elif result.from_disc:
                            self._from_disc_count += 1
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

        # Track the last peer failure so we can surface it if we end up
        # refusing to fall back to upstream.  Without this, --only-peers
        # failures all look like "Not available from peers" even when the
        # peer answered /api/have positively and the actual failure was
        # something more specific (HTTP 404, network, signature mismatch…).
        last_peer_error: Optional[str] = None

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

                    # Peer failed - remember why for later diagnostics
                    last_peer_error = f"{peer.host}: {result.error or 'unknown error'}"

                    # Check if GPG issue (should blacklist)
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
                        last_peer_error = f"{alt_peer.host}: {result.error or 'unknown error'}"
                        if result.blacklist_peer:
                            self.mark_peer_failed(alt_peer, result.blacklist_peer.reason)

            # Fall back to upstream - unless only_peers mode
            if self.downloader.only_peers:
                # Only peers mode: fail if no peer has the package
                logger.debug(f"only_peers mode: skipping upstream for {item.filename}")
                if last_peer_error:
                    error_msg = (
                        f"peer download failed ({last_peer_error}), "
                        f"upstream disabled (--only-peers)"
                    )
                else:
                    error_msg = "no peer has this package (--only-peers mode)"
                return DownloadResult(
                    item=item,
                    success=False,
                    error=error_msg,
                )

            # Download from upstream - source will be set by download_one via callback
            result = self.downloader.download_one(
                item, progress_callback=progress_cb,
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
        # Removable sources become located ``file`` servers before
        # anything is planned: the planner can only prefer a disc it
        # can see, and the mount table is read once here for the whole
        # batch rather than once per package.
        self.downloader.resolve_removable_sources(items)

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

        # Queue work: pre-assigned to servers by proportional bandwidth
        # allocation, then interleaved so consecutive items hit different
        # servers — workers naturally spread across mirrors.
        sorted_items = sorted(items, key=lambda it: it.size or 0, reverse=True)

        # Build speed map from session EWMA + DB fallback
        with self.downloader._session_stats_lock:
            speed_snapshot = dict(self.downloader._session_server_kbps)
        _plan_server_assignments(sorted_items, speed_snapshot)

        # Interleave items by assigned server (round-robin across server
        # queues) so that N workers picking items sequentially each hit a
        # different server.
        interleaved = _interleave_by_server(sorted_items)

        for item in interleaved:
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

        # Mid-session replanning state.  After _REPLAN_AFTER results we
        # check whether actual server speeds diverge from the plan — if
        # so, drain the queue and redistribute remaining items.
        _REPLAN_AFTER = 20          # min results before first replan
        _REPLAN_MIN_REMAINING = 20  # don't replan for a handful of items
        _REPLAN_DRIFT = 0.30        # 30% speed drift triggers replan
        _replanned = False

        # Poll results queue in real-time instead of waiting for join()
        while len(results) < total_items:
            try:
                # Short timeout to stay responsive and allow progress updates
                result = self._results_queue.get(timeout=0.1)
                results.append(result)

                # ── Mid-session replan check ────────────────────────
                if (not _replanned
                        and len(results) >= _REPLAN_AFTER
                        and total_items - len(results) >= _REPLAN_MIN_REMAINING):
                    _replanned = _maybe_replan(
                        self._work_queue, self.downloader, assignments,
                        _REPLAN_DRIFT)

                if result.success:
                    completed_bytes += result.item.size

                # Update global speed tracker (used when per-slot speed is 0)
                self.update_global_speed(completed_bytes)

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
                            slots_status,  # Pass all slots (active and inactive)
                            self.get_global_speed()
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

                # Update global speed tracker even when no result arrived
                self.update_global_speed(completed_bytes)

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
                            slots_status,  # Pass all slots (active and inactive)
                            self.get_global_speed()
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
            'from_discs': self._from_disc_count,
            'from_upstream': self._from_upstream_count,
            'failed_peers': list(self._failed_peers),
            'pending_blacklist': self._pending_blacklist,
        }

        return results, stats


def _assign_local_sources(items: List['DownloadItem']) -> List['DownloadItem']:
    """Send every item a local source can serve to that source.

    A local source — a disc :func:`urpm.core.removable.resolve` has
    found, or a ``file://`` tree — is not a bandwidth competitor.  It
    costs no network, and nothing of the shared Mageia mirrors'
    capacity, so there is no arithmetic to do : it takes what it can
    serve and the proportional allocation shares out the rest.

    Measured on a real 9 → 10 distupgrade before this existed : of
    2747 packages, the mounted disc served 359 and the mirrors 2388,
    while the disc carried 69% of what was being installed.  The
    reason was the planner treating it as the slowest mirror in the
    fleet, because a ``cdrom`` row has no measured bandwidth and the
    fallback was 1 KB/s against mirrors at 74 to 1942.  Trying to give
    it a speed instead would have been the wrong repair: for a disc
    the throughput is beside the point, nobody inserting one expects a
    rate to be negotiated.

    The two streams then run at the same time, so the wall clock is
    the longer of the two rather than their sum.  What the disc cannot
    serve it is not even a candidate for: an updated package is a
    different NEVRA in ``Core Updates``, a medium no pressing is
    attached to.

    Returns:
        The items left for the bandwidth planner, in input order.
    """
    from .config import is_local_server

    remaining: List['DownloadItem'] = []
    local_count = 0
    for item in items:
        local = next((s for s in item.servers if is_local_server(s)), None)
        if local is None:
            remaining.append(item)
            continue
        # First in the list is all it takes : ``download_one`` tries
        # them in order, so a disc pulled out mid-run still falls
        # through to the mirrors behind it.
        item.servers.sort(
            key=lambda s, _id=local.get('id'): 0 if s.get('id') == _id else 1
        )
        local_count += 1

    if local_count:
        logger.info(
            "Download plan: %d of %d items from a local source, "
            "%d left to the mirrors",
            local_count, len(items), len(remaining))
    return remaining


def _plan_server_assignments(items: List['DownloadItem'],
                             server_speeds: Dict[int, float]) -> None:
    """Pre-assign items to servers using proportional bandwidth allocation.

    Computes a theoretical download time *t_target* assuming all servers
    work in parallel at their measured speed, then assigns each server a
    byte budget proportional to its speed.  Items (expected biggest-first)
    are greedily placed into the fastest server that still has budget,
    ensuring fast servers handle the bulk of the volume while slow ones
    only get small packages.

    The result is applied by **reordering each item's** ``servers`` list
    so the assigned server appears first — ``download_one`` simply tries
    servers in list order, giving natural failover to the next-best.

    A local source does not take part in any of this : see
    :func:`_assign_local_sources`.

    Args:
        items: Download items, ideally sorted by size descending.
        server_speeds: ``{server_id: KB/s}`` from session EWMA or DB.
    """
    # Anything a mounted disc or a local tree can serve is settled
    # before the bandwidth arithmetic, and leaves it.
    items = _assign_local_sources(items)
    if not items:
        return

    # Collect all unique servers with their best speed estimate
    speed_map: Dict[int, float] = {}  # server_id → KB/s
    for item in items:
        for s in item.servers:
            sid = s.get('id')
            if sid is not None and sid not in speed_map:
                speed = server_speeds.get(sid, s.get('bandwidth_kbps') or 1)
                speed_map[sid] = max(speed, 1)

    if len(speed_map) < 2:
        return  # Single server or none — nothing to plan

    # Servers sorted by speed descending
    sorted_sids = sorted(speed_map, key=speed_map.get, reverse=True)

    # Target time = total_bytes / total_throughput
    total_bytes = sum(item.size or 0 for item in items)
    total_kbps = sum(speed_map[sid] for sid in sorted_sids)
    if total_kbps <= 0 or total_bytes <= 0:
        return

    t_target = total_bytes / (total_kbps * 1024)  # seconds

    # Byte budget per server: V(i) = t_target × D(i)
    remaining: Dict[int, float] = {
        sid: t_target * speed_map[sid] * 1024
        for sid in sorted_sids
    }

    # First pass — assign each item to the fastest server with budget
    assigned: Dict[int, int] = {}   # item_index → server_id
    unassigned: List[int] = []

    for i, item in enumerate(items):
        item_sids = {s.get('id') for s in item.servers if s.get('id') is not None}
        placed = False
        for sid in sorted_sids:
            if sid in item_sids and remaining[sid] > 0:
                assigned[i] = sid
                remaining[sid] -= (item.size or 0)
                placed = True
                break
        if not placed:
            unassigned.append(i)

    # Second pass — leftovers go to the server with the most remaining budget
    for i in unassigned:
        item_sids = {s.get('id') for s in items[i].servers
                     if s.get('id') is not None}
        best_sid = max(
            (sid for sid in sorted_sids if sid in item_sids),
            key=lambda sid: remaining[sid],
            default=None,
        )
        if best_sid is not None:
            assigned[i] = best_sid
            remaining[best_sid] -= (items[i].size or 0)

    # Apply — reorder each item's servers list so the assigned one is first
    for i, item in enumerate(items):
        preferred = assigned.get(i)
        if preferred is not None:
            item.servers.sort(
                key=lambda s, _pref=preferred: 0 if s.get('id') == _pref else 1
            )

    logger.info(
        "Download plan: %d items across %d servers, target %.1fs "
        "(budgets: %s)",
        len(items), len(speed_map), t_target,
        ", ".join(f"{sid}={b / 1048576:.0f}MB"
                  for sid, b in zip(sorted_sids,
                                    (t_target * speed_map[s] * 1024
                                     for s in sorted_sids)))
    )


def _interleave_by_server(items: List['DownloadItem']) -> List['DownloadItem']:
    """Reorder items so consecutive entries hit different servers.

    Groups items by their first (preferred) server, then yields one item
    from each group in round-robin order.  Within each group the original
    order (biggest-first) is preserved so each server starts with its
    heaviest file.

    This ensures that *N* workers pulling items from the queue each hit
    a different server, naturally avoiding connection storms on a single
    mirror.
    """
    from collections import defaultdict, deque

    buckets: Dict[int, deque] = defaultdict(deque)
    no_server: List['DownloadItem'] = []

    for item in items:
        # First server in the list is the planned preferred one
        if item.servers:
            sid = item.servers[0].get('id')
            if sid is not None:
                buckets[sid].append(item)
                continue
        no_server.append(item)

    # Round-robin across server buckets
    result: List['DownloadItem'] = []
    active = list(buckets.values())
    while active:
        next_round = []
        for bucket in active:
            result.append(bucket.popleft())
            if bucket:
                next_round.append(bucket)
        active = next_round

    # Items without a server go at the end
    result.extend(no_server)
    return result


def _maybe_replan(work_queue, downloader, assignments, drift_threshold):
    """Drain the work queue and replan if server speeds drifted significantly.

    Compares session-measured speeds to the DB-stored speeds that the
    original plan was based on.  If any server's actual speed differs by
    more than *drift_threshold* (fraction), the remaining items are
    re-planned with the fresh speed data and re-interleaved.

    Must be called from the main thread.  Workers block naturally on the
    empty queue (0.5 s timeout) during the brief drain-and-refill window.

    Returns:
        True if a replan was performed, False otherwise.
    """
    with downloader._session_stats_lock:
        session_kbps = dict(downloader._session_server_kbps)

    if not session_kbps:
        return False

    # Check drift: compare session speeds to what the DB had at plan time
    significant_drift = False
    for sid, measured in session_kbps.items():
        if downloader.db:
            server = downloader.db.get_server_by_id(sid)
            if server:
                stored = server.get('bandwidth_kbps') or measured
                if stored > 0:
                    ratio = abs(measured - stored) / stored
                    if ratio > drift_threshold:
                        significant_drift = True
                        logger.info(
                            "Server %s drifted %.0f%%: %d → %d KB/s",
                            server.get('name', sid),
                            ratio * 100, stored, measured)
                        break

    if not significant_drift:
        return False

    # Drain remaining items from the queue (fast — just dequeue)
    remaining = []
    while True:
        try:
            item, assignment = work_queue.get_nowait()
            remaining.append((item, assignment))
        except queue.Empty:
            break

    if len(remaining) < 10:
        # Not worth replanning — put them back
        for item, assignment in remaining:
            work_queue.put((item, assignment))
        return False

    # Re-plan with actual measured speeds
    items_only = [item for item, _ in remaining]
    items_only.sort(key=lambda it: it.size or 0, reverse=True)
    _plan_server_assignments(items_only, session_kbps)
    interleaved = _interleave_by_server(items_only)

    for item in interleaved:
        assignment = assignments.get(item.filename)
        work_queue.put((item, assignment))

    logger.info("Replanned %d remaining items with fresh speed data",
                len(interleaved))
    return True


def cache_path_for(item: DownloadItem, payload_dir: Path, *,
                   create: bool = False) -> Path:
    """Where *item* lives, or would live, under *payload_dir*.

    New schema: ``<payload_dir>/medias/official/<relative_path>/*.rpm``
                ``<payload_dir>/medias/custom/<short_name>/*.rpm``
    Legacy:     ``<payload_dir>/medias/<hostname>/<media_name>/*.rpm``

    Anchored on the payload directory rather than the cache directory so
    a relocated payload keeps the same layout.

    *create* makes the parent directories.  A download needs that; a
    probe does not, and asking for one before the user has confirmed
    the transaction would leave empty directories behind on a refusal.

    The schema is decided on ``relative_path`` alone, not on
    :meth:`DownloadItem.uses_new_schema`, which also requires a
    non-empty server list.  That is the right question for *fetching* —
    no mirror, nothing to fetch — and the wrong one for a path: where a
    file lives cannot depend on how many mirrors happen to serve it.
    The probe runs before the mirror pool is enlarged, so an item can
    legitimately reach here with no server yet and must still be looked
    for where the download will later write it.  ``build_download_items``
    fills ``relative_path`` and ``media_url`` in mutually exclusive
    branches, so the two layouts never compete.
    """
    base = payload_dir
    if item.relative_path:
        if item.is_official:
            media_dir = base / "medias" / "official" / item.relative_path
        else:
            media_dir = base / "medias" / "custom" / item.media_name
    elif item.media_name and item.media_url:
        media_dir = base / "medias" / item.hostname / item.media_name
    else:
        return base / item.filename
    if create:
        media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir / item.filename


def is_cached_at(item: DownloadItem, payload_dir: Path) -> bool:
    """Whether *item* is already on the disk under *payload_dir*.

    Verifies that the file exists, is not empty, and carries the RPM
    magic bytes, which is what separates a usable payload from a
    truncated or corrupted download.  Signature verification happens at
    install time.
    """
    path = cache_path_for(item, payload_dir)
    if not path.exists():
        return False
    if path.stat().st_size == 0:
        return False
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
        return magic == RPM_MAGIC
    except OSError:
        return False


def cached_payload_bytes(items: Iterable[DownloadItem],
                         payload_dir: Path) -> int:
    """Bytes of *items* that will not cross the network.

    Two syscalls and a four-byte read per package, so it is affordable
    before the confirmation prompt — which is the only place the figure
    is worth anything.  Announcing the whole payload to someone whose
    cache already holds most of it answers a question nobody asked.
    """
    return sum(item.size or 0 for item in items
               if is_cached_at(item, payload_dir))


class Downloader:
    """Download manager for RPM packages."""

    def __init__(self, cache_dir: Path = None,
                 use_peers: bool = True, only_peers: bool = False,
                 db: 'PackageDatabase' = None,
                 target_version: str = None, target_arch: str = None,
                 payload_dir: str = None):
        """Initialize downloader.

        Args:
            cache_dir: Directory to store downloaded RPMs
            use_peers: Whether to use P2P peer discovery for downloads
            only_peers: If True, only download from peers (no upstream fallback)
            db: Database for provenance tracking and blacklist (optional)
            target_version: Target Mageia version for P2P queries (e.g., "10")
            target_arch: Target architecture for P2P queries (e.g., "x86_64")
            payload_dir: Override for where ``.rpm`` files are written.
                Wins over the ``download.payload_dir`` config key.
                ``None`` falls back to that key, then to *cache_dir*.

        Note:
            The number of parallel workers is read from
            ``settings.download.parallel`` (defaults to 4); the prior
            ``max_workers`` constructor argument was always overridden
            and is gone.
        """
        self.cache_dir = cache_dir or get_base_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        from .settings import get_settings
        _settings = get_settings()
        self.max_workers = _settings.download.parallel
        self.payload_dir = resolve_payload_dir(payload_dir,
                                               default=self.cache_dir)
        if self.payload_dir != self.cache_dir:
            self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.use_peers = use_peers or only_peers  # only_peers implies use_peers
        self.only_peers = only_peers
        self.db = db
        self.target_version = target_version
        self.target_arch = target_arch
        self._peer_client: Optional[PeerClient] = None

        # In-session server performance tracking (server_id → measured KB/s).
        # Accumulated across downloads in a single urpm invocation so that a
        # slow server identified early is deprioritised for subsequent packages,
        # without waiting for the next session.
        self._session_server_kbps: Dict[int, float] = {}
        self._session_stats_lock = threading.Lock()

        # Active download slots per server (guard-rail for max_per_server).
        self._server_active_slots: Dict[int, int] = {}
        self._server_slots_lock = threading.Lock()

        # Pending cache registrations — collected from worker threads,
        # flushed on the main thread after all downloads complete.
        # This avoids "database is locked" errors from concurrent SQLite
        # writes during parallel downloads.
        # ``(item, cache_path, source_server_id_or_None)`` — server_id
        # is carried through to ``cache_files.served_by_server_id`` so
        # the install retry path can exclude the source mirror on a
        # corrupt-RPM detection even across process restarts.
        self._pending_cache_registrations: List[
            Tuple[DownloadItem, Path, Optional[int]]
        ] = []
        self._pending_cache_lock = threading.Lock()


    def get_cache_path(self, item: DownloadItem) -> Path:
        """Where *item* is written, creating the directory on the way.

        Delegates to :func:`cache_path_for`, which the pre-confirmation
        cache probe also goes through: the two must never disagree on
        where a file lives, and a second copy of the layout rules is
        how they would come to.
        """
        return cache_path_for(item, self.payload_dir, create=True)

    def is_cached(self, item: DownloadItem) -> bool:
        """Whether *item* is already on the disk and usable."""
        return is_cached_at(item, self.payload_dir)

    def _register_cache_file(self, item: DownloadItem, cache_path: Path,
                             served_by_server_id: Optional[int] = None):
        """Register a downloaded file in the cache database for quota tracking.

        Args:
            item: Download item with media info
            cache_path: Path where the file was saved
            served_by_server_id: ID of the server that served the file,
                or None for peer-served bodies.  Persisted to
                ``cache_files.served_by_server_id``.
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
                    file_size=file_size,
                    served_by_server_id=served_by_server_id,
                )
                logger.debug(f"Registered cache file: {item.filename} ({file_size} bytes)")
        except Exception as e:
            # Don't fail the download if cache registration fails — the
            # file is already on disk and usable; the cache_files row is
            # advisory (used for LRU eviction and quota stats).  Surface
            # the stacktrace so a schema drift or DB lock issue does
            # not silently corrode the table.
            logger.warning(
                "Failed to register cache file %s: %s",
                item.filename, e, exc_info=True,
            )

    def _download_from_url(self, url: str, cache_path: Path,
                            progress_callback: Callable[[int, int], None] = None,
                            timeout: int = 30,
                            ip_mode: str = 'auto') -> Tuple[bool, Optional[DownloadError]]:
        """Download a file from URL to cache path.

        Uses **pycurl** (libcurl C bindings) so the entire transfer runs
        in native code without holding the GIL.  This matches wget/curl
        throughput even under heavy multithreading.

        Args:
            url: URL to download
            cache_path: Where to save the file
            progress_callback: Optional callback(downloaded, total)
            timeout: Connection timeout in seconds
            ip_mode: 'auto', 'ipv4', 'ipv6', or 'dual'

        Returns:
            ``(True, None)`` on success, ``(False, DownloadError)`` on
            failure.  The :class:`DownloadError` carries the
            classification (``HARD_HTTP`` vs ``TRANSIENT_NETWORK`` vs
            ``LOCAL_IO``) so callers can decide whether to retry on the
            same mirror or move on.
        """
        temp_path = cache_path.with_suffix('.tmp')
        c = pycurl.Curl()
        f = None
        try:
            f = open(temp_path, 'wb')

            c.setopt(pycurl.URL, url)
            c.setopt(pycurl.WRITEDATA, f)
            c.setopt(pycurl.FOLLOWLOCATION, 1)
            c.setopt(pycurl.MAXREDIRS, 5)
            c.setopt(pycurl.CONNECTTIMEOUT, timeout)
            c.setopt(pycurl.LOW_SPEED_LIMIT, 1024)   # 1 KB/s minimum
            c.setopt(pycurl.LOW_SPEED_TIME, timeout)  # abort if below min
            c.setopt(pycurl.USERAGENT, 'urpm/0.7')
            c.setopt(pycurl.HTTPHEADER, ['Accept-Encoding: identity'])
            # 1 MB receive buffer — keeps TCP window open
            c.setopt(pycurl.BUFFERSIZE, _CHUNK_SIZE)
            c.setopt(pycurl.NOSIGNAL, 1)  # thread-safe

            # IP version preference
            if ip_mode == 'ipv6':
                c.setopt(pycurl.IPRESOLVE, pycurl.IPRESOLVE_V6)
            elif ip_mode in ('ipv4', 'auto', 'dual'):
                c.setopt(pycurl.IPRESOLVE, pycurl.IPRESOLVE_V4)

            # Progress callback — called by libcurl from C, lightweight
            if progress_callback:
                c.setopt(pycurl.NOPROGRESS, 0)
                def _progress(dl_total, dl_now, _ul_total, _ul_now):
                    progress_callback(int(dl_now), int(dl_total))
                c.setopt(pycurl.XFERINFOFUNCTION, _progress)
            else:
                c.setopt(pycurl.NOPROGRESS, 1)

            c.perform()

            http_code = c.getinfo(pycurl.HTTP_CODE)
            if http_code >= 400:
                return False, DownloadError(
                    kind=DownloadErrorKind.HARD_HTTP,
                    message=f"HTTP {http_code}",
                    http_code=http_code,
                )

            f.close()
            f = None
            temp_path.rename(cache_path)
            return True, None

        except pycurl.error as e:
            # libcurl error → transient by default (DNS, timeout, connreset,
            # partial transfer truncated by a proxy, …).  Distinguishing
            # the rare hard-pycurl cases is not worth the table; we let the
            # caller retry once or twice.
            #
            # Except on a ``file://`` source, where there is no network
            # between us and the bytes : the failure is the file not
            # being readable, and asking again cannot change that.
            # This matters because a pressing carries a *subset* of the
            # media it serves (4489 of Core Release's 38861 packages,
            # measured on a Mageia 10 disc), so a miss is the normal
            # case.  Left transient, each one would cost three attempts
            # and three seconds of backoff before falling through to a
            # mirror.
            code, msg = e.args
            kind = (DownloadErrorKind.LOCAL_IO
                    if url.startswith('file://')
                    else DownloadErrorKind.TRANSIENT_NETWORK)
            return False, DownloadError(
                kind=kind,
                message=f"libcurl error: {msg}",
            )
        except (OSError, IOError) as e:
            # Local-side write failure (cache fs full, permissions, …).
            # Retrying the same mirror will not help.
            return False, DownloadError(
                kind=DownloadErrorKind.LOCAL_IO,
                message=str(e),
            )
        # Other unexpected exceptions (programmer error in pycurl
        # handling, ResourceWarning, …) propagate — silencing them
        # here masked real bugs as "download failed" in past releases.
        finally:
            c.close()
            if f is not None:
                f.close()
            # Clean up temp on failure
            if temp_path.exists() and not cache_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _build_package_url(self, server: dict, relative_path: str, filename: str) -> str:
        """Build full package URL from server info.

        *relative_path* is the media's layout as every mirror serves
        it.  A server that serves the same media at its own layout —
        a pressed disc, the only case — carries the override on
        ``media_relative_path``, and it wins.  See
        :func:`urpm.core.config.media_relative_path`.
        """
        from .config import build_server_url
        base_url = build_server_url(server)
        path = server.get('media_relative_path') or relative_path
        return f"{base_url}/{path}/{filename}"

    @staticmethod
    def _resolve_removable_servers(servers: List[dict],
                                   relative_path: str) -> List[dict]:
        """Point the removable sources at the disc that is mounted now.

        A ``cdrom`` server is an identity, not a location : nothing is
        stored about where the disc sits, because nothing about that is
        stable.  Here each one becomes an ordinary ``file`` server
        aimed at the mount point it was just found at, so the transfer
        below neither knows nor cares that a disc is involved.

        Sources whose disc is absent are dropped from the list.  That
        is deliberate for now : a missing disc means one fewer origin,
        and the operation proceeds on the mirrors.  Asking the operator
        to insert it belongs to the decision layer
        (doc/NOTES_AMOVIBLE_MEDIA.md §6.2), which must count what the
        disc would cover before it can ask a question worth answering,
        and that count does not exist this far down.

        The mount table is read once for the whole list, and only when
        a removable source is actually present, so the ordinary
        mirror-only path costs nothing.
        """
        from .removable import is_removable_server, mounted_removable, resolve

        if not any(is_removable_server(s) for s in servers):
            return servers

        mounts = mounted_removable()
        resolved = []
        for server in servers:
            if not is_removable_server(server):
                resolved.append(server)
                continue
            located = resolve(
                server,
                server.get('media_relative_path') or relative_path,
                mounts,
            )
            if located is not None:
                resolved.append(located)
            else:
                logger.debug(
                    f"Removable source {server['name']} skipped: "
                    f"no mounted disc carries this media")
        return resolved

    @staticmethod
    def resolve_removable_sources(items: List[DownloadItem]) -> None:
        """Resolve the removable sources of a whole batch, in place.

        Same work as :meth:`_resolve_removable_servers`, hoisted to the
        batch for two reasons.  The planner has to see a *located* disc
        to prefer it, and an unresolved ``cdrom`` row carries no
        location to prefer.  And the mount table was being read once
        per package: 2747 times on a measured distupgrade, where once
        says the same thing.

        Each item keeps its own server dicts, since the planner
        reorders them per item, but the lookup is cached per
        ``(server, layout)`` pair so a medium's disc is located once
        however many packages come from it.

        Items whose disc is absent simply lose that source and proceed
        on their mirrors, as in the single-item path.
        """
        from .removable import is_removable_server, mounted_removable, resolve

        if not any(is_removable_server(server)
                   for item in items for server in item.servers):
            return

        mounts = mounted_removable()
        located: Dict[Tuple[Any, str], Optional[dict]] = {}
        for item in items:
            if not any(is_removable_server(s) for s in item.servers):
                continue
            resolved = []
            for server in item.servers:
                if not is_removable_server(server):
                    resolved.append(server)
                    continue
                layout = (server.get('media_relative_path')
                          or item.relative_path)
                key = (server.get('id'), layout)
                if key not in located:
                    located[key] = resolve(server, layout, mounts)
                    if located[key] is None:
                        logger.debug(
                            f"Removable source {server['name']} skipped: "
                            f"no mounted disc carries {layout}")
                if located[key] is not None:
                    resolved.append(dict(located[key]))
            item.servers = resolved

    def download_one(self, item: DownloadItem,
                     progress_callback: Callable[[int, int], None] = None,
                     timeout: int = 30,
                     max_retries: int = 3,
                     start_callback: Callable[[str], None] = None) -> DownloadResult:
        """Download a single package with multi-server failover.

        For new schema: tries each server in priority order with retries.
        For legacy schema: uses single media_url with retries.

        Args:
            item: Package to download
            progress_callback: Optional callback(downloaded, total)
            timeout: Connection timeout in seconds
            max_retries: Max retry attempts per server for transient errors
            start_callback: Optional callback(source_name) called when starting download

        Returns:
            DownloadResult with status
        """
        import time
        import socket

        from .config import is_local_server
        from .removable import was_resolved_from_disc

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

            # Removable sources are identities until the disc is found.
            # Done before any other filtering so everything below sees
            # ordinary file:// servers, with their row id intact.  A
            # no-op when the batch was already resolved by
            # ``resolve_removable_sources``, since a located disc is a
            # ``file`` server and no longer answers to
            # ``is_removable_server``.
            servers = self._resolve_removable_servers(
                servers, item.relative_path)
            if not servers:
                return DownloadResult(
                    item=item,
                    success=False,
                    error=("No source available: every server for this media "
                           "is a removable disc and none is mounted")
                )

            # Server order is pre-planned by _plan_server_assignments()
            # (proportional bandwidth allocation).  Here we only apply the
            # max_per_server guard-rail: skip servers that already have too
            # many active downloads, preserving the planned fallback order.
            from .settings import get_settings
            max_per = get_settings().download.max_per_server
            with self._server_slots_lock:
                available = [
                    s for s in servers
                    if self._server_active_slots.get(s.get('id'), 0) < max_per
                ]
            if available:
                servers = available
            # else: all saturated — keep original order as fallback

            # Filter out excluded servers (used on retry to skip failing servers)
            if item.exclude_server_ids:
                servers = [s for s in servers
                           if s.get('id') not in item.exclude_server_ids]
                if not servers:
                    return DownloadResult(
                        item=item,
                        success=False,
                        error="All servers excluded by exclude_server_ids"
                    )

            all_errors = []
            for server in servers:
                url = self._build_package_url(server, item.relative_path, item.filename)
                ip_mode = server.get('ip_mode', 'auto')
                server_id = server.get('id')
                logger.debug(f"Trying server {server['name']} (ip_mode={ip_mode}): {url}")

                if start_callback:
                    start_callback(server['name'])

                # Track active slots for this server (guard-rail accounting)
                if server_id:
                    with self._server_slots_lock:
                        self._server_active_slots[server_id] = (
                            self._server_active_slots.get(server_id, 0) + 1)
                try:
                    # Try this server with retries
                    for attempt in range(max_retries):
                        t_start = time.time()
                        success, error = self._download_from_url(
                            url, cache_path, progress_callback, timeout,
                            ip_mode=ip_mode
                        )
                        elapsed = time.time() - t_start

                        if success:
                            # Verify the downloaded file is actually an RPM
                            is_rpm, rpm_error = is_valid_rpm(cache_path)
                            if not is_rpm:
                                logger.warning(
                                    f"Downloaded file is not a valid RPM: "
                                    f"{rpm_error}")
                                try:
                                    cache_path.unlink()
                                except OSError:
                                    pass
                                all_errors.append(
                                    f"{server['name']}: {rpm_error}")
                                if server_id and self.db:
                                    self.db.update_server_stats(
                                        server_id, success=False)
                                break  # Try next server

                            # Compute bandwidth from file size and wall-clock
                            # time.  Only record if elapsed >= 0.5s to avoid
                            # noise from tiny/cached files.
                            if server_id and elapsed >= 0.5:
                                try:
                                    file_size = cache_path.stat().st_size
                                    kbps = int(file_size / elapsed / 1024)
                                except OSError:
                                    kbps = None

                                if kbps and kbps > 0:
                                    with self._session_stats_lock:
                                        prev = self._session_server_kbps.get(
                                            server_id)
                                        self._session_server_kbps[server_id] = (
                                            kbps if prev is None
                                            else int(0.3 * kbps + 0.7 * prev)
                                        )
                                    if self.db:
                                        self.db.update_server_stats(
                                            server_id, bandwidth_kbps=kbps,
                                            success=True)
                            elif server_id and self.db:
                                self.db.update_server_stats(
                                    server_id, success=True)

                            logger.info(
                                f"Downloaded {item.filename} from "
                                f"{server['name']}")
                            # Defer cache registration to main thread
                            # to avoid SQLite "database is locked" errors.
                            # ``server_id`` flows through so the
                            # provenance lands in
                            # ``cache_files.served_by_server_id``.
                            with self._pending_cache_lock:
                                self._pending_cache_registrations.append(
                                    (item, cache_path, server_id))
                            return DownloadResult(
                                item=item,
                                success=True,
                                path=cache_path,
                                source_server_id=server_id,
                                from_disc=was_resolved_from_disc(server)
                            )

                        # Download failed on this attempt
                        all_errors.append(f"{server['name']}: {error}")
                        if error is not None and error.is_hard:
                            # Hard failure (4xx/5xx or local I/O) — no
                            # point retrying this server, move on to
                            # the next mirror.
                            #
                            # A local source is not charged for it: a
                            # pressing serves a subset of its media by
                            # construction, so a file it does not carry
                            # is a miss and not a fault.  Counting those
                            # would show a disc with tens of thousands
                            # of failures in ``urpm server list``.
                            if (server_id and self.db
                                    and not is_local_server(server)):
                                self.db.update_server_stats(
                                    server_id, success=False)
                            break
                        else:
                            # Transient error — retry with backoff
                            if attempt < max_retries - 1:
                                time.sleep(1 * (attempt + 1))
                    else:
                        # Exhausted retries on this server
                        if server_id and self.db:
                            self.db.update_server_stats(
                                server_id, success=False)
                finally:
                    # Release active slot for this server
                    if server_id:
                        with self._server_slots_lock:
                            cur = self._server_active_slots.get(server_id, 1)
                            self._server_active_slots[server_id] = max(
                                0, cur - 1)

            # All servers failed
            return DownloadResult(
                item=item,
                success=False,
                error=f"All servers failed: {'; '.join(all_errors[-3:])}"
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

                # Defer cache registration to main thread.  This is
                # the legacy URL download path (no server pool, hence
                # no provenance id).
                with self._pending_cache_lock:
                    self._pending_cache_registrations.append(
                        (item, cache_path, None))
                return DownloadResult(
                    item=item,
                    success=True,
                    path=cache_path
                )

            last_error = error
            if error is not None and error.is_hard:
                # Hard failure (4xx/5xx or local I/O) — retrying the
                # same URL won't help.
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

        temp_path = cache_path.with_suffix('.tmp')
        c = pycurl.Curl()
        f = None
        try:
            f = open(temp_path, 'wb')

            c.setopt(pycurl.URL, url)
            c.setopt(pycurl.WRITEDATA, f)
            c.setopt(pycurl.FOLLOWLOCATION, 1)
            c.setopt(pycurl.MAXREDIRS, 5)
            c.setopt(pycurl.CONNECTTIMEOUT, timeout)
            c.setopt(pycurl.LOW_SPEED_LIMIT, 1024)
            c.setopt(pycurl.LOW_SPEED_TIME, timeout)
            c.setopt(pycurl.USERAGENT, 'urpm/0.7')
            c.setopt(pycurl.BUFFERSIZE, _CHUNK_SIZE)
            c.setopt(pycurl.NOSIGNAL, 1)

            if progress_callback:
                c.setopt(pycurl.NOPROGRESS, 0)
                def _progress(dl_total, dl_now, _ut, _un):
                    progress_callback(int(dl_now), int(dl_total))
                c.setopt(pycurl.XFERINFOFUNCTION, _progress)
            else:
                c.setopt(pycurl.NOPROGRESS, 1)

            c.perform()

            http_code = c.getinfo(pycurl.HTTP_CODE)
            if http_code >= 400:
                return DownloadResult(
                    item=item, success=False,
                    error=f"Peer HTTP {http_code}")

            f.close()
            f = None

            # SHA256 checksum — computed after download (fast on SSD)
            sha256 = hashlib.sha256()
            with open(temp_path, 'rb') as hf:
                while True:
                    block = hf.read(_CHUNK_SIZE)
                    if not block:
                        break
                    sha256.update(block)
            checksum = sha256.hexdigest()
            downloaded = temp_path.stat().st_size

            temp_path.rename(cache_path)

            # Verify the downloaded file is actually an RPM
            is_rpm, rpm_error = is_valid_rpm(cache_path)
            if not is_rpm:
                logger.warning(
                    f"Peer {peer.host} served invalid RPM: {rpm_error}")
                try:
                    cache_path.unlink()
                except OSError:
                    pass
                return DownloadResult(
                    item=item, success=False,
                    error=f"Peer served invalid file: {rpm_error}")

            # Verify GPG signature — don't cache tampered RPMs from peers
            sig_ok = verify_rpm_signature(cache_path)
            if not sig_ok:
                logger.warning(
                    "P2P download %s: signature verification failed, "
                    "discarding", item.name)
                try:
                    cache_path.unlink()
                except OSError:
                    pass
                return DownloadResult(
                    item=item, success=False,
                    error="signature verification failed (from peer)",
                    blacklist_peer=PeerToBlacklist(
                        host=peer.host, port=peer.port,
                        reason="bad signature"))

            # Defer cache registration to main thread.  Peer downloads
            # carry no server-pool provenance — pass None for the
            # ``served_by_server_id`` column.
            with self._pending_cache_lock:
                self._pending_cache_registrations.append(
                    (item, cache_path, None))

            return DownloadResult(
                item=item, success=True, path=cache_path,
                peer_info=PeerProvenance(
                    peer_host=peer.host, peer_port=peer.port,
                    checksum_sha256=checksum, file_size=downloaded,
                    verified=True))

        except pycurl.error as e:
            code, msg = e.args
            return DownloadResult(
                item=item, success=False,
                error=f"Peer download failed: {msg}")
        except Exception as e:
            return DownloadResult(
                item=item, success=False,
                error=f"Peer download failed: {e}")
        finally:
            c.close()
            if f is not None:
                f.close()
            if temp_path.exists() and not cache_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    #: Slack kept free beyond the payload itself.  RPMs are unpacked
    #: from this same filesystem in the common case, and a filesystem
    #: driven to exactly zero free bytes fails in far uglier ways than
    #: one that refuses early.
    SPACE_MARGIN_BYTES = 200 * 1024 * 1024

    def assert_space_for(self, items: List[DownloadItem]) -> None:
        """Refuse to start when the payload cannot fit.

        Nothing checked the download target before : a distupgrade
        pulling several GB across a few thousand packages would fill
        the filesystem and fail deep into the transfer, with the
        partial payload still occupying the space that just ran out.

        Args:
            items: What remains to download — cached entries excluded
                by the caller, since they cost nothing.

        Raises:
            InsufficientSpaceError: with required and available figures,
                and the directory concerned, so the operator can act
                without having to work out which filesystem is meant.
        """
        if not items:
            return
        required = sum(item.size for item in items) + self.SPACE_MARGIN_BYTES
        try:
            st = os.statvfs(self.payload_dir)
        except OSError:
            # Unreadable target : let the download surface the real
            # error rather than guess at a space problem.
            return
        available = st.f_bavail * st.f_frsize
        if available < required:
            raise InsufficientSpaceError(
                directory=self.payload_dir,
                required=required,
                available=available,
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

        # Space preflight, once the cached set is known so we only
        # account for what will actually be written.  Checked against
        # the filesystem the payload lands on, which may not be the one
        # holding /var.  Failing here with a figure beats failing at
        # 90% of a multi-GB distupgrade download.
        self.assert_space_for(to_download)

        if progress_callback and cached_count > 0:
            progress_callback("(cache)", cached_count, len(items), downloaded_bytes, total_bytes)

        if not to_download:
            return results, 0, cached_count, {
                'from_peers': 0, 'from_discs': 0, 'from_upstream': 0,
                'peers_used': self.use_peers,
            }

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
                                     item_bytes=None, item_total=None,
                                     active_downloads=None, coordinator_speed=0.0):
                # Offset by cached items
                progress_callback(
                    name,
                    cached_count + pkg_num,
                    len(items),
                    downloaded_bytes + dl_bytes,  # downloaded_bytes has cached bytes
                    total_bytes,
                    item_bytes,  # Per-item progress (optional)
                    item_total,
                    active_downloads,  # All active downloads
                    coordinator_speed  # Global speed from coordinator
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

        # Flush deferred cache registrations on the main thread.
        # Workers collected (item, cache_path, server_id_or_None) tuples
        # instead of writing to SQLite directly, avoiding "database is
        # locked" errors.  Server id is forwarded to
        # ``cache_files.served_by_server_id``.
        if self._pending_cache_registrations:
            registered = 0
            for reg_item, reg_path, reg_server_id in self._pending_cache_registrations:
                self._register_cache_file(reg_item, reg_path,
                                          served_by_server_id=reg_server_id)
                registered += 1
            logger.debug(f"Registered {registered} cache files")
            self._pending_cache_registrations.clear()

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
            'from_discs': stats.get('from_discs', 0),
            'from_upstream': stats.get('from_upstream', 0),
            'failed_peers': stats.get('failed_peers', []),
            # Whether peer discovery was allowed to run at all, so the
            # report can tell « no peer had anything » (a zero worth
            # printing) from « peers were switched off » (a zero that
            # would only be noise).
            'peers_used': self.use_peers,
        }
        return results, downloaded_count, cached_count, peer_stats


def format_speed(bytes_per_sec: float) -> str:
    """Format download speed."""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec / 1024 / 1024:.1f} MB/s"
