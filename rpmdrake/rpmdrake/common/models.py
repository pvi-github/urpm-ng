"""Data models for rpmdrake-ng.

Contains dataclasses for data transfer between Controller and View.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Set

__all__ = [
    "PackageState",
    "InstallReason",
    "PackageDisplayInfo",
    "SectionHeader",
    "FilterState",
]


class PackageState(Enum):
    """Package state filters."""
    UPGRADES = auto()    # Has update available
    INSTALLED = auto()   # Currently installed
    AVAILABLE = auto()   # Not installed
    CONFLICTS = auto()   # Has conflicts


class InstallReason(Enum):
    """Why a package was installed."""
    EXPLICIT = "explicit"    # User requested
    DEPENDENCY = "dep"       # Installed as dependency
    ORPHAN = "orphan"        # No longer needed
    UNKNOWN = "unknown"      # Unknown reason


@dataclass
class SectionHeader:
    """Section separator in the package list.

    By default purely decorative.  When *checkable* is ``True``, the
    checkbox column displays a toggle that selects / deselects every
    package in the section at once (used for the "Mises à jour" section).

    Example title: ``"══ Mises à jour (3) ══"``
    """
    title: str
    checkable: bool = False
    checked: bool = False


@dataclass
class PackageDisplayInfo:
    """Package information for display in the list.

    This is a lightweight DTO containing only what's needed for display.
    Full package details are fetched on demand.
    """
    # Identity
    name: str
    version: str
    release: str
    arch: str
    epoch: int

    # Display
    summary: str
    icon: Optional[str] = None  # AppStream icon path

    # State
    installed: bool = False
    installed_version: Optional[str] = None
    has_update: bool = False
    install_reason: Optional['InstallReason'] = None
    has_conflict: bool = False
    conflict_with: Optional[str] = None

    # Selection
    selected: bool = False

    # Version ordering (False = older version when multiple versions available)
    is_latest: bool = True

    # Row number (for command reference)
    row_number: int = 0

    @property
    def nevra(self) -> str:
        """Full name-[epoch:]version-release.arch string.

        The epoch prefix is included only when epoch > 0, matching the
        standard RPM convention (e.g. ``vim-2:9.1.0-1.mga10.x86_64``).
        """
        evr = f"{self.epoch}:{self.version}" if self.epoch else self.version
        return f"{self.name}-{evr}-{self.release}.{self.arch}"

    @property
    def display_version(self) -> str:
        """Version string for display (with upgrade arrow if applicable)."""
        if self.has_update and self.installed_version:
            return f"{self.installed_version} → {self.version}-{self.release}"
        return f"{self.version}-{self.release}"


@dataclass
class FilterState:
    """Current filter configuration.

    State filters are combinable (checkboxes).
    Display toggles show/hide specific package types.
    """
    # State filters (combinable, at least one must be active)
    states: Set[PackageState] = field(
        default_factory=lambda: {PackageState.UPGRADES}
    )

    # Install reason filters (when viewing installed packages)
    show_explicit: bool = True      # Explicitly installed
    show_dependencies: bool = True  # Installed as dependencies
    show_orphans: bool = True       # Orphan packages

    # Display toggles (show when True, hidden by default)
    show_libs: bool = False      # System/Libraries group
    show_devel: bool = False     # *-devel packages
    show_debug: bool = False     # *-debug* packages
    show_i586: bool = False      # 32-bit on x86_64

    # Special filters (exclusive filter, only show matching)
    show_tasks: bool = False     # Only show task-* meta-packages

    # Language filters (enabled languages, packages visible)
    languages: Set[str] = field(
        default_factory=lambda: {"en", "fr"}
    )

    # Category filter (None = all categories)
    category: Optional[str] = None

    # Search term
    search_term: str = ""

    def clone(self) -> 'FilterState':
        """Create a copy of this filter state."""
        return FilterState(
            states=set(self.states),
            show_explicit=self.show_explicit,
            show_dependencies=self.show_dependencies,
            show_orphans=self.show_orphans,
            show_libs=self.show_libs,
            show_devel=self.show_devel,
            show_debug=self.show_debug,
            show_i586=self.show_i586,
            show_tasks=self.show_tasks,
            languages=set(self.languages),
            category=self.category,
            search_term=self.search_term,
        )
