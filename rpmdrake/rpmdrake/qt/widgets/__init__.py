"""Qt widgets for rpmdrake-ng.

Custom widgets for the package manager interface.
"""

from .search_bar import SearchBar
from .package_list import PackageList, PackageTableModel, SectionHeader
from .filter_panel import FilterPanel           # kept for backward compat
from .filter_zone import FilterZone
from .category_panel import CategoryPanel
from .detail_panel import PackageDetailPanel
from .collapsible_group import CollapsibleGroup
from .download_progress import CollapsibleProgressWidget, SlotInfo

__all__ = [
    "SearchBar",
    "PackageList",
    "PackageTableModel",
    "SectionHeader",
    "FilterPanel",
    "FilterZone",
    "CategoryPanel",
    "PackageDetailPanel",
    "CollapsibleGroup",
    "CollapsibleProgressWidget",
    "SlotInfo",
]
