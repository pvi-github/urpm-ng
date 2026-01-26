"""
SQLite database for urpm package cache

Provides fast queries on package metadata, replacing repeated parsing
of synthesis/hdlist files.
"""

import sqlite3
import hashlib
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Iterator, Set

# Schema version - increment when schema changes
SCHEMA_VERSION = 16

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
    filesize INTEGER DEFAULT 0,

    -- Source tracking
    source TEXT,  -- 'synthesis' or 'hdlist'
    pkg_hash TEXT,
    added_timestamp INTEGER,
    server_last_modified INTEGER,  -- Last-Modified from server (for replication priority)

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
    name TEXT NOT NULL UNIQUE,            -- Display name: 'Core Release', 'Nonfree Updates'
    short_name TEXT NOT NULL,             -- Filesystem-safe: 'core_release', 'nonfree_updates'
    mageia_version TEXT NOT NULL,         -- '9', '10', 'cauldron'
    architecture TEXT NOT NULL,           -- 'x86_64', 'aarch64'
    relative_path TEXT NOT NULL,          -- '9/x86_64/media/core/release'
    is_official INTEGER DEFAULT 1,        -- 0 = custom media
    allow_unsigned INTEGER DEFAULT 0,     -- 1 = skip signature check (custom only)
    enabled INTEGER DEFAULT 1,
    update_media INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 50,

    -- Proxy/replication settings (v11+)
    shared INTEGER DEFAULT 1,             -- 1 = serve to peers, 0 = don't share
    replication_policy TEXT DEFAULT 'on_demand',  -- 'none', 'on_demand', 'seed'
    replication_seeds TEXT,               -- JSON list of rpmsrate sections, e.g. ["INSTALL","CAT_PLASMA5"]
    quota_mb INTEGER,                     -- Per-media quota in MB (NULL = no limit)
    retention_days INTEGER DEFAULT 30,    -- Days to keep cached packages

    -- Sync state
    last_sync INTEGER,
    synthesis_md5 TEXT,
    hdlist_md5 TEXT,

    added_timestamp INTEGER,

    -- Legacy (kept for migration, will be removed later)
    url TEXT,
    mirrorlist TEXT,

    UNIQUE(mageia_version, architecture, short_name)
);

-- Servers (upstream mirrors or local)
CREATE TABLE IF NOT EXISTS server (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,              -- Display name: 'mageia-official', 'distrib-coffee'
    protocol TEXT NOT NULL DEFAULT 'https', -- 'http', 'https', 'file'
    host TEXT NOT NULL,                     -- FQDN: 'mirrors.mageia.org', 'localhost' for file
    base_path TEXT NOT NULL DEFAULT '',     -- '/mageia', '/pub/linux/Mageia', '/mirrors/mageia'
    is_official INTEGER DEFAULT 1,          -- 0 = custom server
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 50,            -- Manual preference (V1: only sort criteria)
    ip_mode TEXT DEFAULT 'auto',            -- 'auto', 'ipv4', 'ipv6', 'dual' (dual = prefer ipv4)
    -- Qualimetry (post-V1, NULL for now)
    latency_ms INTEGER,
    bandwidth_kbps INTEGER,
    failure_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    last_check INTEGER,
    added_timestamp INTEGER,
    UNIQUE(protocol, host, base_path)
);

-- N:M link between servers and media
CREATE TABLE IF NOT EXISTS server_media (
    server_id INTEGER NOT NULL,
    media_id INTEGER NOT NULL,
    added_timestamp INTEGER,
    PRIMARY KEY (server_id, media_id),
    FOREIGN KEY (server_id) REFERENCES server(id) ON DELETE CASCADE,
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE
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

-- Package holds (prevent upgrades and obsoletes replacement)
CREATE TABLE IF NOT EXISTS held_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_name TEXT NOT NULL UNIQUE,  -- exact package name (not pattern)
    reason TEXT,                        -- user note for why it's held
    added_timestamp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_held_packages_name ON held_packages(package_name);

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

-- Cache file tracking for quota management (v11+)
CREATE TABLE IF NOT EXISTS cache_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    media_id INTEGER,
    file_path TEXT NOT NULL,          -- Relative path from medias/
    file_size INTEGER NOT NULL,
    added_time INTEGER NOT NULL,      -- Download timestamp
    last_accessed INTEGER,            -- Last access time (for LRU)
    is_referenced INTEGER DEFAULT 1,  -- Still in current synthesis?
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE,
    UNIQUE(filename, media_id)
);

CREATE INDEX IF NOT EXISTS idx_cache_files_media ON cache_files(media_id);
CREATE INDEX IF NOT EXISTS idx_cache_files_referenced ON cache_files(is_referenced);
CREATE INDEX IF NOT EXISTS idx_cache_files_accessed ON cache_files(last_accessed);

