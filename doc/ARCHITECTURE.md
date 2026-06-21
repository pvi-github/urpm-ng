# urpm-ng Architecture

This document describes the architectural decisions and technical choices of urpm-ng.

## Overview

urpm-ng is composed of three layers:
- **CLI** (`urpm/cli/`): Command-line interface
- **Core** (`urpm/core/`): Shareable library (resolution, DB, download, P2P)
- **Daemon** (`urpm/daemon/`): urpmd service (scheduler, HTTP server, peer discovery)

---

## 1. Media and Server Management

### Principle: Decouple media from servers

A media (e.g., "Core Release") can be served by multiple servers (mirrors). This allows:
- Parallel downloads across multiple servers
- Load distribution and fault tolerance
- Local mirror support (`file://`)

### Local path structure

```
/var/lib/urpm/medias/
├── official/                          # Official Mageia media
│   └── <version>/<arch>/media/<class>/<short_name>/
│       Ex: official/9/x86_64/media/core/release/
└── custom/                            # Third-party media (isolated)
    └── <short_name>/
        Ex: custom/rpmfusion-free/
```

This separation ensures custom media cannot pollute the official tree.

### Database schema

```sql
-- Media (decoupled from servers)
CREATE TABLE media (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,           -- Display: 'Core Release'
    short_name TEXT NOT NULL,            -- ID: 'core_release'
    mageia_version TEXT NOT NULL,        -- '9', '10', 'cauldron'
    architecture TEXT NOT NULL,          -- 'x86_64', 'aarch64'
    relative_path TEXT NOT NULL,         -- '9/x86_64/media/core/release'
    is_official INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1,
    update_media INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 50
);

-- Servers (mirrors)
CREATE TABLE server (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    protocol TEXT NOT NULL DEFAULT 'https',  -- http, https, file
    host TEXT NOT NULL,
    base_path TEXT NOT NULL DEFAULT '',
    is_official INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 50,
    ip_mode TEXT DEFAULT 'auto',             -- auto, ipv4, ipv6, dual
    UNIQUE(protocol, host, base_path)
);

-- N:M relationship
CREATE TABLE server_media (
    server_id INTEGER REFERENCES server(id),
    media_id INTEGER REFERENCES media(id),
    PRIMARY KEY (server_id, media_id)
);
```

### URL construction

```python
if server.protocol == 'file':
    # Local mirror: direct path
    path = f"{server.base_path}/{media.relative_path}"
else:
    # Remote mirror: full URL
    url = f"{server.protocol}://{server.host}{server.base_path}/{media.relative_path}"
```

### Simplified media addition

User provides a URL, the system parses and splits automatically:

```bash
# Official - automatic parsing
urpm media add https://mirrors.mageia.org/mageia/9/x86_64/media/core/release/

# Custom - explicit name required
urpm media add --custom "RPM Fusion Free" rpmfusion-free https://example.org/repo/
```

---

## 2. P2P Network and Peer Discovery

### Vision

Every Mageia machine with urpmd can serve as a mirror for LAN neighbors.
Priority: local peers > remote mirrors.

### Automatic discovery

- UDP broadcast on port 9878
- Each urpmd announces itself periodically
- Active peer list with timeout (180s prod, 45s dev)
- Jitter to prevent thundering herd on broadcasts

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ping` | GET | Health check |
| `/status` | GET | Daemon status |
| `/api/peers` | GET | Known peers list |
| `/api/announce` | POST | Peer announces itself |
| `/api/have` | POST | Check package availability |
| `/media/...` | GET | RPM/metadata download |

### Peer security

- GPG verification of packages downloaded from peers
- Automatic blacklist if package is unsigned/invalid
- Blacklist persistence in SQLite
- `urpm peer blacklist/unblacklist <host>` command

### Download priority

1. Local cache (already downloaded)
2. LAN peers (load-balanced)
3. Upstream mirrors (sorted by priority)

---

## 3. Multi-version Proxying and Quotas

### Goal

A urpmd on Mageia 10 can serve packages for Mageia 9 or 11.

### Replication policies

| Policy | Description |
|--------|-------------|
| `none` | Metadata only |
| `on_demand` | Keep what's downloaded (default) |
| `full` | Full mirror |
| `seed` | Based on rpmsrate (DVD-like) |

### Seed-based replication (implemented)

Uses rpmsrate to create a "DVD-like" mirror:
- Parses rpmsrate-raw with locale pattern detection
- Dependency resolution (Requires + Recommends + file deps)
- `--latest-only` option for single version per package
- Result: ~5 GB comparable to 4.2 GB DVD

```bash
urpm media set --replication=seed core_release
urpm mirror sync
```

### Quotas and retention (to implement)

```sql
ALTER TABLE media ADD COLUMN quota_mb INTEGER;
ALTER TABLE media ADD COLUMN retention_days INTEGER DEFAULT 30;
```

Eviction priority:
1. Files not referenced in current synthesis
2. Obsolescence score (age / priority)

---

## 4. Dependencies and Resolution

### Engine: libsolv

urpm-ng uses libsolv for dependency resolution.

### Supported dependency types

| Type | Support | Behavior |
|------|---------|----------|
| Requires | Yes | Installed automatically |
| Recommends | Yes | Installed unless --no-recommends |
| Suggests | Yes | Not installed unless --with-suggests |
| Supplements | Yes | Parsed and stored |
| Enhances | Yes | Parsed and stored |
| Conflicts | Yes | Blocks installation |
| Obsoletes | Yes | Automatic replacement |

### Alternatives (OR deps)

When multiple packages can satisfy a dependency:
- Interactive mode: asks user
- `--auto` mode: takes first choice
- `--prefer` option: guides choices

```bash
urpm install phpmyadmin --prefer=php:8.4,apache,php-fpm
```

### Versioned families

Automatic detection of families (php8.4, php8.5) with preference for already installed family.

---

## 5. System Protection (autoremove)

### Two protection levels

**Blacklist** (blocking) - Removal = dead machine:
- glibc, basesystem, filesystem, systemd, coreutils, rpm, bash
- grub2, running kernel
- Root filesystem support

**Redlist** (warning) - Asks for confirmation:
- Network, filesystem, desktop tools
- Drivers, fonts, printing

### Configuration

```bash
urpm config blacklist list/add/remove
urpm config redlist list/add/remove
urpm config kernel-keep N  # Number of kernels to keep
```

---

## 6. urpmd: The Daemon

### Scheduler

Periodic tasks:
- Metadata updates (HTTP HEAD to detect changes)
- Pre-download packages when system is idle
- Cache cleanup
- Peer discovery broadcasts

### Idle detection

- CPU: load < threshold
- Network: traffic < threshold
- Jitter to avoid synchronization between machines

### HTTP Server

- Default port: 9876
- ThreadingHTTPServer for parallel requests
- Serves RPMs and metadata to peers

---

## 7. Files and Paths

| Path | Description |
|------|-------------|
| `/var/lib/urpm/` | Database and cache (prod) |
| `/var/lib/urpm-dev/` | Development mode |
| `/etc/urpm/urpm.conf` | Global configuration |
| `/etc/urpm/autoremove.conf` | Blacklist/redlist |
| `/var/lib/rpm/installed-through-deps.list` | Packages installed as dependencies |

---

## 8. Prerequisites

```bash
urpmi python3-solv python3-zstandard
```

- **python3-solv**: dependency resolution (required)
- **python3-zstandard**: synthesis.hdlist.cz decompression (required)

Note: Do not use python3-zstd (Mageia package) which has decompression bugs.
