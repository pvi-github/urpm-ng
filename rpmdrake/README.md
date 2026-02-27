# rpmdrake-ng

Modern graphical package manager for Mageia Linux.

## Overview

rpmdrake-ng is a Qt6-based graphical interface for managing packages on Mageia Linux. It is built on top of the urpm-ng core library, providing a modern and responsive user experience.
<img width="1930" height="1155" alt="image" src="https://github.com/user-attachments/assets/4e498637-6530-404b-8759-616ecd52c5f6" />

## Features

- **Fast search**: Incremental package search with debouncing
- **Package management**: Install, remove, and update packages
- **System updates**: View and apply available updates
- **Media configuration**: Manage package repositories
- **Transaction preview**: Review changes before applying
- **Parallel downloads**: Progress display with multiple download slots
- **Keyboard shortcuts**: Efficient navigation without mouse

## Requirements

- Python 3.9+
- PySide6 (Qt6 for Python)
- urpm-ng-core >= 0.3.0
- PolicyKit (for privilege escalation)

## Installation

### From RPM (recommended)

```bash
sudo urpm install rpmdrake-ng
```

### From source

```bash
cd rpmdrake
pip install -e .
```

## Usage

Launch from the application menu or run:

```bash
rpmdrake
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
