"""Common module for rpmdrake-ng.

Contains UI-agnostic components:
- ViewInterface: Abstract interface for GUI implementations
- Controller: Business logic and state management
- Models: Data transfer objects
- HelperClient: Communication with privileged helper
"""

from .interfaces import ViewInterface
from .models import PackageDisplayInfo, FilterState
from .controller import Controller
from .helper_client import HelperClient, TransactionResult

__all__ = [
    "ViewInterface",
    "PackageDisplayInfo",
    "FilterState",
    "Controller",
    "HelperClient",
    "TransactionResult",
]
