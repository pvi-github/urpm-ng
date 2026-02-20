"""Package constraints: pins and holds database operations."""

import sqlite3
import time
from typing import Dict, List, Set


class ConstraintsMixin:
    """Mixin providing package pins and holds operations.

    Requires:
        - self.conn: sqlite3.Connection
        - self.get_media(name): method to get media info
    """

    def add_pin(self, package_pattern: str, media_pattern: str = None,
                priority: int = 100, version_pattern: str = None,
                comment: str = None) -> int:
        """Add a pin rule for package priority.

        Examples:
            # Prefer firefox from 'Cauldron' media
            add_pin('firefox', 'Cauldron', priority=500)

            # All lib64* packages from 'Core Updates Testing' for testing
            add_pin('lib64*', 'Core Updates Testing', priority=600)

            # Pin all packages from stable with low priority (allow overrides)
            add_pin('*', 'Core Release', priority=50)

        Returns:
            Pin ID
        """
        cursor = self.conn.execute("""
            INSERT INTO pins (package_pattern, media_pattern, priority,
                            version_pattern, comment, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (package_pattern, media_pattern, priority, version_pattern,
              comment, int(time.time())))
        self.conn.commit()
        return cursor.lastrowid

    def remove_pin(self, pin_id: int):
        """Remove a pin rule."""
        self.conn.execute("DELETE FROM pins WHERE id = ?", (pin_id,))
        self.conn.commit()

    def list_pins(self) -> List[Dict]:
        """List all pin rules."""
        cursor = self.conn.execute("""
            SELECT id, package_pattern, media_pattern, priority,
                   version_pattern, comment
            FROM pins ORDER BY priority DESC
        """)
        return [dict(row) for row in cursor]

    def get_pin_priority(self, package_name: str, media_name: str) -> int:
        """Get effective priority for a package from a media, considering pins.

        Returns the highest matching pin priority, or media default priority.
        """
        import fnmatch

        # Get all pins that could match
        cursor = self.conn.execute("SELECT * FROM pins ORDER BY priority DESC")
        pins = [dict(row) for row in cursor]

        for pin in pins:
            pkg_match = fnmatch.fnmatch(package_name.lower(),
                                        pin['package_pattern'].lower())
            media_match = (pin['media_pattern'] is None or
                         fnmatch.fnmatch(media_name, pin['media_pattern']))

            if pkg_match and media_match:
                return pin['priority']

        # No pin match - return media default priority
        media = self.get_media(media_name)
        return media['priority'] if media else 50

    # =========================================================================
    # Package holds (prevent upgrades and obsoletes replacement)
    # =========================================================================

    def add_hold(self, package_name: str, reason: str = None) -> bool:
        """Add a hold on a package.

        Args:
            package_name: Exact package name to hold
            reason: Optional reason for the hold

        Returns:
            True if hold was added, False if already held
        """
        try:
            self.conn.execute("""
                INSERT INTO held_packages (package_name, reason, added_timestamp)
                VALUES (?, ?, ?)
            """, (package_name, reason, int(time.time())))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # Already held

    def remove_hold(self, package_name: str) -> bool:
        """Remove a hold from a package.

        Returns:
            True if hold was removed, False if not held
        """
        cursor = self.conn.execute(
            "DELETE FROM held_packages WHERE package_name = ?",
            (package_name,)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def is_held(self, package_name: str) -> bool:
        """Check if a package is held."""
        cursor = self.conn.execute(
            "SELECT 1 FROM held_packages WHERE package_name = ?",
            (package_name,)
        )
        return cursor.fetchone() is not None

    def list_holds(self) -> List[Dict]:
        """List all held packages."""
        cursor = self.conn.execute("""
            SELECT package_name, reason, added_timestamp
            FROM held_packages
            ORDER BY package_name
        """)
        return [dict(row) for row in cursor]

    def get_held_packages_set(self) -> Set[str]:
        """Get set of all held package names (for fast lookup)."""
        cursor = self.conn.execute("SELECT package_name FROM held_packages")
        return {row[0] for row in cursor}
