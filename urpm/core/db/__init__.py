"""Database operation mixins for PackageDatabase.

Each mixin provides a group of related database operations:
- MediaMixin: Media CRUD operations
- ServerMixin: Server CRUD and server-media linking
- ConstraintsMixin: Package pins and holds
- HistoryMixin: Transaction history
- PeerMixin: Peer tracking and mirror configuration
- CacheMixin: Cache file tracking

The historical ``FilesMixin`` was removed in 0.7.x: package file
lists are no longer cached in SQLite — see
``doc/TODO_SHRINK_FILES_DB.md`` for the rationale.
"""

from .media import MediaMixin
from .server import ServerMixin
from .constraints import ConstraintsMixin
from .history import HistoryMixin
from .peer import PeerMixin
from .cache import CacheMixin

__all__ = [
    'MediaMixin',
    'ServerMixin',
    'ConstraintsMixin',
    'HistoryMixin',
    'PeerMixin',
    'CacheMixin',
]