-- Proxy configuration (v11+)
CREATE TABLE IF NOT EXISTS mirror_config (
    key TEXT PRIMARY KEY,
    value TEXT
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
    7: (8, """
        -- Migration v7 -> v8: Add server/server_media tables and extend media table

        -- Add new columns to media table
        ALTER TABLE media ADD COLUMN short_name TEXT;
        ALTER TABLE media ADD COLUMN mageia_version TEXT;
        ALTER TABLE media ADD COLUMN architecture TEXT;
        ALTER TABLE media ADD COLUMN relative_path TEXT;
        ALTER TABLE media ADD COLUMN is_official INTEGER DEFAULT 1;
        ALTER TABLE media ADD COLUMN allow_unsigned INTEGER DEFAULT 0;

        -- Create server table
        CREATE TABLE IF NOT EXISTS server (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            protocol TEXT NOT NULL DEFAULT 'https',
            host TEXT NOT NULL,
            base_path TEXT NOT NULL DEFAULT '',
            is_official INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 50,
            latency_ms INTEGER,
            bandwidth_kbps INTEGER,
            failure_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            last_check INTEGER,
            added_timestamp INTEGER,
            UNIQUE(protocol, host, base_path)
        );

        -- Create server_media link table
        CREATE TABLE IF NOT EXISTS server_media (
            server_id INTEGER NOT NULL,
            media_id INTEGER NOT NULL,
            added_timestamp INTEGER,
            PRIMARY KEY (server_id, media_id),
            FOREIGN KEY (server_id) REFERENCES server(id) ON DELETE CASCADE,
            FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE
        );

        -- Create indexes for server lookups
        CREATE INDEX IF NOT EXISTS idx_server_host ON server(host);
        CREATE INDEX IF NOT EXISTS idx_server_enabled ON server(enabled);
    """),
    8: (9, """
        -- Migration v8 -> v9: Directory structure migration (handled in Python code)
        -- allow_unsigned column is now added in v7->v8 migration
        SELECT 1;
    """),
    9: (10, """
        -- Migration v9 -> v10: Add ip_mode column to server table
        ALTER TABLE server ADD COLUMN ip_mode TEXT DEFAULT 'auto';
    """),
    10: (11, """
        -- Migration v10 -> v11: Add proxy/replication support

        -- New columns on media table for proxy/replication
        ALTER TABLE media ADD COLUMN proxy_enabled INTEGER DEFAULT 1;
        ALTER TABLE media ADD COLUMN replication_policy TEXT DEFAULT 'on_demand';
        ALTER TABLE media ADD COLUMN replication_since INTEGER;
        ALTER TABLE media ADD COLUMN quota_mb INTEGER;
        ALTER TABLE media ADD COLUMN retention_days INTEGER DEFAULT 30;

        -- Cache file tracking for quota management
        CREATE TABLE IF NOT EXISTS cache_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            media_id INTEGER,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            added_time INTEGER NOT NULL,
            last_accessed INTEGER,
            is_referenced INTEGER DEFAULT 1,
            FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE,
            UNIQUE(filename, media_id)
        );

        CREATE INDEX IF NOT EXISTS idx_cache_files_media ON cache_files(media_id);
        CREATE INDEX IF NOT EXISTS idx_cache_files_referenced ON cache_files(is_referenced);
        CREATE INDEX IF NOT EXISTS idx_cache_files_accessed ON cache_files(last_accessed);

        -- Proxy configuration
        CREATE TABLE IF NOT EXISTS proxy_config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """),
    11: (12, """
        -- Migration v11 -> v12: Add server_last_modified for replication priority
        ALTER TABLE packages ADD COLUMN server_last_modified INTEGER;
    """),
    12: (13, """
        -- Migration v12 -> v13: Add replication_seeds for seed-based replication
        ALTER TABLE media ADD COLUMN replication_seeds TEXT;
    """),
    13: (14, """
        -- Migration v13 -> v14: Rename proxy -> mirror (clearer naming)
        ALTER TABLE media RENAME COLUMN proxy_enabled TO shared;
        ALTER TABLE proxy_config RENAME TO mirror_config;
    """),
    14: (15, """
        -- Migration v14 -> v15: Adding column filesize in packages
        ALTER TABLE packages ADD COLUMN filesize INTEGER DEFAULT 0;
    """),
    15: (16, """
        -- Migration v15 -> v16: Reserved (no-op)
        SELECT 1;
    """),
    15: (16, """
        -- Migration v15 -> v16: Add held_packages table
        CREATE TABLE IF NOT EXISTS held_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL UNIQUE,
            reason TEXT,
            added_timestamp INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_held_packages_name ON held_packages(package_name);
    """),
}


