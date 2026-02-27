# rpmdrake-ng

Modern graphical package manager for Mageia Linux.

rpmdrake-ng is a Qt6-based graphical interface for managing packages on Mageia Linux. It is built on top of the urpm-ng core library, providing a modern and responsive user experience.

<img width="1930" height="1155" alt="image" src="https://github.com/user-attachments/assets/4e498637-6530-404b-8759-616ecd52c5f6" />
<img width="1804" height="953" alt="image" src="https://github.com/user-attachments/assets/dd70b678-ace4-40eb-8fdb-4c6bc4cf2116" />

## Features

- **Fast search**: Incremental package search with debouncing
- **Package management**: Install, remove, and update packages
- **System updates**: View and apply available updates
- **Media configuration**: Manage package repositories
- **Transaction preview**: Review changes before applying
- **Parallel downloads**: Progress display with multiple download slots
- **Keyboard shortcuts**: Efficient navigation without mouse

## Prerequisites

### Distribution

Mageia 9 or Mageia 10.

### Dependencies

rpmdrake-ng requires urpm-ng-core. See the [main README](../README.md) for urpm-ng installation.

## Installation

### RPM Install (one-liner)

rpmdrake-ng is not yet in official Mageia repositories. Install from GitHub releases:

```bash
mkdir -p $HOME/tmp/rpmdrake && cd $HOME/tmp/rpmdrake && \
MGAVER=$(rpm -q --qf '%{version}' mageia-release-Default 2>/dev/null | cut -d. -f1) && \
ARCH=$(uname -m) && \
VER=$(curl -s https://api.github.com/repos/pvi-github/urpm-ng/releases | grep -m1 '"tag_name"' | cut -d'"' -f4) && \
echo "Downloading rpmdrake-ng for Mageia $MGAVER..." && \
curl -s "https://api.github.com/repos/pvi-github/urpm-ng/releases/tags/$VER" | \
  grep browser_download_url | grep 'rpmdrake-ng.*\.rpm"' | cut -d'"' -f4 | \
  grep -v '\.src\.rpm' | grep "mga${MGAVER}" | head -1 | xargs curl -sLO && \
su -c "urpm i $HOME/tmp/rpmdrake/rpmdrake-ng-*.rpm"
```

Note: urpm-ng must be installed first. See [urpm-ng installation](../README.md#installation).

## Usage

Launch from the application menu or run:

```bash
rpmdrake-ng
```

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| Ctrl+F | Focus search |
| Ctrl+A | Select all visible |
| Escape | Deselect all |
| Ctrl+I | Install selected |
| Ctrl+R | Remove selected |
| Ctrl+U | Update selected |
| Ctrl++ | Zoom in |
| Ctrl+- | Zoom out |
| F5 | Refresh package list |

## Architecture

```
rpmdrake/
├── common/           # Shared code (models, controller, interfaces)
│   ├── interfaces.py # ViewInterface ABC
│   ├── models.py     # PackageDisplayInfo, FilterState
│   └── controller.py # Business logic, async search
├── qt/               # Qt6 implementation
│   ├── main.py       # Entry point, MainWindow
│   ├── view.py       # QtView implements ViewInterface
│   └── widgets/      # UI components
│       ├── search_bar.py
│       ├── package_list.py
│       ├── filter_panel.py
│       └── download_progress.py
└── helper/           # Root privilege helper
    └── transaction_helper.py
```

## Building RPM

```bash
cd rpmdrake
make rpm
```

## License

GPL-3.0-or-later

## See Also

- [urpm-ng](../README.md) - Core package manager
- `man rpmdrake` - Manual page
