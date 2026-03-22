# urpm-ng Changelog

## Implemented Features

### Package Management
- **install**: Installation with dependency resolution
- **erase**: Removal with reverse dependency handling
- **upgrade**: Full system update
- **reinstall**: `urpm install --reinstall <pkg>`
- **autoremove**: Cleanup orphans, old kernels, failed deps, build deps
  - `--orphans`, `--kernels`, `--faildeps`, `--buildrequires`, `--all`
  - Blacklist/redlist with two-level protection

### Installation Options
- `--force`: Ignore dependency and conflict issues
- `--reinstall`: Reinstall same version
- `--auto`: Non-interactive mode
- `--test`: Dry-run without modifications
- `--no-recommends`: Skip Recommends
- `--with-suggests`: Include Suggests
- `--buildrequires` (`--builddeps`, `--br`, `-b`): Install build deps from spec/SRPM with tracking

### Dependencies
- **Weak dependencies**: Recommends, Suggests, Supplements, Enhances
- **Alternatives (OR deps)**: Interactive choice or `--auto`
- **--prefer**: Guide resolution choices
  - Version constraints: `--prefer=php:8.4`
  - Positive/negative preferences: `--prefer=apache,-nginx`
- **Versioned families**: Detection php8.4/php8.5, prefer installed family

### Search and Queries
- **search**: Search in names and provides
- **show** / **info**: Detailed information
- **list**: Package listing
- **whatprovides**: Which package provides a capability
- **depends** / **requires**: Package dependencies
- **rdepends** / **whatrequires**: Reverse dependencies
- **why**: Why a package is installed
- **recommends** / **whatrecommends**: Recommendations
- **suggests** / **whatsuggests**: Suggestions

### History
- **history**: Transaction list
- **history N**: Transaction details
- **undo N**: Undo a transaction
- **rollback**: Revert to previous state
- **history --delete**: Delete transactions

### GPG Security
- Signature verification enabled by default
- `--nosignature` to bypass
- `urpm key list`: List keys
- `urpm key import <file|url>`: Import a key
- `urpm key remove <keyid>`: Remove a key
- Auto-import during `media add`

### Media Management
- **media add**: Add with automatic URL parsing
- **media remove**: Remove
- **media list**: List with linked servers
- **media update**: Refresh metadata
- **media enable/disable**: Enable/disable
- Support for official and custom media (separate trees)

### Server Management
- **server list**: List with priority/enabled/ip_mode
- **server add**: Add + IP test + media scan
- **server remove**: Remove with cascade
- **server enable/disable**: Enable/disable
- **server priority**: Change priority
- **server test**: Test connectivity and detect ip_mode
- **server ip-mode**: Force IP mode (auto/ipv4/ipv6/dual)

### P2P Network
- Automatic peer discovery (UDP broadcast)
- Download from LAN peers before remote mirrors
- Load-balancing and automatic fallback
- Auto-blacklist peers providing invalid packages
- `urpm peer list/blacklist/unblacklist`

### urpmd Server
- Scheduler with CPU/network idle detection
- Pre-download updates
- HTTP server to serve peers
- API endpoints: `/ping`, `/status`, `/api/peers`, `/api/have`, etc.
- ThreadingHTTPServer for parallel requests

### Seed-based Replication
- `urpm mirror sync`: Parallel download of seed set
- `urpm media seed-info`: Show calculated set
- `urpm media set --replication=seed`: Enable policy
- `--latest-only`: Single version per package
- Based on rpmsrate (DVD-like, ~5 GB)

### Cache and Download
- **cache clean**: Cache cleanup
- Parallel multi-server downloads
- Per-server ip_mode support (avoids IPv6 timeout)

### Configuration
- `urpm config blacklist list/add/remove`
- `urpm config redlist list/add/remove`
- `urpm config kernel-keep N`

### Display
- `--show-all`, `--flat`, `--json` options
- Automatic multi-column display
- Colors by dependency type

---

## Bug Fixes

### Performance
- **install/upgrade startup**: Time reduced by ~5x (from ~1.9s to ~0.35s)
  - Using native libsolv methods instead of Python

### Commands
- **why and rdepends**: Virtual provides filtering, colors by type
- **Aliases**: requires, whatrequires, recommends, whatrecommends, suggests, whatsuggests
- **Truncated lists**: --show-all, --flat, --json options
- **urpm history alignment**: Headers aligned with data

### Resolution
- **Alternatives and --prefer**: Selection based on REQUIRES/PROVIDES
- **phpmyadmin dependencies**: Correct alternatives display
- **Batched install**: Fixed Tarjan reverse (deps first)

### Misc
- **Peer colors.warn handling**: Uses colors.warning() everywhere
- **Upgrade issue**: Orphans handled with transaction queue
- **Pre-downloading**: Works via urpmd scheduler

---

## Packaging Notes (external to urpm)

### php-webinterface
Issue identified: `php-webinterface` is only provided by webserver-specific packages. Cannot have `lighttpd + php-fpm`.

Suggested solutions for Mageia packagers:
1. `php8.4-fpm` provides `php-webinterface` directly
2. Create `php8.4-fpm-generic` without webserver dependency
3. `-nginx`/`-apache` packages become optional configs
