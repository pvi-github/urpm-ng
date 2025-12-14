"""HTTP server for urpmd."""

import json
import logging
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_PORT = 9876
DEFAULT_HOST = "127.0.0.1"  # Localhost only by default for security


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
        path = parsed.path.rstrip('/')
        query = parse_qs(parsed.query)

        # Route requests
        if path == '/ping' or path == '':
            self.handle_ping()
        elif path == '/status':
            self.handle_status()
        elif path == '/media':
            self.handle_media_list()
        elif path == '/available':
            self.handle_available(query)
        elif path == '/updates':
            self.handle_updates()
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
        if path == '/refresh':
            self.handle_refresh(data)
        elif path == '/available':
            self.handle_available_post(data)
        else:
            self.send_error_json(404, f"Unknown endpoint: {path}")

    def handle_ping(self):
        """Health check endpoint."""
        self.send_json({
            'status': 'ok',
            'service': 'urpmd',
            'version': '0.1.0',
        })

    def handle_status(self):
        """Daemon status endpoint."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        status = self.daemon.get_status()
        self.send_json(status)

    def handle_media_list(self):
        """List configured media."""
        if not self.daemon:
            self.send_error_json(500, "Daemon not initialized")
            return

        media = self.daemon.get_media_list()
        self.send_json({'media': media})

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

        self.server = HTTPServer((self.host, self.port), UrpmdHandler)
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
