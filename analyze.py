# Parse the file to get line ranges for each command
import re

with open('/home/superadmin/Sources/urpm-ng/urpm/cli/main.py', 'r') as f:
    lines = f.readlines()

# Find all function definitions
funcs = {}
for i, line in enumerate(lines, 1):
    if line.startswith('def '):
        match = re.match(r'def (\w+)\(', line)
        if match:
            funcs[match.group(1)] = i

# Sort by line number
sorted_funcs = sorted(funcs.items(), key=lambda x: x[1])

# Group commands by functional area
groups = {
    'Initialization': ['check_dependencies', 'print_missing_dependencies', 'create_parser', 'main'],
    'Debug Utilities': ['_write_debug_file', '_clear_debug_file', '_copy_installed_deps_list', '_notify_urpmd_cache_invalidate'],
    'Query/Search': ['cmd_search', 'cmd_show', '_cmd_search_unavailable', 'cmd_provides', 'cmd_whatprovides', 'cmd_find'],
    'Install/Remove': ['cmd_install', 'cmd_erase', 'cmd_upgrade', 'cmd_download', 'cmd_cleanup'],
    'Package Management': ['cmd_autoremove', 'cmd_mark', 'cmd_hold', 'cmd_unhold', 'cmd_cleandeps'],
    'Media Management': ['cmd_media_list', 'cmd_media_add', 'cmd_media_remove', 'cmd_media_enable', 'cmd_media_disable', 'cmd_media_update',
 'cmd_media_import', 'cmd_media_set', 'cmd_media_seed_info', 'cmd_media_link', 'cmd_media_autoconfig'],
    'Media Helpers': ['_generate_media_name', '_generate_short_name', 'parse_mageia_media_url', 'parse_custom_media_url',
'_fetch_media_pubkey', '_get_gpg_key_info', '_is_key_in_rpm_keyring', '_import_gpg_key', '_import_single_media', 'parse_urpmi_cfg'],
    'Server Management': ['cmd_server_list', 'cmd_server_add', 'cmd_server_remove', 'cmd_server_enable', 'cmd_server_disable',
'cmd_server_priority', 'cmd_server_test', 'cmd_server_ipmode', 'cmd_server_autoconfig'],
    'Server Helpers': ['_generate_server_name'],
    'Mirror Management': ['cmd_mirror_status', 'cmd_mirror_enable', 'cmd_mirror_disable', 'cmd_mirror_quota', 'cmd_mirror_disable_version',
'cmd_mirror_enable_version', 'cmd_mirror_clean', 'cmd_mirror_sync', 'cmd_mirror_ratelimit'],
    'Cache Management': ['cmd_cache_info', 'cmd_cache_clean', 'cmd_cache_rebuild', 'cmd_cache_stats', 'cmd_cache_rebuild_fts'],
    'History/Transactions': ['cmd_history', 'cmd_undo', 'cmd_rollback'],
    'Dependency Analysis': ['cmd_depends', 'cmd_rdepends', 'cmd_recommends', 'cmd_whatrecommends', 'cmd_suggests', 'cmd_whatsuggests',
'cmd_why'],
    'Dependency Helpers': ['_get_rdeps', '_build_rdeps_graph', '_build_installed_reachable_set', '_print_rdep_tree', '_handle_bloc_choices',
 '_get_bloc_label', '_ask_secondary_choice', '_resolve_for_tree', '_print_dep_tree_from_resolution', '_print_dep_tree_from_graph',
'_print_dep_tree_packages', '_print_dep_tree_legacy', '_is_virtual_provide'],
    'Build/Container': ['cmd_mkimage', 'cmd_build', '_cleanup_chroot_for_image', '_find_workspace', '_build_single_package'],
    'Package Listing': ['cmd_list'],
    'Configuration': ['cmd_config', 'cmd_key'],
    'P2P': ['cmd_peer', '_query_daemon_peers'],
    'AppStream': ['cmd_appstream'],
    'Install Helpers': ['_extract_pkg_name', '_extract_family', '_get_installed_families', '_resolve_virtual_package', '_extract_version',
'_group_by_version', '_check_preferences_compatibility', '_add_preferences_to_choices', '_resolve_with_alternatives', '_create_resolver'],
    'Autoremove Helpers': ['_get_running_kernel', '_get_root_fstype', '_get_blacklist', '_get_redlist', '_read_config', '_write_config',
'_get_user_blacklist', '_get_user_redlist', '_get_kernel_keep', '_is_running_kernel', '_find_old_kernels', '_find_faildeps'],
    'Init': ['cmd_init'],
    'Other': ['cmd_not_implemented', '_parse_date']
}

# Calculate ranges for each group
print("=" * 90)
print("FUNCTIONAL GROUPS - LINE RANGES")
print("=" * 90)

for group_name, func_list in groups.items():
    func_list = [f for f in func_list if f in funcs]
    if not func_list:
        continue

    start_line = min(funcs[f] for f in func_list)
    end_line = max(funcs[f] for f in func_list)

    # Estimate size based on next function or EOF
    next_line = 14137
    for fname, fline in sorted_funcs:
        if fline > end_line and fline < next_line:
            next_line = fline

    estimated_size = next_line - start_line

    print(f"\n{group_name}:")
    print(f"  Line range: {start_line} - {end_line}")
    print(f"  Est. size: ~{estimated_size} lines")
    print(f"  Functions: {', '.join(func_list[:5])}" + ("..." if len(func_list) > 5 else ""))

print("\n" + "=" * 90)
