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

from .db import (
    MediaMixin, ServerMixin, ConstraintsMixin,
    HistoryMixin, PeerMixin, CacheMixin, FilesMixin
)

# Schema version - increment when schema changes
SCHEMA_VERSION = 20

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

    -- Files.xml sync (v18+)
    sync_files INTEGER DEFAULT 0,         -- 1 = auto-sync files.xml for urpm find

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

-- Package file lists (from files.xml.lzma)
-- Split into dir_path + filename for efficient indexing:
--   - Search by filename (pg_hba.conf) -> idx_pf_filename
--   - Search by full path -> idx_pf_dir_filename composite
--   - Prefix patterns (mod_*) -> idx_pf_filename with LIKE 'mod_%'
CREATE TABLE IF NOT EXISTS package_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL,
    pkg_nevra TEXT NOT NULL,
    dir_path TEXT NOT NULL,     -- '/usr/lib64/httpd/modules' (without trailing /)
    filename TEXT NOT NULL,     -- 'mod_ssl.so'
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE,
    UNIQUE(media_id, pkg_nevra, dir_path, filename)
);

-- Index on filename for searches by name only (most common)
CREATE INDEX IF NOT EXISTS idx_pf_filename ON package_files(filename);
-- Composite index for full path searches
CREATE INDEX IF NOT EXISTS idx_pf_dir_filename ON package_files(dir_path, filename);
-- Index on media for bulk delete operations
CREATE INDEX IF NOT EXISTS idx_pf_media ON package_files(media_id);

-- Track files.xml sync state per media
CREATE TABLE IF NOT EXISTS files_xml_state (
    media_id INTEGER PRIMARY KEY,
    files_md5 TEXT,              -- MD5 of files.xml.lzma for change detection
    last_sync INTEGER,           -- Timestamp of last import
    file_count INTEGER,          -- Number of files imported
    pkg_count INTEGER,           -- Number of packages imported
    compressed_size INTEGER,     -- Size of files.xml.lzma in bytes (for progress estimation)
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE
);
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
        -- Migration v14 -> v15: Reserved (no-op)
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
    16: (17, """
        -- Migration v16 -> v17: Add package_files table for urpmf functionality
        -- Split into dir_path + filename for efficient indexing
        CREATE TABLE IF NOT EXISTS package_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id INTEGER NOT NULL,
            pkg_nevra TEXT NOT NULL,
            dir_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE,
            UNIQUE(media_id, pkg_nevra, dir_path, filename)
        );
        CREATE INDEX IF NOT EXISTS idx_pf_filename ON package_files(filename);
        CREATE INDEX IF NOT EXISTS idx_pf_dir_filename ON package_files(dir_path, filename);
        CREATE INDEX IF NOT EXISTS idx_pf_media ON package_files(media_id);

        CREATE TABLE files_xml_state (
            media_id INTEGER PRIMARY KEY,
            files_md5 TEXT,
            last_sync INTEGER,
            file_count INTEGER,
            pkg_count INTEGER,
            compressed_size INTEGER,
            FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE
        );
    """),
    17: (18, """
        -- Migration v17 -> v18: Add sync_files option to media
        ALTER TABLE media ADD COLUMN sync_files INTEGER DEFAULT 0;
    """),
    18: (19, """
        -- Migration v18 -> v19: Adding column filesize in packages
        ALTER TABLE packages ADD COLUMN filesize INTEGER DEFAULT 0;
    """),
    19: (20, """
        -- Migration v19 -> v20: FTS5 trigram index for fast file search
        -- Index on pkg_nevra for fast DELETE during incremental sync
        CREATE INDEX IF NOT EXISTS idx_pf_nevra ON package_files(pkg_nevra);

        -- Track FTS index state (FTS table created on first rebuild to avoid corruption)
        CREATE TABLE IF NOT EXISTS fts_state (
            table_name TEXT PRIMARY KEY,
            last_rebuild INTEGER,
            row_count INTEGER,
            is_current INTEGER DEFAULT 0
        );
    """),
}


