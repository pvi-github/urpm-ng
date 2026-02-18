"""CLI helper functions extracted from main.py for better modularity."""

from .package import (
    extract_pkg_name,
    extract_family,
    get_installed_families,
    resolve_virtual_package,
)
from .debug import (
    DEBUG_LAST_INSTALLED_DEPS,
    DEBUG_LAST_REMOVED_DEPS,
    DEBUG_INSTALLED_DEPS_COPY,
    DEBUG_PREV_INSTALLED_DEPS,
    write_debug_file,
    clear_debug_file,
    copy_installed_deps_list,
    notify_urpmd_cache_invalidate,
)
from .kernel import (
    CONFIG_FILE,
    get_running_kernel,
    get_root_fstype,
    get_blacklist,
    get_redlist,
    read_config,
    write_config,
    get_user_blacklist,
    get_user_redlist,
    get_kernel_keep,
    is_running_kernel,
    find_old_kernels,
    find_faildeps,
)

__all__ = [
    # Package helpers
    'extract_pkg_name',
    'extract_family',
    'get_installed_families',
    'resolve_virtual_package',
    # Debug helpers
    'DEBUG_LAST_INSTALLED_DEPS',
    'DEBUG_LAST_REMOVED_DEPS',
    'DEBUG_INSTALLED_DEPS_COPY',
    'DEBUG_PREV_INSTALLED_DEPS',
    'write_debug_file',
    'clear_debug_file',
    'copy_installed_deps_list',
    'notify_urpmd_cache_invalidate',
    # Kernel helpers
    'CONFIG_FILE',
    'get_running_kernel',
    'get_root_fstype',
    'get_blacklist',
    'get_redlist',
    'read_config',
    'write_config',
    'get_user_blacklist',
    'get_user_redlist',
    'get_kernel_keep',
    'is_running_kernel',
    'find_old_kernels',
    'find_faildeps',
]
