"""CLI command modules extracted from main.py for better modularity."""

from .cache import (
    cmd_cache_info,
    cmd_cache_clean,
    cmd_cache_rebuild,
    cmd_cache_stats,
    cmd_cache_rebuild_fts,
)
from .peer import (
    cmd_peer,
)

__all__ = [
    # Cache commands
    'cmd_cache_info',
    'cmd_cache_clean',
    'cmd_cache_rebuild',
    'cmd_cache_stats',
    'cmd_cache_rebuild_fts',
    # Peer commands
    'cmd_peer',
]
