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

# Extended schema with media, config, history tables
SCHEMA = """
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
    action TEXT NOT NULL,  -- 'install', 'remove', 'update'
    command TEXT,
    user TEXT,
    return_code INTEGER
);

CREATE TABLE IF NOT EXISTS history_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id INTEGER NOT NULL,
    pkg_nevra TEXT NOT NULL,
    action TEXT NOT NULL,  -- 'installed', 'removed', 'upgraded'
    FOREIGN KEY (history_id) REFERENCES history(id) ON DELETE CASCADE
);

-- Configuration
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_pkg_name_lower ON packages(name_lower);
CREATE INDEX IF NOT EXISTS idx_pkg_nevra ON packages(nevra);
CREATE INDEX IF NOT EXISTS idx_pkg_media ON packages(media_id);
CREATE INDEX IF NOT EXISTS idx_provides_cap ON provides(capability);
CREATE INDEX IF NOT EXISTS idx_requires_cap ON requires(capability);
CREATE INDEX IF NOT EXISTS idx_conflicts_cap ON conflicts(capability);
CREATE INDEX IF NOT EXISTS idx_obsoletes_cap ON obsoletes(capability);
"""


class PackageDatabase:
    """SQLite database for package metadata cache."""
    
    DEFAULT_PATH = Path.home() / ".cache" / "urpm" / "packages.db"
    
    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file (default: ~/.cache/urpm/packages.db)
        """
        self.db_path = Path(db_path) if db_path else self.DEFAULT_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        
        self._init_schema()
    
    def _init_schema(self):
        """Initialize database schema."""
        self.conn.executescript(SCHEMA)
        self.conn.commit()
    
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
                        source: str = 'synthesis', progress_callback=None):
        """Import packages from a parsed synthesis or hdlist.
        
        Args:
            packages: Iterator of package dictionaries
            media_id: Associated media ID (optional)
            source: Source type ('synthesis' or 'hdlist')
            progress_callback: Optional callback(count, pkg_name)
        """
        self.conn.execute("BEGIN TRANSACTION")
        
        try:
            count = 0
            for pkg in packages:
                self._insert_package(pkg, media_id, source)
                count += 1
                
                if progress_callback and count % 100 == 0:
                    progress_callback(count, pkg.get('name', ''))
            
            self.conn.commit()
            return count
            
        except Exception as e:
            self.conn.rollback()
            raise e
    
    def _insert_package(self, pkg: Dict, media_id: int, source: str):
        """Insert a single package."""
        # Calculate hash
        hash_data = f"{pkg['nevra']}|{pkg.get('summary', '')}"
        pkg_hash = hashlib.sha256(hash_data.encode()).hexdigest()[:16]
        
        cursor = self.conn.execute("""
            INSERT OR REPLACE INTO packages
            (media_id, name, epoch, version, release, arch, name_lower, nevra,
             summary, description, size, group_name, url, license,
             source, pkg_hash, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
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
            int(time.time())
        ))
        
        pkg_id = cursor.lastrowid
        
        # Insert dependencies
        for cap in pkg.get('requires', []):
            self.conn.execute(
                "INSERT INTO requires (pkg_id, capability) VALUES (?, ?)",
                (pkg_id, cap)
            )
        
        for cap in pkg.get('provides', []):
            self.conn.execute(
                "INSERT INTO provides (pkg_id, capability) VALUES (?, ?)",
                (pkg_id, cap)
            )
        
        for cap in pkg.get('conflicts', []):
            self.conn.execute(
                "INSERT INTO conflicts (pkg_id, capability) VALUES (?, ?)",
                (pkg_id, cap)
            )
        
        for cap in pkg.get('obsoletes', []):
            self.conn.execute(
                "INSERT INTO obsoletes (pkg_id, capability) VALUES (?, ?)",
                (pkg_id, cap)
            )
    
    def clear_media_packages(self, media_id: int):
        """Remove all packages from a media."""
        self.conn.execute("DELETE FROM packages WHERE media_id = ?", (media_id,))
        self.conn.commit()
    
    # =========================================================================
    # Package queries
    # =========================================================================
    
    def search(self, pattern: str, limit: int = 50) -> List[Dict]:
        """Search packages by name pattern."""
        cursor = self.conn.execute("""
            SELECT id, name, version, release, arch, nevra, summary, size
            FROM packages
            WHERE name_lower LIKE ?
            ORDER BY name_lower
            LIMIT ?
        """, (f'%{pattern.lower()}%', limit))
        
        return [dict(row) for row in cursor]
    
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
