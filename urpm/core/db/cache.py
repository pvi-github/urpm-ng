"""Cache file tracking database operations."""

import time
from typing import Any, Dict, List, Optional


class CacheMixin:
    """Mixin providing cache file tracking operations.

    Requires:
        - self.conn: sqlite3.Connection
        - self._get_connection(): method returning thread-safe connection
        - self._lock: threading.Lock for thread safety
    """

    def register_cache_file(self, filename: str, media_id: int, file_path: str,
                            file_size: int) -> int:
        """Register a cached file for quota tracking.

        Args:
            filename: RPM filename (e.g., 'foo-1.0-1.mga10.x86_64.rpm')
            media_id: Associated media ID
            file_path: Relative path from medias/ directory
            file_size: File size in bytes

        Returns:
            Cache file ID
        """
        with self._lock:
            now = int(time.time())
            cursor = self.conn.execute("""
                INSERT OR REPLACE INTO cache_files
                (filename, media_id, file_path, file_size, added_time, last_accessed, is_referenced)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (filename, media_id, file_path, file_size, now, now))
            self.conn.commit()
            return cursor.lastrowid

    def get_cache_file(self, filename: str, media_id: int = None) -> Optional[Dict]:
        """Get cache file info by filename. Thread-safe."""
        conn = self._get_connection()
        if media_id:
            cursor = conn.execute(
                "SELECT * FROM cache_files WHERE filename = ? AND media_id = ?",
                (filename, media_id)
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM cache_files WHERE filename = ?", (filename,)
            )
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_cache_file_access(self, filename: str, media_id: int = None):
        """Update last_accessed timestamp for a cache file."""
        now = int(time.time())
        if media_id:
            self.conn.execute(
                "UPDATE cache_files SET last_accessed = ? WHERE filename = ? AND media_id = ?",
                (now, filename, media_id)
            )
        else:
            self.conn.execute(
                "UPDATE cache_files SET last_accessed = ? WHERE filename = ?",
                (now, filename)
            )
        self.conn.commit()

    def list_cache_files(self, media_id: int = None, referenced_only: bool = False,
                         order_by: str = 'added_time', limit: int = None) -> List[Dict]:
        """List cached files.

        Args:
            media_id: Filter by media (None = all)
            referenced_only: Only files still in synthesis
            order_by: 'added_time', 'last_accessed', 'file_size'
            limit: Max results

        Returns:
            List of cache file dicts
        """
        query = "SELECT * FROM cache_files WHERE 1=1"
        params = []

        if media_id:
            query += " AND media_id = ?"
            params.append(media_id)

        if referenced_only:
            query += " AND is_referenced = 1"

        if order_by in ('added_time', 'last_accessed', 'file_size'):
            query += f" ORDER BY {order_by}"
        else:
            query += " ORDER BY added_time"

        if limit:
            query += f" LIMIT {limit}"

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor]

    def mark_cache_files_unreferenced(self, media_id: int, referenced_filenames: List[str]):
        """Mark cache files as unreferenced if not in the provided list.

        Called after sync to mark old package versions as unreferenced.

        Args:
            media_id: Media ID
            referenced_filenames: List of filenames that ARE in current synthesis
        """
        if not referenced_filenames:
            # Mark all files for this media as unreferenced
            self.conn.execute(
                "UPDATE cache_files SET is_referenced = 0 WHERE media_id = ?",
                (media_id,)
            )
        else:
            # First mark all as unreferenced
            self.conn.execute(
                "UPDATE cache_files SET is_referenced = 0 WHERE media_id = ?",
                (media_id,)
            )
            # Then mark the referenced ones
            placeholders = ','.join('?' * len(referenced_filenames))
            self.conn.execute(f"""
                UPDATE cache_files SET is_referenced = 1
                WHERE media_id = ? AND filename IN ({placeholders})
            """, [media_id] + referenced_filenames)
        self.conn.commit()

    def delete_cache_file(self, filename: str, media_id: int = None) -> bool:
        """Delete a cache file record. Thread-safe.

        Note: This only removes the DB record, not the actual file.

        Returns:
            True if a record was deleted
        """
        conn = self._get_connection()
        if media_id:
            cursor = conn.execute(
                "DELETE FROM cache_files WHERE filename = ? AND media_id = ?",
                (filename, media_id)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM cache_files WHERE filename = ?", (filename,)
            )
        conn.commit()
        return cursor.rowcount > 0

    def get_cache_stats(self, media_id: int = None) -> Dict[str, Any]:
        """Get cache statistics.

        Args:
            media_id: Filter by media (None = global stats)

        Returns:
            Dict with total_files, total_size, referenced_files, unreferenced_files, etc.
        """
        if media_id:
            cursor = self.conn.execute("""
                SELECT
                    COUNT(*) as total_files,
                    COALESCE(SUM(file_size), 0) as total_size,
                    COALESCE(SUM(CASE WHEN is_referenced = 1 THEN 1 ELSE 0 END), 0) as referenced_files,
                    COALESCE(SUM(CASE WHEN is_referenced = 0 THEN 1 ELSE 0 END), 0) as unreferenced_files,
                    COALESCE(SUM(CASE WHEN is_referenced = 1 THEN file_size ELSE 0 END), 0) as referenced_size,
                    COALESCE(SUM(CASE WHEN is_referenced = 0 THEN file_size ELSE 0 END), 0) as unreferenced_size,
                    MIN(added_time) as oldest_file,
                    MAX(added_time) as newest_file
                FROM cache_files WHERE media_id = ?
            """, (media_id,))
        else:
            cursor = self.conn.execute("""
                SELECT
                    COUNT(*) as total_files,
                    COALESCE(SUM(file_size), 0) as total_size,
                    COALESCE(SUM(CASE WHEN is_referenced = 1 THEN 1 ELSE 0 END), 0) as referenced_files,
                    COALESCE(SUM(CASE WHEN is_referenced = 0 THEN 1 ELSE 0 END), 0) as unreferenced_files,
                    COALESCE(SUM(CASE WHEN is_referenced = 1 THEN file_size ELSE 0 END), 0) as referenced_size,
                    COALESCE(SUM(CASE WHEN is_referenced = 0 THEN file_size ELSE 0 END), 0) as unreferenced_size,
                    MIN(added_time) as oldest_file,
                    MAX(added_time) as newest_file
                FROM cache_files
            """)

        row = cursor.fetchone()
        return dict(row) if row else {}

    def get_files_to_evict(self, media_id: int = None, max_bytes: int = None,
                           max_age_days: int = None) -> List[Dict]:
        """Get list of files that should be evicted based on criteria.

        Priority: unreferenced files first, then oldest by last_accessed.

        Args:
            media_id: Filter by media (None = all)
            max_bytes: Stop when we have enough bytes to free
            max_age_days: Include files older than this

        Returns:
            List of cache file dicts to evict
        """
        query = """
            SELECT * FROM cache_files
            WHERE 1=1
        """
        params = []

        if media_id:
            query += " AND media_id = ?"
            params.append(media_id)

        if max_age_days:
            cutoff = int(time.time()) - (max_age_days * 86400)
            query += " AND added_time < ?"
            params.append(cutoff)

        # Order: unreferenced first, then oldest accessed
        query += " ORDER BY is_referenced ASC, last_accessed ASC"

        cursor = self.conn.execute(query, params)
        files = [dict(row) for row in cursor]

        if max_bytes:
            # Only return enough files to free max_bytes
            result = []
            total = 0
            for f in files:
                result.append(f)
                total += f['file_size']
                if total >= max_bytes:
                    break
            return result

        return files
