"""Transaction history database operations."""

import sqlite3
import time
from typing import Dict, List, Optional


class HistoryMixin:
    """Mixin providing transaction history operations.

    Requires:
        - self.conn: sqlite3.Connection
    """

    def begin_transaction(self, action: str, command: str = None) -> int:
        """Start a new transaction and return its ID.

        Args:
            action: Transaction type ('install', 'remove', 'upgrade', 'undo', 'rollback')
            command: Full command line that triggered this

        Returns:
            Transaction ID
        """
        import getpass

        cursor = self.conn.execute("""
            INSERT INTO history (timestamp, action, status, command, user)
            VALUES (?, ?, 'running', ?, ?)
        """, (int(time.time()), action, command, getpass.getuser()))
        self.conn.commit()
        return cursor.lastrowid

    def record_package(self, transaction_id: int, nevra: str, name: str,
                       action: str, reason: str, previous_nevra: str = None):
        """Record a package action in a transaction.

        Args:
            transaction_id: Transaction ID from begin_transaction()
            nevra: Package NEVRA (name-epoch:version-release.arch)
            name: Package name (for easier queries)
            action: 'install', 'remove', 'upgrade', 'downgrade'
            reason: 'explicit' or 'dependency'
            previous_nevra: For upgrade/downgrade, the previous version

        Note: Does not commit - batched with complete_transaction().
        """
        self.conn.execute("""
            INSERT INTO history_packages
            (history_id, pkg_nevra, pkg_name, action, reason, previous_nevra)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (transaction_id, nevra, name, action, reason, previous_nevra))

    def _commit_with_retry(self, max_retries: int = 10, base_delay: float = 0.5):
        """Commit with retry and exponential backoff for lock contention.

        Used after RPM transactions when urpmd may hold the database lock.
        """
        import logging
        logger = logging.getLogger(__name__)

        for attempt in range(max_retries):
            try:
                self.conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < max_retries - 1:
                    delay = base_delay * (attempt + 1)
                    if attempt == 0:
                        logger.warning("Database locked, retrying...")
                    time.sleep(delay)
                else:
                    raise

    def complete_transaction(self, transaction_id: int, return_code: int = 0):
        """Mark a transaction as complete."""
        self.conn.execute("""
            UPDATE history SET status = 'complete', return_code = ?
            WHERE id = ?
        """, (return_code, transaction_id))
        self._commit_with_retry()

    def abort_transaction(self, transaction_id: int):
        """Mark a transaction as interrupted."""
        self.conn.execute("""
            UPDATE history SET status = 'interrupted', return_code = -1
            WHERE id = ?
        """, (transaction_id,))
        self._commit_with_retry()

    def list_history(self, limit: int = 20, action_filter: str = None) -> List[Dict]:
        """List recent transactions.

        Args:
            limit: Max number of transactions to return
            action_filter: Filter by action type ('install', 'remove', etc.)

        Returns:
            List of transaction dicts with summary info
        """
        if action_filter:
            cursor = self.conn.execute("""
                SELECT h.*, COUNT(hp.id) as pkg_count,
                       GROUP_CONCAT(CASE WHEN hp.reason = 'explicit' THEN hp.pkg_name END) as explicit_pkgs
                FROM history h
                LEFT JOIN history_packages hp ON hp.history_id = h.id
                WHERE h.action = ?
                GROUP BY h.id
                ORDER BY h.timestamp DESC
                LIMIT ?
            """, (action_filter, limit))
        else:
            cursor = self.conn.execute("""
                SELECT h.*, COUNT(hp.id) as pkg_count,
                       GROUP_CONCAT(CASE WHEN hp.reason = 'explicit' THEN hp.pkg_name END) as explicit_pkgs
                FROM history h
                LEFT JOIN history_packages hp ON hp.history_id = h.id
                GROUP BY h.id
                ORDER BY h.timestamp DESC
                LIMIT ?
            """, (limit,))

        return [dict(row) for row in cursor]

    def get_transaction(self, transaction_id: int) -> Optional[Dict]:
        """Get details of a specific transaction.

        Returns:
            Transaction dict with packages list, or None if not found
        """
        cursor = self.conn.execute(
            "SELECT * FROM history WHERE id = ?", (transaction_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        trans = dict(row)

        # Get packages
        cursor = self.conn.execute("""
            SELECT * FROM history_packages WHERE history_id = ?
            ORDER BY reason DESC, pkg_name
        """, (transaction_id,))
        trans['packages'] = [dict(r) for r in cursor]

        # Separate explicit vs dependency
        trans['explicit'] = [p for p in trans['packages'] if p['reason'] == 'explicit']
        trans['dependencies'] = [p for p in trans['packages'] if p['reason'] == 'dependency']

        return trans

    def mark_undone(self, transaction_id: int, undone_by_id: int):
        """Mark a transaction as undone by another transaction."""
        self.conn.execute("""
            UPDATE history SET undone_by = ? WHERE id = ?
        """, (undone_by_id, transaction_id))
        self.conn.commit()

    def get_interrupted_transactions(self) -> List[Dict]:
        """Get transactions that were interrupted (for cleandeps)."""
        cursor = self.conn.execute("""
            SELECT h.*, COUNT(hp.id) as pkg_count
            FROM history h
            LEFT JOIN history_packages hp ON hp.history_id = h.id
            WHERE h.status = 'interrupted'
            GROUP BY h.id
            ORDER BY h.timestamp DESC
        """)
        return [dict(row) for row in cursor]

    def get_orphan_deps(self, transaction_id: int) -> List[str]:
        """Get dependency packages from an interrupted transaction.

        Returns list of NEVRAs that were installed as deps but transaction didn't complete.
        """
        cursor = self.conn.execute("""
            SELECT pkg_nevra FROM history_packages
            WHERE history_id = ? AND reason = 'dependency' AND action = 'install'
        """, (transaction_id,))
        return [row[0] for row in cursor]
