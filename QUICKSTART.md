# urpm-ng Quickstart

Get started with urpm-ng in 2 minutes.

## Installation via RPM (recommended)

### 1. Install the package

```bash
$ su -c 'urpmi /path/to/urpm-ng-rpm-file.rpm'
```

The package will:
- Install all dependencies
- Configure firewall ports for P2P sharing
- Start the urpmd daemon

### 2. Import media from urpmi & autoconfigure servers

```bash
$ su - c 'urpm media import'

$ su - c 'urpm server autoconfig'
```

### 3. Start using urpm

```bash
# Search/Query
$ su -c 'urpm q firefox'

# Install
$ su - c 'urpm i firefox'

# Upgrade system
$ su -c 'urpm u'

# Remove
$ su -c 'urpm e firefox'

# Clean orphans
$ su -c 'urpm autoremove'
```

That's it! Machines with urpmd on the same LAN auto-discover each other and share cached packages.

---

## Development setup (from git)

For contributors or testing the latest code:

### 1. Install prerequisites

```bash
$ su -c 'urpmi python3-solv python3-zstandard'
```

### 2. Clone

```bash
git clone https://github.com/pvi-github/urpm-ng.git

cd urpm-ng
```

Note : the following commands need to be in urpm-ng directory.

### 3. Configure media

Import from existing urpmi config, as root:
```bash
$ su -c './bin/urpm media import'

$ su -c './bin/urpm media update'
```

Or add manually:
```bash
V=$(grep VERSION_ID /etc/os-release | cut -d= -f2)

A=$(uname -m)

$ su -c './bin/urpm media add https://mirrors.kernel.org/mageia/distrib/$V/$A/media/core/release/'

$ su -c './bin/urpm media add https://mirrors.kernel.org/mageia/distrib/$V/$A/media/core/updates/'
```

### 4. Open firewall ports

For P2P package sharing:
```
TCP 9876   # urpmd HTTP API
UDP 9878   # Peer discovery
```

### 5. Start the daemon

```bash
$ su -c './bin/urpmd'
```

### 6. Use urpm

```bash
$ su -c './bin/urpm search firefox'

$ su -c './bin/urpm install firefox'
```

---

See [README.md](README.md) for full documentation.
