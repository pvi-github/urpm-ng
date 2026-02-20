"""Server management database operations."""

import time
from typing import Dict, List, Optional


class ServerMixin:
    """Mixin providing server CRUD operations.

    Requires:
        - self.conn: sqlite3.Connection
        - self._get_connection(): method returning thread-safe connection
    """

    def add_server(self, name: str, protocol: str, host: str, base_path: str = '',
                   is_official: bool = True, enabled: bool = True,
                   priority: int = 50) -> int:
        """Add a new server.

        Args:
            name: Display name for the server
            protocol: 'http', 'https', or 'file'
            host: FQDN or 'localhost' for file://
            base_path: Base path on the server (e.g., '/mageia')
            is_official: True for official Mageia mirrors
            enabled: Whether the server is enabled
            priority: Manual priority (higher = preferred)

        Returns:
            Server ID
        """
        cursor = self.conn.execute("""
            INSERT INTO server (name, protocol, host, base_path, is_official,
                               enabled, priority, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, protocol, host, base_path, int(is_official),
              int(enabled), priority, int(time.time())))
        self.conn.commit()
        return cursor.lastrowid

    def get_server(self, name: str) -> Optional[Dict]:
        """Get server info by name."""
        cursor = self.conn.execute(
            "SELECT * FROM server WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_server_by_id(self, server_id: int) -> Optional[Dict]:
        """Get server info by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM server WHERE id = ?", (server_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_server_by_location(self, protocol: str, host: str,
                                base_path: str = '') -> Optional[Dict]:
        """Get server by protocol/host/base_path (unique key for upsert)."""
        cursor = self.conn.execute(
            """SELECT * FROM server
               WHERE protocol = ? AND host = ? AND base_path = ?""",
            (protocol, host, base_path)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_servers(self, enabled_only: bool = False) -> List[Dict]:
        """List all servers, ordered by priority (descending)."""
        if enabled_only:
            cursor = self.conn.execute(
                "SELECT * FROM server WHERE enabled = 1 ORDER BY priority DESC, name"
            )
        else:
            cursor = self.conn.execute(
                "SELECT * FROM server ORDER BY priority DESC, name"
            )
        return [dict(row) for row in cursor]

    def remove_server(self, name: str):
        """Remove a server (cascades to server_media links)."""
        self.conn.execute("DELETE FROM server WHERE name = ?", (name,))
        self.conn.commit()

    def enable_server(self, name: str, enabled: bool = True):
        """Enable or disable a server."""
        self.conn.execute(
            "UPDATE server SET enabled = ? WHERE name = ?",
            (int(enabled), name)
        )
        self.conn.commit()

    def set_server_priority(self, name: str, priority: int):
        """Set server priority."""
        self.conn.execute(
            "UPDATE server SET priority = ? WHERE name = ?",
            (priority, name)
        )
        self.conn.commit()

    def set_server_ip_mode(self, name: str, ip_mode: str):
        """Set server IP mode.

        Args:
            name: Server name
            ip_mode: 'auto', 'ipv4', 'ipv6', or 'dual'
        """
        if ip_mode not in ('auto', 'ipv4', 'ipv6', 'dual'):
            raise ValueError(f"Invalid ip_mode: {ip_mode}")
        self.conn.execute(
            "UPDATE server SET ip_mode = ? WHERE name = ?",
            (ip_mode, name)
        )
        self.conn.commit()

    def set_server_ip_mode_by_id(self, server_id: int, ip_mode: str):
        """Set server IP mode by ID.

        Args:
            server_id: Server ID
            ip_mode: 'auto', 'ipv4', 'ipv6', or 'dual'
        """
        if ip_mode not in ('auto', 'ipv4', 'ipv6', 'dual'):
            raise ValueError(f"Invalid ip_mode: {ip_mode}")
        self.conn.execute(
            "UPDATE server SET ip_mode = ? WHERE id = ?",
            (ip_mode, server_id)
        )
        self.conn.commit()

    # =========================================================================
    # Server-Media links
    # =========================================================================

    def link_server_media(self, server_id: int, media_id: int):
        """Create a link between a server and a media."""
        self.conn.execute("""
            INSERT OR IGNORE INTO server_media (server_id, media_id, added_timestamp)
            VALUES (?, ?, ?)
        """, (server_id, media_id, int(time.time())))
        self.conn.commit()

    def unlink_server_media(self, server_id: int, media_id: int):
        """Remove a link between a server and a media."""
        self.conn.execute(
            "DELETE FROM server_media WHERE server_id = ? AND media_id = ?",
            (server_id, media_id)
        )
        self.conn.commit()

    def get_servers_for_media(self, media_id: int, enabled_only: bool = True,
                               limit: int = None) -> List[Dict]:
        """Get all servers that can serve a media, ordered by priority. Thread-safe.

        Args:
            media_id: Media ID
            enabled_only: Only return enabled servers
            limit: Maximum number of servers to return

        Returns:
            List of server dicts, ordered by priority (descending)
        """
        conn = self._get_connection()
        query = """
            SELECT s.* FROM server s
            JOIN server_media sm ON s.id = sm.server_id
            WHERE sm.media_id = ?
        """
        if enabled_only:
            query += " AND s.enabled = 1"
        query += " ORDER BY s.priority DESC, s.name"
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query, (media_id,))
        return [dict(row) for row in cursor]

    def get_media_for_server(self, server_id: int) -> List[Dict]:
        """Get all media served by a server. Thread-safe."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT m.* FROM media m
            JOIN server_media sm ON m.id = sm.media_id
            WHERE sm.server_id = ?
            ORDER BY m.name
        """, (server_id,))
        return [dict(row) for row in cursor]

    def get_best_server_for_media(self, media_id: int) -> Optional[Dict]:
        """Get the best available server for a media.

        Returns the enabled server with highest priority.
        """
        servers = self.get_servers_for_media(media_id, enabled_only=True, limit=1)
        return servers[0] if servers else None

    def server_media_link_exists(self, server_id: int, media_id: int) -> bool:
        """Check if a server-media link exists."""
        cursor = self.conn.execute(
            "SELECT 1 FROM server_media WHERE server_id = ? AND media_id = ?",
            (server_id, media_id)
        )
        return cursor.fetchone() is not None
