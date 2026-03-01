"""Package cleanup commands: autoremove, mark, hold, unhold, cleandeps."""

from typing import TYPE_CHECKING

from ...i18n import _, ngettext, confirm_yes
if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.kernel import (
    get_blacklist as _get_blacklist,
    get_redlist as _get_redlist,
    find_old_kernels as _find_old_kernels,
    find_faildeps as _find_faildeps,
)
from ..helpers.package import extract_pkg_name as _extract_pkg_name


def cmd_autoremove(args, db: 'PackageDatabase') -> int:
    """Handle autoremove command - unified cleanup."""
    import platform
    import signal

    from .. import colors
    from ...core.resolver import Resolver, format_size
    from ...core.install import check_root
    from ...core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from ...core.transaction_queue import TransactionQueue

    # Check for previous background errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(_("Warning: Previous background operation had an error:")))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim(_("  (This message will not appear again)")))
        clear_background_error()

    # Determine which selectors are active
    do_orphans = getattr(args, 'orphans', False)
    do_kernels = getattr(args, 'kernels', False)
    do_faildeps = getattr(args, 'faildeps', False)
    do_all = getattr(args, 'all', False)

    # --all enables everything
    if do_all:
        do_orphans = do_kernels = do_faildeps = True

    # Default to --orphans if no selector specified
    if not (do_orphans or do_kernels or do_faildeps):
        do_orphans = True

    arch = platform.machine()
    resolver = Resolver(db, arch=arch)

    # Collect packages to remove from each selector
    packages_to_remove = []  # List of (name, nevra, size, reason)
    faildeps_trans_ids = []  # Transaction IDs to mark as cleaned

    # --orphans: orphaned packages
    if do_orphans:
        print(_("Searching for orphaned packages..."))
        orphans = resolver.find_all_orphans()
        for o in orphans:
            packages_to_remove.append((o.name, o.nevra, o.size, 'orphan'))
        if orphans:
            print("  " + ngettext(
                "Found {count} orphaned package",
                "Found {count} orphaned packages",
                len(orphans)).format(count=colors.warning(str(len(orphans)))))
        else:
            print(colors.success(_("  No orphaned packages found")))

    # --kernels: old kernels
    if do_kernels:
        print(_("Searching for old kernels..."))
        old_kernels = _find_old_kernels(keep_count=2)
        for name, nevra, size in old_kernels:
            packages_to_remove.append((name, nevra, size, 'old-kernel'))
        if old_kernels:
            print("  " + ngettext(
                "Found {count} old kernel package",
                "Found {count} old kernel packages",
                len(old_kernels)).format(count=colors.warning(str(len(old_kernels)))))
        else:
            print(colors.success(_("  No old kernels found")))

    # --faildeps: orphan deps from interrupted transactions
    if do_faildeps:
        print(_("Searching for failed dependencies..."))
        faildeps, faildeps_trans_ids = _find_faildeps(db)
        for name, nevra in faildeps:
            packages_to_remove.append((name, nevra, 0, 'faildep'))
        if faildeps:
            print("  " + ngettext(
                "Found {count} failed dependency package",
                "Found {count} failed dependency packages",
                len(faildeps)).format(count=colors.warning(str(len(faildeps)))))
        else:
            print(colors.success(_("  No failed dependencies found")))

    # Remove duplicates (keep first occurrence)
    seen = set()
    unique_packages = []
    for name, nevra, size, reason in packages_to_remove:
        if name not in seen:
            seen.add(name)
            unique_packages.append((name, nevra, size, reason))
    packages_to_remove = unique_packages

    if not packages_to_remove:
        print(colors.success(_("\nNothing to remove.")))
        return 0

    # Apply blacklist and redlist protection
    blacklist = _get_blacklist()
    redlist = _get_redlist()

    blocked = []
    warned = []
    safe = []

    for pkg in packages_to_remove:
        name = pkg[0]
        if name in blacklist:
            blocked.append(pkg)
        elif name in redlist:
            warned.append(pkg)
        else:
            safe.append(pkg)

    # Report blocked packages
    if blocked:
        print("\n  " + colors.error(_("BLOCKED ({count})").format(count=len(blocked))) + " - " + _("critical system packages:"))
        for name, nevra, _s, _r in blocked:
            print(f"    {colors.error(nevra)}")
        print(colors.error(_("  These packages cannot be removed (system would be unusable)")))

    # Handle warned packages
    if warned and not getattr(args, 'auto', False):
        print("\n  " + colors.warning(_("WARNING ({count})").format(count=len(warned))) + " - " + _("generally useful packages:"))
        for name, nevra, _s, _r in warned:
            print(f"    {colors.warning(nevra)}")
        try:
            response = input(_("\n  Remove these warned packages anyway? [y/N] "))
            if confirm_yes(response):
                safe.extend(warned)
            else:
                print(_("  Warned packages will be kept"))
        except (KeyboardInterrupt, EOFError):
            print(_("\n  Warned packages will be kept"))

    packages_to_remove = safe

    if not packages_to_remove:
        print(colors.success(_("\nNothing safe to remove.")))
        return 0

    # Display summary
    total_size = sum(size for _n, _v, size, _r in packages_to_remove)
    print("\n" + colors.bold(ngettext(
        "The following package will be removed:",
        "The following {count} packages will be removed:",
        len(packages_to_remove)).format(count=len(packages_to_remove))))
    from .. import display

    # Group by reason for display
    by_reason = {}
    for name, nevra, size, reason in packages_to_remove:
        if reason not in by_reason:
            by_reason[reason] = []
        by_reason[reason].append(nevra)

    reason_labels = {
        'orphan': _('Orphaned packages'),
        'old-kernel': _('Old kernels'),
        'faildep': _('Failed dependencies'),
    }

    for reason, nevras in by_reason.items():
        label = reason_labels.get(reason, reason)
        print("\n  " + colors.error(_("{label} ({count}):").format(label=label, count=len(nevras))))
        display.print_package_list(sorted(nevras), indent=4, color_func=colors.error)

    print("\n" + _("Disk space to free: {size}").format(size=format_size(total_size)))

    # Confirmation
    if not getattr(args, 'auto', False):
        try:
            response = input(_("\nRemove these packages? [y/N] "))
            if not confirm_yes(response):
                print(_("Aborted."))
                return 0
        except (KeyboardInterrupt, EOFError):
            print(_("\nAborted."))
            return 130

    # Check root
    if not check_root():
        print(colors.error(_("Error: autoremove requires root privileges")))
        return 1

    # Build command line for history
    cmd_parts = ["urpm", "autoremove"]
    if do_orphans and not do_all:
        cmd_parts.append("--orphans")
    if do_kernels and not do_all:
        cmd_parts.append("--kernels")
    if do_faildeps and not do_all:
        cmd_parts.append("--faildeps")
    if do_all:
        cmd_parts.append("--all")
    cmd_line = " ".join(cmd_parts)

    transaction_id = db.begin_transaction('autoremove', cmd_line)

    # Record packages
    for name, nevra, size, reason in packages_to_remove:
        db.record_package(transaction_id, nevra, name, 'remove', reason)

    # Setup interrupt handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            print(_("\n\nForce abort!"))
            db.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print(_("\n\nInterrupt requested - finishing current package..."))

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        print("\n" + ngettext(
            "Removing {count} package...",
            "Removing {count} packages...",
            len(packages_to_remove)).format(count=len(packages_to_remove)))
        package_names = [name for name, _v, _s, _r in packages_to_remove]

        # Check if another operation is in progress
        lock = InstallLock()
        if not lock.acquire(blocking=False):
            print(colors.warning(_("  RPM database is locked by another process.")))
            print(colors.dim(_("  Waiting for lock... (Ctrl+C to cancel)")))
            lock.acquire(blocking=True)
        lock.release()  # Release - child will acquire its own lock

        last_erase_shown = [None]

        # Build transaction queue
        from ...core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        queue = TransactionQueue(root=rpm_root or "/")
        queue.add_erase(package_names, operation_id="autoremove")

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_erase_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_erase_shown[0] = name

        # Execute the queue
        sync_mode = getattr(args, 'sync', False)
        queue_result = queue.execute(progress_callback=queue_progress, sync=sync_mode)

        # Print done
        print(f"\r\033[K  [{len(package_names)}/{len(package_names)}] done")

        if not queue_result.success:
            print(colors.error("\n" + _("Removal failed:")))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning("\n  " + _("Autoremove interrupted")))
            db.abort_transaction(transaction_id)
            return 130

        removed_count = queue_result.operations[0].count if queue_result.operations else len(package_names)
        print(colors.success("  " + ngettext(
            "{count} package removed",
            "{count} packages removed",
            removed_count).format(count=removed_count)))

        # Mark faildeps transactions as cleaned
        if faildeps_trans_ids:
            for tid in faildeps_trans_ids:
                db.conn.execute(
                    "UPDATE history SET status = 'cleaned' WHERE id = ?",
                    (tid,)
                )
            db.conn.commit()

        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        resolver.unmark_packages(package_names)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_mark(args, db: 'PackageDatabase') -> int:
    """Handle mark command - mark packages as manual or auto-installed."""
    from .. import colors
    from ...core.resolver import Resolver

    resolver = Resolver(db)

    if args.mark_command in ('manual', 'm', 'explicit'):
        # Mark packages as explicitly installed (remove from unrequested)
        packages = args.packages
        unrequested = resolver._get_unrequested_packages()

        marked = []
        already_manual = []
        not_installed = []

        # Check which packages are installed
        try:
            import rpm
            ts = rpm.TransactionSet()
            installed = set()
            for hdr in ts.dbMatch():
                installed.add(hdr[rpm.RPMTAG_NAME].lower())
        except ImportError:
            print(colors.error(_("Error: rpm module not available")))
            return 1

        for pkg in packages:
            pkg_lower = pkg.lower()
            if pkg_lower not in installed:
                not_installed.append(pkg)
            elif pkg_lower not in unrequested:
                already_manual.append(pkg)
            else:
                marked.append(pkg)

        if not_installed:
            print(colors.warning(_("Not installed: {packages}").format(packages=', '.join(not_installed))))

        if already_manual:
            print(_("Already manual: {packages}").format(packages=', '.join(already_manual)))

        if marked:
            resolver.mark_as_explicit(marked)
            print(colors.success(_("Marked as manual: {packages}").format(packages=', '.join(marked))))
            print(_("These packages are now protected from autoremove."))

        return 0 if marked or already_manual else 1

    elif args.mark_command in ('auto', 'a', 'dep'):
        # Mark packages as auto-installed (add to unrequested)
        packages = args.packages
        unrequested = resolver._get_unrequested_packages()

        marked = []
        already_auto = []
        not_installed = []

        # Check which packages are installed
        try:
            import rpm
            ts = rpm.TransactionSet()
            installed = set()
            for hdr in ts.dbMatch():
                installed.add(hdr[rpm.RPMTAG_NAME].lower())
        except ImportError:
            print(colors.error(_("Error: rpm module not available")))
            return 1

        for pkg in packages:
            pkg_lower = pkg.lower()
            if pkg_lower not in installed:
                not_installed.append(pkg)
            elif pkg_lower in unrequested:
                already_auto.append(pkg)
            else:
                marked.append(pkg)

        if not_installed:
            print(colors.warning(_("Not installed: {packages}").format(packages=', '.join(not_installed))))

        if already_auto:
            print(_("Already auto: {packages}").format(packages=', '.join(already_auto)))

        if marked:
            resolver.mark_as_dependency(marked)
            print(colors.success(_("Marked as auto: {packages}").format(packages=', '.join(marked))))
            print(_("These packages can now be autoremoved if no longer needed."))

        return 0 if marked or already_auto else 1

    elif args.mark_command in ('show', 's', 'list'):
        # Show install reason for packages
        unrequested = resolver._get_unrequested_packages()

        try:
            import rpm
            ts = rpm.TransactionSet()
            installed = {}
            for hdr in ts.dbMatch():
                name = hdr[rpm.RPMTAG_NAME]
                installed[name.lower()] = name
        except ImportError:
            print(colors.error(_("Error: rpm module not available")))
            return 1

        packages = args.packages if args.packages else sorted(installed.keys())

        manual_count = 0
        auto_count = 0

        for pkg in packages:
            pkg_lower = pkg.lower()
            if pkg_lower not in installed:
                print(f"{pkg}: {colors.warning(_('not installed'))}")
            elif pkg_lower in unrequested:
                print(f"{installed[pkg_lower]}: {colors.info(_('auto'))}")
                auto_count += 1
            else:
                print(f"{installed[pkg_lower]}: {colors.success(_('manual'))}")
                manual_count += 1

        if not args.packages:
            print("\n" + _("Total: {manual} manual, {auto} auto").format(manual=manual_count, auto=auto_count))

        return 0

    else:
        print(_("Usage: urpm mark <manual|auto|show> [packages...]"))
        return 1


