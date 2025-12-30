"""Peer discovery for urpmd using UDP broadcast."""

import json
import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, TYPE_CHECKING
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

if TYPE_CHECKING:
    from .daemon import UrpmDaemon

from .. import __version__
from ..core.database import PackageDatabase
from ..core.config import PROD_DISCOVERY_PORT, DEV_DISCOVERY_PORT

logger = logging.getLogger(__name__)

# Discovery settings
DEFAULT_DISCOVERY_PORT = PROD_DISCOVERY_PORT  # UDP port for discovery broadcasts
BROADCAST_INTERVAL = 60  # Seconds between broadcasts
PEER_TIMEOUT = 180  # Seconds before considering a peer dead
DEV_BROADCAST_INTERVAL = 15  # Shorter interval for dev mode
DEV_PEER_TIMEOUT = 45

# Discovery message format
DISCOVERY_MAGIC = b'URPMD1'  # Protocol identifier


@dataclass
class Peer:
    """Represents a discovered urpmd peer."""
    host: str
    port: int
    media: List[str] = field(default_factory=list)
    last_seen: datetime = field(default_factory=datetime.now)
    version: str = ""
    # Proxy info (v11+)
    mirror_enabled: bool = False
    local_version: str = ""  # Peer's local Mageia version
    local_arch: str = ""     # Peer's local architecture
    served_media: List[dict] = field(default_factory=list)  # [{version, arch, types}]

    def is_alive(self, timeout: int = PEER_TIMEOUT) -> bool:
        """Check if peer is still considered alive."""
        return datetime.now() - self.last_seen < timedelta(seconds=timeout)

    def serves_version(self, version: str, arch: str = None) -> bool:
        """Check if this peer serves packages for a given Mageia version."""
        for sm in self.served_media:
            if sm.get('version') == version:
                if arch is None or sm.get('arch') == arch:
                    return True
        return False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'host': self.host,
            'port': self.port,
            'media': self.media,
            'last_seen': self.last_seen.isoformat(),
            'version': self.version,
            'alive': self.is_alive(),
            'mirror_enabled': self.mirror_enabled,
            'local_version': self.local_version,
            'local_arch': self.local_arch,
            'served_media': self.served_media,
        }


