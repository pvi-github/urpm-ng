"""Transaction history database operations."""

import sqlite3
import time
from typing import Dict, List, Optional


class HistoryMixin:
    """Mixin providing transaction history operations.

    Requires:
        - self._conn_read(): context manager yielding a per-thread
          connection for read-only paths.
        - self._conn_write(): context manager yielding a per-thread
          connection with the write lock held.
    """

    def begin_transaction(self, action: str, command: str = None) -> int:
        """Start a new transaction and return its ID.

        Records the current process PID in ``pid_running`` so
        :func:`urpm.core.recovery.check_orphaned_transactions`
        (Phase D, SPEC_DISTUPGRADE §3.D) can detect transactions
        whose owner died by cross-checking with ``_pid_alive`` /
        ``/proc/<pid>/cmdline``.

        Args:
            action: Transaction type ('install', 'remove', 'upgrade', 'undo', 'rollback')
            command: Full command line that triggered this

        Returns:
            Transaction ID
        """
        import getpass
        import os

        with self._conn_write() as conn:
            cursor = conn.execute("""
                INSERT INTO history
                  (timestamp, action, status, command, user, pid_running)
                VALUES (?, ?, 'running', ?, ?, ?)
            """, (int(time.time()), action, command, getpass.getuser(),
                  os.getpid()))
            conn.commit()
            return cursor.lastrowid

    def record_package(self, transaction_id: int, nevra: str, name: str,
                       action: str, reason: str, previous_nevra: str = None):
        """Record a planned package action in a transaction.

        Row starts at ``status='planned'`` (schema default), promoted
        to ``'done' / 'failed' / 'skipped'`` via
        :meth:`record_action_start` / :meth:`record_action_end` as
        the rpm callback progresses (SPEC_DISTUPGRADE §3.B Phase B).

        Args:
            transaction_id: Transaction ID from begin_transaction()
            nevra: Package NEVRA (name-epoch:version-release.arch)
            name: Package name (for easier queries)
            action: 'install', 'remove', 'upgrade', 'downgrade'
            reason: 'explicit' or 'dependency'
            previous_nevra: For upgrade/downgrade, the previous version

        Note: Does not commit - batched with complete_transaction().
        """
        with self._conn_write() as conn:
            conn.execute("""
                INSERT INTO history_packages
                (history_id, pkg_nevra, pkg_name, action, reason, previous_nevra)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (transaction_id, nevra, name, action, reason, previous_nevra))

    def record_action_start(self, transaction_id: int, pkg_nevra: str) -> None:
        """Mark a planned action as in-flight (rpm callback START fired).

        Sets ``started_at`` and flips ``status`` from ``'planned'``
        (or from a previous ``'failed'`` on a retry) to leave the
        actual completion state up to :meth:`record_action_end`.
        Idempotent : repeated calls just refresh ``started_at``.
        """
        with self._conn_write() as conn:
            conn.execute("""
                UPDATE history_packages
                SET started_at = ?
                WHERE history_id = ? AND pkg_nevra = ?
            """, (int(time.time()), transaction_id, pkg_nevra))

    def record_action_end(self, transaction_id: int, pkg_nevra: str,
                          status: str,
                          error_message: Optional[str] = None) -> None:
        """Mark an action as completed with a terminal status.

        Args:
            transaction_id: Transaction ID from begin_transaction().
            pkg_nevra: NEVRA of the package whose action terminated.
            status: One of ``'done'``, ``'failed'``, ``'skipped'``.
                Must not be ``'planned'`` — use :meth:`record_action_start`
                for the in-flight transition.
            error_message: Populated for ``'failed'`` rows.  Kept
                free-form (rpm callback message, exception repr, or
                classification tag from §3.A callback taxonomy).
        """
        if status not in ("done", "failed", "skipped"):
            raise ValueError(
                f"invalid terminal status {status!r}; "
                f"expected one of 'done', 'failed', 'skipped'")
        with self._conn_write() as conn:
            conn.execute("""
                UPDATE history_packages
                SET status = ?, finished_at = ?, error_message = ?
                WHERE history_id = ? AND pkg_nevra = ?
            """, (status, int(time.time()), error_message,
                  transaction_id, pkg_nevra))

    def _commit_with_retry(self, conn, max_retries: int = 10, base_delay: float = 0.5):
        """Commit with retry and exponential backoff for lock contention.

        Used after RPM transactions when urpmd may hold the database lock.
        """
        import logging
        logger = logging.getLogger(__name__)

        for attempt in range(max_retries):
            try:
                conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < max_retries - 1:
                    delay = base_delay * (attempt + 1)
                    if attempt == 0:
                        logger.warning("Database locked, retrying...")
                    time.sleep(delay)
                else:
                    raise

    def record_scriptlet_output(self, transaction_id: int, pkg_name: str,
                                output: str, is_error: bool = False):
        """Record a package's scriptlet output for later review.

        Legacy pre-v34 API kept read/write for callers that haven't
        migrated to :meth:`record_scriptlet_event`.  New code
        (SPEC_DISTUPGRADE §3.C Phase C) should use the typed
        event API instead.
        """
        with self._conn_write() as conn:
            conn.execute("""
                INSERT INTO history_scriptlet_output
                (history_id, pkg_name, is_error, output)
                VALUES (?, ?, ?, ?)
            """, (transaction_id, pkg_name, 1 if is_error else 0, output))

    def record_scriptlet_event(self, transaction_id: int, pkg_name: str,
                               script_type: str, status: str,
                               started_at: int,
                               finished_at: Optional[int] = None,
                               exit_code: Optional[int] = None,
                               output: Optional[str] = None) -> None:
        """Record a typed scriptlet event in ``history_scriptlets``.

        Populated at each RPM callback SCRIPT_START / STOP / ERROR by
        the transaction pipeline (SPEC_DISTUPGRADE §3.C Phase C).

        Args:
            transaction_id: Transaction ID from :meth:`begin_transaction`.
            pkg_name: Package whose scriptlet fired.
            script_type: One of the labels produced by
                :func:`urpm.core.transaction_queue.script_type_label` —
                ``'pre'``, ``'post'``, ``'pretrans'``, ``'posttrans'``,
                ``'preun'``, ``'postun'``, ``'preuntrans'``,
                ``'postuntrans'``, ``'trigger'``.
            status: ``'started'`` (SCRIPT_START), ``'ok'`` (SCRIPT_STOP
                without ERROR), or ``'failed'`` (SCRIPT_ERROR).
            started_at: Epoch seconds when the scriptlet fired.
            finished_at: Epoch seconds when it finished (SCRIPT_STOP);
                ``None`` when still in flight.
            exit_code: Scriptlet exit code (0 on ``'ok'``).
            output: Captured stdout/stderr.  Passed through
                :func:`sanitize_scriptlet_output` before INSERT so no
                Trojan Source / bidi / control-char payload lands in
                the DB.

        Note: does not commit — batched with
        :meth:`complete_transaction`.
        """
        from ..security.sanitize import sanitize_scriptlet_output

        if output is not None:
            output = sanitize_scriptlet_output(output)

        with self._conn_write() as conn:
            conn.execute("""
                INSERT INTO history_scriptlets
                  (history_id, pkg_name, script_type, status,
                   started_at, finished_at, exit_code, output)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (transaction_id, pkg_name, script_type, status,
                  started_at, finished_at, exit_code, output))

    def get_scriptlet_output(self, transaction_id: int) -> List[Dict]:
        """Retrieve scriptlet events + legacy output for a transaction.

        Union of :table:`history_scriptlets` (v34+) and
        :table:`history_scriptlet_output` (pre-v34).  Sorted
        chronologically : new rows carry ``started_at``, legacy rows
        fall back to ``id`` ordering (SPEC_DISTUPGRADE §3.C TC.3).

        Every row exposes the same enriched dict shape so callers
        don't need to branch on the source table :

        - ``pkg_name``            (str)
        - ``script_type``         (str, empty on legacy rows)
        - ``status``              (``'started' / 'ok' / 'failed'``,
          legacy rows report ``'failed'`` when ``is_error`` was set,
          ``'ok'`` otherwise)
        - ``started_at``          (int, None on legacy)
        - ``finished_at``         (int, None on legacy)
        - ``exit_code``           (int, None on legacy)
        - ``output``              (str)
        - ``source``              (``'v34' | 'legacy'``) — lets the
          renderer signal in the UI which table the row came from.
        """
        rows: List[Dict] = []
        with self._conn_read() as conn:
            for r in conn.execute("""
                SELECT pkg_name, script_type, status, started_at,
                       finished_at, exit_code, output
                FROM history_scriptlets
                WHERE history_id = ?
            """, (transaction_id,)):
                rows.append({
                    'pkg_name':    r[0],
                    'script_type': r[1],
                    'status':      r[2],
                    'started_at':  r[3],
                    'finished_at': r[4],
                    'exit_code':   r[5],
                    'output':      r[6],
                    'source':      'v34',
                })
            for r in conn.execute("""
                SELECT id, pkg_name, is_error, output
                FROM history_scriptlet_output
                WHERE history_id = ?
            """, (transaction_id,)):
                rows.append({
                    'pkg_name':    r[1],
                    'script_type': '',
                    'status':      'failed' if r[2] else 'ok',
                    'started_at':  None,
                    'finished_at': None,
                    'exit_code':   None,
                    'output':      r[3],
                    'source':      'legacy',
                    '_legacy_id':  r[0],  # only used by the sort key
                })
        # v34 rows carry started_at, legacy rows fall back to their
        # sequential id.  Sort in one pass on a composite key that
        # keeps the two families interleaved chronologically.
        def _sort_key(row):
            if row['source'] == 'v34':
                # started_at can be NULL if the caller wrote a row
                # without calling record_action_start ; treat as 0.
                return (row['started_at'] or 0, 0)
            return (0, row.get('_legacy_id', 0))
        rows.sort(key=_sort_key)
        # Drop the sort helper before returning.
        for row in rows:
            row.pop('_legacy_id', None)
        return rows

    def complete_transaction(self, transaction_id: int, return_code: int = 0):
        """Mark a transaction as complete and clear pid_running."""
        with self._conn_write() as conn:
            conn.execute("""
                UPDATE history
                SET status = 'complete', return_code = ?, pid_running = NULL
                WHERE id = ?
            """, (return_code, transaction_id))
            self._commit_with_retry(conn)

    def abort_transaction(self, transaction_id: int):
        """Mark a transaction as interrupted and clear pid_running."""
        with self._conn_write() as conn:
            conn.execute("""
                UPDATE history
                SET status = 'interrupted', return_code = -1, pid_running = NULL
                WHERE id = ?
            """, (transaction_id,))
            self._commit_with_retry(conn)

    def list_history(self, limit: int = 20, action_filter: str = None) -> List[Dict]:
        """List recent transactions.

        Args:
            limit: Max number of transactions to return
            action_filter: Filter by action type ('install', 'remove', etc.)

        Returns:
            List of transaction dicts with summary info
        """
        with self._conn_read() as conn:
            if action_filter:
                cursor = conn.execute("""
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
                cursor = conn.execute("""
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
        with self._conn_read() as conn:
            cursor = conn.execute(
                "SELECT * FROM history WHERE id = ?", (transaction_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            trans = dict(row)

            # Get packages
            cursor = conn.execute("""
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
        with self._conn_write() as conn:
            conn.execute("""
                UPDATE history SET undone_by = ? WHERE id = ?
            """, (undone_by_id, transaction_id))
            conn.commit()

    def get_interrupted_transactions(self) -> List[Dict]:
        """Get transactions that were interrupted (for cleandeps)."""
        with self._conn_read() as conn:
            cursor = conn.execute("""
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
        with self._conn_read() as conn:
            cursor = conn.execute("""
                SELECT pkg_nevra FROM history_packages
                WHERE history_id = ? AND reason = 'dependency' AND action = 'install'
            """, (transaction_id,))
            return [row[0] for row in cursor]
