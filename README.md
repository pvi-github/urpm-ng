# urpm-ng

A modern package manager for Mageia Linux, written in Python.

urpm-ng is a complete rewrite of the classic urpmi toolset, providing faster performance, better dependency resolution, and modern features like P2P package sharing.

## Prerequisites

### Distribution

At the moment you need mageia 9 or Mageia 10.

### Firewall ports to open (for P2P sharing)

If you want to use P2P package sharing between LAN machines, open these ports:
- **TCP 9876** (production) or **TCP 9877** (dev mode) - urpmd HTTP API
broadcasts

Use the Mageia Control Center (MCC) > Security > Firewall, or edit `/etc/shorewall/rules.drakx` directly.

## Installation

### Required packages

You'll need :

- **python3-solv** - SAT-based dependency resolution
- **python3-zstandard** - Decompression of synthesis.hdlist.cz files

Normally installation process through RPM install will automatically install them.

If you want to install them manually:

```bash
urpmi python3-solv python3-zstandard
```
### Standard installation

### RPM Install

Get last Release from https://github.com/pvi-github/urpm-ng/releases

Download the RPM file that matches your Mageia version, and install it.

Nota : at first install through rpm it will try to import its config from urpmi

```bash
# Mettez la dernière version à la place du 0.1.16
export URPM_VERSION=0.1.16

curl --follow https://github.com/pvi-github/urpm-ng/releases/download/$URPM_VERSION/urpm-ng-$URPM_VERSION-1.$(rpm -qa | sed -ne "s/^mageia-release-Default[^m]*//p").noarch.rpm -o urpm-ng-$URPM_VERSION-1.$(rpm -qa | sed -ne "s/^mageia-release-Default[^m]*//p").noarch.rpm

urpmi --auto urpm-ng-$URPM_VERSION-1.$(rpm -qa | sed -ne "s/^mageia-release-Default[^m]*//p").noarch.rpm

# Run urpm 
./bin/urpm --help
```

## Configuration

Not needed at the moment.

When installed system-wide (in `/usr/bin/`), urpm uses:
- Database: `/var/lib/urpm/packages.db`
- Daemon port: 9876
- PID file: `/run/urpmd.pid`

### Media sources

How to configure package media sources & mirrors servers.

Nota : for RPM installation these steps should not be needed.

```bash
# List configured media
urpm media list

# If there are none try to import from existing urpmi.cfg
urpm media import /etc/urpmi/urpmi.cfg

# Add a specific media source if needed
urpm media add http://mirror.example.com/distrib/10/x86_64/media/core/release
urpm media add http://mirror.example.com/distrib/10/x86_64/media/core/updates
urpm media add http://mirror.example.com/distrib/10/x86_64/media/core/update_testing

# Configure more servers
urpm server autoconfig

# Update media metadata
urpm media update
```

---

# urpm - Command Line Interface

## Display Options

Most commands support these output options:

```bash
--show-all            # Show all items without truncation
--flat                # One item per line (parsable by scripts)
--json                # JSON output (for programmatic use)
```

By default, long lists are displayed in multi-column format and truncated to 10 lines with "... and N more". Use `--show-all` to see everything.

Examples:
```bash
urpm list installed --flat          # One package per line
urpm search firefox --json          # JSON output
urpm i task-plasma --show-all       # Show all dependencies
```

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
urpm q <pattern>              # Query alias (urpmq compatibility)

# Options
--installed                   # Search only installed packages
--unavailable                 # List installed packages not in any media
```

#### Find unavailable packages

List packages that are installed but no longer available in any configured media (like `urpmq --unavailable`):

```bash
urpm q --unavailable          # List all unavailable packages
urpm q --unavailable php      # Filter by pattern
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
urpm why <package>            # Explain why a package is installed

# Options for depends
--tree                        # Show dependency tree
--installed                   # Only show installed dependencies
--prefer=<prefs>              # Filter by preferences (same syntax as install)

