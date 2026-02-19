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
from .config import (
    cmd_config,
    cmd_key,
)
from .history import (
    cmd_history,
    cmd_undo,
    cmd_rollback,
)
from .server import (
    cmd_server_list,
    cmd_server_add,
    cmd_server_remove,
    cmd_server_enable,
    cmd_server_disable,
    cmd_server_priority,
    cmd_server_test,
    cmd_server_ipmode,
    cmd_server_autoconfig,
)
from .mirror import (
    cmd_mirror_status,
    cmd_mirror_enable,
    cmd_mirror_disable,
    cmd_mirror_quota,
    cmd_mirror_disable_version,
    cmd_mirror_enable_version,
    cmd_mirror_clean,
    cmd_mirror_sync,
    cmd_mirror_ratelimit,
)
from .media import (
    cmd_media_list,
    cmd_init,
    cmd_media_add,
    cmd_media_remove,
    cmd_media_enable,
    cmd_media_disable,
    cmd_media_update,
    cmd_media_import,
    cmd_media_set,
    cmd_media_seed_info,
    cmd_media_link,
    cmd_media_autoconfig,
    parse_urpmi_cfg,
    STANDARD_MEDIA_TYPES,
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
    # Config commands
    'cmd_config',
    'cmd_key',
    # History commands
    'cmd_history',
    'cmd_undo',
    'cmd_rollback',
    # Server commands
    'cmd_server_list',
    'cmd_server_add',
    'cmd_server_remove',
    'cmd_server_enable',
    'cmd_server_disable',
    'cmd_server_priority',
    'cmd_server_test',
    'cmd_server_ipmode',
    'cmd_server_autoconfig',
    # Mirror commands
    'cmd_mirror_status',
    'cmd_mirror_enable',
    'cmd_mirror_disable',
    'cmd_mirror_quota',
    'cmd_mirror_disable_version',
    'cmd_mirror_enable_version',
    'cmd_mirror_clean',
    'cmd_mirror_sync',
    'cmd_mirror_ratelimit',
    # Media commands
    'cmd_media_list',
    'cmd_init',
    'cmd_media_add',
    'cmd_media_remove',
    'cmd_media_enable',
    'cmd_media_disable',
    'cmd_media_update',
    'cmd_media_import',
    'cmd_media_set',
    'cmd_media_seed_info',
    'cmd_media_link',
    'cmd_media_autoconfig',
    'parse_urpmi_cfg',
    'STANDARD_MEDIA_TYPES',
]
