"""Package files and FTS index database operations."""

import sqlite3
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple


class FilesMixin:
    """Mixin providing package files and FTS index operations.

    Requires:
        - self.conn: sqlite3.Connection
        - self._get_connection(): method returning thread-safe connection
        - self._lock: threading.Lock for thread safety
    """

    # =========================================================================
    # Package Files Import/Query
    # =========================================================================

    def import_files_xml(
        self,
        media_id: int,
        files_iterator: Iterator[Tuple[str, List[str]]],
        files_md5: str = None,
        compressed_size: int = None,
        progress_callback: Callable[[int, int], None] = None,
        batch_size: int = 10000
    ) -> Tuple[int, int]:
        """Import package files from files.xml into the database.

        Args:
            media_id: ID of the media these files belong to
            files_iterator: Iterator yielding (nevra, [file_paths]) tuples
            files_md5: MD5 of the files.xml.lzma file (for change detection)
            compressed_size: Size of files.xml.lzma in bytes (for progress estimation)
            progress_callback: Called with (files_imported, packages_imported)
            batch_size: Number of files to insert per transaction

        Returns:
            Tuple of (total_files, total_packages)
        """
        import os.path

        conn = self._get_connection()
        cursor = conn.cursor()

        # Getting rid of indexes for performance
        cursor.execute("DROP INDEX IF EXISTS idx_pf_filename")
        cursor.execute("DROP INDEX IF EXISTS idx_pf_dir_filename")

        # Clear existing files for this media
        cursor.execute("DELETE FROM package_files WHERE media_id = ?", (media_id,))

        total_files = 0
        total_packages = 0
        batch = []

        for nevra, files in files_iterator:
            total_packages += 1
            for filepath in files:
                # Split into dir_path and filename
                dir_path, filename = os.path.split(filepath)
                if not dir_path:
                    dir_path = '/'
                batch.append((media_id, nevra, dir_path, filename))
                total_files += 1

                if len(batch) >= batch_size:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO package_files (media_id, pkg_nevra, dir_path, filename) VALUES (?, ?, ?, ?)",
                        batch
                    )
                    conn.commit()
                    batch = []

                    if progress_callback:
                        progress_callback(total_files, total_packages)

        # Insert remaining batch
        if batch:
            cursor.executemany(
                "INSERT OR IGNORE INTO package_files (media_id, pkg_nevra, dir_path, filename) VALUES (?, ?, ?, ?)",
                batch
            )
            conn.commit()

        # Re-creating indexes to improve search performances
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_filename ON package_files(filename)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_dir_filename ON package_files(dir_path, filename)")

        # Update sync state
        cursor.execute("""
            INSERT OR REPLACE INTO files_xml_state (media_id, files_md5, last_sync, file_count, pkg_count, compressed_size)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (media_id, files_md5, int(time.time()), total_files, total_packages, compressed_size))
        conn.commit()

        if progress_callback:
            progress_callback(total_files, total_packages)

        return total_files, total_packages

    def get_package_files(self, nevra: str) -> List[str]:
        """Get list of files for a specific package.

        Args:
            nevra: Package NEVRA (e.g., bash-5.2.21-1.mga10.x86_64)

        Returns:
            List of full file paths
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT dir_path, filename
            FROM package_files
            WHERE pkg_nevra = ?
            ORDER BY dir_path, filename
        """, (nevra,))

        return [f"{row[0]}/{row[1]}" for row in cursor.fetchall()]

    def search_files(
        self,
        pattern: str,
        media_ids: List[int] = None,
        case_sensitive: bool = False,
        limit: int = 0
    ) -> List[Dict[str, Any]]:
        """Search for files matching a pattern in the database.

        Uses FTS5 trigram index when available (fast for all patterns).
        Falls back to B-tree indexes if FTS not available.

        Args:
            pattern: Search pattern - full path, filename, or glob pattern
            media_ids: Limit search to these media IDs (None = all)
            case_sensitive: If True, match case exactly
            limit: Maximum results (0 = unlimited)

        Returns:
            List of dicts with keys: file_path, pkg_nevra, media_id, media_name
        """
        import logging

        logger = logging.getLogger(__name__)

        # Use FTS if available and current (much faster for all patterns)
        if self.is_fts_index_current():
            try:
                return self.search_files_fts(pattern, media_ids, limit)
            except sqlite3.DatabaseError as e:
                # FTS corrupted - mark as dirty and fall back to B-tree
                if "malformed" in str(e) or "corrupt" in str(e).lower():
                    logger.warning(f"FTS index corrupted, falling back to B-tree search: {e}")
                    self.fts_mark_dirty()
                else:
                    raise

        # Fallback to B-tree index search
        conn = self._get_connection()
        cursor = conn.cursor()

        collate = "" if case_sensitive else " COLLATE NOCASE"
        params = []

        # Convert wildcards to SQL LIKE pattern
        sql_pattern = pattern.replace('*', '%').replace('?', '_')
        has_wildcards = '%' in sql_pattern or '_' in sql_pattern

        if sql_pattern.startswith('/'):
            # Absolute path - use as-is
            full_pattern = sql_pattern
        elif has_wildcards:
            # User specified wildcards explicitly - use as-is
            full_pattern = sql_pattern
        else:
            # No wildcards, no leading / - search for exact filename
            # nvim → %/nvim (file named nvim)
            full_pattern = '%/' + sql_pattern

        # Always search on full path for consistency
        where_clause = f"(pf.dir_path || '/' || pf.filename) LIKE ?{collate}"
        params = [full_pattern]

        # Add media filter
        if media_ids:
            placeholders = ','.join('?' * len(media_ids))
            where_clause += f" AND pf.media_id IN ({placeholders})"
            params.extend(media_ids)

        query = f"""
            SELECT pf.dir_path, pf.filename, pf.pkg_nevra, pf.media_id, m.name as media_name
            FROM package_files pf
            JOIN media m ON pf.media_id = m.id
            WHERE {where_clause}
        """

        if limit > 0:
            query += f" LIMIT {limit}"

        cursor.execute(query, params)

        return [
            {
                'file_path': f"{row[0]}/{row[1]}" if row[0] != '/' else f"/{row[1]}",
                'pkg_nevra': row[2],
                'media_id': row[3],
                'media_name': row[4]
            }
            for row in cursor.fetchall()
        ]

    def get_files_for_package(self, nevra: str, media_id: int = None) -> List[str]:
        """Get all files belonging to a package.

        Args:
            nevra: Package NEVRA
            media_id: Optional media ID filter

        Returns:
            List of file paths
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if media_id:
            cursor.execute(
                "SELECT dir_path, filename FROM package_files WHERE pkg_nevra = ? AND media_id = ? ORDER BY dir_path, filename",
                (nevra, media_id)
            )
        else:
            cursor.execute(
                "SELECT DISTINCT dir_path, filename FROM package_files WHERE pkg_nevra = ? ORDER BY dir_path, filename",
                (nevra,)
            )

        return [
            f"{row[0]}/{row[1]}" if row[0] != '/' else f"/{row[1]}"
            for row in cursor.fetchall()
        ]

    def get_files_xml_state(self, media_id: int) -> Optional[Dict[str, Any]]:
        """Get the files.xml sync state for a media.

        Args:
            media_id: Media ID

        Returns:
            Dict with keys: files_md5, last_sync, file_count, pkg_count, compressed_size
            or None if no sync has been done
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT files_md5, last_sync, file_count, pkg_count, compressed_size FROM files_xml_state WHERE media_id = ?",
            (media_id,)
        )
        row = cursor.fetchone()

        if row:
            return {
                'files_md5': row[0],
                'last_sync': row[1],
                'file_count': row[2],
                'pkg_count': row[3],
                'compressed_size': row[4]
            }
        return None

    def get_files_xml_ratio(self) -> Optional[float]:
        """Get average ratio of file_count / compressed_size across all media.

        Used to estimate total files for progress display on new imports.

        Returns:
            Ratio (files per byte) or None if no data available
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT SUM(file_count), SUM(compressed_size)
            FROM files_xml_state
            WHERE file_count > 0 AND compressed_size > 0
        """)
        row = cursor.fetchone()

        if row and row[0] and row[1]:
            return row[0] / row[1]
        return None

    def get_package_nevras_for_media(self, media_id: int) -> Set[str]:
        """Get all distinct package NEVRAs for a media.

        Used for differential sync to identify what's already in DB.

        Args:
            media_id: Media ID

        Returns:
            Set of package NEVRAs
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT DISTINCT pkg_nevra FROM package_files WHERE media_id = ?",
            (media_id,)
        )
        return {row[0] for row in cursor.fetchall()}

    def delete_package_files_by_nevra(self, media_id: int, nevras: Set[str]):
        """Delete files for specific packages.

        Used for differential sync to remove obsolete packages.
        Also removes corresponding entries from FTS index.

        Args:
            media_id: Media ID
            nevras: Set of package NEVRAs to delete
        """
        import logging
        logger = logging.getLogger(__name__)

        if not nevras:
            return

        conn = self._get_connection()
        cursor = conn.cursor()

        # Use batched deletes for efficiency
        nevra_list = list(nevras)
        batch_size = 500
        fts_available = self.is_fts_available()
        fts_failed = False

        for i in range(0, len(nevra_list), batch_size):
            batch = nevra_list[i:i + batch_size]
            placeholders = ','.join(['?'] * len(batch))

            # Delete from FTS BEFORE deleting from main table (external content mode)
            if fts_available and not fts_failed:
                try:
                    cursor.execute(f"""
                        DELETE FROM package_files_fts
                        WHERE rowid IN (
                            SELECT id FROM package_files
                            WHERE media_id = ? AND pkg_nevra IN ({placeholders})
                        )
                    """, [media_id] + batch)
                except sqlite3.DatabaseError as e:
                    if "malformed" in str(e) or "corrupt" in str(e).lower():
                        logger.warning(f"FTS corrupted during delete, marking dirty: {e}")
                        fts_failed = True
                    else:
                        raise

            # Delete from main table
            cursor.execute(
                f"DELETE FROM package_files WHERE media_id = ? AND pkg_nevra IN ({placeholders})",
                [media_id] + batch
            )

        conn.commit()

        # Mark FTS as needing rebuild if it failed
        if fts_failed:
            self.fts_mark_dirty()

    def insert_package_files_batch(self, media_id: int, nevra: str, files: List[str]):
        """Insert files for a single package.

        Used for differential sync to add new packages.
        Also adds corresponding entries to FTS index.

        Args:
            media_id: Media ID
            nevra: Package NEVRA
            files: List of file paths
        """
        import logging
        logger = logging.getLogger(__name__)

        if not files:
            return

        conn = self._get_connection()
        cursor = conn.cursor()

        # Prepare batch data with split paths
        batch = []
        for file_path in files:
            if '/' in file_path:
                dir_path, filename = file_path.rsplit('/', 1)
                if not dir_path:
                    dir_path = '/'
            else:
                dir_path = ''
                filename = file_path
            batch.append((media_id, nevra, dir_path, filename))

        # Get last ID before insert (for FTS sync)
        fts_available = self.is_fts_available()
        last_id = 0
        if fts_available:
            cursor.execute("SELECT MAX(id) FROM package_files")
            row = cursor.fetchone()
            last_id = row[0] if row and row[0] else 0

        cursor.executemany(
            "INSERT INTO package_files (media_id, pkg_nevra, dir_path, filename) VALUES (?, ?, ?, ?)",
            batch
        )

        # Sync FTS index (insert only newly added rows - ID > last_id)
        if fts_available:
            try:
                cursor.execute("""
                    INSERT INTO package_files_fts(rowid, dir_path, filename)
                    SELECT id, dir_path, filename
                    FROM package_files
                    WHERE id > ?
                """, (last_id,))
            except sqlite3.DatabaseError as e:
                if "malformed" in str(e) or "corrupt" in str(e).lower():
                    logger.warning(f"FTS corrupted during insert, marking dirty: {e}")
                    self.fts_mark_dirty()
                else:
                    raise

        conn.commit()

    def clear_package_files(self, media_id: int = None):
        """Clear package files from the database.

        Args:
            media_id: If specified, only clear files for this media.
                     If None, clear all files.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if media_id:
            cursor.execute("DELETE FROM package_files WHERE media_id = ?", (media_id,))
            cursor.execute("DELETE FROM files_xml_state WHERE media_id = ?", (media_id,))
        else:
            cursor.execute("DELETE FROM package_files")
            cursor.execute("DELETE FROM files_xml_state")

        conn.commit()

    def get_files_stats(self) -> Dict[str, Any]:
        """Get statistics about the package files database.

        Returns:
            Dict with keys: total_files, total_packages, media_stats (list)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Total counts
        cursor.execute("SELECT COUNT(*), COUNT(DISTINCT pkg_nevra) FROM package_files")
        total_files, total_packages = cursor.fetchone()

        # Per-media stats
        cursor.execute("""
            SELECT m.name, fxs.file_count, fxs.pkg_count, fxs.last_sync
            FROM files_xml_state fxs
            JOIN media m ON fxs.media_id = m.id
            ORDER BY m.name
        """)

        media_stats = [
            {
                'media_name': row[0],
                'file_count': row[1],
                'pkg_count': row[2],
                'last_sync': row[3]
            }
            for row in cursor.fetchall()
        ]

        return {
            'total_files': total_files or 0,
            'total_packages': total_packages or 0,
            'media_stats': media_stats
        }

    # =========================================================================
    # FTS5 Index for Fast File Search
    # =========================================================================

    def is_fts_supported(self) -> bool:
        """Check if schema supports FTS (fts_state table exists)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='fts_state'
        """)
        return cursor.fetchone() is not None

    def is_fts_available(self) -> bool:
        """Check if FTS5 table exists and is ready for queries."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='package_files_fts'
        """)
        return cursor.fetchone() is not None

    def is_fts_index_current(self) -> bool:
        """Check if FTS index is populated and current.

        Uses fts_state.is_current flag for O(1) check instead of COUNT(*).
        """
        if not self.is_fts_available():
            return False

        conn = self._get_connection()
        cursor = conn.cursor()

        # Check fts_state flag (fast O(1) lookup)
        cursor.execute("""
            SELECT is_current FROM fts_state WHERE table_name = 'package_files_fts'
        """)
        row = cursor.fetchone()

        if row is None:
            # No state entry - check if both tables are empty (fresh install)
            cursor.execute("SELECT 1 FROM package_files LIMIT 1")
            if cursor.fetchone() is None:
                return True  # Empty is considered current
            return False  # Data exists but no FTS state = needs rebuild

        return row[0] == 1

    def get_fts_stats(self) -> Dict[str, Any]:
        """Get FTS index statistics."""
        conn = self._get_connection()
        cursor = conn.cursor()

        stats = {
            'available': self.is_fts_available(),
            'current': False,
            'pf_count': 0,
            'fts_count': 0,
            'last_rebuild': None
        }

        if not stats['available']:
            return stats

        cursor.execute("SELECT COUNT(*) FROM package_files")
        stats['pf_count'] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM package_files_fts")
        stats['fts_count'] = cursor.fetchone()[0]

        stats['current'] = stats['pf_count'] == stats['fts_count']

        cursor.execute("""
            SELECT last_rebuild FROM fts_state WHERE table_name = 'package_files_fts'
        """)
        row = cursor.fetchone()
        if row:
            stats['last_rebuild'] = row[0]

        return stats

    def _recreate_fts_table(self):
        """Drop and recreate FTS table.

        Called during rebuild or when FTS operations fail with corruption errors.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("DROP TABLE IF EXISTS package_files_fts")
        cursor.execute("""
            CREATE VIRTUAL TABLE package_files_fts USING fts5(
                dir_path,
                filename,
                tokenize = 'trigram',
                content = 'package_files',
                content_rowid = 'id'
            )
        """)
        cursor.execute("DELETE FROM fts_state WHERE table_name = 'package_files_fts'")
        conn.commit()

    def rebuild_fts_index(
        self,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> int:
        """Rebuild FTS index from scratch.

        This is needed after initial migration or to repair a corrupted index.
        Uses batched inserts to show progress and allow interruption.

        If the FTS table is corrupted, it will be automatically recreated.

        Args:
            progress_callback: Called with (current_count, total_count)

        Returns:
            Number of rows indexed
        """
        import logging

        logger = logging.getLogger(__name__)

        conn = self._get_connection()
        cursor = conn.cursor()

        # Check if fts_state table exists (indicates schema supports FTS)
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='fts_state'
        """)
        if cursor.fetchone() is None:
            return 0  # Schema doesn't support FTS yet

        # Get total count for progress
        cursor.execute("SELECT COUNT(*) FROM package_files")
        total = cursor.fetchone()[0]

        if total == 0:
            # Mark as current even if empty
            cursor.execute("""
                INSERT OR REPLACE INTO fts_state (table_name, last_rebuild, row_count, is_current)
                VALUES ('package_files_fts', ?, 0, 1)
            """, (int(time.time()),))
            conn.commit()
            return 0

        # Checkpoint WAL before rebuild to start with clean state
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Close cursor and commit to clear any active statements
        # This is required before _recreate_fts_table() can commit
        cursor.close()
        conn.commit()

        # Drop and recreate FTS table for clean rebuild
        # This creates the table if it doesn't exist, or recreates it for clean state
        self._recreate_fts_table()
        cursor = conn.cursor()  # Get fresh cursor after table recreation

        # Use fast PRAGMAs during rebuild (keep WAL mode active)
        cursor.execute("PRAGMA synchronous = OFF")

        try:
            # Batch insert for progress reporting
            batch_size = 50000
            indexed = 0

            cursor.execute("SELECT MIN(id), MAX(id) FROM package_files")
            min_id, max_id = cursor.fetchone()

            current_id = min_id
            while current_id <= max_id:
                # Insert batch into FTS
                try:
                    cursor.execute("""
                        INSERT INTO package_files_fts(rowid, dir_path, filename)
                        SELECT id, dir_path, filename
                        FROM package_files
                        WHERE id >= ? AND id < ?
                    """, (current_id, current_id + batch_size))
                except sqlite3.DatabaseError as e:
                    if "malformed" in str(e) or "corrupt" in str(e).lower():
                        # FTS corrupted during insert - recreate and restart
                        logger.warning(f"FTS corruption during rebuild: {e}")
                        conn.rollback()
                        self._recreate_fts_table()
                        cursor = conn.cursor()
                        cursor.execute("PRAGMA synchronous = OFF")
                        # Restart from beginning
                        indexed = 0
                        current_id = min_id
                        continue
                    else:
                        raise

                indexed += cursor.rowcount
                current_id += batch_size

                # Commit after each batch to release write lock
                # This allows other processes to access the DB during rebuild
                conn.commit()

                # Checkpoint WAL every 500k rows to prevent it from growing too large
                if indexed % 500000 < batch_size:
                    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                if progress_callback:
                    progress_callback(indexed, total)

            # Update FTS state
            cursor.execute("""
                INSERT OR REPLACE INTO fts_state (table_name, last_rebuild, row_count, is_current)
                VALUES ('package_files_fts', ?, ?, 1)
            """, (int(time.time()), indexed))
            conn.commit()

            return indexed

        except Exception:
            # Rollback on any unhandled exception to release locks
            conn.rollback()
            raise

        finally:
            # Restore safe PRAGMA (WAL mode stays active)
            cursor.execute("PRAGMA synchronous = NORMAL")

    def fts_sync_delete_nevras(self, media_id: int, nevras: Set[str]):
        """Remove entries from FTS index for deleted packages.

        Called by delete_package_files_by_nevra after deleting from main table.
        """
        if not self.is_fts_available() or not nevras:
            return

        # With external content FTS, entries are auto-handled on queries
        # The delete happens in delete_package_files_by_nevra before main delete
        pass

    def fts_sync_insert_nevra(self, media_id: int, nevra: str):
        """Add entries to FTS index for a new package.

        Called by insert_package_files_batch after inserting into main table.
        """
        if not self.is_fts_available():
            return

        conn = self._get_connection()
        cursor = conn.cursor()

        # Insert into FTS from the newly added rows
        cursor.execute("""
            INSERT INTO package_files_fts(rowid, dir_path, filename)
            SELECT id, dir_path, filename
            FROM package_files
            WHERE pkg_nevra = ? AND media_id = ?
        """, (nevra, media_id))

    def fts_mark_dirty(self):
        """Mark FTS index as needing rebuild."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE fts_state SET is_current = 0 WHERE table_name = 'package_files_fts'
        """)
        conn.commit()

    def search_files_fts(
        self,
        pattern: str,
        media_ids: List[int] = None,
        limit: int = 0
    ) -> List[Dict[str, Any]]:
        """Search files using FTS5 trigram index.

        FTS5 trigram tokenizer accelerates LIKE/GLOB queries by using the
        trigram index to find candidate rows. Simply run LIKE on the FTS
        virtual table columns - SQLite handles the optimization automatically.

        Args:
            pattern: Glob pattern (with * wildcards)
            media_ids: Limit to these media IDs
            limit: Max results (0 = unlimited)

        Returns:
            List of dicts with: file_path, pkg_nevra, media_id, media_name
        """
        import re

        conn = self._get_connection()
        cursor = conn.cursor()

        # Convert glob pattern to SQL LIKE pattern
        sql_pattern = pattern.replace('*', '%').replace('?', '_')

        params = []

        # Determine full_pattern based on user input
        has_wildcards = '%' in sql_pattern or '_' in sql_pattern

        if sql_pattern.startswith('/'):
            # Absolute path - use as-is
            full_pattern = sql_pattern
        elif has_wildcards:
            # User specified wildcards explicitly - use as-is
            full_pattern = sql_pattern
        else:
            # No wildcards, no leading / - search for exact filename
            # nvim → %/nvim (file named nvim)
            full_pattern = '%/' + sql_pattern

        # Extract searchable terms for FTS acceleration (>= 3 chars for trigram)
        terms = re.split(r'[%_/]+', full_pattern)
        terms = [t for t in terms if len(t) >= 3]

        if terms:
            # Use longest term for best FTS selectivity
            best_term = max(terms, key=len)
            fts_where = "(dir_path LIKE ? OR filename LIKE ?)"
            params.append(f'%{best_term}%')
            params.append(f'%{best_term}%')
        else:
            # No good search term - full scan
            fts_where = "1=1"

        # Always filter on full path for correctness
        post_filter = "(pf.dir_path || '/' || pf.filename) LIKE ?"
        params.append(full_pattern)

        where_media = ""
        if media_ids:
            placeholders = ','.join(['?'] * len(media_ids))
            where_media = f" AND pf.media_id IN ({placeholders})"
            params.extend(media_ids)

        limit_clause = f" LIMIT {limit}" if limit > 0 else ""

        # Query FTS table to get candidate rowids, then join for full data
        query = f"""
            SELECT pf.dir_path, pf.filename, pf.pkg_nevra, pf.media_id, m.name as media_name
            FROM package_files pf
            JOIN media m ON pf.media_id = m.id
            WHERE pf.id IN (
                SELECT rowid FROM package_files_fts WHERE {fts_where}
            )
            AND {post_filter}{where_media}
            ORDER BY pf.filename, pf.dir_path
            {limit_clause}
        """

        cursor.execute(query, params)

        return [
            {
                'file_path': f"{row[0]}/{row[1]}" if row[0] else row[1],
                'pkg_nevra': row[2],
                'media_id': row[3],
                'media_name': row[4]
            }
            for row in cursor.fetchall()
        ]

    # =========================================================================
    # Fast Import Methods (PRAGMAs, Staging Tables, Atomic Swap)
    # =========================================================================

    def set_fast_import_pragmas(self) -> Dict[str, Any]:
        """Set SQLite PRAGMAs for fast bulk import.

        WARNING: These settings trade durability for speed. Only use for
        bulk imports where data can be regenerated if corrupted.

        Returns:
            Dict with original PRAGMA values for restoration
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Save original values (don't change journal_mode - WAL requires it to stay)
        original = {}
        for pragma in ['synchronous', 'temp_store', 'cache_size']:
            cursor.execute(f"PRAGMA {pragma}")
            original[pragma] = cursor.fetchone()[0]

        # Set fast import PRAGMAs (keep WAL mode - don't touch journal_mode)
        cursor.execute("PRAGMA synchronous = OFF")      # No fsync (dangerous but fast)
        cursor.execute("PRAGMA temp_store = MEMORY")    # Temp tables in RAM
        cursor.execute("PRAGMA cache_size = -64000")    # 64MB cache

        return original

    def restore_pragmas(self, original: Dict[str, Any]):
        """Restore SQLite PRAGMAs to their original values.

        Args:
            original: Dict returned by set_fast_import_pragmas()
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        for pragma, value in original.items():
            if pragma == 'cache_size':
                cursor.execute(f"PRAGMA cache_size = {value}")
            else:
                cursor.execute(f"PRAGMA {pragma} = {value}")

    def create_package_files_staging(self):
        """Create staging table for package files import.

        Creates package_files_new table WITHOUT indexes for fast bulk insert.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Drop if exists (from previous failed import)
        cursor.execute("DROP TABLE IF EXISTS package_files_new")

        # Create staging table without indexes
        cursor.execute("""
            CREATE TABLE package_files_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id INTEGER NOT NULL,
                pkg_nevra TEXT NOT NULL,
                dir_path TEXT NOT NULL,
                filename TEXT NOT NULL
            )
        """)
        # Note: No UNIQUE constraint, no indexes - for maximum insert speed
        # Duplicates will be ignored during insert with INSERT OR IGNORE

        conn.commit()

    def import_files_to_staging(
        self,
        media_id: int,
        files_iterator,
        batch_size: int = 1000,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> Tuple[int, int]:
        """Import files to staging table with batched inserts.

        Args:
            media_id: Media ID for these files
            files_iterator: Iterator yielding (pkg_nevra, file_list) tuples
            batch_size: Number of files per INSERT statement
            progress_callback: Called with (files_imported, packages_imported)

        Returns:
            Tuple of (total_files, total_packages)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        batch = []
        total_files = 0
        total_packages = 0

        for pkg_nevra, file_list in files_iterator:
            total_packages += 1

            for file_path in file_list:
                # Split path into dir_path and filename
                if '/' in file_path:
                    dir_path, filename = file_path.rsplit('/', 1)
                    if not dir_path:
                        dir_path = '/'
                else:
                    dir_path = ''
                    filename = file_path

                batch.append((media_id, pkg_nevra, dir_path, filename))
                total_files += 1

                if len(batch) >= batch_size:
                    cursor.executemany(
                        "INSERT INTO package_files_new (media_id, pkg_nevra, dir_path, filename) VALUES (?, ?, ?, ?)",
                        batch
                    )
                    conn.commit()
                    batch = []

                    if progress_callback:
                        progress_callback(total_files, total_packages)

        # Insert remaining batch
        if batch:
            cursor.executemany(
                "INSERT INTO package_files_new (media_id, pkg_nevra, dir_path, filename) VALUES (?, ?, ?, ?)",
                batch
            )
            conn.commit()

        if progress_callback:
            progress_callback(total_files, total_packages)

        return total_files, total_packages

    def finalize_package_files_atomic(self):
        """Replace package_files with staging table.

        Performs: DROP old → RENAME staging → CREATE indexes
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Drop old table
        cursor.execute("DROP TABLE IF EXISTS package_files")
        conn.commit()

        # Rename staging to production
        cursor.execute("ALTER TABLE package_files_new RENAME TO package_files")
        conn.commit()

        # Create indexes (after data is loaded for efficiency)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_filename ON package_files(filename)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_dir_filename ON package_files(dir_path, filename)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pf_media ON package_files(media_id)")
        conn.commit()

    def abort_package_files_atomic(self):
        """Abort staging import by dropping the staging table.

        Call this if import fails to clean up.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("DROP TABLE IF EXISTS package_files_new")
        conn.commit()

    def update_files_xml_state_batch(self, states: List[Dict[str, Any]]):
        """Update files_xml_state for multiple media at once.

        Args:
            states: List of dicts with keys: media_id, md5, file_count, pkg_count, compressed_size
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        for state in states:
            cursor.execute("""
                INSERT OR REPLACE INTO files_xml_state
                (media_id, last_sync, files_md5, file_count, pkg_count, compressed_size)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                state['media_id'],
                int(time.time()),
                state.get('md5', ''),
                state.get('file_count', 0),
                state.get('pkg_count', 0),
                state.get('compressed_size', 0)
            ))

        conn.commit()