# Options for rdepends
--tree                        # Show reverse dependency tree
--all                         # Show all recursive reverse dependencies (flat)
--depth=N                     # Maximum tree depth (default: 3)
--hide-uninstalled            # Only show paths leading to installed packages
```

Example with preferences:
```bash
# Show phpmyadmin dependencies preferring PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Example with rdepends:
```bash
# Show reverse dependency tree for rtkit, depth 10, only installed paths
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Weak dependencies

```bash
urpm recommends <package>     # Show packages recommended by a package
urpm whatrecommends <package> # Show packages that recommend a package
urpm suggests <package>       # Show packages suggested by a package
urpm whatsuggests <package>   # Show packages that suggest a package
```

### File queries

```bash
urpm provides <package>       # List files provided by a package
urpm whatprovides <file>      # Find which package provides a file
urpm find <pattern>           # Search files in packages (installed + available)
urpm find -i <pattern>        # Search only in installed packages
urpm find -a <pattern>        # Search only in available packages
```

To search in available packages, you need to enable files.xml sync:

```bash
urpm media set --all --sync-files  # Enable files.xml sync on all media
urpm media update --files          # Download files.xml (~500 MB, 10-15 min first time)
```

Once enabled, urpmd will automatically sync files.xml daily when the system is idle.

## Package Marking

```bash
urpm mark manual <package>    # Mark as manually installed
urpm mark auto <package>      # Mark as auto-installed (dependency)
urpm mark show <package>      # Show install reason
```

## Package Holds

Hold packages to prevent upgrades and replacement by obsoletes:

```bash
urpm hold <package>           # Hold a package
urpm hold <package> -r "reason"  # Hold with a reason
urpm hold                     # List held packages
urpm unhold <package>         # Remove hold
```

Held packages are protected from:
- Version upgrades during `urpm upgrade`
- Being replaced by packages that obsolete them

Example:
```bash
# dhcpcd obsoletes dhcp-client, but you want to keep dhcp-client
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Now urpm upgrade will skip dhcp-client and warn:
#   Held packages (1) skipped:
#     dhcp-client (would be obsoleted by dhcpcd)

# To allow the replacement later:
urpm unhold dhcp-client
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
urpm media add <url>          # Add official Mageia media (auto-parsed)
urpm media add --custom "Name" shortname <url>  # Add custom/third-party media
urpm media remove <name>      # Remove a media source
urpm media enable <name>      # Enable a media
urpm media disable <name>     # Disable a media
urpm media update [name]      # Update media metadata
urpm media import <file>      # Import from urpmi.cfg
urpm media link <name> +srv -srv  # Link/unlink servers to a media
urpm media set <name> [opts]  # Modify media settings (sharing, replication, quota...)
urpm media autoconfig -r 10   # Auto-add official Mageia media for release 10
```

### Server-Media linking

Link or unlink servers to specific media sources:

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Add servers
urpm media link "Core Updates" -oldserver          # Remove server
urpm media link "Core Release" +all                # Add all available servers
urpm media link "Core Release" -all +preferred     # Reset and add one
```

Note: When adding servers, urpm checks if the media is actually available on the server before linking.

### Auto-configure media

Automatically add official Mageia media for a release:

```bash
urpm media autoconfig --release 10              # Add all official media for Mageia 10
urpm media autoconfig -r cauldron               # Add media for Cauldron
urpm media autoconfig -r 10 --no-nonfree        # Skip nonfree media
urpm media autoconfig -r 10 --no-tainted        # Skip tainted media
urpm media autoconfig -r 10 -n                  # Dry-run: show what would be added
```

### Media settings

Configure media sharing and replication:

```bash
urpm media set "Core Release" --shared=yes           # Share with P2P peers
urpm media set "Core Release" --replication=seed     # Full replication (DVD-like)
urpm media set "Core Release" --replication=on_demand  # Cache downloaded packages
urpm media set "Core Release" --quota=5G             # Limit cache size
urpm media set "Core Release" --retention=30         # Keep packages 30 days
urpm media set "Core Release" --priority=10          # Higher priority
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Seed sections
urpm media set "Core Release" --sync-files           # Enable files.xml sync for urpm find
urpm media set --all --sync-files                    # Enable on all media
```

