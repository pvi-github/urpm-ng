"""
SQLite database for urpm package cache

Provides fast queries on package metadata, replacing repeated parsing
of synthesis/hdlist files.
"""

import sqlite3
import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Iterator

# Schema version - increment when schema changes
SCHEMA_VERSION = 7

# Extended schema with media, config, history tables
SCHEMA = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER PRIMARY KEY
);

-- Packages table
CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER,
    
    -- NEVRA
    name TEXT NOT NULL,
    epoch INTEGER DEFAULT 0,
    version TEXT NOT NULL,
    release TEXT NOT NULL,
    arch TEXT NOT NULL,
    
    -- Computed fields for fast search
    name_lower TEXT NOT NULL,
    nevra TEXT NOT NULL,
    
    -- Metadata
    summary TEXT,
    description TEXT,
    size INTEGER DEFAULT 0,
    group_name TEXT,
    url TEXT,
    license TEXT,
    
    -- Source tracking
    source TEXT,  -- 'synthesis' or 'hdlist'
    pkg_hash TEXT,
    added_timestamp INTEGER,
    
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE,
    UNIQUE(nevra, media_id)
);

-- Dependencies
CREATE TABLE IF NOT EXISTS requires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    operator TEXT,
    version TEXT,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS provides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    operator TEXT,
    version TEXT,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    operator TEXT,
    version TEXT,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS obsoletes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    operator TEXT,
    version TEXT,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

-- Weak dependencies (RPM 4.12+)
CREATE TABLE IF NOT EXISTS recommends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS suggests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS enhances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pkg_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    FOREIGN KEY (pkg_id) REFERENCES packages(id) ON DELETE CASCADE
);

-- Media (repositories)
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url TEXT,
    mirrorlist TEXT,
    enabled INTEGER DEFAULT 1,
    update_media INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 50,
    
    -- Sync state
    last_sync INTEGER,
    synthesis_md5 TEXT,
    hdlist_md5 TEXT,
    
    added_timestamp INTEGER
);

-- Transaction history
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    action TEXT NOT NULL,      -- 'install', 'remove', 'upgrade', 'undo', 'rollback'
    status TEXT DEFAULT 'running',  -- 'running', 'complete', 'interrupted'
    command TEXT,              -- full command line
    user TEXT,
    return_code INTEGER,
    undone_by INTEGER,         -- transaction ID that undid this one (NULL if not undone)
    FOREIGN KEY (undone_by) REFERENCES history(id)
);

CREATE TABLE IF NOT EXISTS history_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id INTEGER NOT NULL,
    pkg_nevra TEXT NOT NULL,
    pkg_name TEXT NOT NULL,    -- for easier queries
    action TEXT NOT NULL,      -- 'install', 'remove', 'upgrade', 'downgrade'
    reason TEXT NOT NULL,      -- 'explicit', 'dependency'
    previous_nevra TEXT,       -- for upgrade/downgrade: what was there before
    FOREIGN KEY (history_id) REFERENCES history(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp);
CREATE INDEX IF NOT EXISTS idx_history_status ON history(status);
CREATE INDEX IF NOT EXISTS idx_history_pkg_name ON history_packages(pkg_name);

-- Configuration
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Package pinning (per-package priority overrides)
-- Similar to APT pinning: allows devs/testers to prefer specific media for specific packages
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_pattern TEXT NOT NULL,  -- glob pattern: 'firefox', 'lib64*', '*'
    media_pattern TEXT,             -- media name pattern (NULL = any media)
    priority INTEGER DEFAULT 100,   -- higher = preferred (overrides media.priority)
    version_pattern TEXT,           -- optional version constraint: '>=120.0', '<2.0'
    comment TEXT,                   -- user note
    added_timestamp INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pins_pattern ON pins(package_pattern);

-- Peer tracking for P2P downloads (provenance)
CREATE TABLE IF NOT EXISTS peer_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,          -- Full path to the downloaded file
    peer_host TEXT NOT NULL,
    peer_port INTEGER NOT NULL,
    download_time INTEGER NOT NULL,
    file_size INTEGER,
    checksum_sha256 TEXT,
    verified INTEGER DEFAULT 0,       -- 1 if GPG/checksum verified
    UNIQUE(file_path)
);

