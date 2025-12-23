# urpm-ng

A modern package manager for Mageia Linux, written in Python.

urpm-ng is a complete rewrite of the classic urpmi toolset, providing faster performance, better dependency resolution, and modern features like P2P package sharing.

## Prerequisites

### Required packages

```bash
urpmi python3-solv python3-zstandard
```

- **python3-solv** - SAT-based dependency resolution
- **python3-zstandard** - Decompression of synthesis.hdlist.cz files

### Firewall ports (for P2P sharing)

If you want to use P2P package sharing between LAN machines, open these ports:
- **TCP 9876** (production) or **TCP 9877** (dev mode) - urpmd HTTP API
- **UDP 9878** (production) or **UDP 9879** (dev mode) - Peer discovery broadcasts

Use the Mageia Control Center (MCC) > Security > Firewall, or edit `/etc/shorewall/rules` directly.

## Installation

### Development mode

Clone the repository and run directly (as root):

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# Switch to dev mode
touch .urpm.local

# Run daemon (without backgorund mode)
./bin/urpmd --dev

# Run urpm (in an other terminal)
cd /where/is/urpm-ng
./bin/urpm --help
```

In dev mode, data is stored in `/var/lib/urpm-dev/` and the daemon uses port 9877.

### Production mode

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# just incase make sure dev mode is off
rm -f .urpm.local

# Run daemon
./bin/urpmd

# Run urpm 
./bin/urpm --help
```

When installed system-wide (in `/usr/bin/`), urpm uses:
- Database: `/var/lib/urpm/packages.db`
- Daemon port: 9876
- PID file: `/run/urpmd.pid`

## Configuration

### Dev mode configuration

Create a `.urpm.local` file in the project root to customize dev mode:

```ini
# Custom base directory (optional)
base_dir=/path/lib/urpm-dev
```

### Media sources

Configure package sources (mirrors):

```bash
# List configured media
urpm media list

# Import from existing urpmi.cfg
urpm media import /etc/urpmi/urpmi.cfg

# Add a specific media source
urpm media add "Core Release" http://mirror.example.com/distrib/10/x86_64/media/core/release

# Update media metadata
urpm media update
```

---

# urpm - Command Line Interface

## Package Management

### Install packages

```bash
urpm install <package>        # Install a package
urpm i <package>              # Short alias

# Options
--auto                        # Non-interactive mode
--test                        # Dry run (simulation)
--without-recommends          # Skip recommended packages
--with-suggests               # Also install suggested packages
--force                       # Force despite dependency problems
--nosignature                 # Skip GPG verification (not recommended)
--prefer=<prefs>              # Guide alternative choices (see below)
```

#### Preference-guided installation

When installing packages with alternatives (e.g., phpmyadmin that can use different PHP versions and web servers), use `--prefer` to guide choices:

```bash
# Prefer PHP 8.4 with Apache and php-fpm, exclude mod_php
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# Prefer nginx instead of apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Preference syntax:
- `capability:version` - Version constraint (e.g., `php:8.4`)
- `pattern` - Prefer packages providing this capability (e.g., `apache`, `php-fpm`)
- `-pattern` - Disfavor packages matching this (e.g., `-apache-mod_php`)

Preferences work by checking package REQUIRES and PROVIDES, not package names.

### Remove packages

```bash
urpm erase <package>          # Remove a package
urpm e <package>              # Short alias
urpm remove <package>         # Alternative alias

# Options
--auto                        # Non-interactive mode
--erase-recommends            # Also remove packages only recommended (not required)
--keep-suggests               # Keep packages that are suggested by remaining packages
--force                       # Force despite dependency problems
```

### Upgrade system

```bash
urpm upgrade                  # Upgrade all packages
urpm up                       # Short alias
urpm upgrade <package>        # Upgrade specific packages

# Options
--auto                        # Non-interactive mode
--without-recommends          # Skip recommended packages
--with-suggests               # Also install suggested packages
```

### Auto-remove orphans

```bash
urpm autoremove               # Remove unused dependencies
urpm ar                       # Short alias