Examples:
```bash
# Add official Mageia media (server and media auto-detected)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Add custom third-party media
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Server Management

Servers are mirror sources that can serve multiple media. urpm supports multiple servers per media for load balancing and failover.

```bash
urpm server list              # List configured servers
urpm server add <name> <url>  # Add a server (tests IP and scans media)
urpm server remove <name>     # Remove a server
urpm server enable <name>     # Enable a server
urpm server disable <name>    # Disable a server
urpm server priority <name> <n>  # Set server priority (higher = preferred)
urpm server test [name]       # Test connectivity and detect IP mode
urpm server ip-mode <name> <mode>  # Set IP mode (auto/ipv4/ipv6/dual)
```

### Server list

Options for urpm server list:
```bash
--all                 # Show all servers including those disabled
```

### IP Mode

Each server has an IP mode to handle IPv4/IPv6 connectivity:
- `auto` - Let system decide (may cause 30s timeout if IPv6 fails)
- `ipv4` - Force IPv4 only
- `ipv6` - Force IPv6 only
- `dual` - Both work, prefer IPv4 (recommended for dual-stack servers)

IP mode is auto-detected when adding a server. Use `server test` to re-detect or `server ip-mode` to set manually.

## Peer Management

When urpmd is running on multiple machines on the same LAN, they discover each other and share cached packages (P2P).

```bash
urpm peer list                # List discovered peers
urpm peer downloads           # Show download statistics from peers
urpm peer blacklist <host>    # Block a peer (e.g., if providing bad packages)
urpm peer unblacklist <host>  # Unblock a peer
urpm peer clean               # Remove stale/offline peers from list
```

### Local-only mode

Use `--only-peers` to download exclusively from LAN peers without falling back to upstream mirrors:

```bash
urpm i --only-peers firefox   # Install only if available from peers
urpm u --only-peers           # Upgrade only with packages from peers
urpm download --only-peers pkg  # Download only from peers
```

This is useful for air-gapped networks or when you want to ensure all packages come from trusted local sources.

## Cache Management

```bash
urpm cache info               # Show cache information
urpm cache clean              # Remove orphan RPMs from cache
urpm cache rebuild            # Rebuild database from synthesis
urpm cache stats              # Detailed statistics
```

## Mirror / Replication

urpm-ng can replicate a subset of packages locally, similar to a DVD installation set. This is useful for install parties or offline installations.

### Seed-based replication

Replication uses the `rpmsrate-raw` file from Mageia to determine which packages to mirror (same logic as DVD content).

```bash
# Enable seed-based replication on a media
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# View the computed seed set
urpm media seed-info "Core Release"
# Output:
#   Sections: INSTALL, CAT_PLASMA5, CAT_GNOME, ...
#   Seed packages from rpmsrate: 437
#   Locale patterns: 3
#   Expanded locale packages: +237
#   With dependencies: 2300 packages
#   Estimated size: ~3.5 GB

# Force sync (download missing packages)
urpm mirror sync

# Sync only latest version of each package (smaller, DVD-like)
urpm mirror sync --latest-only
```

### How it works

1. Parses `/usr/share/meta-task/rpmsrate-raw` (from meta-task package)
2. Extracts packages from sections: INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE, etc.
3. Expands locale patterns (e.g., `libreoffice-langpack-ar` → all langpacks)
4. Resolves dependencies (Requires + Recommends)
5. Downloads missing packages in parallel

The default seed sections cover all major desktop environments and applications, resulting in ~5 GB of packages (comparable to a Mageia DVD).

### Replication policies

```bash
urpm media set <name> --replication=none       # Metadata only, no packages
urpm media set <name> --replication=on_demand  # Cache what's downloaded (default)
urpm media set <name> --replication=seed       # DVD-like content from rpmsrate
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

## Build Dependencies

Install build dependencies for RPM building:

```bash
urpm install --builddeps foo.spec       # From spec file
urpm install --builddeps foo.src.rpm    # From source RPM
urpm i -b                                # Auto-detect in RPM build tree

# Options
--sync                        # Wait for all scriptlets to complete
```

## Container Build System

urpm provides a complete container-based build system for RPM packages using Docker or Podman.

### Create build image

```bash
urpm mkimage --release 10 --tag mageia:10-build

# Options
-r, --release <version>       # Mageia version (e.g., 10, cauldron)
-t, --tag <tag>               # Image tag (e.g., mageia:10-build)
--arch <arch>                 # Target architecture (default: host)
-p, --packages <list>         # Additional packages (comma-separated)
--runtime docker|podman       # Container runtime (default: auto-detect)
--keep-chroot                 # Keep temporary chroot after image creation
-w, --workdir <path>          # Working directory for chroot (default: /tmp)
```

