"""
Peer client for P2P package downloads.

Discovers urpmd peers on the LAN and queries them for package availability.
Used by the Downloader to distribute downloads across peers + upstream mirrors.
"""

import json
import logging
import socket
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import PROD_PORT, DEV_PORT, PROD_DISCOVERY_PORT, DEV_DISCOVERY_PORT, is_dev_mode
from .. import __version__

logger = logging.getLogger(__name__)

# Discovery constants
DISCOVERY_MAGIC = b'URPMD1'
DEFAULT_URPMD_PORT = PROD_PORT
DEV_URPMD_PORT = DEV_PORT
DEFAULT_DISCOVERY_PORT = PROD_DISCOVERY_PORT


@dataclass
class Peer:
    """A discovered urpmd peer."""
    host: str
    port: int
    media: List[str] = field(default_factory=list)
    mirror_enabled: bool = False
    local_version: str = ''
    local_arch: str = ''
    served_media: List[dict] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __hash__(self):
        return hash((self.host, self.port))

    def __eq__(self, other):
        if not isinstance(other, Peer):
            return False
        return self.host == other.host and self.port == other.port


@dataclass
class PeerPackageInfo:
    """Info about a package available on a peer."""
    filename: str
    size: int
    path: str  # Relative path for download URL
    peer: Peer