# Options
--auto                        # Non-interactive mode
--include-warned              # Also remove packages in redlist
```

## Search and Query

### Search packages

```bash
urpm search <pattern>         # Search by name/summary
urpm s <pattern>              # Short alias

# Options
--installed                   # Search only installed packages
```

### Show package info

```bash
urpm show <package>           # Show package details
urpm info <package>           # Alias
```

### List packages

```bash
urpm list installed           # List installed packages
urpm list available           # List available packages
urpm list updates             # List available updates
urpm list upgradable          # Alias for updates
```

### Dependencies

```bash
urpm depends <package>        # Show what a package requires
urpm rdepends <package>       # Show what requires a package (reverse deps)

# Options for depends
--tree                        # Show dependency tree
--installed                   # Only show installed dependencies
--prefer=<prefs>              # Filter by preferences (same syntax as install)
```

Example with preferences:
```bash
# Show phpmyadmin dependencies preferring PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

### File queries

```bash
urpm provides <package>       # List files provided by a package
urpm whatprovides <file>      # Find which package provides a file
urpm find <pattern>           # Search files in packages
```

## Package Marking

```bash
urpm mark manual <package>    # Mark as manually installed
urpm mark auto <package>      # Mark as auto-installed (dependency)
urpm mark show <package>      # Show install reason
```

## History and Undo

```bash
urpm history                  # Show transaction history
urpm history <id>             # Show details of a transaction

urpm undo [id]                # Undo a transaction (default: last)

urpm rollback <n>             # Rollback last n transactions
urpm rollback to <id>         # Rollback to a specific transaction
urpm rollback to <date>       # Rollback to a date (YYYY-MM-DD)
```

## Media Management

```bash
urpm media list               # List configured media
urpm media add <name> <url>   # Add a media source
urpm media remove <name>      # Remove a media source
urpm media enable <name>      # Enable a media
urpm media disable <name>     # Disable a media
urpm media update [name]      # Update media metadata
urpm media import <file>      # Import from urpmi.cfg
```

## Cache Management

```bash
urpm cache info               # Show cache information
urpm cache clean              # Remove orphan RPMs from cache
urpm cache rebuild            # Rebuild database from synthesis
urpm cache stats              # Detailed statistics
```

## Configuration

### Blacklist (never install/upgrade)

```bash
urpm config blacklist list    # Show blacklisted packages
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (warn before auto-remove)

```bash
urpm config redlist list      # Show redlisted packages
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Kernel management

```bash
urpm config kernel-keep       # Show how many kernels to keep
urpm config kernel-keep <n>   # Set number of kernels to keep
```

## GPG Keys

```bash
urpm key list                 # List installed GPG keys
urpm key import <file|url>    # Import a GPG key
urpm key remove <keyid>       # Remove a GPG key
```

## Orphan Cleanup

```bash
urpm cleandeps                # Find and remove orphaned dependencies
```

---

# urpmd - Background Daemon

urpmd is a background service providing:
- HTTP API for package operations
- Scheduled background tasks
- P2P peer discovery for LAN package sharing

## Running the daemon

```bash
# Dev mode (port 9877, user data in ~/.cache/urpm/)
./bin/urpmd

# Production mode (port 9876, requires root)
urpmd
```

## API Endpoints

### GET endpoints

| Endpoint | Description |
|----------|-------------|
| `/` | Service info |
| `/api/ping` | Health check |
| `/api/status` | Daemon status |
| `/api/media` | List configured media |
| `/api/available` | List available packages |
| `/api/updates` | List available updates |
| `/api/peers` | List discovered LAN peers |

### POST endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/refresh` | Refresh media metadata |
| `/api/available` | Query available packages |
| `/api/announce` | Announce packages to peers |
| `/api/have` | Query if peer has specific packages |

## Scheduled Tasks

The daemon automatically performs:
- Media metadata sync
- Cache cleanup
- Updates availability check
- Peer discovery (mDNS)

## P2P Package Sharing

When multiple machines on the same LAN run urpmd, they automatically discover each other and can share cached RPM packages, reducing bandwidth usage.

---

# License

GPL-3.0 - See LICENSE file for details.

# Authors

- Maât (Pascal Vilarem)
- Claude (AI assistant)
