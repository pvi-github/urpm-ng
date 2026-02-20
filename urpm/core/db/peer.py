"""Peer tracking and mirror configuration database operations."""

import time
from typing import Dict, List, Optional


class PeerMixin:
    """Mixin providing peer tracking and mirror configuration operations.

    Requires:
        - self.conn: sqlite3.Connection
    """

    # =========================================================================
    # Peer download tracking
    # =========================================================================

    def record_peer_download(self, filename: str, file_path: str, peer_host: str,
                             peer_port: int, file_size: int = None,
                             checksum_sha256: str = None, verified: bool = False):
        """Record a package downloaded from a peer for provenance tracking."""
        self.conn.execute("""
            INSERT OR REPLACE INTO peer_downloads
            (filename, file_path, peer_host, peer_port, download_time, file_size,
             checksum_sha256, verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (filename, file_path, peer_host, peer_port, int(time.time()),
              file_size, checksum_sha256, int(verified)))
        self.conn.commit()

    def get_peer_downloads(self, peer_host: str = None, limit: int = 100) -> List[Dict]:
        """Get list of packages downloaded from peers.

        Args:
            peer_host: Filter by peer host (None = all peers)
            limit: Max results to return
        """
        if peer_host:
            cursor = self.conn.execute("""
                SELECT * FROM peer_downloads
                WHERE peer_host = ?
                ORDER BY download_time DESC
                LIMIT ?
            """, (peer_host, limit))
        else:
            cursor = self.conn.execute("""
                SELECT * FROM peer_downloads
                ORDER BY download_time DESC
                LIMIT ?
            """, (limit,))
        return [dict(row) for row in cursor]

    def get_peer_stats(self) -> List[Dict]:
        """Get download statistics per peer."""
        cursor = self.conn.execute("""
            SELECT peer_host, peer_port,
                   COUNT(*) as download_count,
                   SUM(file_size) as total_bytes,
                   MIN(download_time) as first_download,
                   MAX(download_time) as last_download,
                   SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) as verified_count
            FROM peer_downloads
            GROUP BY peer_host, peer_port
            ORDER BY download_count DESC
        """)
        return [dict(row) for row in cursor]

    def delete_peer_downloads(self, peer_host: str) -> int:
        """Delete download records for a peer.

        Returns:
            Number of records deleted
        """
        cursor = self.conn.execute(
            "DELETE FROM peer_downloads WHERE peer_host = ?", (peer_host,)
        )
        self.conn.commit()
        return cursor.rowcount

    def get_files_from_peer(self, peer_host: str) -> List[str]:
        """Get list of file paths downloaded from a specific peer."""
        cursor = self.conn.execute(
            "SELECT file_path FROM peer_downloads WHERE peer_host = ?",
            (peer_host,)
        )
        return [row[0] for row in cursor]

    # =========================================================================
    # Peer blacklist
    # =========================================================================

    def blacklist_peer(self, peer_host: str, peer_port: int = None, reason: str = None):
        """Add a peer to the blacklist."""
        self.conn.execute("""
            INSERT OR REPLACE INTO peer_blacklist
            (peer_host, peer_port, reason, blacklist_time)
            VALUES (?, ?, ?, ?)
        """, (peer_host, peer_port, reason, int(time.time())))
        self.conn.commit()

    def unblacklist_peer(self, peer_host: str, peer_port: int = None):
        """Remove a peer from the blacklist."""
        if peer_port is not None:
            self.conn.execute(
                "DELETE FROM peer_blacklist WHERE peer_host = ? AND peer_port = ?",
                (peer_host, peer_port)
            )
        else:
            self.conn.execute(
                "DELETE FROM peer_blacklist WHERE peer_host = ?",
                (peer_host,)
            )
        self.conn.commit()

    def is_peer_blacklisted(self, peer_host: str, peer_port: int = None) -> bool:
        """Check if a peer is blacklisted."""
        # Check exact match first
        if peer_port is not None:
            cursor = self.conn.execute("""
                SELECT 1 FROM peer_blacklist
                WHERE peer_host = ? AND (peer_port = ? OR peer_port IS NULL)
            """, (peer_host, peer_port))
        else:
            cursor = self.conn.execute(
                "SELECT 1 FROM peer_blacklist WHERE peer_host = ?",
                (peer_host,)
            )
        return cursor.fetchone() is not None

    def list_blacklisted_peers(self) -> List[Dict]:
        """Get list of blacklisted peers."""
        cursor = self.conn.execute("""
            SELECT * FROM peer_blacklist
            ORDER BY blacklist_time DESC
        """)
        return [dict(row) for row in cursor]

    # =========================================================================
    # Mirror configuration
    # =========================================================================

    def get_mirror_config(self, key: str, default: str = None) -> Optional[str]:
        """Get a mirror configuration value."""
        cursor = self.conn.execute(
            "SELECT value FROM mirror_config WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        return row[0] if row else default

    def set_mirror_config(self, key: str, value: str):
        """Set a mirror configuration value."""
        self.conn.execute(
            "INSERT OR REPLACE INTO mirror_config (key, value) VALUES (?, ?)",
            (key, value)
        )
        self.conn.commit()

    def get_all_mirror_config(self) -> Dict[str, str]:
        """Get all mirror configuration values."""
        cursor = self.conn.execute("SELECT key, value FROM mirror_config")
        return {row[0]: row[1] for row in cursor}

    def is_mirror_enabled(self) -> bool:
        """Check if mirror mode is globally enabled."""
        return self.get_mirror_config('enabled', '0') == '1'

    def get_disabled_mirror_versions(self) -> List[str]:
        """Get list of Mageia versions disabled for mirroring."""
        disabled = self.get_mirror_config('disabled_versions', '')
        if not disabled:
            return []
        return [v.strip() for v in disabled.split(',') if v.strip()]