CREATE INDEX IF NOT EXISTS idx_peer_downloads_host ON peer_downloads(peer_host);
CREATE INDEX IF NOT EXISTS idx_peer_downloads_filename ON peer_downloads(filename);

-- Peer blacklist
CREATE TABLE IF NOT EXISTS peer_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_host TEXT NOT NULL,
    peer_port INTEGER,                -- NULL = all ports for this host
    reason TEXT,
    blacklist_time INTEGER NOT NULL,
    UNIQUE(peer_host, peer_port)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_pkg_name_lower ON packages(name_lower);
CREATE INDEX IF NOT EXISTS idx_pkg_nevra ON packages(nevra);
CREATE INDEX IF NOT EXISTS idx_pkg_media ON packages(media_id);
CREATE INDEX IF NOT EXISTS idx_provides_cap ON provides(capability);
CREATE INDEX IF NOT EXISTS idx_provides_pkg ON provides(pkg_id);
CREATE INDEX IF NOT EXISTS idx_requires_cap ON requires(capability);
CREATE INDEX IF NOT EXISTS idx_requires_pkg ON requires(pkg_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_cap ON conflicts(capability);
CREATE INDEX IF NOT EXISTS idx_conflicts_pkg ON conflicts(pkg_id);
CREATE INDEX IF NOT EXISTS idx_obsoletes_cap ON obsoletes(capability);
CREATE INDEX IF NOT EXISTS idx_obsoletes_pkg ON obsoletes(pkg_id);
"""

# Migrations: dict of from_version -> (to_version, sql_script)
# Each migration upgrades from one version to the next
MIGRATIONS = {
    6: (7, """
        -- Migration v6 -> v7: Add peer tracking tables
        CREATE TABLE IF NOT EXISTS peer_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            peer_host TEXT NOT NULL,
            peer_port INTEGER NOT NULL,
            download_time INTEGER NOT NULL,
            file_size INTEGER,
            checksum_sha256 TEXT,
            verified INTEGER DEFAULT 0,
            UNIQUE(file_path)
        );
        CREATE INDEX IF NOT EXISTS idx_peer_downloads_host ON peer_downloads(peer_host);
        CREATE INDEX IF NOT EXISTS idx_peer_downloads_filename ON peer_downloads(filename);

        CREATE TABLE IF NOT EXISTS peer_blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_host TEXT NOT NULL,
            peer_port INTEGER,
            reason TEXT,
            blacklist_time INTEGER NOT NULL,
            UNIQUE(peer_host, peer_port)
        );
    """),
}


class PackageDatabase:
    """SQLite database for package metadata cache."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file.
                     If None, auto-detects based on .urpm.local or environment.
        """
        if db_path is None:
            from .config import get_db_path
            db_path = get_db_path()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        
        self._init_schema()
    
    def _init_schema(self):
        """Initialize or migrate database schema."""
        # Check existing schema version
        try:
            cursor = self.conn.execute("SELECT version FROM schema_info LIMIT 1")
            row = cursor.fetchone()
            current_version = row[0] if row else 0
        except sqlite3.OperationalError:
            current_version = 0

        if current_version == 0:
            # Fresh database - create full schema
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_info (version) VALUES (?)",
                (SCHEMA_VERSION,)
            )
            self.conn.commit()
        elif current_version < SCHEMA_VERSION:
            # Apply migrations incrementally
            self._apply_migrations(current_version)
        elif current_version > SCHEMA_VERSION:
            # Future version - warn but try to continue
            import logging
            logging.warning(
                f"Database schema version {current_version} is newer than "
                f"supported version {SCHEMA_VERSION}. Consider upgrading urpm."
            )

    def _apply_migrations(self, from_version: int):
        """Apply all migrations from from_version to SCHEMA_VERSION."""
        import logging
        logger = logging.getLogger(__name__)

        version = from_version
        while version < SCHEMA_VERSION:
            if version not in MIGRATIONS:
                # No migration path - must recreate (shouldn't happen with proper migrations)
                logger.error(
                    f"No migration from version {version}. "
                    f"Database will be recreated (data loss!)."
                )
                self.conn.close()
                self.db_path.unlink(missing_ok=True)
                self.conn = sqlite3.connect(str(self.db_path))
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.conn.executescript(SCHEMA)
                version = SCHEMA_VERSION
                break

            to_version, migration_sql = MIGRATIONS[version]
            logger.info(f"Migrating database schema v{version} -> v{to_version}")

            try:
                self.conn.executescript(migration_sql)
                self.conn.execute(
                    "UPDATE schema_info SET version = ?", (to_version,)
                )
                self.conn.commit()
                version = to_version
            except sqlite3.Error as e:
                logger.error(f"Migration v{version} -> v{to_version} failed: {e}")
                raise RuntimeError(f"Database migration failed: {e}")

        logger.info(f"Database schema is now at version {SCHEMA_VERSION}")
    
    def close(self):
        """Close database connection."""
        self.conn.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    # =========================================================================
    # Media management
    # =========================================================================
    
    def add_media(self, name: str, url: str = None, mirrorlist: str = None,
                  enabled: bool = True, update: bool = False) -> int:
        """Add a new media source.
        
        Returns:
            Media ID
        """
        cursor = self.conn.execute("""
            INSERT INTO media (name, url, mirrorlist, enabled, update_media, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, url, mirrorlist, int(enabled), int(update), int(time.time())))
        self.conn.commit()
        return cursor.lastrowid
    
    def remove_media(self, name: str):
        """Remove a media source and all its packages."""
        self.conn.execute("DELETE FROM media WHERE name = ?", (name,))
        self.conn.commit()
    
    def get_media(self, name: str) -> Optional[Dict]:
        """Get media info by name."""
        cursor = self.conn.execute(
            "SELECT * FROM media WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def list_media(self) -> List[Dict]:
        """List all media sources."""
        cursor = self.conn.execute("SELECT * FROM media ORDER BY priority, name")
        return [dict(row) for row in cursor]
    
    def enable_media(self, name: str, enabled: bool = True):
        """Enable or disable a media source."""
        self.conn.execute(
            "UPDATE media SET enabled = ? WHERE name = ?",
            (int(enabled), name)
        )
        self.conn.commit()
    
    # =========================================================================
    # Package import
    # =========================================================================

    def import_packages(self, packages: Iterator[Dict], media_id: int = None,
                        source: str = 'synthesis', progress_callback=None,
                        batch_size: int = 1000):
        """Import packages from a parsed synthesis or hdlist.

        Uses bulk inserts for performance.

        Args:
            packages: Iterator of package dictionaries
            media_id: Associated media ID (optional)
            source: Source type ('synthesis' or 'hdlist')
            progress_callback: Optional callback(count, pkg_name)
            batch_size: Number of packages per batch
        """
        timestamp = int(time.time())
        count = 0

        # Accumulators for batch inserts
        pkg_rows = []
        # We'll need to track nevra -> pkg_id mapping for deps
        # So we insert packages first, then query their IDs

        # Collect all packages first
        all_packages = list(packages)
        total = len(all_packages)

        if progress_callback:
            progress_callback(0, "preparing...")

        # Begin transaction
        self.conn.execute("BEGIN TRANSACTION")

        try:
            # Bulk insert packages
            pkg_rows = []
            for pkg in all_packages:
                hash_data = f"{pkg['nevra']}|{pkg.get('summary', '')}"
                pkg_hash = hashlib.sha256(hash_data.encode()).hexdigest()[:16]

                pkg_rows.append((
                    media_id,
                    pkg['name'],
                    pkg.get('epoch', 0),
                    pkg['version'],
                    pkg['release'],
                    pkg['arch'],
                    pkg['name'].lower(),
                    pkg['nevra'],
                    pkg.get('summary', ''),
                    pkg.get('description', ''),
                    pkg.get('size', 0),
                    pkg.get('group', ''),
                    pkg.get('url', ''),
                    pkg.get('license', ''),
                    source,
                    pkg_hash,
                    timestamp
                ))

            self.conn.executemany("""
                INSERT OR REPLACE INTO packages
                (media_id, name, epoch, version, release, arch, name_lower, nevra,
                 summary, description, size, group_name, url, license,
                 source, pkg_hash, added_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, pkg_rows)

            if progress_callback:
                progress_callback(total, "packages inserted, indexing deps...")

            # Build nevra -> pkg_id mapping
            cursor = self.conn.execute(
                "SELECT id, nevra FROM packages WHERE media_id = ?",
                (media_id,)
            )
            nevra_to_id = {row[1]: row[0] for row in cursor}

            # Collect all dependencies
            requires_rows = []
            provides_rows = []
            conflicts_rows = []
            obsoletes_rows = []
            recommends_rows = []
            suggests_rows = []
            supplements_rows = []
            enhances_rows = []

            for pkg in all_packages:
                pkg_id = nevra_to_id.get(pkg['nevra'])
                if not pkg_id:
                    continue

                for cap in pkg.get('requires', []):
                    requires_rows.append((pkg_id, cap))
                for cap in pkg.get('provides', []):
                    provides_rows.append((pkg_id, cap))
                for cap in pkg.get('conflicts', []):
                    conflicts_rows.append((pkg_id, cap))
                for cap in pkg.get('obsoletes', []):
                    obsoletes_rows.append((pkg_id, cap))
                # Weak dependencies
                for cap in pkg.get('recommends', []):
                    recommends_rows.append((pkg_id, cap))
                for cap in pkg.get('suggests', []):
                    suggests_rows.append((pkg_id, cap))
                for cap in pkg.get('supplements', []):
                    supplements_rows.append((pkg_id, cap))
                for cap in pkg.get('enhances', []):
                    enhances_rows.append((pkg_id, cap))

            # Bulk insert dependencies
            if requires_rows:
                self.conn.executemany(
                    "INSERT INTO requires (pkg_id, capability) VALUES (?, ?)",
                    requires_rows
                )
            if provides_rows:
                self.conn.executemany(
                    "INSERT INTO provides (pkg_id, capability) VALUES (?, ?)",
                    provides_rows
                )
            if conflicts_rows:
                self.conn.executemany(
                    "INSERT INTO conflicts (pkg_id, capability) VALUES (?, ?)",
                    conflicts_rows
                )
            if obsoletes_rows:
                self.conn.executemany(
                    "INSERT INTO obsoletes (pkg_id, capability) VALUES (?, ?)",
                    obsoletes_rows
                )
            # Weak dependencies
            if recommends_rows:
                self.conn.executemany(
                    "INSERT INTO recommends (pkg_id, capability) VALUES (?, ?)",
                    recommends_rows
                )
            if suggests_rows:
                self.conn.executemany(
                    "INSERT INTO suggests (pkg_id, capability) VALUES (?, ?)",
                    suggests_rows
                )
            if supplements_rows:
                self.conn.executemany(
                    "INSERT INTO supplements (pkg_id, capability) VALUES (?, ?)",
                    supplements_rows
                )
            if enhances_rows:
                self.conn.executemany(
                    "INSERT INTO enhances (pkg_id, capability) VALUES (?, ?)",
                    enhances_rows
                )

            self.conn.commit()

            if progress_callback:
                progress_callback(total, "done")

            return total

        except Exception as e:
            self.conn.rollback()
            raise e
    
    def clear_media_packages(self, media_id: int):
        """Remove all packages from a media.

        Deletes from child tables first to avoid slow CASCADE.
        """
        # Delete dependencies first (faster than CASCADE)
        pkg_subquery = "(SELECT id FROM packages WHERE media_id = ?)"
        for table in ('requires', 'provides', 'conflicts', 'obsoletes',
                      'recommends', 'suggests', 'supplements', 'enhances'):
            self.conn.execute(
                f"DELETE FROM {table} WHERE pkg_id IN {pkg_subquery}",
                (media_id,)
            )

        # Now delete packages
        self.conn.execute("DELETE FROM packages WHERE media_id = ?", (media_id,))
        self.conn.commit()
    
    # =========================================================================
    # Package queries
    # =========================================================================
    
    def search(self, pattern: str, limit: int = 50, search_provides: bool = False) -> List[Dict]:
        """Search packages by name pattern, optionally also in provides.

        Args:
            pattern: Search pattern (case-insensitive substring match)
            limit: Maximum results to return
            search_provides: If True, also search in provides capabilities

        Returns:
            List of package dicts. If found via provides, includes 'matched_provide' key.
        """
        pattern_lower = f'%{pattern.lower()}%'
        results = []
        seen_ids = set()

        # Search by name
        cursor = self.conn.execute("""
            SELECT id, name, version, release, arch, nevra, summary, size
            FROM packages
            WHERE name_lower LIKE ?
            ORDER BY name_lower
            LIMIT ?
        """, (pattern_lower, limit))

        for row in cursor:
            pkg = dict(row)
            results.append(pkg)
            seen_ids.add(pkg['id'])

        # Search in provides if requested and we have room for more results
        if search_provides and len(results) < limit:
            remaining = limit - len(results)
            cursor = self.conn.execute("""
                SELECT DISTINCT p.id, p.name, p.version, p.release, p.arch,
                       p.nevra, p.summary, p.size, pr.capability as matched_provide
                FROM packages p
                JOIN provides pr ON pr.pkg_id = p.id
                WHERE LOWER(pr.capability) LIKE ?
                ORDER BY p.name_lower
                LIMIT ?
            """, (pattern_lower, remaining + len(seen_ids)))  # Get extra to filter dupes

            for row in cursor:
                pkg = dict(row)
                if pkg['id'] not in seen_ids:
                    seen_ids.add(pkg['id'])
                    results.append(pkg)
                    if len(results) >= limit:
                        break

        return results
    
    def get_package(self, name: str) -> Optional[Dict]:
        """Get a package by exact name (latest version)."""
        cursor = self.conn.execute("""
            SELECT * FROM packages
            WHERE name_lower = ?
            ORDER BY epoch DESC, version DESC, release DESC
            LIMIT 1
        """, (name.lower(),))

        row = cursor.fetchone()
        if not row:
            return None

        pkg = dict(row)
        pkg['requires'] = self._get_deps(pkg['id'], 'requires')
        pkg['provides'] = self._get_deps(pkg['id'], 'provides')
        pkg['conflicts'] = self._get_deps(pkg['id'], 'conflicts')
        pkg['obsoletes'] = self._get_deps(pkg['id'], 'obsoletes')

        return pkg

    def get_package_by_nevra(self, nevra: str) -> Optional[Dict]:
        """Get a package by exact NEVRA."""
        cursor = self.conn.execute("""
            SELECT * FROM packages
            WHERE nevra = ?
            LIMIT 1
        """, (nevra,))

        row = cursor.fetchone()
        if not row:
            return None

        pkg = dict(row)
        pkg['requires'] = self._get_deps(pkg['id'], 'requires')
        pkg['provides'] = self._get_deps(pkg['id'], 'provides')
        pkg['conflicts'] = self._get_deps(pkg['id'], 'conflicts')
        pkg['obsoletes'] = self._get_deps(pkg['id'], 'obsoletes')

        return pkg

    def get_package_smart(self, identifier: str) -> Optional[Dict]:
        """Get a package by name or NEVRA.

        If identifier looks like a NEVRA (contains version pattern),
        try exact NEVRA match first, then fall back to name.
        """
        import re
        # Check if it looks like a NEVRA
        if re.search(r'-\d+[.:]', identifier):
            # Try NEVRA first
            pkg = self.get_package_by_nevra(identifier)
            if pkg:
                return pkg
            # Extract name and try that
            match = re.match(r'^(.+?)-\d+[.:]', identifier)
            if match:
                return self.get_package(match.group(1))
            return None
        else:
            return self.get_package(identifier)
    
    def _get_deps(self, pkg_id: int, table: str) -> List[str]:
        """Get dependencies from a specific table."""
        cursor = self.conn.execute(
            f"SELECT capability FROM {table} WHERE pkg_id = ?",
            (pkg_id,)
        )
        return [row[0] for row in cursor]
    
    def whatprovides(self, capability: str) -> List[Dict]:
        """Find packages that provide a capability."""
        cursor = self.conn.execute("""
            SELECT p.id, p.name, p.version, p.release, p.arch, p.nevra
            FROM packages p
            JOIN provides pr ON pr.pkg_id = p.id
            WHERE pr.capability = ?
            ORDER BY p.name_lower
        """, (capability,))
        
        return [dict(row) for row in cursor]
    
    def whatrequires(self, capability: str, limit: int = 50) -> List[Dict]:
        """Find packages that require a capability."""
        cursor = self.conn.execute("""
            SELECT p.id, p.name, p.version, p.release, p.arch, p.nevra
            FROM packages p
            JOIN requires r ON r.pkg_id = p.id
            WHERE r.capability = ?
            ORDER BY p.name_lower
            LIMIT ?
        """, (capability, limit))
        
        return [dict(row) for row in cursor]
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        stats = {}
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM packages")
        stats['packages'] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM provides")
        stats['provides'] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM requires")
        stats['requires'] = cursor.fetchone()[0]
        
        cursor = self.conn.execute("SELECT COUNT(*) FROM media")
        stats['media'] = cursor.fetchone()[0]
        
        stats['db_size_mb'] = self.db_path.stat().st_size / 1024 / 1024
        stats['db_path'] = str(self.db_path)
        
        return stats
    
    # =========================================================================
    # Configuration
    # =========================================================================
    
    def get_config(self, key: str, default: str = None) -> Optional[str]:
        """Get a configuration value."""
        cursor = self.conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        return row[0] if row else default
    
    def set_config(self, key: str, value: str):
        """Set a configuration value."""
        self.conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value)
        )
        self.conn.commit()

    # =========================================================================
    # Pinning (per-package priority overrides)
    # =========================================================================

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
    # Multi-media package resolution
    # =========================================================================

    def get_all_versions(self, name: str) -> List[Dict]:
        """Get all versions of a package from all media.

        Returns packages sorted by effective priority (pins + media priority),
        then by version (newest first).
        """
        cursor = self.conn.execute("""
            SELECT p.*, m.name as media_name, m.priority as media_priority
            FROM packages p
            LEFT JOIN media m ON p.media_id = m.id
            WHERE p.name_lower = ?
            ORDER BY p.epoch DESC, p.version DESC, p.release DESC
        """, (name.lower(),))

        packages = [dict(row) for row in cursor]

        # Add effective priority considering pins
        for pkg in packages:
            pkg['effective_priority'] = self.get_pin_priority(
                pkg['name'],
                pkg.get('media_name', '')
            )

        # Sort by effective priority (desc), then version
        packages.sort(key=lambda p: (
            -p['effective_priority'],
            -p.get('epoch', 0),
            p.get('version', ''),
            p.get('release', '')
        ), reverse=False)

        # Re-sort properly: priority desc, then EVR desc
        packages.sort(key=lambda p: p['effective_priority'], reverse=True)

        return packages

    def get_best_package(self, name: str) -> Optional[Dict]:
        """Get the best version of a package considering priorities.

        Takes into account:
        1. Pin rules (highest priority)
        2. Media priority
        3. Package version (EVR comparison)
        """
        versions = self.get_all_versions(name)
        if not versions:
            return None

        # First package is best (sorted by priority, then version)
        best = versions[0]
        best['requires'] = self._get_deps(best['id'], 'requires')
        best['provides'] = self._get_deps(best['id'], 'provides')
        best['conflicts'] = self._get_deps(best['id'], 'conflicts')
        best['obsoletes'] = self._get_deps(best['id'], 'obsoletes')

        return best

    # =========================================================================
    # Transaction History
    # =========================================================================

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
        """
        self.conn.execute("""
            INSERT INTO history_packages
            (history_id, pkg_nevra, pkg_name, action, reason, previous_nevra)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (transaction_id, nevra, name, action, reason, previous_nevra))
        self.conn.commit()

    def complete_transaction(self, transaction_id: int, return_code: int = 0):
        """Mark a transaction as complete."""
        self.conn.execute("""
            UPDATE history SET status = 'complete', return_code = ?
            WHERE id = ?
        """, (return_code, transaction_id))
        self.conn.commit()

    def abort_transaction(self, transaction_id: int):
        """Mark a transaction as interrupted."""
        self.conn.execute("""
            UPDATE history SET status = 'interrupted', return_code = -1
            WHERE id = ?
        """, (transaction_id,))
        self.conn.commit()

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

    # =========================================================================
    # Peer tracking (P2P provenance)
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