def cmd_hold(args, db: 'PackageDatabase') -> int:
    """Handle hold command - hold packages to prevent upgrades and obsoletes."""
    from .. import colors
    from datetime import datetime

    # List holds if no packages or --list
    if args.list_holds or not args.packages:
        holds = db.list_holds()
        if not holds:
            print(_("No packages are held."))
            return 0

        print(_("Held packages ({count}):").format(count=len(holds)) + "\n")
        for hold in holds:
            ts = datetime.fromtimestamp(hold['added_timestamp'])
            reason = f" - {hold['reason']}" if hold['reason'] else ""
            print(f"  {colors.warning(hold['package_name'])}{reason}")
            print("    " + _("(held since {date})").format(date=ts.strftime('%Y-%m-%d %H:%M')))
        return 0

    # Hold packages
    added = []
    already_held = []

    for pkg in args.packages:
        if db.add_hold(pkg, args.reason):
            added.append(pkg)
        else:
            already_held.append(pkg)

    if already_held:
        print(_("Already held: {packages}").format(packages=', '.join(already_held)))

    if added:
        print(colors.success(_("Held: {packages}").format(packages=', '.join(added))))
        print(_("These packages will be protected from upgrades and obsoletes replacement."))

    return 0 if added or already_held else 1


def cmd_unhold(args, db: 'PackageDatabase') -> int:
    """Handle unhold command - remove hold from packages."""
    from .. import colors

    removed = []
    not_held = []

    for pkg in args.packages:
        if db.remove_hold(pkg):
            removed.append(pkg)
        else:
            not_held.append(pkg)

    if not_held:
        print(_("Not held: {packages}").format(packages=', '.join(not_held)))

    if removed:
        print(colors.success(_("Unheld: {packages}").format(packages=', '.join(removed))))
        print(_("These packages can now be upgraded and replaced by obsoletes."))

    return 0 if removed else 1


