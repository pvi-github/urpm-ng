"""Media management database operations."""

import time
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


class MediaMixin:
    """Mixin providing media CRUD operations.

    Requires:
        - self.conn: sqlite3.Connection
        - self._get_connection(): method returning thread-safe connection
        - self._lock: threading.Lock for thread safety
    """

    def add_media(self, name: str, short_name: str, mageia_version: str,
                  architecture: str, relative_path: str,
                  is_official: bool = True, allow_unsigned: bool = False,
                  enabled: bool = True, update_media: bool = False,
                  priority: int = 50, url: str = None,
                  mirrorlist: str = None) -> int:
        """Add a new media source.

        Args:
            name: Display name (e.g., 'Core Release')
            short_name: Filesystem-safe identifier (e.g., 'core_release')
            mageia_version: Mageia version (e.g., '9', 'cauldron')
            architecture: Architecture (e.g., 'x86_64')
            relative_path: Relative path for URL construction
            is_official: True for official Mageia media
            allow_unsigned: Allow unsigned packages (custom media only)
            enabled: Whether the media is enabled
            update_media: Whether this is an update media
            priority: Priority for package selection
            url: Legacy URL field (deprecated)
            mirrorlist: Legacy mirrorlist field (deprecated)

        Returns:
            Media ID
        """
        cursor = self.conn.execute("""
            INSERT INTO media (name, short_name, mageia_version, architecture,
                              relative_path, is_official, allow_unsigned,
                              enabled, update_media, priority, url,
                              mirrorlist, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, short_name, mageia_version, architecture, relative_path,
              int(is_official), int(allow_unsigned), int(enabled),
              int(update_media), priority, url, mirrorlist, int(time.time())))
        self.conn.commit()
        return cursor.lastrowid

    def add_media_legacy(self, name: str, url: str = None, mirrorlist: str = None,
                         enabled: bool = True, update: bool = False) -> int:
        """Add a new media source (legacy API for compatibility).

        DEPRECATED: Use add_media() with new parameters instead.

        Returns:
            Media ID
        """
        cursor = self.conn.execute("""
            INSERT INTO media (name, url, mirrorlist, enabled, update_media,
                              short_name, mageia_version, architecture,
                              relative_path, is_official, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, url, mirrorlist, int(enabled), int(update),
              name.lower().replace(' ', '_'),  # short_name placeholder
              'unknown', 'unknown', '',  # version, arch, path placeholders
              1,  # is_official default
              int(time.time())))
        self.conn.commit()
        return cursor.lastrowid

    def remove_media(self, name: str):
        """Remove a media source and all its packages."""
        self.conn.execute("DELETE FROM media WHERE name = ?", (name,))
        self.conn.commit()

    def get_media(self, name: str) -> Optional[Dict]:
        """Get media info by name. Thread-safe."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM media WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_media(self) -> List[Dict]:
        """List all media sources. Thread-safe."""
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM media ORDER BY priority, name")
        return [dict(row) for row in cursor]

    def enable_media(self, name: str, enabled: bool = True):
        """Enable or disable a media source."""
        self.conn.execute(
            "UPDATE media SET enabled = ? WHERE name = ?",
            (int(enabled), name)
        )
        self.conn.commit()

    def set_media_sync_files(self, name: str, enabled: bool = True):
        """Enable or disable files.xml sync for a media.

        When enabled, urpmd will auto-sync files.xml for this media,
        allowing `urpm find` to search in available packages.

        Args:
            name: Media name
            enabled: True to enable sync, False to disable
        """
        conn = self._get_connection()
        conn.execute(
            "UPDATE media SET sync_files = ? WHERE name = ?",
            (int(enabled), name)
        )
        conn.commit()

    # The ``sync_files`` opt-in (and the column it relied on) was
    # removed in schema v28: ``urpm f`` now streams ``files.xml.lzma``
    # directly, so there is nothing to "sync" anymore.  The three
    # methods below remain as compatibility stubs for callers that
    # have not been migrated yet (``urpm media set --sync-files``,
    # the daemon's idle ``files.xml`` job, …).  See
    # ``doc/TODO_SHRINK_FILES_DB.md``.

    def set_all_media_sync_files(self, enabled: bool = True, enabled_only: bool = True) -> int:
        return 0

    def get_media_with_sync_files(self) -> List[Dict]:
        return []

    def has_any_sync_files_media(self) -> bool:
        return False

    def update_media_sync_info(self, media_id: int, synthesis_md5: str,
                              synthesis_last_modified: str = None):
        """Update media sync timestamp, MD5, and Last-Modified. Thread-safe."""
        conn = self._get_connection()
        with self._lock:
            conn.execute("""
                UPDATE media SET last_sync = ?, synthesis_md5 = ?,
                    synthesis_last_modified = ?
                WHERE id = ?
            """, (int(time.time()), synthesis_md5, synthesis_last_modified,
                  media_id))
            conn.commit()

    def get_media_by_id(self, media_id: int) -> Optional[Dict]:
        """Get media info by ID."""
        cursor = self.conn.execute(
            "SELECT * FROM media WHERE id = ?", (media_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_media_by_version_arch_shortname(self, version: str, arch: str,
                                             short_name: str) -> Optional[Dict]:
        """Get media by version, architecture and short_name (unique key)."""
        cursor = self.conn.execute(
            """SELECT * FROM media
               WHERE mageia_version = ? AND architecture = ? AND short_name = ?""",
            (version, arch, short_name)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_media_mirror_settings(self, media_id: int,
                                      shared: bool = None,
                                      replication_policy: str = None,
                                      replication_seeds: List[str] = None,
                                      quota_mb: int = None,
                                      retention_days: int = None):
        """Update mirror/replication settings for a media.

        Args:
            media_id: Media ID
            shared: Whether to serve this media to peers
            replication_policy: 'none', 'on_demand', 'seed'
            replication_seeds: List of rpmsrate sections for policy='seed'
                              e.g., ['INSTALL', 'CAT_PLASMA5', 'CAT_GNOME']
            quota_mb: Per-media quota in MB (None to clear)
            retention_days: Days to keep cached packages
        """
        import json

        updates = []
        params = []

        if shared is not None:
            updates.append("shared = ?")
            params.append(int(shared))

        if replication_policy is not None:
            if replication_policy not in ('none', 'on_demand', 'seed'):
                raise ValueError(f"Invalid replication_policy: {replication_policy}")
            updates.append("replication_policy = ?")
            params.append(replication_policy)

        if replication_seeds is not None:
            updates.append("replication_seeds = ?")
            params.append(json.dumps(replication_seeds) if replication_seeds else None)

        if quota_mb is not None:
            updates.append("quota_mb = ?")
            params.append(quota_mb if quota_mb > 0 else None)

        if retention_days is not None:
            updates.append("retention_days = ?")
            params.append(retention_days)

        if not updates:
            return

        params.append(media_id)
        self.conn.execute(
            f"UPDATE media SET {', '.join(updates)} WHERE id = ?",
            params
        )
        self.conn.commit()

    def list_media_for_sharing(self, version: str = None, arch: str = None) -> List[Dict]:
        """List media available for sharing with peers.

        Filters by:
        - shared = 1
        - Global mirror enabled
        - Version not in disabled_versions
        - Optionally matching version/arch

        Args:
            version: Filter by Mageia version (e.g., '10')
            arch: Filter by architecture (e.g., 'x86_64')

        Returns:
            List of media dicts that can be served to peers
        """
        # Check global mirror enabled
        if not self.is_mirror_enabled():
            return []

        disabled_versions = self.get_disabled_mirror_versions()

        query = """
            SELECT * FROM media
            WHERE enabled = 1 AND shared = 1
        """
        params = []

        if version:
            query += " AND mageia_version = ?"
            params.append(version)

        if arch:
            query += " AND architecture = ?"
            params.append(arch)

        query += " ORDER BY priority DESC, name"

        cursor = self.conn.execute(query, params)
        media_list = [dict(row) for row in cursor]

        # Filter out disabled versions
        if disabled_versions:
            media_list = [m for m in media_list
                         if m['mageia_version'] not in disabled_versions]

        return media_list

    def record_media_update_delta(self, media_id: int, changed_at: int,
                                  delta_seconds: int | None):
        """Record a content change timestamp and delta for adaptive scheduling.

        Args:
            media_id: Media ID.
            changed_at: Unix timestamp when content change was detected.
            delta_seconds: Seconds since previous content change, or None if first.
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                "INSERT INTO media_update_deltas (media_id, changed_at, delta_seconds) "
                "VALUES (?, ?, ?)",
                (media_id, changed_at, delta_seconds),
            )
            conn.commit()

    def get_media_update_deltas(self, media_id: int, limit: int = 30) -> list[int]:
        """Get recent delta_seconds for a media (newest first).

        Only returns non-None deltas (skips the first-ever record).

        Args:
            media_id: Media ID.
            limit: Maximum number of deltas to return.

        Returns:
            List of delta_seconds values (integers), newest first.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT delta_seconds FROM media_update_deltas "
            "WHERE media_id = ? AND delta_seconds IS NOT NULL "
            "ORDER BY changed_at DESC LIMIT ?",
            (media_id, limit),
        )
        return [row[0] for row in cursor.fetchall()]

    def prune_media_update_deltas(self, media_id: int, keep: int = 30):
        """Keep only the N most recent deltas for a media.

        Args:
            media_id: Media ID.
            keep: Number of most recent records to keep.
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                "DELETE FROM media_update_deltas "
                "WHERE media_id = ? AND id NOT IN ("
                "  SELECT id FROM media_update_deltas "
                "  WHERE media_id = ? ORDER BY changed_at DESC LIMIT ?"
                ")",
                (media_id, media_id, keep),
            )
            conn.commit()

    def update_media_adaptive_state(self, media_id: int, period: int | None,
                                    mu: float | None, sigma: float | None,
                                    last_changed: int | None):
        """Cache computed adaptive scheduling state on the media row.

        Args:
            media_id: Media ID.
            period: Computed F(media) in seconds, or None to clear.
            mu: Mean update interval in seconds.
            sigma: Standard deviation of update intervals in seconds.
            last_changed: Unix timestamp of last real content change.
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                "UPDATE media SET adaptive_period = ?, adaptive_mu = ?, "
                "adaptive_sigma = ?, adaptive_last_changed = ? WHERE id = ?",
                (period, mu, sigma, last_changed, media_id),
            )
            conn.commit()
