"""Main urpmd daemon logic."""

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# Imports are relative to package - bin/urpmd handles sys.path
from ..core.database import PackageDatabase
from .server import UrpmdServer, DEFAULT_PORT, DEFAULT_HOST
from .scheduler import Scheduler

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DB_PATH = "/var/lib/urpm/packages.db"
DEFAULT_CONFIG_PATH = "/etc/urpm/urpmd.conf"
DEFAULT_CACHE_DIR = "/var/cache/urpm"
DEFAULT_PID_FILE = "/run/urpmd.pid"


class UrpmDaemon:
    """Main urpmd daemon class."""

    def __init__(self,
                 db_path: str = DEFAULT_DB_PATH,
                 host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT,
                 cache_dir: str = DEFAULT_CACHE_DIR):
        self.db_path = db_path
        self.host = host
        self.port = port
        self.cache_dir = Path(cache_dir)

        self.db: Optional[PackageDatabase] = None
        self.server: Optional[UrpmdServer] = None
        self.scheduler: Optional[Scheduler] = None

        self._running = False
        self._start_time: Optional[datetime] = None
        self._last_refresh: Optional[datetime] = None

    def start(self, foreground: bool = False):
        """Start the daemon.

        Args:
            foreground: If True, run in foreground. If False, daemonize.
        """
        if not foreground:
            self._daemonize()

        self._setup_signals()
        self._running = True
        self._start_time = datetime.now()

        # Initialize database
        logger.info(f"Opening database: {self.db_path}")
        self.db = PackageDatabase(self.db_path)

        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Start HTTP server
        self.server = UrpmdServer(self.host, self.port)
        self.server.start(self)

        # Start scheduler for background tasks
        self.scheduler = Scheduler(self)
        self.scheduler.start()

        logger.info("urpmd started successfully")

        # Run HTTP server (blocking)
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            self.stop()

    def stop(self):
        """Stop the daemon."""
        logger.info("Stopping urpmd...")
        self._running = False

        if self.scheduler:
            self.scheduler.stop()

        if self.server:
            self.server.stop()

        if self.db:
            self.db.close()

        logger.info("urpmd stopped")

    def _daemonize(self):
        """Daemonize the process (double fork)."""
        # First fork
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError as e:
            logger.error(f"First fork failed: {e}")
            sys.exit(1)

        # Decouple from parent
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # Second fork
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError as e:
            logger.error(f"Second fork failed: {e}")
            sys.exit(1)

        # Redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        with open('/dev/null', 'r') as devnull:
            os.dup2(devnull.fileno(), sys.stdin.fileno())
        with open('/dev/null', 'a+') as devnull:
            os.dup2(devnull.fileno(), sys.stdout.fileno())
            os.dup2(devnull.fileno(), sys.stderr.fileno())

        # Write PID file
        pid = os.getpid()
        with open(DEFAULT_PID_FILE, 'w') as f:
            f.write(str(pid))

    def _setup_signals(self):
        """Setup signal handlers."""
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_reload)

    def _signal_handler(self, signum, frame):
        """Handle termination signals."""
        logger.info(f"Received signal {signum}")
        self._running = False
        if self.server:
            # Use a thread to shutdown to avoid blocking the signal handler
            threading.Thread(target=self.server.stop).start()

    def _signal_reload(self, signum, frame):
        """Handle reload signal (SIGHUP)."""
        logger.info("Received SIGHUP, reloading configuration...")
        # TODO: Implement config reload
        self.refresh_metadata(force=True)

    # ========== API Methods ==========

    def get_status(self) -> Dict[str, Any]:
        """Get daemon status."""
        uptime = None
        if self._start_time:
            uptime = (datetime.now() - self._start_time).total_seconds()

        return {
            'running': self._running,
            'start_time': self._start_time.isoformat() if self._start_time else None,
            'uptime_seconds': uptime,
            'last_refresh': self._last_refresh.isoformat() if self._last_refresh else None,
            'db_path': self.db_path,
            'cache_dir': str(self.cache_dir),
            'host': self.host,
            'port': self.port,
        }

    def get_media_list(self) -> List[Dict[str, Any]]:
        """Get list of configured media."""
        if not self.db:
            return []

        media = []
        for m in self.db.list_media():
            media.append({
                'name': m['name'],
                'url': m['url'],
                'enabled': m['enabled'],
                'update': m['update'],
                'last_updated': m.get('last_updated'),
                'package_count': m.get('package_count', 0),
            })
        return media

    def check_available(self, packages: List[str]) -> Dict[str, Any]:
        """Check availability of packages.

        Args:
            packages: List of package names to check

        Returns:
            Dict with availability info for each package
        """
        if not self.db:
            return {'error': 'Database not initialized', 'packages': {}}

        result = {}
        for pkg_name in packages:
            pkg_info = self.db.get_package(pkg_name)
            if pkg_info:
                result[pkg_name] = {
                    'available': True,
                    'version': pkg_info.get('version'),
                    'release': pkg_info.get('release'),
                    'arch': pkg_info.get('arch'),
                    'media': pkg_info.get('media'),
                    'summary': pkg_info.get('summary'),
                }
            else:
                # Try search
                matches = self.db.search(pkg_name, limit=5)
                result[pkg_name] = {
                    'available': False,
                    'suggestions': [m['name'] for m in matches] if matches else [],
                }

        return {'packages': result}

    def get_available_updates(self) -> Dict[str, Any]:
        """Get list of packages with available updates."""
        if not self.db:
            return {'error': 'Database not initialized', 'updates': []}

        # Use resolver to find updates
        import platform
        from ..core.resolver import Resolver

        try:
            arch = platform.machine()
            resolver = Resolver(self.db, arch=arch)
            result = resolver.resolve_upgrade([])

            updates = []
            for action in result.actions:
                updates.append({
                    'name': action.name,
                    'current': action.from_evr,
                    'available': action.evr,
                    'arch': action.arch,
                    'size': action.size,
                })

            return {
                'count': len(updates),
                'updates': updates,
                'total_size': result.download_size,
            }
        except Exception as e:
            logger.error(f"Error checking updates: {e}")
            return {'error': str(e), 'updates': []}

    def refresh_metadata(self, media_name: Optional[str] = None,
                         force: bool = False) -> Dict[str, Any]:
        """Refresh metadata for media.

        Args:
            media_name: Specific media to refresh, or None for all
            force: Force refresh even if up-to-date

        Returns:
            Dict with refresh results
        """
        if not self.db:
            return {'error': 'Database not initialized'}

        from ..core.media import MediaManager

        try:
            manager = MediaManager(self.db)

            if media_name:
                results = {media_name: manager.update_media(media_name, force=force)}
            else:
                results = {}
                for media in self.db.list_media():
                    if media['enabled']:
                        name = media['name']
                        results[name] = manager.update_media(name, force=force)

            self._last_refresh = datetime.now()

            return {
                'success': True,
                'timestamp': self._last_refresh.isoformat(),
                'results': results,
            }
        except Exception as e:
            logger.error(f"Error refreshing metadata: {e}")
            return {'error': str(e)}


