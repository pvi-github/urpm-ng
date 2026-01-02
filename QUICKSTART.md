# urpm-ng Quickstart

Get started with urpm-ng in 2 minutes.

## Installation via RPM (recommended)

### 1. Install the package

As root :
```bash
# urpmi urpm-ng
```

The package will:
- Install all dependencies
- Configure firewall ports for P2P sharing
- Start the urpmd daemon

### 2. Import media from urpmi

As root :
```bash
# urpm media import
```

### 3. Start using urpm

```bash
# Search
urpm search firefox

# Install
# urpm install firefox

# Upgrade system
# urpm upgrade

# Remove
# urpm erase firefox

# Clean orphans
# urpm autoremove
```

That's it! Machines with urpmd on the same LAN auto-discover each other and share cached packages.

---

## Development setup (from git)

For contributors or testing the latest code:

### 1. Install prerequisites

As root:
```bash
# urpmi python3-solv python3-zstandard
```

### 2. Clone

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
```

### 3. Configure media

Import from existing urpmi config, as root:
```bash
# ./bin/urpm media import
# ./bin/urpm media update
```

Or add manually, as root:
```bash
V=$(grep VERSION_ID /etc/os-release | cut -d= -f2)
A=$(uname -m)
# ./bin/urpm media add https://mirrors.kernel.org/mageia/distrib/$V/$A/media/core/release/
# ./bin/urpm media add https://mirrors.kernel.org/mageia/distrib/$V/$A/media/core/updates/
```

### 4. Open firewall ports

For P2P package sharing:
```
TCP 9876   # urpmd HTTP API
UDP 9878   # Peer discovery
```

### 5. Start the daemon

As root:
```bash
# ./bin/urpmd
```

### 6. Use urpm

```bash
# ./bin/urpm search firefox
# ./bin/urpm install firefox
```

---

See [README.md](README.md) for full documentation.
