# urpm D-Bus Service

D-Bus interface for urpm package management operations.

## Service Details

| Property | Value |
|----------|-------|
| Bus name | `org.mageia.Urpm.v1` |
| Object path | `/org/mageia/Urpm/v1` |
| Interface | `org.mageia.Urpm.v1` |
| Bus type | System bus |

## Authentication

- **Read-only methods**: No authentication required
- **Write methods**: PolicyKit authentication via `org.mageia.urpm.*` actions

PolicyKit actions:
- `org.mageia.urpm.query` - Search (no auth)
- `org.mageia.urpm.install` - Install packages
- `org.mageia.urpm.remove` - Remove packages
- `org.mageia.urpm.upgrade` - System upgrade
- `org.mageia.urpm.refresh` - Refresh metadata
- `org.mageia.urpm.media-manage` - Manage media sources

## Methods

### Read-only

| Method | Arguments | Returns | Description |
|--------|-----------|---------|-------------|
| `SearchPackages` | `s` pattern, `b` search_provides | `s` JSON | Search packages |
| `GetPackageInfo` | `s` identifier | `s` JSON | Package details |
| `ResolvePackages` | `as` names | `s` JSON | Batch resolve status |
| `GetUpdates` | - | `s` JSON | Available updates |
| `PreviewInstall` | `as` packages | `s` JSON | Dry-run resolution |
| `SearchFiles` | `s` pattern | `s` JSON | Search files |
| `GetPackageFiles` | `s` nevra | `s` JSON | Files in package |
| `GetInstalledPackages` | - | `s` JSON | All installed |
| `WhatRequires` | `s` package | `s` JSON | Reverse deps |
| `DownloadPackages` | `as` packages, `s` dir | `s` JSON | Download only |
| `CancelOperation` | - | `b` success | Cancel current op |

### Write (async)

| Method | Arguments | Returns | Description |
|--------|-----------|---------|-------------|
| `InstallPackages` | `as` packages, `a{sv}` options | `b` success, `s` error | Install |
| `InstallFiles` | `as` paths | `s` JSON | Install local RPMs |
| `RemovePackages` | `as` packages, `a{sv}` options | `b` success, `s` error | Remove |
| `UpgradePackages` | `a{sv}` options | `b` success, `s` error | Full upgrade |
| `RefreshMetadata` | - | `b` success, `s` error | Sync media |

## Signals

| Signal | Arguments | Description |
|--------|-----------|-------------|
| `OperationProgress` | `s` op_id, `s` phase, `s` package, `u` current, `u` total, `s` message | Progress update |
| `OperationComplete` | `s` op_id, `b` success, `s` message | Operation finished |

### Progress phases

- `resolving` - Dependency resolution
- `downloading` - Package download
- `installing` - RPM installation
- `removing` - RPM removal
- `upgrading` - System upgrade
- `refreshing` - Metadata sync

## Usage Examples

### busctl

```bash
# Search packages
busctl call org.mageia.Urpm.v1 /org/mageia/Urpm/v1 \
  org.mageia.Urpm.v1 SearchPackages sb "firefox" false

# Get updates
busctl call org.mageia.Urpm.v1 /org/mageia/Urpm/v1 \
  org.mageia.Urpm.v1 GetUpdates

# Install (requires auth)
busctl call org.mageia.Urpm.v1 /org/mageia/Urpm/v1 \
  org.mageia.Urpm.v1 InstallPackages "asa{sv}" 2 "vim" "htop" 0
```

### gdbus

```bash
# Introspect
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1

# Search
gdbus call --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1 \
  --method org.mageia.Urpm.v1.SearchPackages "firefox" false

# Monitor signals
gdbus monitor --system --dest org.mageia.Urpm.v1
```

### Python (GLib)

```python
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)

# Search
result = bus.call_sync(
    'org.mageia.Urpm.v1',
    '/org/mageia/Urpm/v1',
    'org.mageia.Urpm.v1',
    'SearchPackages',
    GLib.Variant('(sb)', ('firefox', False)),
    GLib.VariantType.new('(s)'),
    Gio.DBusCallFlags.NONE,
    -1, None
)
print(result.unpack()[0])  # JSON string
```

## Files

- `service.py` - Main D-Bus service implementation
- `org.mageia.Urpm.v1.xml` - Introspection XML
- `../data/org.mageia.Urpm.v1.conf` - D-Bus policy
- `../data/org.mageia.Urpm.v1.service` - D-Bus activation
- `../data/urpm-dbus.service` - systemd unit
- `../data/org.mageia.urpm.policy` - PolicyKit actions

## Running

```bash
# Via systemd (production)
sudo systemctl start urpm-dbus

# Direct (debug)
sudo urpm-dbus-service --debug
```
