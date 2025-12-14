"""Background task scheduler for urpmd."""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .daemon import UrpmDaemon

logger = logging.getLogger(__name__)

# Default intervals (in seconds)
DEFAULT_METADATA_CHECK_INTERVAL = 3600  # 1 hour
DEFAULT_PREDOWNLOAD_CHECK_INTERVAL = 7200  # 2 hours
DEFAULT_CACHE_CLEANUP_INTERVAL = 86400  # 24 hours


class Scheduler:
    """Background task scheduler for urpmd.

    Handles:
    - Periodic metadata refresh (checking if updates are needed)
    - Pre-downloading packages for pending updates
    - Cache cleanup
    """

    def __init__(self, daemon: 'UrpmDaemon'):
        self.daemon = daemon
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Task intervals (configurable)
        self.metadata_interval = DEFAULT_METADATA_CHECK_INTERVAL
        self.predownload_interval = DEFAULT_PREDOWNLOAD_CHECK_INTERVAL
        self.cleanup_interval = DEFAULT_CACHE_CLEANUP_INTERVAL

        # Last run times
        self._last_metadata_check: Optional[datetime] = None
        self._last_predownload: Optional[datetime] = None
        self._last_cleanup: Optional[datetime] = None

        # Pre-download settings
        self.predownload_enabled = True
        self.max_predownload_size = 500 * 1024 * 1024  # 500 MB default

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
        # Initial delay to let the daemon fully initialize
        time.sleep(10)

        while self._running:
            try:
                self._check_tasks()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Sleep between checks (short intervals to allow quick shutdown)
            for _ in range(60):  # Check every minute
                if not self._running:
                    break
                time.sleep(1)

    def _check_tasks(self):
        """Check if any scheduled tasks should run."""
        now = datetime.now()

        # Check metadata refresh
        if self._should_run_task(self._last_metadata_check, self.metadata_interval):
            self._run_metadata_check()
            self._last_metadata_check = now

        # Check pre-download
        if self.predownload_enabled and self._should_run_task(
                self._last_predownload, self.predownload_interval):
            self._run_predownload()
            self._last_predownload = now

        # Check cache cleanup
        if self._should_run_task(self._last_cleanup, self.cleanup_interval):
            self._run_cache_cleanup()
            self._last_cleanup = now

    def _should_run_task(self, last_run: Optional[datetime],
                         interval: int) -> bool:
        """Check if a task should run based on last run time."""
        if last_run is None:
            return True
        elapsed = (datetime.now() - last_run).total_seconds()
        return elapsed >= interval

    def _run_metadata_check(self):
        """Check if metadata needs refreshing."""
        logger.info("Running scheduled metadata check")

        if not self.daemon.db:
            return

        # Check each enabled media for staleness
        for media in self.daemon.db.list_media():
            if not media['enabled']:
                continue

            name = media['name']
            last_updated = media.get('last_updated')

            # Consider stale if not updated in 24 hours
            if last_updated:
                try:
                    # Parse ISO format
                    if isinstance(last_updated, str):
                        last_dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                    else:
                        last_dt = datetime.fromtimestamp(last_updated)

                    age = datetime.now() - last_dt.replace(tzinfo=None)
                    if age < timedelta(hours=24):
                        logger.debug(f"Media {name} is fresh (age: {age})")
                        continue
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not parse last_updated for {name}: {e}")

            # Refresh this media
            logger.info(f"Refreshing stale media: {name}")
            try:
                self.daemon.refresh_metadata(name)
            except Exception as e:
                logger.error(f"Failed to refresh {name}: {e}")

    def _run_predownload(self):
        """Pre-download packages for pending updates."""
        logger.info("Running scheduled pre-download check")

        if not self.daemon.db:
            return

        try:
            # Get available updates
            updates = self.daemon.get_available_updates()

            if 'error' in updates:
                logger.error(f"Error getting updates: {updates['error']}")
                return

            update_list = updates.get('updates', [])
            if not update_list:
                logger.debug("No updates to pre-download")
                return

            total_size = updates.get('total_size', 0)
            if total_size > self.max_predownload_size:
                logger.info(f"Updates too large to pre-download: {total_size / 1024 / 1024:.1f} MB")
                return

            # Check network idle (simple heuristic: check if it's night time)
            hour = datetime.now().hour
            if not (0 <= hour <= 6 or 22 <= hour <= 23):
                logger.debug("Skipping pre-download during active hours")
                return

            # Pre-download packages
            logger.info(f"Pre-downloading {len(update_list)} packages ({total_size / 1024 / 1024:.1f} MB)")
            self._predownload_packages(update_list)

        except Exception as e:
            logger.error(f"Pre-download error: {e}")

    def _predownload_packages(self, updates: list):
        """Download packages for updates.

        Args:
            updates: List of update dicts with name, available version, etc.
        """
        from ..core.download import Downloader, DownloadItem

        if not self.daemon.db:
            return

        downloader = Downloader(cache_dir=str(self.daemon.cache_dir))

        items = []
        for update in updates:
            pkg_name = update['name']
            pkg_info = self.daemon.db.get_package(pkg_name)
            if not pkg_info:
                continue

            url = pkg_info.get('url')
            filename = pkg_info.get('filename')
            if url and filename:
                items.append(DownloadItem(
                    url=url,
                    filename=filename,
                    size=update.get('size', 0),
                ))

        if items:
            # Download with progress logging
            def progress_callback(item, downloaded, total):
                if total > 0:
                    pct = downloaded * 100 // total
                    logger.debug(f"Pre-downloading {item.filename}: {pct}%")

            result = downloader.download(items, progress_callback)
            logger.info(f"Pre-download complete: {result.downloaded} downloaded, "
                       f"{result.cached} cached, {len(result.errors)} errors")

    def _run_cache_cleanup(self):
        """Clean up old cached packages."""
        logger.info("Running scheduled cache cleanup")

        if not self.daemon.cache_dir.exists():
            return

        try:
            # Get all RPMs currently referenced in synthesis
            referenced_files = set()
            if self.daemon.db:
                for media in self.daemon.db.list_media():
                    # Get all package filenames from this media
                    # TODO: Implement proper method in DB
                    pass

            # For now, just clean files older than 30 days that aren't in cache manifest
            import os
            from pathlib import Path

            cutoff = time.time() - (30 * 24 * 3600)  # 30 days

            cleaned = 0
            cleaned_size = 0

            for rpm_file in self.daemon.cache_dir.glob('**/*.rpm'):
                try:
                    stat = rpm_file.stat()
                    if stat.st_mtime < cutoff:
                        size = stat.st_size
                        rpm_file.unlink()
                        cleaned += 1
                        cleaned_size += size
                        logger.debug(f"Removed old cached file: {rpm_file.name}")
                except OSError as e:
                    logger.warning(f"Could not remove {rpm_file}: {e}")

            if cleaned > 0:
                logger.info(f"Cache cleanup: removed {cleaned} files ({cleaned_size / 1024 / 1024:.1f} MB)")
            else:
                logger.debug("Cache cleanup: no files to remove")

        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")