def cmd_cleandeps(args, db: 'PackageDatabase') -> int:
    """Handle cleandeps command - remove orphan deps from interrupted transactions."""
    import signal
    import platform
    from ...core.install import check_root
    from ...core.resolver import Resolver
    from ...core.transaction_queue import TransactionQueue
    from ...core.background_install import InstallLock
    from .. import colors

    interrupted = db.get_interrupted_transactions()

    if not interrupted:
        print(_("No interrupted transactions found"))
        return 0

    # Collect all orphan deps
    all_orphans = []
    interrupted_ids = []
    for trans in interrupted:
        orphans = db.get_orphan_deps(trans['id'])
        if orphans:
            all_orphans.extend(orphans)
            interrupted_ids.append(trans['id'])

    if not all_orphans:
        print(_("No orphan dependencies to clean"))
        return 0

    # Remove duplicates while preserving order
    seen = set()
    unique_orphans = []
    for nevra in all_orphans:
        if nevra not in seen:
            seen.add(nevra)
            unique_orphans.append(nevra)
    all_orphans = unique_orphans

    print("\n" + _("Found {count} orphan dependencies from {trans} interrupted transaction(s):").format(
        count=len(all_orphans), trans=len(interrupted)))
    from .. import display
    display.print_package_list(all_orphans, max_lines=10)

    if not args.auto:
        try:
            answer = input(_("\nRemove these packages? [y/N] "))
            if not confirm_yes(answer):
                print(_("Aborted"))
                return 1
        except EOFError:
            print(_("\nAborted"))
            return 1

    # Check root
    if not check_root():
        print(_("Error: cleandeps requires root privileges"))
        return 1

    # Record transaction
    cmd_line = 'urpm cleandeps'
    transaction_id = db.begin_transaction('cleandeps', cmd_line)

    # Setup Ctrl+C handler
    interrupted_flag = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted_flag[0]:
            print(_("\n\nForce abort!"))
            db.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted_flag[0] = True
            print(_("\n\nInterrupt requested - finishing current package..."))

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # Extract package names from NEVRAs for removal
        packages_to_erase = []
        for nevra in all_orphans:
            # Extract name from nevra (e.g., "foo-1.0-1.mga9.x86_64" -> "foo")
            name = _extract_pkg_name(nevra)
            packages_to_erase.append(name)
            # Record in transaction
            db.record_package(transaction_id, nevra, name, 'remove', 'cleandeps')

        # Check if another operation is in progress
        lock = InstallLock()
        if not lock.acquire(blocking=False):
            print(colors.warning(_("  RPM database is locked by another process.")))
            print(colors.dim(_("  Waiting for lock... (Ctrl+C to cancel)")))
            lock.acquire(blocking=True)
        lock.release()  # Release - child will acquire its own lock

        # Erase packages
        print("\n" + ngettext(
            "Erasing {count} orphan dependency...",
            "Erasing {count} orphan dependencies...",
            len(packages_to_erase)).format(count=len(packages_to_erase)))

        last_erase_shown = [None]

        # Build transaction queue
        from ...core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        queue = TransactionQueue(root=rpm_root or "/")
        queue.add_erase(packages_to_erase, operation_id="cleandeps")

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_erase_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_erase_shown[0] = name

        # Execute the queue
        sync_mode = getattr(args, 'sync', False)
        queue_result = queue.execute(progress_callback=queue_progress, sync=sync_mode)

        # Print done
        print(f"\r\033[K  [{len(packages_to_erase)}/{len(packages_to_erase)}] done")

        if not queue_result.success:
            print(colors.error("\n" + _("Erase failed:")))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:5]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            db.abort_transaction(transaction_id)
            return 1

        if interrupted_flag[0]:
            erased_count = queue_result.operations[0].count if queue_result.operations else 0
            print(colors.warning("\n  " + _("Erase interrupted after {count} packages").format(count=erased_count)))
            db.abort_transaction(transaction_id)
            return 130

        erased_count = queue_result.operations[0].count if queue_result.operations else len(packages_to_erase)
        print(colors.success("  " + ngettext(
            "{count} package erased",
            "{count} packages erased",
            erased_count).format(count=erased_count)))

        # Mark interrupted transactions as cleaned
        for tid in interrupted_ids:
            db.conn.execute(
                "UPDATE history SET status = 'cleaned' WHERE id = ?",
                (tid,)
            )
        db.conn.commit()

        db.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        if packages_to_erase:
            arch = platform.machine()
            resolver = Resolver(db, arch=arch)
            resolver.unmark_packages(packages_to_erase)

        return 0

    except Exception as e:
        db.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)
