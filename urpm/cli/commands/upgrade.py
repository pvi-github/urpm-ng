"""Package upgrade command."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.debug import (
    DEBUG_LAST_INSTALLED_DEPS,
    DEBUG_LAST_REMOVED_DEPS,
    DEBUG_PREV_INSTALLED_DEPS,
    write_debug_file as _write_debug_file,
    clear_debug_file as _clear_debug_file,
    copy_installed_deps_list as _copy_installed_deps_list,
)
from ..helpers.resolver import create_resolver as _create_resolver


def cmd_upgrade(args, db: 'PackageDatabase') -> int:
    """Handle upgrade command - upgrade packages."""
    import signal
    import time

    from .. import colors
    from ...core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from ...core.operations import PackageOperations, InstallOptions

    # Check for previous background install errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # Debug: save previous state and clear debug files at start
    _copy_installed_deps_list(dest=DEBUG_PREV_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_REMOVED_DEPS)

    from ...core.resolver import format_size, set_solver_debug
    from ...core.install import check_root
    from pathlib import Path
    from ...core.rpm import is_local_rpm, read_rpm_header
    from ...core.download import verify_rpm_signature

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    watched_pkgs = getattr(args, 'watched', None)
    if watched_pkgs:
        watched_pkgs = [p.strip() for p in watched_pkgs.split(',')]
    if debug_solver or watched_pkgs:
        set_solver_debug(enabled=debug_solver, watched=watched_pkgs)

    # Determine what to upgrade
    packages = getattr(args, 'packages', []) or []
    # No packages = full system upgrade (apt-style: urpm upgrade = upgrade all)
    upgrade_all = not packages

    # Separate local RPM files from package names
    local_rpm_paths = []
    local_rpm_infos = []
    package_names = []
    verify_sigs = not getattr(args, 'nosignature', False)

    for pkg in packages:
        if is_local_rpm(pkg):
            path = Path(pkg)
            if not path.exists():
                print(colors.error(f"Error: file not found: {pkg}"))
                return 1
            # Read RPM header
            info = read_rpm_header(path)
            if not info:
                print(colors.error(f"Error: cannot read RPM file: {pkg}"))
                return 1
            # Verify signature
            if verify_sigs:
                valid, error = verify_rpm_signature(path)
                if not valid:
                    print(colors.error(f"Error: signature verification failed for {pkg}"))
                    print(colors.error(f"  {error}"))
                    print(colors.dim("  Use --nosignature to skip verification (not recommended)"))
                    return 1
            local_rpm_paths.append(str(path.resolve()))
            local_rpm_infos.append(info)
        else:
            package_names.append(pkg)

    # If we have local RPMs, show what we're upgrading
    if local_rpm_infos:
        print(f"Local RPM files ({len(local_rpm_infos)}):")
        for info in local_rpm_infos:
            print(f"  {info['nevra']}")

    # Check root
    if not check_root():
        print(colors.error("Error: upgrade requires root privileges"))
        return 1

    # Resolve upgrades
    # For upgrades, don't install recommends by default (unlike install)
    # This matches urpmi --auto-update behavior
    install_recommends = getattr(args, 'with_recommends', False)
    resolver = _create_resolver(db, args, install_recommends=install_recommends)

    # Add local RPMs to resolver pool before resolution
    if local_rpm_infos:
        resolver.add_local_rpms(local_rpm_infos)
        # Add local package names to the list
        for info in local_rpm_infos:
            package_names.append(info['name'])

    if upgrade_all:
        print("Resolving system upgrade...")
        result = resolver.resolve_upgrade()
    else:
        print(f"Resolving upgrade for: {', '.join(package_names)}")
        # Build set of local package names for special handling
        local_pkg_names = {info['name'] for info in local_rpm_infos}
        result = resolver.resolve_upgrade(package_names, local_packages=local_pkg_names)

    if not result.success:
        print(colors.error("Resolution failed:"))
        for prob in result.problems:
            print(f"  {colors.error(prob)}")
        return 1

    # Show warnings for held packages
    held_count = 0
    if hasattr(resolver, '_held_upgrade_warnings') and resolver._held_upgrade_warnings:
        held_count += len(resolver._held_upgrade_warnings)
    if hasattr(resolver, '_held_obsolete_warnings') and resolver._held_obsolete_warnings:
        held_count += len(resolver._held_obsolete_warnings)

    if held_count > 0:
        print(colors.warning(f"\nHeld packages ({held_count}) skipped:"))
        if hasattr(resolver, '_held_upgrade_warnings') and resolver._held_upgrade_warnings:
            for held_pkg in resolver._held_upgrade_warnings:
                print(f"  {colors.warning(held_pkg)} (upgrade skipped)")
        if hasattr(resolver, '_held_obsolete_warnings') and resolver._held_obsolete_warnings:
            for held_pkg, obsoleting_pkg in resolver._held_obsolete_warnings:
                print(f"  {colors.warning(held_pkg)} (would be obsoleted by {obsoleting_pkg})")
        print(f"\n  Use '{colors.dim('urpm unhold <package>')}' to allow changes.")

    if not result.actions:
        print(colors.success("All packages are up to date."))
        return 0

    # Categorize actions
    upgrades = [a for a in result.actions if a.action.value == 'upgrade']
    installs = [a for a in result.actions if a.action.value == 'install']
    removes = [a for a in result.actions if a.action.value == 'remove']
    downgrades = [a for a in result.actions if a.action.value == 'downgrade']

    # Find orphaned dependencies (unless --noerase-orphans)
    # Exclude packages already in removes to avoid duplicates
    orphans = []
    if upgrades and not getattr(args, 'noerase_orphans', False):
        removes_names = {a.name for a in removes}
        orphans = [o for o in resolver.find_upgrade_orphans(upgrades)
                   if o.name not in removes_names]

    # Show packages by category
    from .. import colors, display
    print(f"\n{colors.bold('Transaction summary:')}")
    if upgrades:
        print(f"\n  {colors.info(f'Upgrade ({len(upgrades)}):')}")
        pkg_names = [a.nevra for a in sorted(upgrades, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.info)
    if installs:
        print(f"\n  {colors.success(f'Install ({len(installs)}) - new dependencies:')}")
        pkg_names = [a.nevra for a in sorted(installs, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.success)
    if removes:
        print(f"\n  {colors.error(f'Remove ({len(removes)}) - obsoleted:')}")
        pkg_names = [a.nevra for a in sorted(removes, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)
    if downgrades:
        print(f"\n  {colors.warning(f'Downgrade ({len(downgrades)}):')}")
        pkg_names = [a.nevra for a in sorted(downgrades, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.warning)
    if orphans:
        print(f"\n  {colors.error(f'Remove ({len(orphans)}) - orphaned dependencies:')}")
        pkg_names = [a.nevra for a in sorted(orphans, key=lambda x: x.name.lower())]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)

    if result.install_size > 0:
        print(f"\nDownload size: {format_size(result.install_size)}")

    # Confirmation
    if not getattr(args, 'auto', False):
        try:
            response = input("\nProceed with upgrade? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    if getattr(args, 'test', False):
        print("\n(dry run - no changes made)")
        return 0

    # Build download items and download
    ops = PackageOperations(db)
    download_items, local_action_paths = ops.build_download_items(
        result.actions, resolver, local_rpm_infos
    )

    dl_results = []
    downloaded = 0

    if download_items:
        print(f"\nDownloading {len(download_items)} packages...")
        dl_opts = InstallOptions(
            use_peers=not getattr(args, 'no_peers', False),
            only_peers=getattr(args, 'only_peers', False),
        )

        # Multi-line progress display using DownloadProgressDisplay
        from .. import display
        progress_display = display.DownloadProgressDisplay(num_workers=4)

        def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                     item_bytes=None, item_total=None, slots_status=None):
            global_speed = 0.0
            if slots_status:
                for slot, prog in slots_status:
                    if prog is not None:
                        global_speed += prog.get_speed()
            progress_display.update(
                pkg_num, pkg_total, bytes_done, bytes_total,
                slots_status or [], global_speed
            )

        download_start = time.time()
        dl_results, downloaded, cached, peer_stats = ops.download_packages(
            download_items, options=dl_opts, progress_callback=progress,
            urpm_root=getattr(args, 'urpm_root', None)
        )
        download_elapsed = time.time() - download_start
        progress_display.finish()

        # Check failures
        failed = [r for r in dl_results if not r.success]
        if failed:
            print(colors.error(f"\n{len(failed)} download(s) failed:"))
            for r in failed[:5]:
                print(f"  {colors.error(r.item.name)}: {r.error}")
            return 1

        # Download summary with P2P stats and timing
        cache_str = colors.warning(str(cached)) if cached > 0 else colors.dim(str(cached))
        from_peers = peer_stats.get('from_peers', 0)
        from_upstream = peer_stats.get('from_upstream', 0)
        time_str = display.format_duration(download_elapsed)
        if from_peers > 0:
            print(f"  {colors.success(f'{downloaded} downloaded')} ({from_peers} from peers, {from_upstream} from mirrors), {cache_str} from cache in {time_str}")
        else:
            print(f"  {colors.success(f'{downloaded} downloaded')}, {cache_str} from cache in {time_str}")

        if downloaded > 0:
            PackageOperations.notify_urpmd_cache_invalidate()

    rpm_paths = [r.path for r in dl_results if r.success and r.path]
    rpm_paths.extend(local_action_paths)

    # Set correct reasons on actions for transaction history
    from ...core.resolver import InstallReason
    explicit_names = set(p.lower() for p in package_names) if package_names else set()
    for action in result.actions:
        if action.name.lower() in explicit_names or upgrade_all:
            action.reason = InstallReason.EXPLICIT
        else:
            action.reason = InstallReason.DEPENDENCY

    # Include orphans in transaction recording (with 'orphan' reason)
    all_record_actions = list(result.actions)
    orphan_names = [a.name for a in orphans] if orphans else []
    if orphans:
        for o in orphans:
            o.reason = 'orphan'
        all_record_actions.extend(orphans)

    # Record transaction
    if upgrade_all:
        cmd_line = "urpm upgrade"
    else:
        cmd_line = "urpm update " + " ".join(package_names)
    transaction_id = ops.begin_transaction('upgrade', cmd_line, all_record_actions)

    # Setup interrupt handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            print("\n\nForce abort!")
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        from ...core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        upgrade_opts = InstallOptions(
            verify_signatures=not getattr(args, 'nosignature', False),
            force=getattr(args, 'force', False),
            test=getattr(args, 'test', False),
            root=rpm_root or "/",
            sync=getattr(args, 'sync', False),
        )

        remove_names = [a.name for a in removes] if removes else []

        if not rpm_paths and not remove_names and not orphan_names:
            ops.complete_transaction(transaction_id)
            return 0

        # Check if another install is in progress
        lock = InstallLock()
        if not lock.acquire(blocking=False):
            print(colors.warning("  RPM database is locked by another process."))
            print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
            lock.acquire(blocking=True)
        lock.release()  # Release - child will acquire its own lock

        # Progress tracking
        last_shown = [None]
        current_phase = [""]

        def queue_progress(op_id: str, name: str, current: int, total: int):
            if current_phase[0] != op_id:
                current_phase[0] = op_id
                if op_id == "upgrade":
                    print(f"\nUpgrading {total} packages...")
                last_shown[0] = None
            if last_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_shown[0] = name

        queue_result = ops.execute_upgrade(
            rpm_paths,
            erase_names=remove_names,
            orphan_names=orphan_names or None,
            options=upgrade_opts,
            progress_callback=queue_progress,
        )

        # Clear the line after last progress
        print(f"\r\033[K", end='')

        if interrupted[0]:
            print(colors.warning(f"\n  Operation interrupted"))
            ops.abort_transaction(transaction_id)
            return 130

        # Process results for each operation
        upgrade_success = True

        for op_result in queue_result.operations:
            if op_result.operation_id == "upgrade":
                if op_result.success:
                    msg_parts = []
                    if op_result.count > 0:
                        msg_parts.append(f"{op_result.count} packages upgraded")
                    if remove_names:
                        msg_parts.append(f"{len(remove_names)} removed (obsoleted)")
                    if msg_parts:
                        print(colors.success(f"  {', '.join(msg_parts)}"))
                else:
                    upgrade_success = False
                    print(colors.error(f"\nUpgrade failed:"))
                    for err in op_result.errors[:3]:
                        print(f"  {colors.error(err)}")

        # Orphan cleanup runs in background - just display status
        if orphan_names:
            print(colors.dim(f"  {len(orphan_names)} orphaned packages being removed in background..."))
            resolver.unmark_packages(orphan_names)

        if not upgrade_success:
            ops.abort_transaction(transaction_id)
            return 1

        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        new_deps = [a.name for a in result.actions if a.action.value == 'install']
        if new_deps:
            resolver.mark_as_dependency(new_deps)
            _write_debug_file(DEBUG_LAST_INSTALLED_DEPS, new_deps)
        removed = [a.name for a in result.actions if a.action.value == 'remove']
        if removed:
            resolver.unmark_packages(removed)

        # Debug: write orphans that were removed
        if orphan_names:
            _write_debug_file(DEBUG_LAST_REMOVED_DEPS, orphan_names)

        # Debug: copy the installed-through-deps.list for inspection
        _copy_installed_deps_list()

        return 0

    except Exception as e:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