### Build packages

```bash
# Build from source RPM
urpm build --image mageia:10-build ./foo-1.0-1.mga10.src.rpm

# Build from spec file (sources auto-copied from SOURCES/)
urpm build --image mageia:10-build ./workspace/SPECS/foo.spec

# Multiple builds in parallel
urpm build --image mageia:10-build *.src.rpm --parallel 4

# Options
-i, --image <tag>             # Docker/Podman image to use
-o, --output <dir>            # Output directory (default: ./build-output)
--runtime docker|podman       # Container runtime (default: auto-detect)
-j, --parallel <N>            # Number of parallel builds (default: 1)
--keep-container              # Keep container after build (for debugging)
```

### Workspace layout

For spec file builds, urpm supports the standard RPM workspace layout:

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

Results are placed in:
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Example workflow

```bash
# 1. Create build image (once)
urpm mkimage --release 10 --tag mga:10-build

# 2. Build a package
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Check results
ls ./build-output/
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

# GUI Integration (Discover / GNOME Software)

urpm-ng provides a PackageKit backend allowing graphical software centers to manage packages.

## Installation

```bash
urpm install pk-backend-urpm
```

This installs:
- `libpk_backend_urpm.so` - PackageKit backend
- D-Bus service `org.mageia.Urpm.v1` - Privileged operations
- PolicyKit policies - Authorization prompts
- AppStream configuration - Software catalog metadata

## Supported Applications

- **KDE Discover** - Full support (search, install, remove, updates)
- **GNOME Software** - Full support (search, install, remove, updates)

## How It Works

```
┌─────────────────┐
│  Discover /     │
│  GNOME Software │
└────────┬────────┘
         │
┌────────▼────────┐
│   PackageKit    │
│ (libpk_backend_ │
│    urpm.so)     │
└────────┬────────┘
         │
┌────────▼────────┐
│  D-Bus Service  │
│  + PolicyKit    │
│ (org.mageia.    │
│   Urpm.v1)      │
└────────┬────────┘
         │
┌────────▼────────┐
│  urpm-ng core   │
│  (Python)       │
└─────────────────┘
```

## Troubleshooting

```bash
# Check if D-Bus service is running
systemctl status urpm-dbus.service

# Check PackageKit backend
pkcon backend-details

# Restart services after update
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# Check D-Bus interface
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Development & contributing

## Prerequisites

### Firewall ports to open

If you want to use P2P package sharing between LAN machines, open these ports:
- **UDP 9878** (production) or **UDP 9879** (dev mode) - Peer discovery 

### Setting up your environment

Clone the repository :

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Dev mode configuration

Create a `.urpm.local` file in the project root to customize dev mode:

```bash
cd /where/is/urpm-ng

# Dev mode (port 9877, user data in ~/var/lib/urpm-dev/)
# Switch to dev mode
touch .urpm.local
```

Nota, you can change where urpm & uprmd put their data by editing .urpm.local file :
```ini
# Custom base directory (optional)
base_dir=/path/lib/urpm-dev
```

In dev mode, by default, data is stored in `/var/lib/urpm-dev/` and the daemon uses port 9877.

**Note that in dev mode urpmd will only interact with other urpmd in dev mode.**

## Running the daemon

```bash
# Run daemon (as root, without background mode)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## Running urpm

```bash
# Run urpm (as root in a specific concole)

cd /where/is/urpm-ng

./bin/urpm --help

```

## Coding, testing, contributing...

TODO.

contribution can be of various sorts : code, testing, translating, giving feedback... no contribution it too tiny. Even telling that you spoetted a typo in documentation is valuable.

This part will also  talk about creating branches, submitting merge requests, doing clean commits, state of the art commit messages, commenting code, writing state of the art issues...

---

# Known Issues / TODO

- **`urpm find` performance** - Search in files.xml is slower than urpmf (2.5s vs 0.6s). Needs optimization.

---

# License

GPL-3.0 - See LICENSE file for details.

# Authors

- Maât (Pascal Vilarem)
- Papoteur (Mageia Contributor)
- Claude (AI assistant)
