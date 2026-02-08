"""Background task scheduler for urpmd."""

import logging
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, TYPE_CHECKING
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

if TYPE_CHECKING:
    from .daemon import UrpmDaemon

from ..core.database import PackageDatabase

logger = logging.getLogger(__name__)

# Default intervals (in seconds)
DEFAULT_METADATA_CHECK_INTERVAL = 3600  # 1 hour
DEFAULT_PREDOWNLOAD_CHECK_INTERVAL = 7200  # 2 hours
DEFAULT_REPLICATION_CHECK_INTERVAL = 1800  # 30 minutes
DEFAULT_FETCH_DATES_INTERVAL = 300  # 5 minutes
DEFAULT_FILES_XML_CHECK_INTERVAL = 86400  # 24 hours
# Note: cache cleanup runs after each predownload, not independently

# Dev mode intervals (shorter for testing)
DEV_METADATA_CHECK_INTERVAL = 60  # 1 minute
DEV_PREDOWNLOAD_CHECK_INTERVAL = 120  # 2 minutes
DEV_REPLICATION_CHECK_INTERVAL = 30  # 30 seconds
DEV_FETCH_DATES_INTERVAL = 20  # 20 seconds
DEV_FILES_XML_CHECK_INTERVAL = 60  # 1 minute


class Scheduler:
    """Background task scheduler for urpmd.

    Handles:
    - Periodic metadata refresh (checking if updates are needed)
    - Pre-downloading packages for pending updates
    - Replication for media with replication_policy='full'
    - Cache cleanup

    Has its own database connection (SQLite requires separate connections per thread).
    """

    def __init__(self, daemon: 'UrpmDaemon', dev_mode: bool = False):
        self.daemon = daemon
        self.db_path = daemon.db_path
        self.base_dir = daemon.base_dir
        self.dev_mode = dev_mode
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Own database connection (created in thread)
        self.db: Optional[PackageDatabase] = None

        # TASK INTERVALS
        # ---------------
        # - metadata_interval: How often to check if synthesis files changed (HTTP HEAD)
        # - predownload_interval: How often to check for updates and pre-download them
        # - tick_interval: Scheduler's check frequency (quantization unit for all delays)
        #
        # All delays are rounded to tick_interval multiples, so:
        #   PROD: tick=60s  → delays are in minutes (60s, 120s, 180s...)
        #   DEV:  tick=10s  → delays are in 10s increments (10s, 20s, 30s...)
        #
        # Note: cache cleanup runs after each predownload, not on its own schedule.
        #
        if dev_mode:
            self.metadata_interval = DEV_METADATA_CHECK_INTERVAL    # 60s
            self.predownload_interval = DEV_PREDOWNLOAD_CHECK_INTERVAL  # 120s
            self.replication_interval = DEV_REPLICATION_CHECK_INTERVAL  # 30s
            self.fetch_dates_interval = DEV_FETCH_DATES_INTERVAL  # 20s
            self.files_xml_interval = DEV_FILES_XML_CHECK_INTERVAL  # 60s
            self.tick_interval = 10  # Check every 10s in dev mode
            logger.info("Dev mode: using short intervals (metadata=%ds, predownload=%ds, replication=%ds, tick=%ds)",
                       DEV_METADATA_CHECK_INTERVAL, DEV_PREDOWNLOAD_CHECK_INTERVAL,
                       DEV_REPLICATION_CHECK_INTERVAL, self.tick_interval)
        else:
            self.metadata_interval = DEFAULT_METADATA_CHECK_INTERVAL    # 3600s (1h)
            self.predownload_interval = DEFAULT_PREDOWNLOAD_CHECK_INTERVAL  # 7200s (2h)
            self.replication_interval = DEFAULT_REPLICATION_CHECK_INTERVAL  # 1800s (30m)
            self.fetch_dates_interval = DEFAULT_FETCH_DATES_INTERVAL  # 300s (5m)
            self.files_xml_interval = DEFAULT_FILES_XML_CHECK_INTERVAL  # 86400s (24h)
            self.tick_interval = 60  # Check every minute in production

        # JITTER (thundering herd prevention)
        # -----------------------------------
        # Random variation ±30% applied to each interval to desynchronize
        # multiple machines. Without jitter, all machines started at the same
        # time would hit the servers simultaneously.
        self.jitter_factor = 0.30

        # Last run times
        self._last_metadata_check: Optional[datetime] = None
        self._last_predownload: Optional[datetime] = None
        self._last_cleanup: Optional[datetime] = None

        # Next scheduled times (with jitter applied)
        self._next_metadata_check: Optional[float] = None
        self._next_predownload: Optional[float] = None
        self._next_replication_check: Optional[float] = None
        self._next_fetch_dates_check: Optional[float] = None
        self._next_files_xml_check: Optional[float] = None
        self._next_cleanup: Optional[float] = None

        # Pre-download settings
        self.predownload_enabled = True
        self.max_predownload_size = 500 * 1024 * 1024  # 500 MB default

        # Idle detection thresholds (configurable)
        self.max_cpu_load = 0.5  # 1-minute load average threshold
        self.max_net_kbps = 100  # KB/s threshold for network "idle"

        # Network activity sampling
        self._last_net_sample: Optional[tuple] = None  # (timestamp, rx_bytes, tx_bytes)
        self._last_net_sample_time: Optional[float] = None

    def start(self):
        """Start the scheduler in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def _run(self):
        """Main scheduler loop."""
        # Create own database connection in this thread
        logger.debug(f"Scheduler opening database: {self.db_path}")
        self.db = PackageDatabase(self.db_path)

        # Initial delay to let the daemon fully initialize
        time.sleep(10)

        # Reconcile cache on startup (handles files deleted while daemon was stopped)
        try:
            from ..core.cache import CacheManager
            cache_mgr = CacheManager(self.db, self.base_dir)
            logger.info("Startup cache reconcile starting...")
            reconcile_result = cache_mgr.reconcile()
            logger.info(f"Startup cache reconcile done: removed {reconcile_result['orphan_records_removed']} orphan records, "
                       f"added {reconcile_result['untracked_files_added']} untracked files")
        except Exception as e:
            logger.warning(f"Startup cache reconcile failed: {e}", exc_info=True)

        # Check if FTS index needs rebuild (after migration/upgrade)
        try:
            if self.db.is_fts_supported() and not self.db.is_fts_index_current():
                fts_stats = self.db.get_fts_stats()
                main_count = fts_stats.get('main_table_count', 0)
                fts_count = fts_stats.get('fts_row_count', 0)
                logger.info(f"FTS index needs rebuild ({fts_count} indexed, {main_count} in table)")
                self._rebuild_fts_index()
        except Exception as e:
            logger.warning(f"FTS index check failed: {e}", exc_info=True)

        try:
            while self._running:
                try:
                    self._check_tasks()
                except Exception as e:
                    logger.error(f"Scheduler error: {e}")

                # Sleep between checks (short intervals to allow quick shutdown)
                for _ in range(self.tick_interval):
                    if not self._running:
                        break
                    time.sleep(1)
        finally:
            # Close our database connection
            if self.db:
                self.db.close()
                self.db = None
                logger.debug("Scheduler closed database connection")

    def _check_tasks(self):
        """Check if any scheduled tasks should run."""
        now = time.time()

        # Check metadata refresh
        if self._should_run_task('metadata', now):
            self._run_metadata_check()
            self._schedule_next('metadata', now, self.metadata_interval)

        # Check pre-download
        if self.predownload_enabled and self._should_run_task('predownload', now):
            self._run_predownload()
            self._schedule_next('predownload', now, self.predownload_interval)

        # Check replication (for media with replication_policy='full')
        if self._should_run_task('replication', now):
            self._run_replication()
            self._schedule_next('replication', now, self.replication_interval)

        # Fetch server dates for replication priority (runs more often, lightweight)
        if self._should_run_task('fetch_dates', now):
            self._run_fetch_server_dates()
            self._schedule_next('fetch_dates', now, self.fetch_dates_interval)

        # Sync files.xml for urpm find (once per day, when idle)
        if self._should_run_task('files_xml', now):
            self._run_files_xml_sync()
            self._schedule_next('files_xml', now, self.files_xml_interval)

        # Note: cache cleanup runs after predownload, not independently
        # (see _run_predownload)

    def _should_run_task(self, task_name: str, now: float) -> bool:
        """Check if a task should run based on jittered schedule.

        Scheduling is quantized to tick_interval (scheduler's check frequency).
        This ensures displayed delays match actual execution times.

        On first call, schedules an initial offset to desynchronize multiple
        machines (thundering herd prevention).
        """
        next_time = getattr(self, f'_next_{task_name}_check', None)

        if next_time is None:
            # FIRST RUN SCHEDULING
            # ---------------------
            # Goal: Desynchronize machines so they don't all hit servers at once.
            #
            # We pick a random offset between 1 tick and 50% of the base interval.
            # The offset is quantized to tick_interval (the scheduler's check
            # frequency), so "first run in 30s" means exactly 30s, not "sometime
            # between 30-90s depending on when the next tick happens".
            #
            # Example with tick=10s, metadata_interval=60s:
            #   max_ticks = 60 * 0.5 / 10 = 3 ticks
            #   initial_ticks = random 1-3 → e.g., 2
            #   initial_offset = 2 * 10 = 20s
            #
            base_interval = getattr(self, f'{task_name}_interval', 3600)
            max_ticks = max(1, int(base_interval * 0.5 / self.tick_interval))
            initial_ticks = random.randint(1, max_ticks)
            initial_offset = initial_ticks * self.tick_interval
            setattr(self, f'_next_{task_name}_check', now + initial_offset)
            logger.debug(f"Task {task_name}: first run in {initial_offset}s ({initial_ticks} ticks)")
            return False

        return now >= next_time

    def _schedule_next(self, task_name: str, now: float, base_interval: int):
        """Schedule next run with jitter applied.

        Adds random jitter (±30% by default) to the base interval to prevent
        synchronized requests from multiple machines (thundering herd).

        The final interval is quantized to tick_interval to ensure the displayed
        delay matches actual execution time.

        Example with tick=10s, base_interval=60s, jitter_factor=0.30:
          jitter = random -0.30 to +0.30 → e.g., +0.15
          actual_interval = 60 * 1.15 = 69s
          ticks = round(69 / 10) = 7 ticks
          actual_interval = 7 * 10 = 70s (quantized)
        """
        # Apply jitter: ±jitter_factor around base interval
        jitter = random.uniform(-self.jitter_factor, self.jitter_factor)
        actual_interval = base_interval * (1 + jitter)

        # Quantize to tick_interval (round to nearest tick, minimum 1)
        ticks = max(1, round(actual_interval / self.tick_interval))
        actual_interval = ticks * self.tick_interval

        next_time = now + actual_interval
        setattr(self, f'_next_{task_name}_check', next_time)
        logger.debug(f"Task {task_name}: next run in {actual_interval}s ({ticks} ticks)")

    def _run_metadata_check(self):
        """Check if metadata needs refreshing using HTTP HEAD.

        Compares remote Last-Modified/Content-Length with local file.
        """
        logger.info("Running scheduled metadata check")

        if not self.db:
            logger.warning("No database connection")
            return

        from ..core.config import get_hostname_from_url, get_media_local_path

        # Check each enabled media
        media_list = self.db.list_media()
        logger.debug(f"Found {len(media_list)} media in database")

        for media in media_list:
            if not media['enabled']:
                continue

            name = media['name']
            relative_path = media.get('relative_path', '')
            url = media.get('url', '')

            # Get local synthesis file path
            # New schema: <base_dir>/medias/official/<relative_path>/media_info/synthesis.hdlist.cz
            # Legacy: <base_dir>/medias/<hostname>/<media_name>/media_info/synthesis.hdlist.cz
            if relative_path:
                media_dir = get_media_local_path(media)
                local_synthesis = media_dir / "media_info" / "synthesis.hdlist.cz"
            elif url:
                hostname = get_hostname_from_url(url)
                local_synthesis = self.base_dir / "medias" / hostname / name / "media_info" / "synthesis.hdlist.cz"
            else:
                logger.debug(f"Media {name}: no relative_path or url, skipping")
                continue

            logger.debug(f"Media {name}: checking local={local_synthesis}")

            # Build synthesis URL
            # New schema: use server + relative_path
            # Legacy: use media.url directly
            if relative_path:
                # Get best server for this media
                server = self.db.get_best_server_for_media(media['id'])
                if server:
                    from ..core.config import build_media_url
                    base_url = build_media_url(server, media)
                    synthesis_url = f"{base_url}/media_info/synthesis.hdlist.cz"
                elif url:
                    synthesis_url = url.rstrip('/') + '/media_info/synthesis.hdlist.cz'
                else:
                    logger.debug(f"Media {name}: no server available, skipping")
                    continue
            else:
                synthesis_url = url.rstrip('/') + '/media_info/synthesis.hdlist.cz'
            logger.debug(f"Media {name}: remote={synthesis_url}")

            # Check if synthesis has changed using HTTP HEAD vs local file
            has_changed = self._check_synthesis_changed(synthesis_url, local_synthesis)
            logger.debug(f"Media {name}: has_changed={has_changed}")

            if has_changed:
                logger.info(f"Media {name}: synthesis changed, refreshing")
                try:
                    self._refresh_media(name)
                except Exception as e:
                    logger.error(f"Failed to refresh {name}: {e}")
            else:
                logger.debug(f"Media {name}: synthesis unchanged")

    def _check_synthesis_changed(self, url: str, local_path: Path) -> bool:
        """Check if remote synthesis differs from local file.

        Compares Content-Length and Last-Modified from HTTP HEAD
        with local file size and mtime.

        Args:
            url: Remote synthesis URL
            local_path: Path to local synthesis file

        Returns:
            True if file has changed or local doesn't exist
        """
        from email.utils import parsedate_to_datetime

        # If local file doesn't exist, we need to download
        if not local_path.exists():
            logger.debug(f"Local file missing: {local_path}")
            return True

        try:
            local_stat = local_path.stat()
            local_size = local_stat.st_size
            local_mtime = local_stat.st_mtime
            logger.debug(f"Local file: size={local_size}, mtime={local_mtime}")
        except OSError as e:
            logger.warning(f"Could not stat local file {local_path}: {e}")
            return True

        try:
            req = Request(url, method='HEAD')
            req.add_header('User-Agent', 'urpmd/0.1')

            response = urlopen(req, timeout=30)

            # Get remote file info
            remote_size_str = response.headers.get('Content-Length')
            remote_last_mod = response.headers.get('Last-Modified')
            logger.debug(f"Remote: size={remote_size_str}, last_mod={remote_last_mod}")

            # Compare sizes
            if remote_size_str:
                remote_size = int(remote_size_str)
                if remote_size != local_size:
                    logger.debug(f"Size differs: local={local_size}, remote={remote_size}")
                    return True

            # Compare dates
            if remote_last_mod:
                try:
                    remote_dt = parsedate_to_datetime(remote_last_mod)
                    remote_mtime = remote_dt.timestamp()
                    # Remote is newer if its mtime > local mtime
                    if remote_mtime > local_mtime:
                        logger.debug(f"Remote is newer: local={local_mtime}, remote={remote_mtime}")
                        return True
                except (ValueError, TypeError):
                    pass  # Can't parse date, rely on size check

            # Size matches and remote is not newer
            return False

        except HTTPError as e:
            logger.warning(f"HTTP HEAD failed for {url}: {e.code}")
            return True  # Assume changed on error

        except (URLError, OSError) as e:
            logger.warning(f"Could not check {url}: {e}")
            return True  # Assume changed on error

    def _run_predownload(self):
        """Pre-download packages for pending updates."""
        logger.info("Running scheduled pre-download check")

        if not self.db:
            return

        try:
            # Get available updates
            updates = self._get_available_updates()

            if not updates:
                logger.debug("No updates to pre-download")
                return

            total_size = updates.get('total_size', 0)
            update_list = updates.get('updates', [])

            if total_size > self.max_predownload_size:
                logger.info(f"Updates too large to pre-download: {total_size / 1024 / 1024:.1f} MB")
                return

            # Check if system is idle enough for background downloads
            if not self._is_system_idle():
                logger.debug("Skipping pre-download: system not idle")
                return

            # Pre-download packages
            logger.info(f"Pre-downloading {len(update_list)} packages ({total_size / 1024 / 1024:.1f} MB)")
            self._predownload_packages(update_list)

            # Run cache cleanup after predownload completes
            self._run_cache_cleanup()

        except Exception as e:
            logger.error(f"Pre-download error: {e}")

    def _predownload_packages(self, updates: list):
        """Download packages for updates.

        Args:
            updates: List of update dicts with name, available version, arch, media_name, etc.
        """
        from ..core.download import Downloader, DownloadItem

        if not self.db:
            return

        downloader = Downloader(cache_dir=self.base_dir, db=self.db)

        # Cache media info and servers to avoid repeated DB lookups
        media_cache = {}
        servers_cache = {}
        for media in self.db.list_media():
            media_cache[media['name']] = media
            if media.get('id'):
                servers = self.db.get_servers_for_media(media['id'], enabled_only=True)
                servers_cache[media['id']] = [dict(s) for s in servers]

        items = []
        for update in updates:
            media_name = update.get('media_name', '')
            media = media_cache.get(media_name)
            if not media:
                logger.debug(f"No media for {update['name']} (media={media_name})")
                continue

            # Parse EVR to extract version and release
            # EVR format: [epoch:]version-release
            evr = update.get('available', '')
            if ':' in evr:
                evr = evr.split(':', 1)[1]  # Strip epoch
            if '-' in evr:
                version, release = evr.rsplit('-', 1)
            else:
                version = evr
                release = '1'

            # Use new schema if available, fallback to legacy URL
            if media.get('relative_path'):
                servers = servers_cache.get(media['id'], [])
                items.append(DownloadItem(
                    name=update['name'],
                    version=version,
                    release=release,
                    arch=update['arch'],
                    media_id=media['id'],
                    relative_path=media['relative_path'],
                    is_official=bool(media.get('is_official', 1)),
                    servers=servers,
                    media_name=media_name,
                    size=update.get('size', 0),
                ))
            elif media.get('url'):
                items.append(DownloadItem(
                    name=update['name'],
                    version=version,
                    release=release,
                    arch=update['arch'],
                    media_url=media['url'],
                    media_name=media_name,
                    size=update.get('size', 0),
                ))

        if items:
            # Download with progress logging (rate-limited)
            # Callback signature: (name, pkg_num, pkg_total, bytes_done, bytes_total,
            #                      item_bytes, item_total, active_downloads)
            last_log = [0, 0]  # [last_pct, last_pkg_num]

            def progress_callback(name, pkg_num, pkg_total, bytes_done, bytes_total,
                                  item_bytes=None, item_total=None, active_downloads=None):
                if bytes_total > 0:
                    pct = bytes_done * 100 // bytes_total
                    # Only log when percentage changes by 5% or new package
                    if pct >= last_log[0] + 5 or pkg_num != last_log[1]:
                        logger.debug(f"Pre-downloading {name}: {pct}% ({pkg_num}/{pkg_total})")
                        last_log[0] = pct
                        last_log[1] = pkg_num

            results, downloaded, cached, peer_stats = downloader.download_all(items, progress_callback)
            errors = [r for r in results if not r.success]
            logger.info(f"Pre-download complete: {downloaded} downloaded, "
                       f"{cached} cached, {len(errors)} errors")

            # Invalidate RPM index so peers see new packages
            if downloaded > 0:
                self.daemon.invalidate_rpm_index()

    def _run_cache_cleanup(self):
        """Clean up cached packages based on quotas and retention policies."""
        logger.info("Running scheduled cache cleanup")

        if not self.db:
            return

        try:
            from ..core.cache import CacheManager

            cache_mgr = CacheManager(self.db, self.base_dir)

            # First reconcile DB with filesystem (handles manual deletions)
            reconcile_result = cache_mgr.reconcile()
            if reconcile_result['orphan_records_removed'] > 0:
                logger.info(f"Cache reconcile: removed {reconcile_result['orphan_records_removed']} orphan DB records")
            if reconcile_result['untracked_files_added'] > 0:
                logger.info(f"Cache reconcile: registered {reconcile_result['untracked_files_added']} untracked files")

            result = cache_mgr.enforce_quotas(dry_run=False)

            if result['total_deleted'] > 0:
                logger.info(
                    f"Cache cleanup: removed {result['total_deleted']} files "
                    f"({result['total_bytes'] / 1024 / 1024:.1f} MB) - "
                    f"unreferenced: {result['unreferenced_deleted']}, "
                    f"retention: {result['retention_deleted']}, "
                    f"quota: {result['quota_deleted']}"
                )
            else:
                logger.debug("Cache cleanup: no files to remove")

            if result['errors']:
                for err in result['errors'][:5]:  # Log first 5 errors
                    logger.warning(f"Cleanup error: {err}")

        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")

    def _run_replication(self):
        """Replicate packages for media with replication_policy='seed'.

        For 'seed' policy: uses rpmsrate-raw to determine which packages to replicate.
        Only packages in the seed set (+ their dependencies) are downloaded.

        Respects quotas and idle detection.
        """
        if not self.db:
            return

        # Find media with replication_policy='seed'
        media_to_replicate = []
        for media in self.db.list_media():
            if media.get('replication_policy') == 'seed' and media.get('enabled'):
                media_to_replicate.append(media)

        if not media_to_replicate:
            logger.debug("No media with replication_policy='seed'")
            return

        logger.info(f"Checking replication for {len(media_to_replicate)} media")

        # Check if system is idle enough for background downloads
        if not self._is_system_idle():
            logger.debug("Skipping replication: system not idle")
            return

        # Compute seed set once (shared across all media)
        seed_names = self._compute_seed_set(media_to_replicate)
        if not seed_names:
            logger.debug("No seed set computed (rpmsrate-raw not found or empty)")
            return

        logger.info(f"Seed set: {len(seed_names)} package names")

        from ..core.download import Downloader, DownloadItem

        for media in media_to_replicate:
            try:
                self._replicate_media(media, seed_names=seed_names)
            except Exception as e:
                logger.error(f"Replication error for {media['name']}: {e}")

    def _compute_seed_set(self, media_list: list) -> set:
        """Compute the seed set from rpmsrate-raw.

        Parses rpmsrate-raw and extracts packages from the configured sections,
        then resolves dependencies to get the complete set.

        Args:
            media_list: List of media dicts (to get replication_seeds config)

        Returns:
            Set of package names in the seed set
        """
        import json
        from pathlib import Path
        from ..core.rpmsrate import RpmsrateParser, DEFAULT_RPMSRATE_PATH

        # Default sections (same as DVD content)
        DEFAULT_SEED_SECTIONS = [
            'INSTALL',
            'CAT_PLASMA5', 'CAT_GNOME', 'CAT_XFCE', 'CAT_MATE', 'CAT_LXDE', 'CAT_LXQT',
            'CAT_X', 'CAT_SYSTEM', 'CAT_NETWORKING_WWW', 'CAT_OFFICE',
        ]

        # Collect all sections from all media
        all_sections = set()
        for media in media_list:
            seeds_json = media.get('replication_seeds')
            if seeds_json:
                try:
                    sections = json.loads(seeds_json)
                    all_sections.update(sections)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid replication_seeds JSON for media {media['name']}")
            else:
                # Default: DVD-equivalent content
                all_sections.update(DEFAULT_SEED_SECTIONS)

        if not all_sections:
            return set()

        # Parse rpmsrate-raw
        try:
            parser = RpmsrateParser(DEFAULT_RPMSRATE_PATH)
            parser.parse()
        except FileNotFoundError:
            logger.warning(f"rpmsrate-raw not found at {DEFAULT_RPMSRATE_PATH}")
            return set()
        except Exception as e:
            logger.error(f"Error parsing rpmsrate-raw: {e}")
            return set()

        # Get active categories (CAT_xxx sections)
        active_categories = [s for s in all_sections if s.startswith('CAT_')]

        # Extract packages from sections
        seed_packages = parser.get_packages(
            sections=list(all_sections),
            active_categories=active_categories,
            ignore_conditions=['DRIVER', 'HW', 'HW_CAT'],
            min_priority=4
        )

        logger.info(f"rpmsrate sections {list(all_sections)}: {len(seed_packages)} seed packages")

        # Expand with dependencies using collect_dependencies
        # This collects all required packages without checking conflicts
        # (conflicts are fine for replication - DVD has conflicting packages)
        result = self.db.collect_dependencies(seed_packages)
        full_set = result['packages']
        deps_count = len(full_set) - len(seed_packages & full_set)

        logger.info(f"With dependencies: {len(full_set)} packages (+{deps_count} deps)")

        if result['not_found']:
            logger.debug(f"Seeds not found: {result['not_found']}")

        return full_set

    def _replicate_media(self, media: dict, seed_names: set = None):
        """Replicate a single media (download missing packages).

        Args:
            media: Media dict with id, name, relative_path, etc.
            seed_names: Set of package names to replicate (if None, replicate all)
        """
        from ..core.download import Downloader, DownloadItem

        media_id = media['id']
        media_name = media['name']

        # Get all packages in this media, sorted by server date (newest first)
        all_packages = self.db.get_packages_for_media(media_id, order_by='server_date')
        if not all_packages:
            logger.debug(f"Media {media_name}: no packages in synthesis")
            return

        # Filter by seed set if provided
        if seed_names:
            all_packages = [p for p in all_packages if p['name'] in seed_names]
            if not all_packages:
                logger.debug(f"Media {media_name}: no packages match seed set")
                return
            logger.debug(f"Media {media_name}: {len(all_packages)} packages in seed set")

        # Keep only the latest version of each package name (like --latest-only)
        from ..core.rpm import filter_latest_versions
        all_packages = filter_latest_versions(all_packages)

        # Only replicate packages with known server dates
        # Others will be picked up once HEAD job fetches their dates
        packages_with_dates = [p for p in all_packages if p.get('server_last_modified')]
        packages_without_dates = len(all_packages) - len(packages_with_dates)

        if packages_without_dates > 0:
            logger.info(f"Media {media_name}: {packages_without_dates} packages waiting for server dates")

        if not packages_with_dates:
            logger.debug(f"Media {media_name}: no packages with server dates yet, waiting for HEAD job")
            return

        all_packages = packages_with_dates

        # Get already cached files for this media
        cached_files = set()
        for cf in self.db.list_cache_files(media_id=media_id):
            cached_files.add(cf['filename'])

        # Find missing packages
        missing = []
        missing_size = 0
        for pkg in all_packages:
            filename = pkg['filename']
            if filename not in cached_files:
                missing.append(pkg)
                missing_size += pkg.get('size', 0) or 0

        if not missing:
            logger.debug(f"Media {media_name}: all {len(all_packages)} packages already cached")
            return

        logger.info(f"Media {media_name}: {len(missing)}/{len(all_packages)} packages missing "
                   f"({missing_size / 1024 / 1024:.1f} MB)")

        # Check quota and limit downloads to what fits
        quota_mb = media.get('quota_mb')
        available_bytes = None
        if quota_mb:
            # Use actual disk usage (more reliable than DB stats)
            from ..core.cache import CacheManager
            cache_mgr = CacheManager(self.db, self.base_dir)
            disk_stats = cache_mgr.get_disk_usage(media_id=media_id)
            current_bytes = disk_stats.get('total_size', 0)
            quota_bytes = quota_mb * 1024 * 1024
            available_bytes = quota_bytes - current_bytes

            if available_bytes <= 0:
                logger.info(f"Media {media_name}: quota reached ({current_bytes / 1024 / 1024:.1f}/{quota_mb} MB)")
                return

            # Filter missing packages to fit within quota
            if missing_size > available_bytes:
                logger.info(f"Media {media_name}: limiting to {available_bytes / 1024 / 1024:.1f} MB "
                           f"(quota: {quota_mb} MB, used: {current_bytes / 1024 / 1024:.1f} MB)")
                # Sort by size (smallest first) to maximize package count
                missing.sort(key=lambda p: p.get('size', 0) or 0)
                limited = []
                limited_size = 0
                for pkg in missing:
                    pkg_size = pkg.get('size', 0) or 0
                    if limited_size + pkg_size <= available_bytes:
                        limited.append(pkg)
                        limited_size += pkg_size
                missing = limited
                missing_size = limited_size
                logger.info(f"Media {media_name}: will download {len(missing)} packages ({missing_size / 1024 / 1024:.1f} MB)")

        # Get servers for this media
        servers = self.db.get_servers_for_media(media_id, enabled_only=True)
        servers = [dict(s) for s in servers]

        if not servers and not media.get('url'):
            logger.warning(f"Media {media_name}: no servers available")
            return

        # Build download items
        items = []
        for pkg in missing:
            if media.get('relative_path'):
                items.append(DownloadItem(
                    name=pkg['name'],
                    version=pkg['version'],
                    release=pkg['release'],
                    arch=pkg['arch'],
                    media_id=media_id,
                    relative_path=media['relative_path'],
                    is_official=bool(media.get('is_official', 1)),
                    servers=servers,
                    media_name=media_name,
                    size=pkg.get('size', 0),
                ))
            elif media.get('url'):
                items.append(DownloadItem(
                    name=pkg['name'],
                    version=pkg['version'],
                    release=pkg['release'],
                    arch=pkg['arch'],
                    media_url=media['url'],
                    media_name=media_name,
                    size=pkg.get('size', 0),
                ))

        if not items:
            return

        # Download in batches to show progress
        batch_size = 50
        downloader = Downloader(cache_dir=self.base_dir, db=self.db)

        total_downloaded = 0
        total_cached = 0
        total_errors = 0

        for i in range(0, len(items), batch_size):
            if not self._running:
                logger.info("Replication interrupted by shutdown")
                break

            batch = items[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(items) + batch_size - 1) // batch_size

            logger.info(f"Media {media_name}: downloading batch {batch_num}/{total_batches} "
                       f"({len(batch)} packages)")

            def progress_callback(name, pkg_num, pkg_total, bytes_done, bytes_total,
                                  item_bytes=None, item_total=None, active_downloads=None):
                pass  # Silent progress for background replication

            results, downloaded, cached, peer_stats = downloader.download_all(batch, progress_callback)
            errors = [r for r in results if not r.success]

            total_downloaded += downloaded
            total_cached += cached
            total_errors += len(errors)

            # Check if we should continue (system still idle?)
            if not self._is_system_idle():
                logger.info(f"Media {media_name}: pausing replication (system busy)")
                break

        logger.info(f"Media {media_name}: replication complete - "
                   f"{total_downloaded} downloaded, {total_cached} cached, {total_errors} errors")

        # Invalidate RPM index so peers see new packages
        if total_downloaded > 0:
            self.daemon.invalidate_rpm_index()

    def _run_fetch_server_dates(self):
        """Fetch Last-Modified dates from server for packages missing server_last_modified.

        This runs periodically to get publication dates for replication priority.
        Rate-limited and distributed across mirrors to avoid hammering servers.
        """
        if not self.db:
            return

        # Only for media with replication=full
        media_to_process = []
        for media in self.db.list_media():
            if media.get('replication_policy') == 'full' and media.get('enabled'):
                media_to_process.append(media)

        if not media_to_process:
            return

        from email.utils import parsedate_to_datetime
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError
        from ..core.config import build_media_url

        # Rate limit: max requests per run
        max_requests_per_run = 100 if self.dev_mode else 500
        requests_made = 0

        for media in media_to_process:
            media_id = media['id']
            media_name = media['name']

            # Get packages needing dates
            packages = self.db.get_packages_needing_server_dates(
                media_id, limit=max_requests_per_run - requests_made
            )

            if not packages:
                continue

            logger.info(f"Media {media_name}: fetching server dates for {len(packages)} packages")

            # Get all servers for this media (for round-robin distribution)
            servers = self.db.get_servers_for_media(media_id, enabled_only=True)
            servers = [dict(s) for s in servers]

            if not servers and not media.get('url'):
                logger.warning(f"Media {media_name}: no server available for HEAD requests")
                continue

            # Fallback to legacy URL if no servers
            if not servers and media.get('url'):
                servers = [{'url': media['url'].rstrip('/')}]

            updates = []
            errors = 0

            for i, pkg in enumerate(packages):
                if not self._running:
                    break

                # Check if system is still idle (every 50 requests)
                if requests_made > 0 and requests_made % 50 == 0:
                    if not self._is_system_idle():
                        logger.debug("Pausing HEAD fetches: system busy")
                        break

                filename = pkg['filename']

                # Round-robin across servers
                server = servers[i % len(servers)]
                if 'url' in server:
                    base_url = server['url']
                else:
                    base_url = build_media_url(server, media)

                url = f"{base_url}/{filename}"

                try:
                    req = Request(url, method='HEAD')
                    req.add_header('User-Agent', 'urpmd/0.1')

                    response = urlopen(req, timeout=10)
                    last_modified = response.headers.get('Last-Modified')

                    if last_modified:
                        try:
                            dt = parsedate_to_datetime(last_modified)
                            timestamp = int(dt.timestamp())
                            updates.append((pkg['id'], timestamp))
                        except (ValueError, TypeError):
                            pass

                except (URLError, HTTPError, OSError) as e:
                    errors += 1
                    if errors <= 3:
                        logger.debug(f"HEAD failed for {filename}: {e}")

                requests_made += 1

                # Rate limit: 100ms pause every 10 requests = ~100 req/s max
                if requests_made % 10 == 0:
                    time.sleep(0.1)

            # Batch update
            if updates:
                self.db.update_server_last_modified_batch(updates)
                logger.info(f"Media {media_name}: updated {len(updates)} server dates "
                           f"({errors} errors)")

            if requests_made >= max_requests_per_run:
                logger.debug(f"Reached max requests per run ({max_requests_per_run})")
                break

    def _run_files_xml_sync(self):
        """Sync files.xml for media with sync_files enabled.

        This enables `urpm find` to search in available packages.
        Only runs if:
        - At least one media has sync_files=1
        - System is idle (CPU and network)
        - Last sync was > 24h ago (enforced by interval)
        """
        if not self.db:
            return

        # Check if any media has sync_files enabled
        if not self.db.has_any_sync_files_media():
            logger.debug("No media with sync_files enabled, skipping files.xml sync")
            return

        # Check if system is idle
        if not self._is_system_idle():
            logger.debug("Skipping files.xml sync: system not idle")
            return

        logger.info("Running scheduled files.xml sync")

        try:
            from ..core.sync import sync_all_files_xml

            def progress_callback(media_name, stage, dl_current, dl_total,
                                  import_current, import_total):
                # Silent background sync - just log key events
                if stage == 'done':
                    logger.debug(f"files.xml {media_name}: sync complete ({import_current} files)")
                elif stage == 'error':
                    logger.warning(f"files.xml {media_name}: sync failed")

            results = sync_all_files_xml(self.db, progress_callback, force=False)

            # Log summary
            synced = sum(1 for _, r in results if r.success and r.files_count > 0)
            skipped = sum(1 for _, r in results if r.success and r.files_count == 0)
            errors = sum(1 for _, r in results if not r.success)

            if synced > 0 or errors > 0:
                logger.info(f"files.xml sync complete: {synced} synced, {skipped} unchanged, {errors} errors")
            else:
                logger.debug("files.xml sync: all media up-to-date")

        except Exception as e:
            logger.error(f"files.xml sync error: {e}")

    def _refresh_media(self, media_name: str):
        """Refresh metadata for a specific media.

        Uses own database connection.
        """
        from ..core.sync import sync_media

        if not self.db:
            return

        result = sync_media(self.db, media_name, force=True)
        if result.success:
            logger.info(f"Media {media_name}: synced {result.packages_count} packages")
        else:
            logger.error(f"Media {media_name}: sync failed - {result.error}")

    def _get_available_updates(self) -> Optional[dict]:
        """Get list of packages with available updates.

        Uses own database connection.

        Returns:
            Dict with 'updates' list and 'total_size', or None on error
        """
        if not self.db:
            return None

        import platform
        from ..core.resolver import Resolver

        try:
            arch = platform.machine()
            resolver = Resolver(self.db, arch=arch)
            result = resolver.resolve_upgrade([])

            updates = []
            total_size = 0
            for action in result.actions:
                updates.append({
                    'name': action.name,
                    'current': action.from_evr,
                    'available': action.evr,
                    'arch': action.arch,
                    'size': action.size,
                    'media_name': action.media_name,
                })
                total_size += action.size or 0

            return {
                'count': len(updates),
                'updates': updates,
                'total_size': total_size,
            }
        except Exception as e:
            logger.error(f"Error checking updates: {e}")
            return None

    # ========== System Idle Detection ==========

    def _is_system_idle(self) -> bool:
        """Check if system is idle enough for background downloads.

        Checks CPU load and network activity to determine if downloads
        would disturb the user.

        Returns:
            True if system appears idle, False otherwise
        """
        cpu_idle = self._is_cpu_idle()
        net_idle = self._is_network_idle()

        if not cpu_idle:
            logger.debug(f"CPU not idle (load > {self.max_cpu_load})")
            return False

        if not net_idle:
            logger.debug(f"Network not idle (> {self.max_net_kbps} KB/s)")
            return False

        return True

    def _is_cpu_idle(self) -> bool:
        """Check if CPU load is low enough.

        Uses /proc/loadavg for 1-minute load average.
        """
        try:
            with open('/proc/loadavg', 'r') as f:
                loadavg = f.read().strip()
            # Format: "0.00 0.01 0.05 1/234 12345"
            load_1min = float(loadavg.split()[0])
            return load_1min < self.max_cpu_load
        except (IOError, ValueError, IndexError) as e:
            logger.warning(f"Could not read CPU load: {e}")
            return True  # Assume idle if we can't check

    def _is_network_idle(self) -> bool:
        """Check if network activity is low enough.

        Measures bytes transferred since last check using /proc/net/dev.
        """
        try:
            rx_bytes, tx_bytes = self._get_network_bytes()
            now = time.time()

            if self._last_net_sample is None:
                # First sample, store and assume idle
                self._last_net_sample = (rx_bytes, tx_bytes)
                self._last_net_sample_time = now
                return True

            # Calculate rate since last sample
            elapsed = now - self._last_net_sample_time
            if elapsed < 1:
                return True  # Not enough time passed

            prev_rx, prev_tx = self._last_net_sample
            rx_rate = (rx_bytes - prev_rx) / elapsed / 1024  # KB/s
            tx_rate = (tx_bytes - prev_tx) / elapsed / 1024  # KB/s
            total_rate = rx_rate + tx_rate

            # Update sample
            self._last_net_sample = (rx_bytes, tx_bytes)
            self._last_net_sample_time = now

            return total_rate < self.max_net_kbps

        except Exception as e:
            logger.warning(f"Could not check network activity: {e}")
            return True  # Assume idle if we can't check

    def _get_network_bytes(self) -> tuple:
        """Get total network bytes (rx, tx) from /proc/net/dev.

        Sums all interfaces except lo.
        """
        total_rx = 0
        total_tx = 0

        with open('/proc/net/dev', 'r') as f:
            for line in f:
                if ':' not in line:
                    continue
                parts = line.split(':')
                iface = parts[0].strip()

                # Skip loopback
                if iface == 'lo':
                    continue

                # Parse stats: rx_bytes is field 0, tx_bytes is field 8
                stats = parts[1].split()
                if len(stats) >= 9:
                    total_rx += int(stats[0])
                    total_tx += int(stats[8])

        return total_rx, total_tx

    def _rebuild_fts_index(self):
        """Rebuild FTS index for fast file search.

        Called on startup if the FTS index is out of sync (e.g., after
        database migration from a version without FTS).
        """
        if not self.db:
            return

        logger.info("Rebuilding FTS index for fast file search...")

        try:
            start_time = time.time()
            last_log_time = [start_time]

            def progress_callback(current, total):
                now = time.time()
                # Log progress every 30 seconds
                if now - last_log_time[0] >= 30:
                    pct = current * 100 // total if total > 0 else 0
                    elapsed = now - start_time
                    logger.info(f"FTS rebuild: {pct}% ({current:,}/{total:,} files, {elapsed:.0f}s)")
                    last_log_time[0] = now

            self.db.rebuild_fts_index(progress_callback=progress_callback)

            elapsed = time.time() - start_time
            stats = self.db.get_fts_stats()
            logger.info(f"FTS rebuild complete: {stats.get('fts_row_count', 0):,} files indexed in {elapsed:.1f}s")

        except Exception as e:
            logger.error(f"FTS rebuild failed: {e}", exc_info=True)