def main():
    """Main entry point for urpmd."""
    import argparse

    parser = argparse.ArgumentParser(
        description='urpmd - urpm daemon for intelligent cache management'
    )
    parser.add_argument(
        '-f', '--foreground',
        action='store_true',
        help='Run in foreground (do not daemonize)'
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=DEFAULT_PORT,
        help=f'HTTP port (default: {DEFAULT_PORT})'
    )
    parser.add_argument(
        '-H', '--host',
        default=DEFAULT_HOST,
        help=f'HTTP host (default: {DEFAULT_HOST})'
    )
    parser.add_argument(
        '-d', '--db',
        default=DEFAULT_DB_PATH,
        help=f'Database path (default: {DEFAULT_DB_PATH})'
    )
    parser.add_argument(
        '-c', '--cache',
        default=DEFAULT_CACHE_DIR,
        help=f'Cache directory (default: {DEFAULT_CACHE_DIR})'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose logging'
    )

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO

    if args.foreground:
        # Log to stderr when in foreground
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
    else:
        # Log to syslog when daemonized
        handler = logging.handlers.SysLogHandler(address='/dev/log')
        handler.setFormatter(logging.Formatter(
            'urpmd: %(levelname)s - %(message)s'
        ))

    logging.basicConfig(level=level, handlers=[handler])

    # Create and start daemon
    daemon = UrpmDaemon(
        db_path=args.db,
        host=args.host,
        port=args.port,
        cache_dir=args.cache,
    )

    daemon.start(foreground=args.foreground)


if __name__ == '__main__':
    main()
