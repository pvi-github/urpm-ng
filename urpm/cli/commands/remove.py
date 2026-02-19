"""Package removal command."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.package import extract_pkg_name as _extract_pkg_name
from ..helpers.debug import (
    DEBUG_LAST_REMOVED_DEPS,
    DEBUG_PREV_INSTALLED_DEPS,
    write_debug_file as _write_debug_file,
    clear_debug_file as _clear_debug_file,
    copy_installed_deps_list as _copy_installed_deps_list,
)
from ..helpers.resolver import create_resolver as _create_resolver


def cmd_erase(args, db: 'PackageDatabase') -> int:
    """Handle erase (remove) command."""
    import platform
    import signal

    from ...core.resolver import Resolver, format_size, set_solver_debug
    from ...core.install import check_root
    from ...core.operations import PackageOperations, InstallOptions
    from ...core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from .. import colors

    # Check for previous background errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # If --auto-orphans without packages, delegate to cmd_autoremove (urpme compat)
    clean_deps = getattr(args, 'auto_orphans', False)
    if clean_deps and not args.packages:
        from ..main import cmd_autoremove
        return cmd_autoremove(args, db)

    # Must have packages if not --auto-orphans
    if not args.packages:
        print(colors.error("Error: no packages specified"))
        print(colors.dim("  Use --auto-orphans to remove orphan dependencies"))
        return 1

    # Debug: save previous state and clear debug files at start
    _copy_installed_deps_list(dest=DEBUG_PREV_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_REMOVED_DEPS)

    # Check root
    if not check_root():
        print(colors.error("Error: erase requires root privileges"))
        return 1

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    if debug_solver:
        set_solver_debug(enabled=True)

    # Resolve what to remove
    resolver = _create_resolver(db, args)
    result = resolver.resolve_remove(args.packages, clean_deps=False)

    if not result.success:
        print(colors.error("Resolution failed:"))
        for prob in result.problems:
            print(f"  {colors.error(prob)}")
        return 1

    if not result.actions:
        print(colors.info("Nothing to erase."))
        return 0

    # Separate explicit requests from reverse dependencies
    explicit_names = set()
    for pkg in args.packages:
        pkg_name = _extract_pkg_name(pkg).lower()
        explicit_names.add(pkg_name)

    # Also include packages that provide what the user requested
    for action in result.actions:
        pkg_info = db.get_package(action.name)
        if pkg_info and pkg_info.get('provides'):
            for prov in pkg_info['provides']:
                prov_name = prov.split('[')[0].split('=')[0].split('<')[0].split('>')[0].strip().lower()
                if prov_name in explicit_names:
                    explicit_names.add(action.name.lower())
                    break

    explicit = [a for a in result.actions if a.name.lower() in explicit_names]
    deps = [a for a in result.actions if a.name.lower() not in explicit_names]

    # Find orphaned dependencies unless --keep-orphans
    keep_orphans = getattr(args, 'keep_orphans', False)
    orphans = []
    include_orphans = False

    if not keep_orphans:
        erase_names = [a.name for a in result.actions]
        orphans = resolver.find_erase_orphans(
            erase_names,
            erase_recommends=getattr(args, 'erase_recommends', False),
            keep_suggests=getattr(args, 'keep_suggests', False)
        )

    all_actions = list(result.actions)
    total_size = result.remove_size

    # Show what will be erased (without orphans first)
    print(f"\n{colors.bold(f'The following {len(all_actions)} package(s) will be erased:')}")
    from .. import display

    if explicit:
        print(f"\n  {colors.info(f'Requested ({len(explicit)}):')}")
        pkg_names = [a.nevra for a in explicit]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)

    if deps:
        print(f"\n  {colors.warning(f'Reverse dependencies ({len(deps)}):')}")
        pkg_names = [a.nevra for a in deps]
        display.print_package_list(pkg_names, indent=4, color_func=colors.warning)

    # Handle orphans: ask or auto-include
    if orphans:
        # Determine if we should auto-include orphans
        # --auto-orphans OR (--auto AND NOT --keep-orphans)
        auto_include_orphans = clean_deps or (args.auto and not keep_orphans)

        if auto_include_orphans:
            # Include orphans automatically
            include_orphans = True
            print(f"\n  {colors.warning(f'Orphaned dependencies ({len(orphans)}):')}")
            pkg_names = [a.nevra for a in orphans]
            display.print_package_list(pkg_names, indent=4, color_func=colors.warning)
        else:
            # Ask user about orphans
            print(f"\n  {colors.dim(f'Orphaned dependencies that could be removed ({len(orphans)}):')}")
            pkg_names = [a.nevra for a in orphans]
            display.print_package_list(pkg_names, indent=4, color_func=colors.dim)
            try:
                response = input(f"\n  Also remove these {len(orphans)} orphaned packages? [y/N] ")
                include_orphans = response.lower() in ('y', 'yes')
                if include_orphans:
                    print(colors.success("  Orphans will be removed"))
                else:
                    print(colors.dim("  Orphans will be kept"))
            except (KeyboardInterrupt, EOFError):
                print("\n  Orphans will be kept")
                include_orphans = False

    # Add orphans to the removal if confirmed
    if include_orphans and orphans:
        all_actions = all_actions + orphans
        for o in orphans:
            total_size += o.size

    if total_size > 0:
        print(f"\nDisk space freed: {colors.success(format_size(total_size))}")

    # Confirmation
    if not args.auto:
        try:
            response = input("\nProceed with removal? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted.")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130

    if args.test:
        print("\n(dry run - no changes made)")
        return 0

    # Set correct reasons on actions for transaction history
    from ...core.resolver import InstallReason
    for action in all_actions:
        if action.name.lower() in explicit_names:
            action.reason = InstallReason.EXPLICIT
        else:
            action.reason = InstallReason.DEPENDENCY

    # Record transaction
    ops = PackageOperations(db)
    cmd_line = ' '.join(['urpm', 'erase'] + args.packages)
    transaction_id = ops.begin_transaction('erase', cmd_line, all_actions)

    # Setup Ctrl+C handler
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
            print("Press Ctrl+C again to force abort (may leave system inconsistent)")

    signal.signal(signal.SIGINT, sigint_handler)

    # Erase packages (all from resolution, including reverse deps and orphans)
    print(colors.info(f"\nErasing {len(all_actions)} packages..."))
    packages_to_erase = [action.name for action in all_actions]

    # Check if another operation is in progress
    lock = InstallLock()
    if not lock.acquire(blocking=False):
        print(colors.warning("  RPM database is locked by another process."))
        print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
        lock.acquire(blocking=True)
    lock.release()  # Release - child will acquire its own lock

    last_erase_shown = [None]

    try:
        from ...core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        erase_opts = InstallOptions(
            force=getattr(args, 'force', False),
            test=getattr(args, 'test', False),
            root=rpm_root or "/",
            sync=getattr(args, 'sync', False),
        )

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_erase_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_erase_shown[0] = name

        queue_result = ops.execute_erase(
            packages_to_erase, options=erase_opts, progress_callback=queue_progress
        )

        # Print done
        print(f"\r\033[K  [{len(packages_to_erase)}/{len(packages_to_erase)}] done")

        if not queue_result.success:
            print(colors.error(f"\nErase failed:"))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            if not erase_opts.force:
                print(colors.dim("  Use --force to ignore dependency problems"))
            ops.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Erase interrupted"))
            ops.abort_transaction(transaction_id)
            return 130

        erased_count = queue_result.operations[0].count if queue_result.operations else len(packages_to_erase)
        print(colors.success(f"  {erased_count} packages erased"))
        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        erased_packages = [action.name for action in all_actions]
        resolver.unmark_packages(erased_packages)

        # Debug: write orphans that were removed
        orphan_names = [o.name for o in orphans]
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


