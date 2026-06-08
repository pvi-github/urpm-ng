"""Server management database operations."""

import time
from typing import Dict, List, Optional


# ── Reputation event categories ───────────────────────────────────────
# Penalty applied to the rolling reputation score per failure.  Higher
# numbers deprioritise the server faster in mirror selection.  Kept here
# because the values are referenced by both the recording site
# (downloader / retry path) and the read site (``get_servers_for_media``)
# and we want a single source of truth.
REPUTATION_WEIGHTS = {
    "corrupt": 10,    # RPM body fails preflight or rpm-level digest
    "http_5xx": 5,    # Server-side error response
    "network": 5,     # Timeout / DNS / connreset / partial transfer
    "http_4xx": 3,    # Not-found / not-allowed: missing content on this mirror
    "slow": 2,        # Sustained slow transfer (informational deprioritisation)
}


class ServerMixin:
    """Mixin providing server CRUD operations.

    Requires:
        - self.conn: sqlite3.Connection
        - self._get_connection(): method returning thread-safe connection
    """

    def add_server(self, name: str, protocol: str, host: str, base_path: str = '',
                   is_official: bool = True, enabled: bool = True,
                   priority: int = 50, country: Optional[str] = None) -> int:
        """Add a new server.

        Args:
            name: Display name for the server
            protocol: 'http', 'https', or 'file'
            host: FQDN or 'localhost' for file://
            base_path: Base path on the server (e.g., '/mageia')
            is_official: True for official Mageia mirrors
            enabled: Whether the server is enabled
            priority: Manual priority (higher = preferred)
            country: ISO 3166 two-letter country code (e.g. 'FR'),
                or None if unknown.

        Returns:
            Server ID
        """
        cursor = self.conn.execute("""
            INSERT INTO server (name, protocol, host, base_path, is_official,
                               enabled, priority, country, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, protocol, host, base_path, int(is_official),
              int(enabled), priority, country, int(time.time())))
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
        """Get server by host/base_path (unique key).

        *protocol* is accepted for call-site convenience but is **not**
        part of the lookup — the unique constraint is ``(host, base_path)``.
        """
        cursor = self.conn.execute(
            "SELECT * FROM server WHERE host = ? AND base_path = ?",
            (host, base_path)
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

    def set_server_country_by_id(self, server_id: int, country: str):
        """Set the ISO 3166 country code for a server.

        Used by :func:`~urpm.core.mirrorlist.backfill_server_countries`
        to populate the ``country`` column for servers added before geo
        filtering was available.

        Args:
            server_id: Server ID.
            country: Two-letter ISO 3166 country code (e.g. ``'FR'``).
        """
        self.conn.execute(
            "UPDATE server SET country = ? WHERE id = ?",
            (country, server_id)
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

    def update_server_stats(self, server_id: int, *,
                             bandwidth_kbps: int = None,
                             success: bool = None,
                             latency_ms: int = None):
        """Update server performance statistics using exponential moving average.

        Uses EWMA (α=0.3): new_avg = 0.3 × measurement + 0.7 × current_avg.
        This gives inertia — a single slow download won't trash a good server,
        and a single fast download won't rescue a bad one. Sustained performance
        changes are reflected after several downloads.

        Args:
            server_id: Server ID to update
            bandwidth_kbps: Measured download speed in KB/s (None = no update)
            success: True if download succeeded, False if failed (None = no update)
            latency_ms: Measured round-trip latency in ms (None = no update)
        """
        # EWMA weight for the new measurement vs accumulated history.
        # Using SQL CASE expressions keeps the read-modify-write fully inside
        # SQLite, making it atomic — no separate SELECT needed, no race condition
        # when multiple download workers update the same server concurrently.
        ALPHA = 0.3

        conn = self._get_connection()
        now = int(time.time())

        set_parts = ["last_check = ?"]
        params: list = [now]

        if bandwidth_kbps is not None and bandwidth_kbps > 0:
            set_parts.append(
                "bandwidth_kbps = CASE "
                "WHEN bandwidth_kbps IS NULL THEN ? "
                f"ELSE CAST({ALPHA} * ? + {1 - ALPHA} * bandwidth_kbps AS INTEGER) "
                "END"
            )
            # Same value used for both the NULL branch and the EWMA branch.
            params += [bandwidth_kbps, bandwidth_kbps]

        if latency_ms is not None and latency_ms > 0:
            set_parts.append(
                "latency_ms = CASE "
                "WHEN latency_ms IS NULL THEN ? "
                f"ELSE CAST({ALPHA} * ? + {1 - ALPHA} * latency_ms AS INTEGER) "
                "END"
            )
            params += [latency_ms, latency_ms]

        if success is True:
            set_parts.append("success_count = COALESCE(success_count, 0) + 1")
        elif success is False:
            set_parts.append("failure_count = COALESCE(failure_count, 0) + 1")

        params.append(server_id)
        with self._lock:
            conn.execute(
                f"UPDATE server SET {', '.join(set_parts)} WHERE id = ?",
                params
            )
            conn.commit()

    def get_servers_for_media(self, media_id: int, enabled_only: bool = True,
                               limit: int = None,
                               reputation_window_hours: int = 24,
                               include_blacklisted: bool = False) -> List[Dict]:
        """Get all servers that can serve a media, ordered by preference. Thread-safe.

        Ordering precedence (bug #3 iteration B):

        1. Manual priority (user intent — highest first).
        2. Sliding-window reputation score — servers with recent
           corruption / 4xx / 5xx / network / slow events sink to the
           bottom of their priority tier.  Servers with no events score
           a clean 100.
        3. Measured bandwidth as tiebreaker (higher = faster).  Servers
           never measured count as 0 KB/s.

        Blacklisted servers (``blacklisted_at`` IS NOT NULL — set by
        :meth:`blacklist_server` on a signature failure) are filtered
        out unconditionally unless ``include_blacklisted=True`` is
        passed.  That flag exists only for diagnostic / admin commands
        like ``urpm server list``.

        Args:
            media_id: Media ID.
            enabled_only: Only return enabled servers.
            limit: Maximum number of servers to return.
            reputation_window_hours: Width of the sliding window used
                to compute the reputation score.  24 by default.
            include_blacklisted: When True, do not filter out
                blacklisted servers (used by admin UIs).

        Returns:
            List of server dicts, best server first.  Each dict carries
            a ``reputation_score`` key (integer 0-100) computed inline.
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - reputation_window_hours * 3600
        # Subquery uses the (server_id, ts) index for an efficient
        # sliding-window SUM(weight).  No event rows → 0 penalty →
        # score 100, preserving today's ordering for healthy mirrors.
        query = """
            SELECT s.*,
                   MAX(0, 100 - COALESCE((
                       SELECT SUM(weight)
                       FROM server_failure_events
                       WHERE server_id = s.id AND ts > ?
                   ), 0)) AS reputation_score
            FROM server s
            JOIN server_media sm ON s.id = sm.server_id
            WHERE sm.media_id = ?
        """
        params: List = [cutoff, media_id]
        if enabled_only:
            query += " AND s.enabled = 1"
        if not include_blacklisted:
            query += " AND s.blacklisted_at IS NULL"
        query += (
            " ORDER BY s.priority DESC, reputation_score DESC, "
            "COALESCE(s.bandwidth_kbps, 0) DESC, s.name"
        )
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor]

    # ── Security blacklist ────────────────────────────────────────────

    def blacklist_server(self, server_id: int, reason: str,
                         detail: Optional[str] = None) -> None:
        """Mark a server as compromised — out of every mirror pool.

        Triggered when the install pipeline observes a signature
        failure on an RPM the server just served.  Reactivation
        requires a human running ``urpm server unblacklist <name>``
        after manual verification (intentional: a security
        blacklisting does not auto-clear).

        Also appends an event row to ``server_failure_events`` with
        category ``'signature'`` and weight 0, so ``urpm server status``
        can show the full compromise history (filename, message, when)
        — the short ``blacklist_reason`` on the server row only
        captures the most recent event.

        Args:
            server_id: Server that just served the tampered RPM.
            reason: Short human-readable summary stored on the
                server row (last-write-wins).  Shown in the alert
                banner and ``urpm server list``.
            detail: Optional longer context for the event log.  When
                ``None``, ``reason`` is reused so the event row is
                self-explanatory.
        """
        now = int(time.time())
        conn = self._get_connection()
        # Reset acknowledgement: a fresh blacklisting event always
        # re-arms the persistent banner reminder even if the user had
        # acknowledged a previous one.
        conn.execute(
            "UPDATE server SET blacklisted_at = ?, blacklist_reason = ?, "
            "blacklist_acknowledged_at = NULL WHERE id = ?",
            (now, reason, server_id),
        )
        conn.execute(
            "INSERT INTO server_failure_events "
            "(server_id, ts, category, weight, detail) VALUES (?, ?, ?, ?, ?)",
            (server_id, now, "signature", 0, detail or reason),
        )
        conn.commit()

    def unblacklist_server(self, server_id: int) -> bool:
        """Clear the security blacklist on a server.

        Also clears the acknowledgement timestamp so a subsequent
        blacklisting event will trigger the banner again from
        scratch.

        Returns:
            True if a row was actually cleared (the server existed and
            was blacklisted), False if it was not blacklisted to begin
            with.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "UPDATE server SET blacklisted_at = NULL, blacklist_reason = NULL, "
            "blacklist_acknowledged_at = NULL "
            "WHERE id = ? AND blacklisted_at IS NOT NULL",
            (server_id,),
        )
        conn.commit()
        return cursor.rowcount > 0

    def acknowledge_blacklist(self, server_id: int) -> bool:
        """Stop nagging the persistent banner for this server.

        The server stays in the mirror-pool exclusion list (manual
        ``unblacklist`` still required to reactivate); this only marks
        "the user has seen the alert and is handling it".

        Returns:
            True when an acknowledgement was actually stored, False
            when the server is not blacklisted or already
            acknowledged.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "UPDATE server SET blacklist_acknowledged_at = ? "
            "WHERE id = ? AND blacklisted_at IS NOT NULL "
            "AND blacklist_acknowledged_at IS NULL",
            (int(time.time()), server_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def is_blacklisted(self, server_id: int) -> bool:
        """Return True when ``server_id`` is currently blacklisted."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT blacklisted_at FROM server WHERE id = ?", (server_id,),
        ).fetchone()
        return bool(row and row["blacklisted_at"] is not None)

    def list_blacklisted_servers(self, unacknowledged_only: bool = False) -> List[Dict]:
        """Return every currently blacklisted server with its reason.

        Used by the persistent banner reminder shown at the start of
        ``urpm install`` / ``upgrade`` / ``media update`` runs while
        any compromise is unresolved, and by ``urpm server list`` /
        ``urpm server status`` for admin inspection.

        Args:
            unacknowledged_only: When True, skip servers the user
                already acknowledged with ``urpm server ack-blacklist``.
                Used by the persistent banner so it stops nagging
                after the user has seen the alert.
        """
        conn = self._get_connection()
        query = "SELECT * FROM server WHERE blacklisted_at IS NOT NULL"
        if unacknowledged_only:
            query += " AND blacklist_acknowledged_at IS NULL"
        query += " ORDER BY blacklisted_at DESC"
        cursor = conn.execute(query)
        return [dict(row) for row in cursor]

    # ── Reputation event log ──────────────────────────────────────────

    def record_server_failure(self, server_id: int, category: str,
                              detail: Optional[str] = None,
                              weight: Optional[int] = None) -> None:
        """Append a failure event for ``server_id``.

        The reputation score consulted by :meth:`get_servers_for_media`
        is a sliding-window ``SUM(weight)`` over this table.

        Args:
            server_id: Server that misbehaved.
            category: One of the keys in :data:`REPUTATION_WEIGHTS`.
                Unknown categories are recorded with weight 0 so the
                row stays in the audit log but does not move the
                score — callers should still use the canonical names.
            detail: Optional debugging context (filename, error
                message…).  Not consulted by the scoring path; visible
                in ``urpm server status`` for forensics.
            weight: Override the category default (rarely needed).
        """
        if weight is None:
            weight = REPUTATION_WEIGHTS.get(category, 0)
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO server_failure_events "
            "(server_id, ts, category, weight, detail) VALUES (?, ?, ?, ?, ?)",
            (server_id, int(time.time()), category, weight, detail),
        )
        conn.commit()

    def get_server_reputation_score(self, server_id: int,
                                    window_hours: int = 24) -> int:
        """Return the reputation score in ``[0, 100]`` for ``server_id``.

        Score is ``100 - SUM(weight)`` of events recorded within the
        last ``window_hours`` hours, clamped to 0 below.  100 means no
        recent failures.
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - window_hours * 3600
        row = conn.execute(
            "SELECT COALESCE(SUM(weight), 0) AS total "
            "FROM server_failure_events "
            "WHERE server_id = ? AND ts > ?",
            (server_id, cutoff),
        ).fetchone()
        return max(0, 100 - (row["total"] if row else 0))

    def get_server_recent_failures(self, server_id: int,
                                   window_hours: int = 24,
                                   limit: int = 20) -> List[Dict]:
        """Return the recent failure events for forensic inspection.

        Used by ``urpm server status <name>`` to show why a mirror's
        reputation is low.
        """
        conn = self._get_connection()
        cutoff = int(time.time()) - window_hours * 3600
        cursor = conn.execute(
            "SELECT * FROM server_failure_events "
            "WHERE server_id = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT ?",
            (server_id, cutoff, limit),
        )
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