class PackageDatabase(
    MediaMixin, ServerMixin, ConstraintsMixin,
    HistoryMixin, PeerMixin, CacheMixin, FilesMixin
):
    """SQLite database for package metadata cache.

    Thread-safety: Uses thread-local connections to allow safe concurrent access
    from multiple threads (e.g., ThreadPoolExecutor in sync_all_media).

    Process-safety: Uses WAL mode and busy_timeout for concurrent access from
    multiple processes (CLI + urpmd daemon).

    Functionality is provided by mixins:
    - MediaMixin: Media CRUD operations
    - ServerMixin: Server CRUD and server-media linking
    - ConstraintsMixin: Package pins and holds
    - HistoryMixin: Transaction history
    - PeerMixin: Peer tracking and mirror configuration
    - CacheMixin: Cache file tracking
    - FilesMixin: Package files and FTS index
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
                # Retry logic for database lock (urpmd may be running)
                max_retries = 10
                retry_delay = 0.5
                for attempt in range(max_retries):
                    try:
                        self.conn.executescript(migration_sql)
                        self.conn.execute(
                            "UPDATE schema_info SET version = ?", (to_version,)
                        )
                        self.conn.commit()
                        break
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e) and attempt < max_retries - 1:
                            if attempt == 0:
                                logger.warning("Database locked (urpmd running?), waiting...")
                            import time
                            time.sleep(retry_delay)
                        else:
                            raise

                # Run post-migration data fixups
                if version == 7 and to_version == 8:
                    self._migrate_v7_to_v8_data(logger)
                elif version == 8 and to_version == 9:
                    # For databases that migrated v7->v8 before directory migration was added
                    self._migrate_media_directories(logger)
                elif version == 9 and to_version == 10:
                    self._migrate_v9_to_v10_test_servers(logger)
                elif version == 18 and to_version == 19:
                    print("A new column 'filesize' has been added in database. To populate it, launch the command:\n   'urpm media update'")
                version = to_version
            except sqlite3.Error as e:
                logger.error(f"Migration v{version} -> v{to_version} failed: {e}")
                if "locked" in str(e):
                    raise RuntimeError(
                        f"Database migration failed: database is locked.\n"
                        f"Try: sudo systemctl stop urpmd && urpm --version && sudo systemctl start urpmd"
                    )
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

    def _get_accepted_versions(self) -> Optional[set]:
        """Get the set of accepted media versions for queries.

        Uses get_accepted_versions() which respects the version-mode config.
        Returns None if no version filtering should be applied.
        """
        from .config import get_accepted_versions, get_system_version

        accepted, needs_choice, info = get_accepted_versions(self)

        if accepted:
            return accepted

        if needs_choice:
            # Ambiguous (mix of cauldron + numeric) - accept all
            return None

        # Fallback to system version
        sv = get_system_version()
        return {sv} if sv else None

    def _build_version_filter(self, table_alias: str = "m") -> tuple:
        """Build SQL version filter clause and params.

        Returns:
            (join_clause, where_clause, params) where:
            - join_clause: JOIN media ... (or empty string)
            - where_clause: AND m.mageia_version IN (...) (or empty string)
            - params: tuple of version values
        """
        accepted = self._get_accepted_versions()
        if not accepted:
            return "", "", ()

        join_clause = f"JOIN media {table_alias} ON p.media_id = {table_alias}.id"
        placeholders = ','.join('?' * len(accepted))
        where_clause = f"AND {table_alias}.mageia_version IN ({placeholders})"
        return join_clause, where_clause, tuple(accepted)

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
        pattern_lower = f'%{pattern.lower()}%'
        results = []
        seen_ids = set()

        # Build version filter (respects version-mode config)
        version_join, version_filter, version_params = self._build_version_filter()
        base_params = (pattern_lower,) + version_params

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

        # Add installed status by checking RPM database
        for pkg in results:
            pkg['installed'] = self._is_installed(pkg['name'])

        return results

    def get_package(self, name: str) -> Optional[Dict]:
        """Get a package by exact name (latest version).

        Filters by system version to avoid returning packages from other
        Mageia versions (e.g., mga9 packages on a mga10 system).
        """
        # Build version filter (respects version-mode config)
        version_join, version_filter, version_params = self._build_version_filter()

        if version_join:
            cursor = self.conn.execute(f"""
                SELECT p.* FROM packages p
                {version_join}
                WHERE p.name_lower = ? {version_filter}
                ORDER BY p.epoch DESC, p.version DESC, p.release DESC
                LIMIT 1
            """, (name.lower(),) + version_params)
        else:
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
        pkg['installed'] = self._is_installed(pkg['name'])

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
        pkg['installed'] = self._is_installed(pkg['name'])

        return pkg

    def _is_installed(self, name: str) -> bool:
        """Check if a package is installed in the RPM database."""
        try:
            import subprocess
            # Use rpm -q directly to avoid any Python rpm module caching issues
            result = subprocess.run(
                ['rpm', '-q', name],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

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

    def get_packages_by_names(self, names: List[str]) -> List[Dict]:
        """Batch get packages by names (for resolve operations).

        Returns basic info only (no dependencies) for efficiency.
        Much faster than calling get_package() N times.

        Args:
            names: List of package names

        Returns:
            List of dicts with name, version, release, arch, summary, installed
        """
        if not names:
            return []

        # Build version filter
        version_join, version_filter, version_params = self._build_version_filter()

        # Query all packages at once
        placeholders = ','.join(['?' for _ in names])
        names_lower = [n.lower() for n in names]

        if version_join:
            query = f"""
                SELECT p.name, p.version, p.release, p.arch, p.summary
                FROM packages p
                {version_join}
                WHERE p.name_lower IN ({placeholders}) {version_filter}
            """
            params = tuple(names_lower) + version_params
        else:
            query = f"""
                SELECT name, version, release, arch, summary
                FROM packages
                WHERE name_lower IN ({placeholders})
            """
            params = tuple(names_lower)

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()

        # Build result dict by name (handle duplicates - keep first/latest)
        seen = {}
        for row in rows:
            name = row[0]
            if name not in seen:
                seen[name] = {
                    'name': name,
                    'version': row[1],
                    'release': row[2],
                    'arch': row[3],
                    'summary': row[4] or '',
                }

        # Check installed status in batch using single rpm call
        import subprocess
        installed_set = set()
        try:
            # Query all at once: rpm -q returns 0 for each installed package
            result = subprocess.run(
                ['rpm', '-q', '--qf', '%{NAME}\\n'] + list(seen.keys()),
                capture_output=True,
                timeout=30
            )
            # Parse output - rpm prints package name for installed, error for not
            for line in result.stdout.decode().splitlines():
                line = line.strip()
                if line and not line.startswith('package '):  # skip "package X is not installed"
                    installed_set.add(line)
        except Exception:
            pass

        # Build results preserving input order
        results = []
        for name in names:
            if name in seen:
                pkg = seen[name].copy()
                pkg['installed'] = name in installed_set
                results.append(pkg)

        return results

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
        """Set a configuration value. If value is None, delete the key."""
        if value is None:
            self.conn.execute("DELETE FROM config WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value)
            )
        self.conn.commit()

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
        # Build version filter (respects version-mode config)
        accepted = self._get_accepted_versions()

        if accepted:
            placeholders = ','.join('?' * len(accepted))
            cursor = self.conn.execute(f"""
                SELECT p.*, m.name as media_name, m.priority as media_priority
                FROM packages p
                JOIN media m ON p.media_id = m.id
                WHERE p.name_lower = ? AND m.mageia_version IN ({placeholders})
                ORDER BY p.epoch DESC, p.version DESC, p.release DESC
            """, (name.lower(),) + tuple(accepted))
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