class PeerClient:
    """Client for discovering peers and querying package availability."""

    def __init__(self, urpmd_port: int = None, timeout: float = 2.0, dev_mode: bool = False):
        """Initialize peer client.

        Args:
            urpmd_port: Local urpmd port to query (None = auto-detect)
            timeout: Timeout for HTTP requests in seconds
            dev_mode: If True, use dev discovery port (9879) instead of prod (9878)
        """
        self.urpmd_port = urpmd_port
        self.timeout = timeout
        self.dev_mode = dev_mode
        self.discovery_port = DEV_DISCOVERY_PORT if dev_mode else PROD_DISCOVERY_PORT
        self._peers: List[Peer] = []

    def discover_peers(self) -> List[Peer]:
        """Discover peers on the LAN.

        First tries local urpmd (which maintains a peer list),
        then falls back to direct UDP broadcast scan,
        then tries container host fallback.

        Returns:
            List of discovered peers
        """
        # Try local urpmd first (already has peer list from discovery)
        peers = self._query_local_urpmd()
        if peers:
            logger.info(f"Got {len(peers)} peers from local urpmd")
            self._peers = peers
            return peers

        # Fallback: direct UDP scan
        peers = self._scan_lan_udp()
        if peers:
            logger.info(f"Found {len(peers)} peers via UDP scan")
            self._peers = peers
            return peers

        # Container fallback: try to reach host urpmd
        peers = self._try_container_host()
        if peers:
            logger.info(f"Found {len(peers)} peers via container host fallback")
        else:
            logger.debug("No peers found")
        self._peers = peers
        return peers

    def filter_peers_for_version(self, peers: List[Peer], version: str, arch: str = None) -> List[Peer]:
        """Filter peers to only those serving a specific Mageia version.

        Args:
            peers: List of peers to filter
            version: Mageia version (e.g., '10')
            arch: Architecture (e.g., 'x86_64'), None = any

        Returns:
            Peers that serve the specified version/arch
        """
        result = []
        for peer in peers:
            # Always include peers without served_media info (legacy or same version)
            if not peer.served_media:
                # Legacy peer or same version - include if version matches
                if peer.local_version == version or not peer.local_version:
                    result.append(peer)
                continue

            # Check if peer serves this version/arch
            if peer.serves_version(version, arch):
                result.append(peer)

        return result

    def discover_peers_for_version(self, version: str, arch: str = None) -> List[Peer]:
        """Discover peers that serve a specific Mageia version.

        Convenience method that combines discover_peers() with filter_peers_for_version().

        Args:
            version: Mageia version (e.g., '10')
            arch: Architecture (e.g., 'x86_64'), None = any

        Returns:
            Filtered list of peers
        """
        all_peers = self.discover_peers()
        return self.filter_peers_for_version(all_peers, version, arch)

    def _query_local_urpmd(self) -> List[Peer]:
        """Query local urpmd for known peers.

        Includes local urpmd itself as first peer.
        """
        ports_to_try = []
        if self.urpmd_port:
            ports_to_try = [self.urpmd_port]
        else:
            # Try dev port first, then prod
            ports_to_try = [DEV_URPMD_PORT, DEFAULT_URPMD_PORT]

        for port in ports_to_try:
            try:
                url = f"http://127.0.0.1:{port}/api/peers"
                req = urllib.request.Request(url)
                req.add_header('Accept', 'application/json')

                with urllib.request.urlopen(req, timeout=1) as response:
                    data = json.loads(response.read().decode('utf-8'))

                    # Local urpmd first
                    peers = [Peer(host='127.0.0.1', port=port)]

                    # Other peers
                    for p in data.get('peers', []):
                        if p.get('alive', True):
                            peers.append(Peer(
                                host=p['host'],
                                port=p['port'],
                                media=p.get('media', []),
                                mirror_enabled=p.get('mirror_enabled', False),
                                local_version=p.get('local_version', ''),
                                local_arch=p.get('local_arch', ''),
                                served_media=p.get('served_media', [])
                            ))
                    return peers

            except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
                continue

        return []

    def _try_container_host(self) -> List[Peer]:
        """Try to reach host urpmd from inside a container.

        Tries container-specific hostnames managed by the runtime:
        - host.containers.internal (podman)
        - host.docker.internal (docker)

        Returns:
            List with host peer if reachable, empty otherwise
        """
        port = DEV_URPMD_PORT if self.dev_mode else DEFAULT_URPMD_PORT

        # Container-specific hostnames (managed by runtime, safe to trust)
        hostnames = [
            'host.containers.internal',  # podman
            'host.docker.internal',      # docker
        ]

        for host in hostnames:
            try:
                url = f"http://{host}:{port}/api/status"
                req = urllib.request.Request(url)
                req.add_header('Accept', 'application/json')

                with urllib.request.urlopen(req, timeout=1) as response:
                    if response.status == 200:
                        logger.debug(f"Found host urpmd at {host}:{port}")
                        return [Peer(host=host, port=port)]

            except (urllib.error.URLError, urllib.error.HTTPError, OSError, socket.timeout):
                continue

        return []

    def _scan_lan_udp(self) -> List[Peer]:
        """Direct UDP broadcast scan for peers."""
        peers = []
        seen = set()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(self.timeout)

            # Send discovery broadcast
            local_ip = self._get_local_ip()
            message = {
                'host': local_ip,
                'port': 0,  # We're not a server
                'version': __version__,
            }
            data = DISCOVERY_MAGIC + json.dumps(message).encode('utf-8')
            sock.sendto(data, ('<broadcast>', self.discovery_port))

            # Collect responses
            while True:
                try:
                    response_data, addr = sock.recvfrom(4096)
                    if not response_data.startswith(DISCOVERY_MAGIC):
                        continue

                    msg = json.loads(response_data[len(DISCOVERY_MAGIC):].decode('utf-8'))
                    peer_host = msg.get('host', addr[0])
                    peer_port = msg.get('port')

                    if peer_port and peer_host != local_ip:
                        key = (peer_host, peer_port)
                        if key not in seen:
                            seen.add(key)
                            peers.append(Peer(host=peer_host, port=peer_port))

                except socket.timeout:
                    break
                except (json.JSONDecodeError, KeyError):
                    continue

            sock.close()

        except OSError as e:
            logger.debug(f"UDP scan failed: {e}")

        return peers

    def _get_local_ip(self) -> str:
        """Get local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return '127.0.0.1'

    def query_peers_have(self, peers: List[Peer], filenames: List[str],
                         version: str = None, arch: str = None
                         ) -> Dict[str, List[PeerPackageInfo]]:
        """Query multiple peers for package availability.

        Args:
            peers: List of peers to query
            filenames: List of RPM filenames to check
            version: Optional Mageia version filter (e.g., "10", "cauldron")
            arch: Optional architecture filter (e.g., "x86_64")

        Returns:
            Dict mapping filename -> list of PeerPackageInfo (peers that have it)
        """
        if not peers or not filenames:
            return {}

        # Query all peers in parallel
        results: Dict[str, List[PeerPackageInfo]] = {f: [] for f in filenames}

        def query_one_peer(peer: Peer) -> Optional[Dict]:
            """Query a single peer."""
            try:
                url = f"{peer.base_url}/api/have"
                # Build payload with optional version/arch filters
                payload_dict = {'packages': filenames}
                if version:
                    payload_dict['version'] = version
                if arch:
                    payload_dict['arch'] = arch
                payload = json.dumps(payload_dict).encode('utf-8')

                req = urllib.request.Request(url, data=payload, method='POST')
                req.add_header('Content-Type', 'application/json')
                req.add_header('Accept', 'application/json')

                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    return {'peer': peer, 'data': data}

            except (urllib.error.URLError, urllib.error.HTTPError,
                    OSError, json.JSONDecodeError) as e:
                logger.debug(f"Failed to query peer {peer.host}:{peer.port}: {e}")
                return None

        # Parallel queries
        with ThreadPoolExecutor(max_workers=min(len(peers), 10)) as executor:
            futures = {executor.submit(query_one_peer, peer): peer for peer in peers}

            for future in as_completed(futures):
                result = future.result()
                if not result:
                    continue

                peer = result['peer']
                data = result['data']

                for pkg_info in data.get('available', []):
                    filename = pkg_info.get('filename')
                    if filename in results:
                        results[filename].append(PeerPackageInfo(
                            filename=filename,
                            size=pkg_info.get('size', 0),
                            path=pkg_info.get('path', ''),
                            peer=peer
                        ))

        return results

    def query_have(self, filenames: List[str], version: str = None,
                   arch: str = None) -> Dict[str, List[PeerPackageInfo]]:
        """Discover peers and query them for package availability.

        Convenience method that combines discover_peers() and query_peers_have().

        Args:
            filenames: List of RPM filenames to check
            version: Optional Mageia version filter
            arch: Optional architecture filter

        Returns:
            Dict mapping filename -> list of PeerPackageInfo
        """
        peers = self.discover_peers()
        if not peers:
            return {f: [] for f in filenames}
        return self.query_peers_have(peers, filenames, version=version, arch=arch)


@dataclass
class DownloadAssignment:
    """Assignment of a package to a download source."""
    filename: str
    source: str  # 'peer' or 'upstream'
    peer: Optional[Peer] = None  # If source == 'peer'
    peer_path: str = ""  # Path for peer download URL
    size: int = 0


def create_download_plan(
    filenames: List[str],
    peer_availability: Dict[str, List[PeerPackageInfo]]
) -> List[DownloadAssignment]:
    """Create a load-balanced download plan.

    Distributes packages across peers that have them, using round-robin
    to balance the load. Packages not available on any peer are assigned
    to upstream mirrors.

    Args:
        filenames: List of all packages to download
        peer_availability: Dict from query_peers_have() mapping filename -> peer infos

    Returns:
        List of DownloadAssignment specifying where to download each package
    """
    assignments = []

    # Track how many packages each peer is assigned (for balancing)
    peer_load: Dict[Peer, int] = {}

    for filename in filenames:
        available_on = peer_availability.get(filename, [])

        if not available_on:
            # No peer has it -> upstream
            assignments.append(DownloadAssignment(
                filename=filename,
                source='upstream'
            ))
        else:
            # Pick peer with lowest current load (load balancing)
            # Initialize load counters for new peers
            for info in available_on:
                if info.peer not in peer_load:
                    peer_load[info.peer] = 0

            # Find peer with minimum load among those that have this package
            best_info = min(available_on, key=lambda info: peer_load[info.peer])

            assignments.append(DownloadAssignment(
                filename=filename,
                source='peer',
                peer=best_info.peer,
                peer_path=best_info.path,
                size=best_info.size
            ))

            # Update load counter
            peer_load[best_info.peer] += 1

    return assignments


def summarize_download_plan(assignments: List[DownloadAssignment]) -> Dict[str, any]:
    """Summarize a download plan for display.

    Returns:
        Dict with 'from_peers', 'from_upstream', 'peer_breakdown'
    """
    from_peers = []
    from_upstream = []
    peer_breakdown: Dict[str, int] = {}  # peer host -> count

    for a in assignments:
        if a.source == 'peer':
            from_peers.append(a.filename)
            host = a.peer.host if a.peer else 'unknown'
            peer_breakdown[host] = peer_breakdown.get(host, 0) + 1
        else:
            from_upstream.append(a.filename)

    return {
        'from_peers': from_peers,
        'from_upstream': from_upstream,
        'from_peers_count': len(from_peers),
        'from_upstream_count': len(from_upstream),
        'peer_breakdown': peer_breakdown,
    }


# Module-level convenience
_client: Optional[PeerClient] = None


def get_peer_client() -> PeerClient:
    """Get or create the default peer client."""
    global _client
    if _client is None:
        _client = PeerClient(dev_mode=is_dev_mode())
    return _client