class PackageDatabase:
    """SQLite database for package metadata cache.

    Thread-safety: Uses thread-local connections to allow safe concurrent access
    from multiple threads (e.g., ThreadPoolExecutor in sync_all_media).

    Process-safety: Uses WAL mode and busy_timeout for concurrent access from
    multiple processes (CLI + urpmd daemon).
    """

    # Timeout for waiting on locked database (5 seconds)
    BUSY_TIMEOUT_MS = 5000

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

        # Lock for thread-safe database access (used with ThreadingHTTPServer)
        self._lock = threading.RLock()

        # Thread-local storage for per-thread connections
        self._local = threading.local()

        # Main thread connection (also stored in _local for consistency)
        self._main_thread_id = threading.get_ident()
        self.conn = self._create_connection()
        self._local.conn = self.conn

        self._init_schema()

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with proper settings.

        Returns:
            Configured SQLite connection
        """
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL mode for concurrent reads + single writer
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL sync is safe with WAL (only FULL needed for rollback journal)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Wait up to 5 seconds if database is locked (inter-process safety)
        conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Get the connection for the current thread.

        Creates a new connection if this thread doesn't have one yet.
        This enables safe concurrent access from ThreadPoolExecutor.

        Returns:
            SQLite connection for current thread
        """
        # Check if this thread already has a connection
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            return conn

        # Create new connection for this thread
        conn = self._create_connection()
        self._local.conn = conn
        return conn

    def _detect_schema_version(self) -> int:
        """Detect schema version from existing tables when schema_info is missing.

        This handles databases created before schema_info was added.

        Returns:
            Detected schema version (0 if empty/unknown)
        """
        import logging
        logger = logging.getLogger(__name__)

        # Check if media table exists
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='media'"
        )
        if not cursor.fetchone():
            return 0  # Empty database

        # Check for v8 features (server table)
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='server'"
        )
        if cursor.fetchone():
            # v8 - has server table
            # Create schema_info if missing
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER PRIMARY KEY
                )
            """)
            self.conn.execute("INSERT OR REPLACE INTO schema_info (version) VALUES (8)")
            self.conn.commit()
            logger.info("Detected schema version 8 (has server table)")
            return 8

        # Check for v7 features (peer_downloads table)
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='peer_downloads'"
        )
        if cursor.fetchone():
            # v7 - has peer_downloads but no server
            # Create schema_info table and set version
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER PRIMARY KEY
                )
            """)
            self.conn.execute("INSERT OR REPLACE INTO schema_info (version) VALUES (7)")
            self.conn.commit()
            logger.info("Detected schema version 7 (has peer_downloads, no server)")
            return 7

        # Check for v6 features (config table with kernel_keep)
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config'"
        )
        if cursor.fetchone():
            # v6 or earlier with config
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER PRIMARY KEY
                )
            """)
            self.conn.execute("INSERT OR REPLACE INTO schema_info (version) VALUES (6)")
            self.conn.commit()
            logger.info("Detected schema version 6 (has config table)")
            return 6

        # Has media table but nothing else - assume old version
        logger.warning("Unknown schema version, assuming version 6")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_info (
                version INTEGER PRIMARY KEY
            )
        """)
        self.conn.execute("INSERT OR REPLACE INTO schema_info (version) VALUES (6)")
        self.conn.commit()
        return 6

    def _init_schema(self):
        """Initialize or migrate database schema."""
        # Check existing schema version
        try:
            cursor = self.conn.execute("SELECT version FROM schema_info LIMIT 1")
            row = cursor.fetchone()
            current_version = row[0] if row else 0
        except sqlite3.OperationalError:
            # No schema_info table - detect version from existing schema
            current_version = self._detect_schema_version()

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
                self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
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

                # Run post-migration data fixups
                if version == 7 and to_version == 8:
                    self._migrate_v7_to_v8_data(logger)
                elif version == 8 and to_version == 9:
                    # For databases that migrated v7->v8 before directory migration was added
                    self._migrate_media_directories(logger)
                elif version == 9 and to_version == 10:
                    self._migrate_v9_to_v10_test_servers(logger)
                version = to_version
            except sqlite3.Error as e:
                logger.error(f"Migration v{version} -> v{to_version} failed: {e}")
                raise RuntimeError(f"Database migration failed: {e}")

        logger.info(f"Database schema is now at version {SCHEMA_VERSION}")

    def _migrate_v7_to_v8_data(self, logger):
        """Migrate v7 media data to v8 format.

        - Parse existing media URLs
        - Fill in new columns (short_name, mageia_version, architecture, relative_path)
        - Create server entries
        - Link servers to media
        """
        from urllib.parse import urlparse

        # Known patterns for parsing
        KNOWN_VERSIONS = {'7', '8', '9', '10', 'cauldron'}
        KNOWN_ARCHES = {'x86_64', 'aarch64', 'armv7hl', 'i586', 'i686'}
        KNOWN_CLASSES = {'core', 'nonfree', 'tainted', 'debug'}
        KNOWN_TYPES = {'release', 'updates', 'backports', 'backports_testing',
                       'updates_testing', 'testing'}

        def parse_mageia_url(url):
            """Parse a Mageia media URL into components."""
            if not url:
                return None

            parsed = urlparse(url.rstrip('/'))
            if parsed.scheme == 'file':
                protocol = 'file'
                host = ''
                path = parsed.path
            elif parsed.scheme in ('http', 'https'):
                protocol = parsed.scheme
                host = parsed.netloc
                path = parsed.path
            else:
                return None

            parts = [p for p in path.split('/') if p]

            # Look for 'media' in path
            try:
                media_idx = parts.index('media')
            except ValueError:
                return None

            if media_idx < 2 or len(parts) < media_idx + 3:
                return None

            # Check for debug media: .../media/debug/{class}/{type}
            is_debug = False
            if parts[media_idx + 1] == 'debug':
                is_debug = True
                if len(parts) < media_idx + 4:
                    return None
                class_name = parts[media_idx + 2]
                type_name = parts[media_idx + 3]
            else:
                class_name = parts[media_idx + 1]
                type_name = parts[media_idx + 2]

            if class_name not in KNOWN_CLASSES:
                return None
            if type_name not in KNOWN_TYPES:
                return None

            arch = parts[media_idx - 1]
            version = parts[media_idx - 2]

            if arch not in KNOWN_ARCHES:
                return None
            if version not in KNOWN_VERSIONS:
                return None

            version_idx = media_idx - 2
            base_path_parts = parts[:version_idx]
            if base_path_parts:
                base_path = '/' + '/'.join(base_path_parts)
            else:
                base_path = ''

            relative_path = '/'.join(parts[version_idx:])
            if is_debug:
                short_name = f"debug_{class_name}_{type_name}"
            else:
                short_name = f"{class_name}_{type_name}"

            return {
                'protocol': protocol,
                'host': host,
                'base_path': base_path,
                'relative_path': relative_path,
                'version': version,
                'arch': arch,
                'class_name': class_name,
                'type_name': type_name,
                'short_name': short_name,
            }

        # Get all media with URLs
        cursor = self.conn.execute(
            "SELECT id, name, url FROM media WHERE url IS NOT NULL AND url != ''"
        )
        media_rows = cursor.fetchall()

        logger.info(f"Migrating {len(media_rows)} media entries to v8 format")

        # Track servers we've created (by protocol+host+base_path)
        servers = {}  # (protocol, host, base_path) -> server_id

        migrated = 0
        failed = 0

        for row in media_rows:
            media_id = row['id']
            media_name = row['name']
            url = row['url']

            parsed = parse_mageia_url(url)
            if not parsed:
                # Can't parse - keep as legacy
                logger.warning(f"Could not parse URL for media '{media_name}': {url}")
                # Set placeholder values so the schema is valid
                self.conn.execute("""
                    UPDATE media
                    SET short_name = ?, mageia_version = ?, architecture = ?,
                        relative_path = ?, is_official = 1
                    WHERE id = ?
                """, (media_name.lower().replace(' ', '_'), 'unknown', 'unknown',
                      '', media_id))
                failed += 1
                continue

            # Update media with parsed values
            self.conn.execute("""
                UPDATE media
                SET short_name = ?, mageia_version = ?, architecture = ?,
                    relative_path = ?, is_official = 1
                WHERE id = ?
            """, (parsed['short_name'], parsed['version'], parsed['arch'],
                  parsed['relative_path'], media_id))

            # Create or reuse server
            server_key = (parsed['protocol'], parsed['host'], parsed['base_path'])
            if server_key not in servers:
                # Generate server name
                if parsed['protocol'] == 'file':
                    server_name = 'local-mirror'
                else:
                    host = parsed['host']
                    if '.' in host:
                        first_part = host.split('.')[0]
                        if first_part in ('mirrors', 'mirror', 'ftp', 'www'):
                            parts = host.split('.')
                            if len(parts) > 1:
                                first_part = parts[1]
                        server_name = first_part
                    else:
                        server_name = host

                # Make unique if needed
                base_name = server_name
                counter = 1
                while True:
                    existing = self.conn.execute(
                        "SELECT id FROM server WHERE name = ?", (server_name,)
                    ).fetchone()
                    if not existing:
                        break
                    counter += 1
                    server_name = f"{base_name}-{counter}"

                # Insert server
                cursor = self.conn.execute("""
                    INSERT INTO server (name, protocol, host, base_path,
                                       is_official, enabled, priority, added_timestamp)
                    VALUES (?, ?, ?, ?, 1, 1, 50, ?)
                """, (server_name, parsed['protocol'], parsed['host'],
                      parsed['base_path'], int(time.time())))
                server_id = cursor.lastrowid
                servers[server_key] = server_id
                logger.info(f"Created server '{server_name}' (id={server_id})")
            else:
                server_id = servers[server_key]

            # Create server_media link
            self.conn.execute("""
                INSERT OR IGNORE INTO server_media (server_id, media_id, added_timestamp)
                VALUES (?, ?, ?)
            """, (server_id, media_id, int(time.time())))

            migrated += 1

        self.conn.commit()
        logger.info(f"Migration complete: {migrated} migrated, {failed} could not be parsed")

        # Migrate directory structure: <hostname>/<media_name>/ -> official/<relative_path>/
        self._migrate_media_directories(logger)

    def _migrate_media_directories(self, logger):
        """Migrate media directories from old to new structure.

        Old: <base_dir>/medias/<hostname>/<media_name>/
        New: <base_dir>/medias/official/<relative_path>/
             <base_dir>/medias/custom/<short_name>/
        """
        import shutil
        from .config import get_base_dir

        base_dir = get_base_dir()
        medias_dir = base_dir / "medias"

        if not medias_dir.exists():
            logger.info("No medias directory to migrate")
            return

        # Get all media with their old URL (for hostname extraction) and new paths
        cursor = self.conn.execute("""
            SELECT m.id, m.name, m.url, m.relative_path, m.is_official, m.short_name
            FROM media m
            WHERE m.relative_path IS NOT NULL AND m.relative_path != ''
        """)

        moved = 0
        for row in cursor:
            media_name = row['name']
            url = row['url']
            relative_path = row['relative_path']
            is_official = row['is_official']
            short_name = row['short_name']

            if not url:
                continue

            # Extract hostname from URL for old path
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme == 'file':
                hostname = 'local'
            else:
                hostname = parsed.netloc or 'local'

            # Old path: <medias>/<hostname>/<media_name>/
            old_path = medias_dir / hostname / media_name

            # New path based on official or custom
            if is_official:
                new_path = medias_dir / "official" / relative_path
            else:
                new_path = medias_dir / "custom" / short_name

            if not old_path.exists():
                continue

            if new_path.exists():
                logger.warning(f"Target path already exists, skipping: {new_path}")
                continue

            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_path), str(new_path))
                logger.info(f"Moved {old_path} -> {new_path}")
                moved += 1
            except Exception as e:
                logger.warning(f"Failed to move {old_path}: {e}")

        # Clean up empty hostname directories
        if medias_dir.exists():
            for item in medias_dir.iterdir():
                if item.is_dir() and item.name not in ('official', 'custom'):
                    try:
                        # Only remove if empty
                        if not any(item.iterdir()):
                            item.rmdir()
                            logger.info(f"Removed empty directory: {item}")
                    except Exception:
                        pass

        if moved:
            logger.info(f"Migrated {moved} media directories to new structure")

    def _migrate_v9_to_v10_test_servers(self, logger):
        """Test all servers for IPv4/IPv6 connectivity and update ip_mode."""
        from .config import test_server_ip_connectivity

        cursor = self.conn.execute(
            "SELECT id, name, protocol, host FROM server WHERE protocol IN ('http', 'https')"
        )
        servers = cursor.fetchall()

        if not servers:
            logger.info("No remote servers to test for IP connectivity")
            return

        logger.info(f"Testing IP connectivity for {len(servers)} server(s)...")

        for srv in servers:
            host = srv['host']
            port = 443 if srv['protocol'] == 'https' else 80

            try:
                ip_mode = test_server_ip_connectivity(host, port, timeout=5.0)
                self.conn.execute(
                    "UPDATE server SET ip_mode = ? WHERE id = ?",
                    (ip_mode, srv['id'])
                )
                logger.info(f"  {srv['name']} ({host}): {ip_mode}")
            except Exception as e:
                logger.warning(f"  {srv['name']} ({host}): test failed ({e}), keeping 'auto'")

        self.conn.commit()

    def _migrate_v15_to_v16_filesize(self, logger):
        """Add filesize to packages, populate the DB with them"""
        main.cmd_media_update(None,self)

    def close(self):
        """Close database connection.

        Note: Thread-local connections are automatically cleaned up when
        threads end (e.g., when ThreadPoolExecutor workers terminate).
        """
        # Close main thread connection
        if self.conn:
            self.conn.close()
            self.conn = None
        # Clear thread-local reference if in main thread
        if hasattr(self._local, 'conn'):
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # =========================================================================
    # Media management
    # =========================================================================

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
        # Generate placeholder values for required fields
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

    def update_media_sync_info(self, media_id: int, synthesis_md5: str):
        """Update media sync timestamp and MD5. Thread-safe."""
        conn = self._get_connection()
        with self._lock:
            conn.execute("""
                UPDATE media SET last_sync = ?, synthesis_md5 = ?
                WHERE id = ?
            """, (int(time.time()), synthesis_md5, media_id))
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

    # =========================================================================
    # Server management
    # =========================================================================

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

    # =========================================================================
    # Package import
    # =========================================================================

    def import_packages(self, packages: Iterator[Dict], media_id: int = None,
                        source: str = 'synthesis', progress_callback=None,
                        batch_size: int = 1000):
        """Import packages from a parsed synthesis or hdlist.

        Uses bulk inserts for performance. Thread-safe via lock and
        thread-local connections.

        Args:
            packages: Iterator of package dictionaries
            media_id: Associated media ID (optional)
            source: Source type ('synthesis' or 'hdlist')
            progress_callback: Optional callback(count, pkg_name)
            batch_size: Number of packages per batch
        """
        conn = self._get_connection()
        with self._lock:
            return self._import_packages_unlocked(
                conn, packages, media_id, source, progress_callback, batch_size
            )

    def _import_packages_unlocked(self, conn: sqlite3.Connection,
                                  packages: Iterator[Dict], media_id: int = None,
                                  source: str = 'synthesis', progress_callback=None,
                                  batch_size: int = 1000):
        """Internal import implementation (must hold lock).

        Args:
            conn: SQLite connection to use (thread-local)
        """
        import os
        import logging
        pkg_logger = logging.getLogger(__name__)

        timestamp = int(time.time())

        # Collect all packages first
        all_packages = list(packages)
        total = len(all_packages)
        imported_nevras = {pkg['nevra'] for pkg in all_packages}

        if progress_callback:
            progress_callback(0, "preparing...")

        # Begin transaction
        conn.execute("BEGIN TRANSACTION")

        try:
            # Step 1: Delete all dependencies for this media (they can change)
            if media_id:
                if progress_callback:
                    progress_callback(0, "clearing dependencies...")
                for table in ('requires', 'provides', 'conflicts', 'obsoletes',
                              'recommends', 'suggests', 'supplements', 'enhances'):
                    conn.execute(f"""
                        DELETE FROM {table} WHERE pkg_id IN
                        (SELECT id FROM packages WHERE media_id = ?)
                    """, (media_id,))

            # Step 2: Find obsolete packages (in DB but not in new synthesis)
            obsolete_packages = []
            if media_id:
                cursor = conn.execute(
                    "SELECT id, nevra, name, version, release, arch FROM packages WHERE media_id = ?",
                    (media_id,)
                )
                for row in cursor:
                    if row['nevra'] not in imported_nevras:
                        obsolete_packages.append(dict(row))

            # Step 3: Delete local cached files for obsolete packages
            if obsolete_packages:
                if progress_callback:
                    progress_callback(0, f"removing {len(obsolete_packages)} obsolete packages...")

                from .config import get_base_dir
                base_dir = get_base_dir()

                for pkg in obsolete_packages:
                    filename = f"{pkg['name']}-{pkg['version']}-{pkg['release']}.{pkg['arch']}.rpm"
                    # Remove from cache_files and get path
                    cache_entry = self.get_cache_file(filename, media_id)
                    if cache_entry:
                        file_path = base_dir / "medias" / cache_entry['file_path']
                        try:
                            if file_path.exists():
                                os.unlink(file_path)
                                pkg_logger.debug(f"Deleted obsolete cached file: {filename}")
                        except OSError as e:
                            pkg_logger.warning(f"Could not delete {file_path}: {e}")
                        self.delete_cache_file(filename, media_id)

                # Delete obsolete packages from DB
                obsolete_ids = [p['id'] for p in obsolete_packages]
                placeholders = ','.join('?' * len(obsolete_ids))
                conn.execute(
                    f"DELETE FROM packages WHERE id IN ({placeholders})",
                    obsolete_ids
                )

            # Step 4: UPSERT packages (preserves added_timestamp and server_last_modified)
            if progress_callback:
                progress_callback(0, "upserting packages...")

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
                    pkg['filesize'],
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

            conn.executemany("""
                INSERT INTO packages
                (media_id, name, epoch, version, release, arch, name_lower, nevra,
                 filesize, summary, description, size, group_name, url, license,
                 source, pkg_hash, added_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nevra, media_id) DO UPDATE SET
                    name = excluded.name,
                    epoch = excluded.epoch,
                    version = excluded.version,
                    release = excluded.release,
                    arch = excluded.arch,
                    name_lower = excluded.name_lower,
                    filesize = excluded.filesize,
                    summary = excluded.summary,
                    description = excluded.description,
                    size = excluded.size,
                    group_name = excluded.group_name,
                    url = excluded.url,
                    license = excluded.license,
                    source = excluded.source,
                    pkg_hash = excluded.pkg_hash
                    -- added_timestamp and server_last_modified NOT updated
            """, pkg_rows)

            if progress_callback:
                progress_callback(total, "indexing deps...")

            # Build nevra -> pkg_id mapping
            cursor = conn.execute(
                "SELECT id, nevra FROM packages WHERE media_id = ?",
                (media_id,)
            )
            nevra_to_id = {row[1]: row[0] for row in cursor}

            # Step 5: Collect and insert all dependencies
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
                conn.executemany(
                    "INSERT INTO requires (pkg_id, capability) VALUES (?, ?)",
                    requires_rows
                )
            if provides_rows:
                conn.executemany(
                    "INSERT INTO provides (pkg_id, capability) VALUES (?, ?)",
                    provides_rows
                )
            if conflicts_rows:
                conn.executemany(
                    "INSERT INTO conflicts (pkg_id, capability) VALUES (?, ?)",
                    conflicts_rows
                )
            if obsoletes_rows:
                conn.executemany(
                    "INSERT INTO obsoletes (pkg_id, capability) VALUES (?, ?)",
                    obsoletes_rows
                )
            # Weak dependencies
            if recommends_rows:
                conn.executemany(
                    "INSERT INTO recommends (pkg_id, capability) VALUES (?, ?)",
                    recommends_rows
                )
            if suggests_rows:
                conn.executemany(
                    "INSERT INTO suggests (pkg_id, capability) VALUES (?, ?)",
                    suggests_rows
                )
            if supplements_rows:
                conn.executemany(
                    "INSERT INTO supplements (pkg_id, capability) VALUES (?, ?)",
                    supplements_rows
                )
            if enhances_rows:
                conn.executemany(
                    "INSERT INTO enhances (pkg_id, capability) VALUES (?, ?)",
                    enhances_rows
                )

            conn.commit()

            if progress_callback:
                progress_callback(total, "done")

            return total

        except Exception as e:
            conn.rollback()
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

    def search(self, pattern: str, limit: int = None, search_provides: bool = False) -> List[Dict]:
        """Search packages by name pattern, optionally also in provides.

        Filters by system version to avoid returning packages from other
        Mageia versions (e.g., mga9 packages on a mga10 system).

        Args:
            pattern: Search pattern (case-insensitive substring match)
            limit: Maximum results to return (None = no limit)
            search_provides: If True, also search in provides capabilities

        Returns:
            List of package dicts. If found via provides, includes 'matched_provide' key.
        """
        from .config import get_system_version
        system_version = get_system_version()

        pattern_lower = f'%{pattern.lower()}%'
        results = []
        seen_ids = set()

        # Build version filter
        if system_version:
            version_join = "JOIN media m ON p.media_id = m.id"
            version_filter = "AND m.mageia_version = ?"
            base_params = (pattern_lower, system_version)
        else:
            version_join = ""
            version_filter = ""
            base_params = (pattern_lower,)

        # Search by name
        if limit:
            cursor = self.conn.execute(f"""
                SELECT p.id, p.name, p.version, p.release, p.arch, p.nevra, p.summary, p.size
                FROM packages p
                {version_join}
                WHERE p.name_lower LIKE ? {version_filter}
                ORDER BY p.name_lower
                LIMIT ?
            """, base_params + (limit,))
        else:
            cursor = self.conn.execute(f"""
                SELECT p.id, p.name, p.version, p.release, p.arch, p.nevra, p.summary, p.size
                FROM packages p
                {version_join}
                WHERE p.name_lower LIKE ? {version_filter}
                ORDER BY p.name_lower
            """, base_params)

        for row in cursor:
            pkg = dict(row)
            results.append(pkg)
            seen_ids.add(pkg['id'])

        # Search in provides if requested
        if search_provides and (limit is None or len(results) < limit):
            if limit:
                remaining = limit - len(results)
                cursor = self.conn.execute(f"""
                    SELECT DISTINCT p.id, p.name, p.version, p.release, p.arch,
                           p.nevra, p.summary, p.size, pr.capability as matched_provide
                    FROM packages p
                    JOIN provides pr ON pr.pkg_id = p.id
                    {version_join.replace('p.media_id', 'p.media_id') if version_join else ''}
                    WHERE LOWER(pr.capability) LIKE ? {version_filter}
                    ORDER BY p.name_lower
                    LIMIT ?
                """, base_params + (remaining + len(seen_ids),))
            else:
                cursor = self.conn.execute(f"""
                    SELECT DISTINCT p.id, p.name, p.version, p.release, p.arch,
                           p.nevra, p.summary, p.size, pr.capability as matched_provide
                    FROM packages p
                    JOIN provides pr ON pr.pkg_id = p.id
                    {version_join.replace('p.media_id', 'p.media_id') if version_join else ''}
                    WHERE LOWER(pr.capability) LIKE ? {version_filter}
                    ORDER BY p.name_lower
                """, base_params)

            for row in cursor:
                pkg = dict(row)
                if pkg['id'] not in seen_ids:
                    seen_ids.add(pkg['id'])
                    results.append(pkg)
                    if limit and len(results) >= limit:
                        break

        return results

    def get_package(self, name: str) -> Optional[Dict]:
        """Get a package by exact name (latest version).

        Filters by system version to avoid returning packages from other
        Mageia versions (e.g., mga9 packages on a mga10 system).
        """
        from .config import get_system_version
        system_version = get_system_version()

        if system_version:
            # Filter by system version via media join
            cursor = self.conn.execute("""
                SELECT p.* FROM packages p
                JOIN media m ON p.media_id = m.id
                WHERE p.name_lower = ? AND m.mageia_version = ?
                ORDER BY p.epoch DESC, p.version DESC, p.release DESC
                LIMIT 1
            """, (name.lower(), system_version))
        else:
            # Fallback if system version unknown
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
        pkg['recommends'] = self._get_deps(pkg['id'], 'recommends')
        pkg['suggests'] = self._get_deps(pkg['id'], 'suggests')

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
        pkg['recommends'] = self._get_deps(pkg['id'], 'recommends')
        pkg['suggests'] = self._get_deps(pkg['id'], 'suggests')

        return pkg

    def find_package_by_nevra(self, name: str, evr: str, arch: str) -> Optional[Dict]:
        """Find a package by name, evr (epoch:version-release), and arch.

        Args:
            name: Package name
            evr: Epoch:version-release (e.g., "3:4.4.3P1-4" or "4.4.3P1-4")
            arch: Architecture (e.g., "x86_64")

        Returns:
            Package dict if found, None otherwise.
        """
        # Parse evr to extract epoch, version, release
        if ':' in evr:
            epoch_str, ver_rel = evr.split(':', 1)
            epoch = int(epoch_str) if epoch_str else 0
        else:
            epoch = 0
            ver_rel = evr

        if '-' in ver_rel:
            version, release = ver_rel.rsplit('-', 1)
        else:
            version = ver_rel
            release = '1'

        # Search with all components
        cursor = self.conn.execute("""
            SELECT * FROM packages
            WHERE name_lower = ? AND version = ? AND release = ? AND arch = ?
            AND (epoch = ? OR (epoch IS NULL AND ? = 0))
            LIMIT 1
        """, (name.lower(), version, release, arch, epoch, epoch))

        row = cursor.fetchone()
        if not row:
            # Try without epoch constraint (some packages may not have epoch stored)
            cursor = self.conn.execute("""
                SELECT * FROM packages
                WHERE name_lower = ? AND version = ? AND release = ? AND arch = ?
                LIMIT 1
            """, (name.lower(), version, release, arch))
            row = cursor.fetchone()

        if not row:
            return None

        pkg = dict(row)
        pkg['requires'] = self._get_deps(pkg['id'], 'requires')
        pkg['provides'] = self._get_deps(pkg['id'], 'provides')
        pkg['conflicts'] = self._get_deps(pkg['id'], 'conflicts')
        pkg['obsoletes'] = self._get_deps(pkg['id'], 'obsoletes')
        pkg['recommends'] = self._get_deps(pkg['id'], 'recommends')
        pkg['suggests'] = self._get_deps(pkg['id'], 'suggests')

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

    def whatrecommends(self, capability: str, limit: int = 50) -> List[Dict]:
        """Find packages that recommend a capability."""
        cursor = self.conn.execute("""
            SELECT p.id, p.name, p.version, p.release, p.arch, p.nevra
            FROM packages p
            JOIN recommends r ON r.pkg_id = p.id
            WHERE r.capability = ?
            ORDER BY p.name_lower
            LIMIT ?
        """, (capability, limit))

        return [dict(row) for row in cursor]

    def whatsuggests(self, capability: str, limit: int = 50) -> List[Dict]:
        """Find packages that suggest a capability."""
        cursor = self.conn.execute("""
            SELECT p.id, p.name, p.version, p.release, p.arch, p.nevra
            FROM packages p
            JOIN suggests s ON s.pkg_id = p.id
            WHERE s.capability = ?
            ORDER BY p.name_lower
            LIMIT ?
        """, (capability, limit))

        return [dict(row) for row in cursor]

    def get_packages_for_media(self, media_id: int, with_filename: bool = True,
                                order_by: str = 'name') -> List[Dict]:
        """Get all packages in a media.

        Args:
            media_id: Media ID
            with_filename: If True, include the RPM filename
            order_by: 'name' (default), 'server_date' (newest first), 'added' (newest first)

        Returns:
            List of package dicts with name, version, release, arch, size, filename
        """
        order_clause = "p.name_lower"
        if order_by == 'server_date':
            order_clause = "p.server_last_modified DESC NULLS LAST, p.name_lower"
        elif order_by == 'added':
            order_clause = "p.added_timestamp DESC, p.name_lower"

        cursor = self.conn.execute(f"""
            SELECT p.id, p.name, p.version, p.release, p.epoch, p.arch,
                   p.nevra, p.size, p.server_last_modified, m.name as media_name
            FROM packages p
            LEFT JOIN media m ON p.media_id = m.id
            WHERE p.media_id = ?
            ORDER BY {order_clause}
        """, (media_id,))

        results = []
        for row in cursor:
            pkg = dict(row)
            if with_filename:
                # Build RPM filename: name-version-release.arch.rpm
                pkg['filename'] = f"{pkg['name']}-{pkg['version']}-{pkg['release']}.{pkg['arch']}.rpm"
            results.append(pkg)

        return results

    def get_media_packages_filenames(self, media_id: int) -> Set[str]:
        """Get set of all RPM filenames in a media.

        Efficient method for checking which packages exist.

        Args:
            media_id: Media ID

        Returns:
            Set of RPM filenames (e.g., {'foo-1.0-1.mga10.x86_64.rpm', ...})
        """
        cursor = self.conn.execute("""
            SELECT name, version, release, arch
            FROM packages
            WHERE media_id = ?
        """, (media_id,))

        return {
            f"{row['name']}-{row['version']}-{row['release']}.{row['arch']}.rpm"
            for row in cursor
        }

    def get_packages_needing_server_dates(self, media_id: int, limit: int = None) -> List[Dict]:
        """Get packages that don't have server_last_modified set.

        Used by the background job to fetch HEAD for these packages.

        Args:
            media_id: Media ID
            limit: Max number to return (None = all)

        Returns:
            List of package dicts with id, nevra, name, version, release, arch
        """
        query = """
            SELECT id, nevra, name, version, release, arch
            FROM packages
            WHERE media_id = ? AND server_last_modified IS NULL
            ORDER BY name_lower
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = self.conn.execute(query, (media_id,))
        results = []
        for row in cursor:
            pkg = dict(row)
            pkg['filename'] = f"{pkg['name']}-{pkg['version']}-{pkg['release']}.{pkg['arch']}.rpm"
            results.append(pkg)
        return results

    def update_server_last_modified(self, package_id: int, timestamp: int):
        """Update server_last_modified for a package.

        Args:
            package_id: Package ID
            timestamp: Unix timestamp from server's Last-Modified header
        """
        self.conn.execute(
            "UPDATE packages SET server_last_modified = ? WHERE id = ?",
            (timestamp, package_id)
        )
        self.conn.commit()

    def update_server_last_modified_batch(self, updates: List[tuple]):
        """Batch update server_last_modified for multiple packages.

        Args:
            updates: List of (package_id, timestamp) tuples
        """
        self.conn.executemany(
            "UPDATE packages SET server_last_modified = ? WHERE id = ?",
            [(ts, pkg_id) for pkg_id, ts in updates]
        )
        self.conn.commit()

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

    # =========================================================================
    # Multi-media package resolution
    # =========================================================================

    def get_all_versions(self, name: str) -> List[Dict]:
        """Get all versions of a package from all media.

        Filters by system version to avoid returning packages from other
        Mageia versions (e.g., mga9 packages on a mga10 system).

        Returns packages sorted by effective priority (pins + media priority),
        then by version (newest first).
        """
        from .config import get_system_version
        system_version = get_system_version()

        if system_version:
            cursor = self.conn.execute("""
                SELECT p.*, m.name as media_name, m.priority as media_priority
                FROM packages p
                JOIN media m ON p.media_id = m.id
                WHERE p.name_lower = ? AND m.mageia_version = ?
                ORDER BY p.epoch DESC, p.version DESC, p.release DESC
            """, (name.lower(), system_version))
        else:
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
        best['recommends'] = self._get_deps(best['id'], 'recommends')
        best['suggests'] = self._get_deps(best['id'], 'suggests')

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

    # =========================================================================
    # Cache file tracking
    # =========================================================================

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

    # =========================================================================
    # Media mirror/replication settings
    # =========================================================================

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

    # =========================================================================
    # Dependency collection (for replication, not installation)
    # =========================================================================

    def collect_dependencies(self, package_names: set, media_ids: List[int] = None,
                              include_recommends: bool = True,
                              include_file_deps: bool = True) -> Dict:
        """Collect all dependencies recursively for a set of packages.

        Unlike resolve_install(), this does NOT check conflicts - it just collects
        all packages that would be needed. This is for replication purposes where
        conflicting packages can coexist as RPM files.

        Args:
            package_names: Set of package names to start from
            media_ids: Optional list of media IDs to limit search (None = all media)
            include_recommends: If True, also follow Recommends (weak deps) - default True
            include_file_deps: If True, also follow file dependencies (/usr/bin/...) - default True

        Returns:
            Dict with:
                - 'packages': Set of all package names (seeds + dependencies)
                - 'not_found': Set of package names that couldn't be resolved
                - 'total_size': Total size in bytes
        """
        # Load all data into memory for fast lookup
        media_filter = ""
        params = []
        if media_ids:
            placeholders = ",".join("?" * len(media_ids))
            media_filter = f" WHERE media_id IN ({placeholders})"
            params = list(media_ids)

        # Load packages: name_lower -> (id, name, size)
        cursor = self.conn.execute(f"""
            SELECT id, name, name_lower, COALESCE(size, 0) FROM packages {media_filter}
        """, params)
        pkg_by_name = {}  # name_lower -> (id, name, size)
        pkg_by_id = {}    # id -> (name, size)
        for pkg_id, name, name_lower, size in cursor:
            # Keep highest version (first seen due to ORDER BY in original)
            if name_lower not in pkg_by_name:
                pkg_by_name[name_lower] = (pkg_id, name, size)
            pkg_by_id[pkg_id] = (name, size)

        # Load requires: pkg_id -> [capabilities]
        cursor = self.conn.execute("SELECT pkg_id, capability FROM requires")
        requires_by_pkg = {}
        for pkg_id, cap in cursor:
            if pkg_id not in requires_by_pkg:
                requires_by_pkg[pkg_id] = []
            requires_by_pkg[pkg_id].append(cap)

        # Load recommends if needed: pkg_id -> [capabilities]
        recommends_by_pkg = {}
        if include_recommends:
            cursor = self.conn.execute("SELECT pkg_id, capability FROM recommends")
            for pkg_id, cap in cursor:
                if pkg_id not in recommends_by_pkg:
                    recommends_by_pkg[pkg_id] = []
                recommends_by_pkg[pkg_id].append(cap)

        # Load provides: capability_base -> [pkg_ids]
        # We strip version info for matching
        cursor = self.conn.execute("SELECT pkg_id, capability FROM provides")
        provides_by_cap = {}
        for pkg_id, cap in cursor:
            cap_base = cap.split()[0]  # Remove version constraints
            if cap_base not in provides_by_cap:
                provides_by_cap[cap_base] = set()
            provides_by_cap[cap_base].add(pkg_id)

        # Now do the recursive collection in memory
        result_ids = set()
        not_found = set()
        to_process = set(package_names)
        processed_caps = set()

        while to_process:
            pkg_name = to_process.pop()

            if pkg_name.lower() in {pkg_by_id[pid][0].lower() for pid in result_ids}:
                continue

            pkg_info = pkg_by_name.get(pkg_name.lower())
            if not pkg_info:
                not_found.add(pkg_name)
                continue

            pkg_id, name, size = pkg_info
            result_ids.add(pkg_id)

            # Get all dependencies for this package (requires + recommends)
            all_deps = requires_by_pkg.get(pkg_id, [])
            if include_recommends:
                all_deps = all_deps + recommends_by_pkg.get(pkg_id, [])

            for cap in all_deps:
                if cap in processed_caps:
                    continue
                processed_caps.add(cap)

                # Skip rpmlib and config dependencies (internal RPM stuff)
                if cap.startswith(('rpmlib(', 'config(')):
                    continue

                # Skip file dependencies if not requested
                if cap.startswith('/') and not include_file_deps:
                    continue

                cap_base = cap.split()[0]
                provider_ids = provides_by_cap.get(cap_base, set())

                # Filter to packages in our media if needed
                if media_ids:
                    provider_ids = {pid for pid in provider_ids if pid in pkg_by_id}

                for prov_id in provider_ids:
                    if prov_id not in result_ids:
                        prov_name = pkg_by_id[prov_id][0]
                        to_process.add(prov_name)

        # Calculate total size
        total_size = sum(pkg_by_id[pid][1] for pid in result_ids if pid in pkg_by_id)
        result_names = {pkg_by_id[pid][0] for pid in result_ids if pid in pkg_by_id}

        return {
            'packages': result_names,
            'not_found': not_found,
            'total_size': total_size,
        }
