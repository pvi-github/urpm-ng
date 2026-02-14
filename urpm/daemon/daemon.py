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
from ..core.config import (
    PROD_BASE_DIR, PROD_DB_PATH, PROD_PID_FILE, PROD_PORT,
    DEV_BASE_DIR, DEV_DB_PATH, DEV_PID_FILE, DEV_PORT,
    is_dev_mode, get_db_path, get_base_dir,
)
from .server import UrpmdServer, DEFAULT_PORT, DEFAULT_HOST
from .scheduler import Scheduler
from .discovery import PeerDiscovery

logger = logging.getLogger(__name__)


class UrpmDaemon:
    """Main urpmd daemon class."""

    def __init__(self,
                 db_path: str,
                 base_dir: str,
                 host: str,
                 port: int,
                 pid_file: str,
                 dev_mode: bool = False):
        self.db_path = db_path
        self.base_dir = Path(base_dir)
        self.host = host
        self.port = port
        self.pid_file = pid_file
        self.dev_mode = dev_mode

        self.db: Optional[PackageDatabase] = None
        self.server: Optional[UrpmdServer] = None
        self.scheduler: Optional[Scheduler] = None
        self.discovery: Optional[PeerDiscovery] = None

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

        # Ensure base directory exists
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Start HTTP server
        self.server = UrpmdServer(self.host, self.port)
        self.server.start(self)

        # Start scheduler for background tasks
        self.scheduler = Scheduler(self, dev_mode=self.dev_mode)
        self.scheduler.start()

        # Start peer discovery
        self.discovery = PeerDiscovery(self, dev_mode=self.dev_mode)
        self.discovery.start()

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

        if self.discovery:
            self.discovery.stop()

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
        with open(self.pid_file, 'w') as f:
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
            'db_path': str(self.db_path),
            'base_dir': str(self.base_dir),
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
                'update_media': m.get('update_media', 0),
                'last_sync': m.get('last_sync'),
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
            total_size = 0
            for action in result.actions:
                updates.append({
                    'name': action.name,
                    'current': action.from_evr,
                    'available': action.evr,
                    'arch': action.arch,
                    'size': action.size,
                })
                total_size += action.size or 0

            return {
                'count': len(updates),
                'updates': updates,
                'total_size': total_size,
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

        from ..core.sync import sync_media

        try:
            if media_name:
                result = sync_media(self.db, media_name, force=force)
                results = {media_name: {'success': result.success, 'packages': result.packages_count}}
            else:
                results = {}
                for media in self.db.list_media():
                    if media['enabled']:
                        name = media['name']
                        result = sync_media(self.db, name, force=force)
                        results[name] = {'success': result.success, 'packages': result.packages_count}

            self._last_refresh = datetime.now()

            return {
                'success': True,
                'timestamp': self._last_refresh.isoformat(),
                'results': results,
            }
        except Exception as e:
            logger.error(f"Error refreshing metadata: {e}")
            return {'error': str(e)}

    def rebuild_fts(self) -> Dict[str, Any]:
        """Rebuild FTS index for fast file search.

        Returns:
            Dict with rebuild results
        """
        if not self.scheduler:
            return {'error': 'Scheduler not initialized'}

        try:
            import time
            start_time = time.time()

            # Call scheduler's rebuild method
            self.scheduler._rebuild_fts_index()

            elapsed = time.time() - start_time
            stats = self.db.get_fts_stats() if self.db else {}

            return {
                'success': True,
                'indexed': stats.get('fts_count', 0),
                'elapsed': round(elapsed, 1),
            }
        except Exception as e:
            logger.error(f"Error rebuilding FTS index: {e}")
            return {'error': str(e)}

    def get_peers(self) -> List[Dict[str, Any]]:
        """Get list of known peers."""
        if self.discovery:
            return self.discovery.get_peers()
        return []

    def register_peer(self, host: str, port: int, media: List[str],
                       mirror_enabled: bool = False, local_version: str = "",
                       local_arch: str = "", served_media: List[Dict] = None) -> Dict[str, Any]:
        """Register or update a peer."""
        if self.discovery:
            return self.discovery.register_peer(
                host, port, media,
                mirror_enabled=mirror_enabled,
                local_version=local_version,
                local_arch=local_arch,
                served_media=served_media
            )
        return {'error': 'Discovery not initialized'}

    def check_have_packages(self, packages: List[str], version: str = None,
                            arch: str = None) -> Dict[str, Any]:
        """Check which packages are available in local cache.

        Searches recursively for RPM files in medias/ directory.
        Structure: official/<version>/<arch>/media/<type>/<release>/*.rpm

        Args:
            packages: List of RPM filenames to check
            version: Optional Mageia version filter (e.g., "10", "cauldron")
            arch: Optional architecture filter (e.g., "x86_64", "aarch64")

        Returns:
            Dict with 'available' (list with filename, size, path) and 'missing'

        When version/arch are specified, only packages from matching paths are
        returned. This enables multi-release support for chroot builds where
        a host (e.g., mga9) serves packages for a different release (e.g., mga10).
        """
        available = []
        missing = []

        medias_dir = self.base_dir / "medias"

        if not medias_dir.exists():
            return {
                'available': [],
                'missing': packages,
                'available_count': 0,
                'missing_count': len(packages),
            }

        # Build index of all available RPMs (filename -> relative path)
        # This is more efficient when checking many packages
        if not hasattr(self, '_rpm_index') or self._rpm_index is None:
            self._build_rpm_index()

        # Build path prefix filter for version/arch
        # Path structure: official/<version>/<arch>/media/...
        path_prefix = None
        if version and arch:
            path_prefix = f"official/{version}/{arch}/"
        elif version:
            path_prefix = f"official/{version}/"

        for filename in packages:
            if not filename or not filename.endswith('.rpm'):
                missing.append(filename or '<invalid>')
                continue

            if filename in self._rpm_index:
                info = self._rpm_index[filename]
                path = info['path']

                # Apply version/arch filter
                if path_prefix:
                    if not path.startswith(path_prefix):
                        missing.append(filename)
                        continue
                elif arch:
                    # Check arch in path without version filter
                    # Path format: official/<version>/<arch>/media/...
                    parts = path.split('/')
                    if len(parts) >= 3 and parts[2] != arch:
                        missing.append(filename)
                        continue

                available.append({
                    'filename': filename,
                    'size': info['size'],
                    'path': path,
                })
            else:
                missing.append(filename)

        return {
            'available': available,
            'missing': missing,
            'available_count': len(available),
            'missing_count': len(missing),
        }

    def _build_rpm_index(self):
        """Build index of all RPM files in medias directory and file:// servers."""
        self._rpm_index = {}
        medias_dir = self.base_dir / "medias"

        # Index files from cache directory
        if medias_dir.exists():
            for rpm_path in medias_dir.rglob("*.rpm"):
                if rpm_path.is_file():
                    try:
                        filename = rpm_path.name
                        size = rpm_path.stat().st_size
                        # Path relative to medias/ for URL construction
                        rel_path = str(rpm_path.relative_to(medias_dir))
                        self._rpm_index[filename] = {
                            'size': size,
                            'path': rel_path,
                        }
                    except OSError:
                        continue

        # Index files from file:// servers (local mirrors)
        if self.db:
            try:
                # Get all media with their file:// servers
                for media in self.db.list_media():
                    if not media.get('shared'):
                        continue  # Skip non-shared media

                    servers = self.db.get_servers_for_media(media['id'], enabled_only=True)
                    for server in servers:
                        if server['protocol'] != 'file':
                            continue

                        # Build local path: server.base_path + media.relative_path
                        local_path = Path(server['base_path']) / media['relative_path']
                        if not local_path.exists():
                            continue

                        # URL path: official/<relative_path>/<filename>
                        url_path_prefix = f"official/{media['relative_path']}"

                        # Index all RPMs in local mirror
                        for rpm_path in local_path.glob("*.rpm"):
                            if rpm_path.is_file():
                                try:
                                    filename = rpm_path.name
                                    # Don't overwrite if already indexed from cache
                                    if filename not in self._rpm_index:
                                        size = rpm_path.stat().st_size
                                        self._rpm_index[filename] = {
                                            'size': size,
                                            'path': f"{url_path_prefix}/{filename}",
                                        }
                                except OSError:
                                    continue
            except Exception:
                pass  # Ignore database errors, use cache only

    def invalidate_rpm_index(self):
        """Invalidate the RPM index so it will be rebuilt on next check."""
        self._rpm_index = None


class ColoredFormatter(logging.Formatter):
    """Colored log formatter for terminal output."""

    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[2m',      # Dim
        'INFO': '',              # Normal (no color)
        'WARNING': '\033[93m',   # Yellow/orange
        'ERROR': '\033[91m',     # Bright red
        'CRITICAL': '\033[91m',  # Bright red
    }
    RESET = '\033[0m'

    def format(self, record):
        # Get base formatted message
        message = super().format(record)

        # Apply color based on level
        color = self.COLORS.get(record.levelname, '')
        if color:
            return f"{color}{message}{self.RESET}"
        return message


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
        help=f'HTTP port (default: {PROD_PORT} prod, {DEV_PORT} dev)'
    )
    parser.add_argument(
        '-H', '--host',
        default=DEFAULT_HOST,
        help=f'HTTP host (default: {DEFAULT_HOST})'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose logging'
    )
    parser.add_argument(
        '--dev',
        action='store_true',
        help=f'Force development mode (auto-detected from .urpm.local or dev tree)'
    )
    parser.add_argument(
        '--prod',
        action='store_true',
        help=f'Force production mode (ignore .urpm.local)'
    )

    args = parser.parse_args()

    # Determine mode: explicit flags override auto-detection
    if args.dev and args.prod:
        print("Error: cannot specify both --dev and --prod", file=sys.stderr)
        sys.exit(1)

    if args.prod:
        dev_mode = False
    elif args.dev:
        dev_mode = True
    else:
        # Auto-detect based on .urpm.local or running from dev tree
        dev_mode = is_dev_mode()

    # Select paths based on mode
    if dev_mode:
        db_path = get_db_path(dev_mode=True)
        base_dir = get_base_dir(dev_mode=True)
        pid_file = DEV_PID_FILE
        port = args.port or DEV_PORT
        # Dev mode: listen on all interfaces for P2P testing
        if args.host == DEFAULT_HOST:
            args.host = '0.0.0.0'
        args.foreground = True
        args.verbose = True
    else:
        db_path = get_db_path(dev_mode=False)
        base_dir = get_base_dir(dev_mode=False)
        pid_file = PROD_PID_FILE
        port = args.port or PROD_PORT

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO

    if args.foreground:
        # Log to stderr when in foreground with colors
        handler = logging.StreamHandler()
        handler.setFormatter(ColoredFormatter(
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
        db_path=db_path,
        base_dir=base_dir,
        host=args.host,
        port=port,
        pid_file=pid_file,
        dev_mode=dev_mode,
    )

    daemon.start(foreground=args.foreground)


if __name__ == '__main__':
    main()