class PeerDiscovery:
    """Handles peer discovery via UDP broadcast and HTTP announcements.

    Discovery works in two ways:
    1. UDP broadcast: Periodically broadcast presence on LAN
    2. HTTP announce: When a broadcast is received, contact the peer via HTTP
       to exchange detailed information (media list, etc.)

    Has its own database connection (SQLite requires separate connections per thread).
    """

    def __init__(self, daemon: 'UrpmDaemon', dev_mode: bool = False):
        self.daemon = daemon
        self.db_path = daemon.db_path
        self.dev_mode = dev_mode

        # Own database connection (created in listener thread)
        self._db: Optional['PackageDatabase'] = None

        # Peers indexed by "host:port"
        self.peers: Dict[str, Peer] = {}
        self._peers_lock = threading.Lock()

        # Settings
        if dev_mode:
            self.broadcast_interval = DEV_BROADCAST_INTERVAL
            self.peer_timeout = DEV_PEER_TIMEOUT
            self.discovery_port = DEV_DISCOVERY_PORT
        else:
            self.broadcast_interval = BROADCAST_INTERVAL
            self.peer_timeout = PEER_TIMEOUT
            self.discovery_port = PROD_DISCOVERY_PORT

        # UDP socket for discovery
        self._udp_socket: Optional[socket.socket] = None

        # Threads
        self._running = False
        self._broadcast_thread: Optional[threading.Thread] = None
        self._listener_thread: Optional[threading.Thread] = None

    def start(self):
        """Start peer discovery."""
        self._running = True

        # Start UDP listener
        try:
            self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._udp_socket.bind(('', self.discovery_port))
            self._udp_socket.settimeout(1.0)  # Allow periodic checks for shutdown

            self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._listener_thread.start()
            logger.info(f"Peer discovery listening on UDP port {self.discovery_port}")
        except OSError as e:
            logger.warning(f"Could not start UDP discovery: {e}")

        # Start broadcast thread
        self._broadcast_thread = threading.Thread(target=self._broadcast_loop, daemon=True)
        self._broadcast_thread.start()
        logger.info("Peer discovery started")

    def stop(self):
        """Stop peer discovery."""
        self._running = False

        if self._udp_socket:
            try:
                self._udp_socket.close()
            except OSError:
                pass

        if self._listener_thread:
            self._listener_thread.join(timeout=2)
        if self._broadcast_thread:
            self._broadcast_thread.join(timeout=2)

        # DB connection is closed in _listen_loop's finally block

        logger.info("Peer discovery stopped")

    def get_peers(self) -> List[dict]:
        """Get list of known peers."""
        with self._peers_lock:
            # Clean up dead peers
            dead_peers = [
                key for key, peer in self.peers.items()
                if not peer.is_alive(self.peer_timeout)
            ]
            for key in dead_peers:
                logger.debug(f"Removing dead peer: {key}")
                del self.peers[key]

            return [peer.to_dict() for peer in self.peers.values()]

    def register_peer(self, host: str, port: int, media: List[str],
                       mirror_enabled: bool = False, local_version: str = "",
                       local_arch: str = "", served_media: List[dict] = None) -> dict:
        """Register or update a peer (called when receiving HTTP announce).

        Args:
            host: Peer host
            port: Peer port
            media: List of media names
            mirror_enabled: Whether peer has proxy mode enabled
            local_version: Peer's local Mageia version
            local_arch: Peer's architecture
            served_media: List of {version, arch, types} dicts
        """
        key = f"{host}:{port}"
        served_media = served_media or []

        with self._peers_lock:
            if key in self.peers:
                # Update existing peer
                peer = self.peers[key]
                peer.media = media
                peer.last_seen = datetime.now()
                peer.mirror_enabled = mirror_enabled
                peer.local_version = local_version
                peer.local_arch = local_arch
                peer.served_media = served_media
                logger.debug(f"Updated peer: {key}")
            else:
                # New peer
                peer = Peer(
                    host=host, port=port, media=media,
                    mirror_enabled=mirror_enabled,
                    local_version=local_version,
                    local_arch=local_arch,
                    served_media=served_media
                )
                self.peers[key] = peer
                served_info = f", serves {len(served_media)} version(s)" if served_media else ""
                logger.info(f"New peer discovered: {key} with {len(media)} media{served_info}")

        return {'status': 'ok', 'registered': True}

    def _broadcast_loop(self):
        """Periodically broadcast our presence with jitter."""
        # Initial random delay (1-50% of interval) to desynchronize machines
        # that start at the same time (install party, power outage recovery, etc.)
        initial_delay = random.randint(1, self.broadcast_interval // 2)
        logger.debug(f"Peer discovery: first broadcast in {initial_delay}s")

        for _ in range(initial_delay):
            if not self._running:
                return
            time.sleep(1)

        while self._running:
            try:
                self._send_broadcast()
            except Exception as e:
                logger.debug(f"Broadcast error: {e}")

            # Apply jitter: ±30% to prevent synchronization
            jitter = random.uniform(-0.30, 0.30)
            actual_interval = int(self.broadcast_interval * (1 + jitter))
            actual_interval = max(10, actual_interval)  # Minimum 10s

            # Sleep in small increments to allow quick shutdown
            for _ in range(actual_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _send_broadcast(self):
        """Send UDP broadcast announcing our presence."""
        if not self._udp_socket:
            return

        # Build broadcast message
        message = {
            'host': self._get_local_ip(),
            'port': self.daemon.port,
            'version': __version__,
        }

        data = DISCOVERY_MAGIC + json.dumps(message).encode('utf-8')

        try:
            # Send to broadcast address
            self._udp_socket.sendto(data, ('<broadcast>', self.discovery_port))
            logger.debug("Sent discovery broadcast")
        except OSError as e:
            logger.debug(f"Broadcast send failed: {e}")

    def _listen_loop(self):
        """Listen for discovery broadcasts from other peers."""
        # Create own database connection for this thread (SQLite thread safety)
        self._db = PackageDatabase(self.db_path)
        logger.debug("Discovery thread: database connection opened")

        try:
            while self._running:
                try:
                    data, addr = self._udp_socket.recvfrom(4096)
                    self._handle_broadcast(data, addr)
                except socket.timeout:
                    continue
                except OSError:
                    if self._running:
                        logger.debug("UDP socket error")
                    break
        finally:
            # Close DB connection when thread exits
            if self._db:
                self._db.close()
                logger.debug("Discovery thread: database connection closed")

    def _handle_broadcast(self, data: bytes, addr: tuple):
        """Handle received discovery broadcast."""
        sender_ip, sender_port = addr

        # Verify magic header
        if not data.startswith(DISCOVERY_MAGIC):
            return

        try:
            message = json.loads(data[len(DISCOVERY_MAGIC):].decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        peer_host = message.get('host', sender_ip)
        peer_port = message.get('port')

        if not peer_port:
            return

        # Don't register ourselves
        if self._is_self(peer_host, peer_port):
            return

        logger.debug(f"Received broadcast from {peer_host}:{peer_port}")

        # Contact peer via HTTP to get full info and register
        self._contact_peer(peer_host, peer_port)

    def _contact_peer(self, host: str, port: int):
        """Contact a peer via HTTP to exchange information."""
        key = f"{host}:{port}"

        # Check if we recently contacted this peer
        with self._peers_lock:
            if key in self.peers:
                peer = self.peers[key]
                # Don't re-contact if seen recently
                if datetime.now() - peer.last_seen < timedelta(seconds=self.broadcast_interval // 2):
                    return

        try:
            # Get peer's media list
            url = f"http://{host}:{port}/api/media"
            req = Request(url, method='GET')
            req.add_header('User-Agent', 'urpmd/0.1')
            req.add_header('Accept', 'application/json')

            response = urlopen(req, timeout=5)
            data = json.loads(response.read().decode('utf-8'))

            media_list = [m['name'] for m in data.get('media', [])]

            # Register the peer locally
            self.register_peer(host, port, media_list)

            # Announce ourselves to the peer
            self._announce_to_peer(host, port)

        except (URLError, HTTPError, json.JSONDecodeError, OSError) as e:
            logger.debug(f"Could not contact peer {key}: {e}")

    def _announce_to_peer(self, host: str, port: int):
        """Send HTTP announce to a peer."""
        try:
            import platform
            url = f"http://{host}:{port}/api/announce"

            # Get our media list and proxy info (use own DB connection for thread safety)
            media_list = []
            served_media = []  # [{version, arch, types}]
            mirror_enabled = False
            local_version = ""
            local_arch = platform.machine()

            if self._db:
                mirror_enabled = self._db.is_mirror_enabled()

                # Group media by version/arch for served_media
                version_arch_types = {}  # (version, arch) -> [types]

                for m in self._db.list_media():
                    media_list.append(m['name'])

                    # Get local Mageia version from first enabled media
                    if not local_version and m.get('enabled'):
                        local_version = m.get('mageia_version', '')

                    # Only include in served_media if proxy is enabled for this media
                    if mirror_enabled and m.get('shared', 1) and m.get('enabled'):
                        key = (m.get('mageia_version', ''), m.get('architecture', ''))
                        if key not in version_arch_types:
                            version_arch_types[key] = []
                        version_arch_types[key].append(m.get('short_name', m['name']))

                # Build served_media list
                for (ver, arch), types in version_arch_types.items():
                    served_media.append({
                        'version': ver,
                        'arch': arch,
                        'types': types
                    })

            payload = json.dumps({
                'host': self._get_local_ip(),
                'port': self.daemon.port,
                'media': media_list,
                'mirror_enabled': mirror_enabled,
                'local_version': local_version,
                'local_arch': local_arch,
                'served_media': served_media,
            }).encode('utf-8')

            req = Request(url, data=payload, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('User-Agent', 'urpmd/0.1')

            urlopen(req, timeout=5)
            logger.debug(f"Announced to peer {host}:{port}")

        except (URLError, HTTPError, OSError) as e:
            logger.debug(f"Could not announce to peer {host}:{port}: {e}")

    def _is_self(self, host: str, port: int) -> bool:
        """Check if host:port refers to ourselves."""
        if port != self.daemon.port:
            return False

        local_ip = self._get_local_ip()
        if host == local_ip:
            return True
        if host in ('127.0.0.1', 'localhost', '::1'):
            return True

        return False

    def _get_local_ip(self) -> str:
        """Get local IP address for LAN communication."""
        try:
            # Create a dummy connection to determine local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return '127.0.0.1'
