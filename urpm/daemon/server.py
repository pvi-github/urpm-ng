"""HTTP server for urpmd."""

import json
import logging
import mimetypes
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs, unquote

from .. import __version__

logger = logging.getLogger(__name__)

# Initialize mimetypes
mimetypes.init()
mimetypes.add_type('application/x-rpm', '.rpm')
mimetypes.add_type('application/x-compressed', '.cz')

# Default configuration
DEFAULT_PORT = 9876
DEFAULT_HOST = "0.0.0.0"  # All interfaces for P2P (firewall controls access)


class UrpmdHandler(BaseHTTPRequestHandler):
    """HTTP request handler for urpmd."""

    # Reference to the daemon instance (set by UrpmdServer)
    daemon = None

    def log_message(self, format, *args):
        """Override to use logging module."""
        logger.info("%s - %s", self.address_string(), format % args)

    def send_json(self, data: Dict[str, Any], status: int = 200):
        """Send JSON response."""
        body = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str):
        """Send JSON error response."""
        self.send_json({'error': message}, status)

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path)  # Decode URL-encoded characters
        query = parse_qs(parsed.query)

        # Route requests
        if path == '/' or path == '':
            self.handle_root()
        elif path == '/api/ping':
            self.handle_ping()
        elif path == '/api/status':
            self.handle_status()
        elif path == '/api/media':
            self.handle_media_api()
        elif path == '/api/available':
            self.handle_available(query)
        elif path == '/api/updates':
            self.handle_updates()
        elif path == '/api/peers':
            self.handle_peers()
        elif path.startswith('/media'):
            # File serving endpoint
            self.handle_media_files(path)
        else:
            self.send_error_json(404, f"Unknown endpoint: {path}")

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        # Read body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b''

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_error_json(400, "Invalid JSON")
            return

        # Route requests
        if path == '/api/refresh':
            self.handle_refresh(data)
        elif path == '/api/available':
            self.handle_available_post(data)
        elif path == '/api/announce':
            self.handle_announce(data)
        elif path == '/api/have':
            self.handle_have(data)
        elif path == '/api/invalidate-cache':
            self.handle_invalidate_cache()
        elif path == '/api/rebuild-fts':
            self.handle_rebuild_fts()
        else:
            self.send_error_json(404, f"Unknown endpoint: {path}")

    def handle_root(self):
        """Root endpoint - service info."""
        self.send_json({
            'service': 'urpmd',
            'version': __version__,
            'endpoints': {
                'api': ['/api/ping', '/api/status', '/api/media', '/api/available',
                        '/api/updates', '/api/refresh', '/api/peers', '/api/announce',
                        '/api/have', '/api/rebuild-fts'],
                'files': ['/media/'],
            }
        })

    def handle_ping(self):
        """Health check endpoint."""
        self.send_json({
            'status': 'ok',
            'service': 'urpmd',
            'version': __version__,
        })

    def handle_status(self):
        """Daemon status endpoint."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        status = self.daemon.get_status()
        self.send_json(status)

    def handle_media_api(self):
        """List configured media (JSON API)."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        media = self.daemon.get_media_list()
        self.send_json({'media': media})

    def handle_media_files(self, path: str):
        """Serve media files and directory listings.

        URL structure (hierarchical navigation of medias/ directory):
            /media/                    → list top-level dirs (official, custom)
            /media/official/           → list versions (10, 9, ...)
            /media/official/10/        → list architectures (x86_64, i686)
            /media/official/10/x86_64/ → continue navigating...
            /media/.../file.rpm        → serve file

        The path structure mirrors the local cache:
            <base_dir>/medias/official/<version>/<arch>/media/<type>/<release>/
        """
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        # Parse path: /media/[level1]/[level2]/[subpath]
        # With new structure: level1=official/custom, level2=version, subpath=arch/...
        parts = path.split('/')
        # parts[0] = '', parts[1] = 'media', parts[2] = level1, etc.

        if len(parts) <= 2 or (len(parts) == 3 and parts[2] == ''):
            # /media/ → list top-level directories
            self._list_top_level()
        elif len(parts) == 3 or (len(parts) == 4 and parts[3] == ''):
            # /media/<level1>/ → list subdirectories
            level1 = parts[2]
            self._list_subdirs(level1)
        else:
            # /media/<level1>/<level2>/... → navigate deeper or serve file
            level1 = parts[2]
            level2 = parts[3]
            subpath = '/'.join(parts[4:]) if len(parts) > 4 else ''
            self._serve_path(level1, level2, subpath)

    def _list_top_level(self):
        """List top-level directories (official, custom)."""
        medias_dir = self.daemon.base_dir / "medias"

        if not medias_dir.exists():
            self.send_json({'directories': [], 'count': 0})
            return

        dirs = []
        for entry in sorted(medias_dir.iterdir()):
            if entry.is_dir():
                dirs.append(entry.name)

        # Check Accept header for response format
        accept = self.headers.get('Accept', '')
        if 'text/html' in accept or 'application/json' not in accept:
            self._send_directory_html('/', dirs, is_root=True)
        else:
            self.send_json({'directories': dirs, 'count': len(dirs)})

    def _list_subdirs(self, parent: str):
        """List subdirectories of a parent directory."""
        parent_dir = self.daemon.base_dir / "medias" / parent

        if not parent_dir.exists():
            self.send_error_json(404, f"Not found: {parent}")
            return

        subdirs = []
        for entry in sorted(parent_dir.iterdir()):
            if entry.is_dir():
                subdirs.append(entry.name)

        accept = self.headers.get('Accept', '')
        if 'text/html' in accept or 'application/json' not in accept:
            self._send_directory_html(f'/{parent}/', subdirs)
        else:
            self.send_json({'path': parent, 'directories': subdirs, 'count': len(subdirs)})

    def _serve_path(self, level1: str, level2: str, subpath: str):
        """Serve a file or directory listing."""
        base_dir = self.daemon.base_dir / "medias" / level1 / level2
        target_path = base_dir / subpath if subpath else base_dir

        # Security: prevent path traversal
        try:
            target_path = target_path.resolve()
            base_dir_resolved = base_dir.resolve()
            if not str(target_path).startswith(str(base_dir_resolved)):
                self.send_error_json(403, "Access denied")
                return
        except (OSError, ValueError):
            self.send_error_json(400, "Invalid path")
            return

        # If file doesn't exist in cache, try file:// servers
        if not target_path.exists() and subpath and self.daemon.db:
            alt_path = self._find_in_file_server(level2, subpath)
            if alt_path:
                target_path = alt_path

        if not target_path.exists():
            self.send_error_json(404, f"Not found: {level1}/{level2}/{subpath}" if subpath else f"{level1}/{level2}")
            return

        if target_path.is_dir():
            self._send_directory_listing(target_path, level1, level2, subpath)
        else:
            self._send_file(target_path)

    def _find_in_file_server(self, version: str, subpath: str) -> Optional[Path]:
        """Find a file in file:// servers.

        Args:
            version: Mageia version (e.g., "10")
            subpath: Path after version (e.g., "x86_64/media/core/release/foo.rpm")

        Returns:
            Path to file if found, None otherwise
        """
        # Reconstruct relative_path: version/subpath_without_filename
        subpath_parts = subpath.split('/')
        if len(subpath_parts) < 4:
            return None

        # Check if last part is a file
        if '.' in subpath_parts[-1]:
            dir_path = '/'.join(subpath_parts[:-1])
            filename = subpath_parts[-1]
        else:
            return None  # Directory listing not supported from file:// servers

        relative_path = f"{version}/{dir_path}"

        try:
            # Find media by relative_path
            cursor = self.daemon.db.conn.execute(
                "SELECT id FROM media WHERE relative_path = ? AND shared = 1",
                (relative_path,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            media_id = row['id']

            # Get file:// servers for this media
            servers = self.daemon.db.get_servers_for_media(media_id, enabled_only=True)
            for server in servers:
                if server['protocol'] != 'file':
                    continue

                # Build full path
                local_path = Path(server['base_path']) / relative_path / filename

                # Security check
                try:
                    local_path = local_path.resolve()
                    base_resolved = Path(server['base_path']).resolve()
                    if not str(local_path).startswith(str(base_resolved)):
                        continue  # Path traversal attempt
                except (OSError, ValueError):
                    continue

                if local_path.exists() and local_path.is_file():
                    return local_path

        except Exception:
            pass

        return None

    def _send_directory_listing(self, dir_path: Path, level1: str, level2: str, subpath: str):
        """Send directory listing as JSON or HTML."""
        entries = []
        for entry in sorted(dir_path.iterdir()):
            stat = entry.stat()
            entries.append({
                'name': entry.name,
                'type': 'dir' if entry.is_dir() else 'file',
                'size': stat.st_size if entry.is_file() else None,
            })

        accept = self.headers.get('Accept', '')
        if 'text/html' in accept or 'application/json' not in accept:
            names = [e['name'] + ('/' if e['type'] == 'dir' else '') for e in entries]
            current_path = f'/{level1}/{level2}'
            if subpath:
                current_path += f'/{subpath}'
            self._send_directory_html(current_path, names)
        else:
            self.send_json({
                'path': f'{level1}/{level2}/{subpath}' if subpath else f'{level1}/{level2}',
                'entries': entries,
                'count': len(entries),
            })

    def _send_directory_html(self, current_path: str, items: List[str], is_root: bool = False):
        """Send directory listing as HTML."""
        title = f"Index of /media{current_path}"
        lines = [
            '<!DOCTYPE html>',
            '<html><head>',
            f'<title>{title}</title>',
            '<style>',
            'body { font-family: monospace; margin: 2em; }',
            'a { text-decoration: none; }',
            'a:hover { text-decoration: underline; }',
            '.dir { color: #0066cc; }',
            '.file { color: #333; }',
            '</style>',
            '</head><body>',
            f'<h1>{title}</h1>',
            '<hr><pre>',
        ]

        # Parent directory link
        if not is_root:
            parent = '/'.join(current_path.rstrip('/').split('/')[:-1]) or '/'
            lines.append(f'<a href="/media{parent}">..</a>')

        # List items
        for item in items:
            is_dir = item.endswith('/')
            css_class = 'dir' if is_dir else 'file'
            href = f"/media{current_path.rstrip('/')}/{item}"
            lines.append(f'<a class="{css_class}" href="{href}">{item}</a>')

        lines.extend([
            '</pre><hr>',
            '<p><em>urpmd file server</em></p>',
            '</body></html>',
        ])

        body = '\n'.join(lines).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path):
        """Send a file with appropriate Content-Type."""
        try:
            stat = file_path.stat()
            file_size = stat.st_size
        except OSError as e:
            self.send_error_json(500, f"Cannot read file: {e}")
            return

        # Determine content type
        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = 'application/octet-stream'

        # Send headers
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', file_size)
        self.send_header('Content-Disposition', f'inline; filename="{file_path.name}"')
        self.end_headers()

        # Send file content
        try:
            with open(file_path, 'rb') as f:
                # Send in chunks for large files
                chunk_size = 64 * 1024  # 64 KB
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, BrokenPipeError) as e:
            logger.warning(f"Error sending file {file_path}: {e}")

    def handle_available(self, query: Dict[str, list]):
        """Check package availability (GET with query params)."""
        packages = query.get('pkg', [])
        if not packages:
            self.send_error_json(400, "Missing 'pkg' parameter")
            return

        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        result = self.daemon.check_available(packages)
        self.send_json(result)

    def handle_available_post(self, data: Dict[str, Any]):
        """Check package availability (POST with JSON body)."""
        packages = data.get('packages', [])
        if not packages:
            self.send_error_json(400, "Missing 'packages' in request body")
            return

        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        result = self.daemon.check_available(packages)
        self.send_json(result)

    def handle_updates(self):
        """List available updates."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        updates = self.daemon.get_available_updates()
        self.send_json(updates)

    def handle_refresh(self, data: Dict[str, Any]):
        """Trigger metadata refresh."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        media_name = data.get('media')  # None = all media
        force = data.get('force', False)

        result = self.daemon.refresh_metadata(media_name, force)
        self.send_json(result)

    def handle_invalidate_cache(self):
        """Invalidate the RPM cache index so it will be rebuilt on next query."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        self.daemon.invalidate_rpm_index()
        self.send_json({'status': 'ok', 'message': 'Cache index invalidated'})

    def handle_rebuild_fts(self):
        """Rebuild FTS index for fast file search."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        result = self.daemon.rebuild_fts()
        self.send_json(result)

    def handle_peers(self):
        """List known peers."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        peers = self.daemon.get_peers()
        self.send_json({'peers': peers, 'count': len(peers)})

    def handle_announce(self, data: Dict[str, Any]):
        """Handle peer announcement (called by other urpmd instances)."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        # Get peer info from request
        host = data.get('host')
        port = data.get('port')
        media_list = data.get('media', [])

        # Mirror/sharing fields (v11+, renamed from proxy_enabled in v14)
        # Support both names for backward compatibility
        mirror_enabled = data.get('mirror_enabled', data.get('proxy_enabled', False))
        local_version = data.get('local_version', '')
        local_arch = data.get('local_arch', '')
        served_media = data.get('served_media', [])

        if not host or not port:
            self.send_error_json(400, "Missing 'host' or 'port' in request")
            return

        # Register the peer
        result = self.daemon.register_peer(
            host, port, media_list,
            mirror_enabled=mirror_enabled,
            local_version=local_version,
            local_arch=local_arch,
            served_media=served_media
        )
        self.send_json(result)

    def handle_have(self, data: Dict[str, Any]):
        """Check which packages are available in local cache.

        Request body:
            {
                "packages": ["foo-1.0-1.mga10.x86_64.rpm", "bar-2.0-1.mga10.x86_64.rpm", ...],
                "version": "10",      # Optional: filter to specific Mageia version
                "arch": "x86_64"      # Optional: filter to specific architecture
            }

        Response:
            {
                "available": [
                    {"filename": "foo-1.0-1.mga10.x86_64.rpm", "size": 12345,
                     "path": "official/10/x86_64/media/core/release/foo-1.0-1.mga10.x86_64.rpm"}
                ],
                "missing": ["bar-2.0-1.mga10.x86_64.rpm"],
                "available_count": 1,
                "missing_count": 1
            }

        The 'path' can be used to download: http://peer:port/media/{path}

        When version/arch are specified, only packages from matching media paths
        are returned (e.g., official/10/x86_64/...). This enables multi-release
        support for chroot builds.
        """
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        packages = data.get('packages', [])
        if not packages:
            self.send_error_json(400, "Missing 'packages' in request body")
            return

        if not isinstance(packages, list):
            self.send_error_json(400, "'packages' must be a list of filenames")
            return

        # Optional version/arch filters for multi-release support
        version = data.get('version')
        arch = data.get('arch')

        result = self.daemon.check_have_packages(packages, version=version, arch=arch)
        self.send_json(result)


class UrpmdServer:
    """urpmd HTTP server wrapper."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, daemon):
        """Start the HTTP server.

        Args:
            daemon: The UrpmDaemon instance to handle requests
        """
        # Set daemon reference on handler class
        UrpmdHandler.daemon = daemon

        # Use ThreadingHTTPServer for concurrent request handling
        # This allows multiple parallel downloads from peers
        self.server = ThreadingHTTPServer((self.host, self.port), UrpmdHandler)
        logger.info(f"urpmd HTTP server listening on {self.host}:{self.port}")

    def serve_forever(self):
        """Run server in current thread (blocking)."""
        if self.server:
            self.server.serve_forever()

    def start_background(self, daemon):
        """Start server in background thread."""
        self.start(daemon)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the server."""
        if self.server:
            self.server.shutdown()
            logger.info("urpmd HTTP server stopped")
