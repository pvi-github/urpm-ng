"""Package removal command."""

from typing import TYPE_CHECKING
from ...i18n import _, ngettext, confirm_yes

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.package import extract_pkg_name as _extract_pkg_name
from ..helpers.resolver import create_resolver as _create_resolver


def cmd_erase(args, db: 'PackageDatabase') -> int:
    """Handle erase (remove) command."""
    import os
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
        print(colors.warning(_("Warning: Previous background operation had an error:")))
        print(colors.warning("  {error}").format(error=prev_error))
        print(colors.dim(_("  (This message will not appear again)")))
        clear_background_error()

    # If --auto-orphans without packages, delegate to cmd_autoremove (urpme compat)
    clean_deps = getattr(args, 'auto_orphans', False)
    if clean_deps and not args.packages:
        from ..main import cmd_autoremove
        return cmd_autoremove(args, db)

    # Must have packages if not --auto-orphans
    if not args.packages:
        print(colors.error(_("Error: no packages specified")))
        print(colors.dim(_("  Use --auto-orphans to remove orphan dependencies")))
        return 1

    # Check root (not required for chroot operations)
    allow_no_root = getattr(args, 'allow_no_root', False)
    if not allow_no_root and not check_root():
        print(colors.error(_("Error: erase requires root privileges")))
        return 1

    # Set up debug if requested
    _debug = getattr(args, 'debug', None) or ''
    _debug_parts = {d.strip() for d in _debug.split(',')} if _debug else set()
    if 'all' in _debug_parts:
        _debug_parts.update(('solver', 'tsrun'))
    if 'solver' in _debug_parts:
        set_solver_debug(enabled=True)
    if 'tsrun' in _debug_parts:
        from ...core.transaction_queue import set_tsrun_debug
        set_tsrun_debug(enabled=True)

    # Resolve what to remove
    resolver = _create_resolver(db, args)
    result = resolver.resolve_remove(args.packages, clean_deps=False)

    if not result.success:
        print(colors.error(_("Resolution failed:")))
        for prob in result.problems:
            print(f"  {colors.error(prob)}")
        return 1

    if not result.actions:
        print(colors.info(_("Nothing to erase.")))
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
    print("\n{msg}".format(msg=colors.bold(ngettext(
        "The following package will be erased:",
        "The following {count} packages will be erased:",
        len(all_actions)).format(count=len(all_actions)))))
    from .. import display

    if explicit:
        print("\n  {msg}".format(msg=colors.info(_("Requested ({count}):").format(count=len(explicit)))))
        pkg_names = [a.nevra for a in explicit]
        display.print_package_list(pkg_names, indent=4, color_func=colors.error)

    if deps:
        print("\n  {msg}".format(msg=colors.warning(_("Reverse dependencies ({count}):").format(count=len(deps)))))
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
            print("\n  {msg}".format(msg=colors.warning(_("Orphaned dependencies ({count}):").format(count=len(orphans)))))
            pkg_names = [a.nevra for a in orphans]
            display.print_package_list(pkg_names, indent=4, color_func=colors.warning)
        else:
            # Ask user about orphans
            print("\n  {msg}".format(msg=colors.dim(_("Orphaned dependencies that could be removed ({count}):").format(count=len(orphans)))))
            pkg_names = [a.nevra for a in orphans]
            display.print_package_list(pkg_names, indent=4, color_func=colors.dim)
            try:
                response = input("\n  " + ngettext(
                    "Also remove this orphaned package? [y/N] ",
                    "Also remove these {count} orphaned packages? [y/N] ",
                    len(orphans)).format(count=len(orphans)))
                include_orphans = confirm_yes(response)
                if include_orphans:
                    print(colors.success(_("  Orphans will be removed")))
                else:
                    print(colors.dim(_("  Orphans will be kept")))
            except (KeyboardInterrupt, EOFError):
                print("\n  " + _("Orphans will be kept"))
                include_orphans = False

    # Add orphans to the removal if confirmed
    if include_orphans and orphans:
        all_actions = all_actions + orphans
        for o in orphans:
            total_size += o.size

    if total_size > 0:
        print("\n" + _("Disk space freed: {size}").format(size=colors.success(format_size(total_size))))

    # Confirmation
    if not args.auto:
        try:
            response = input("\n" + _("Proceed with removal? [y/N] "))
            if not confirm_yes(response):
                print(_("Aborted."))
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\n" + _("Aborted."))
            return 130

    if args.test:
        print("\n" + _("(dry run - no changes made)"))
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
            print("\n\n" + _("Force abort!"))
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\n" + _("Interrupt requested - finishing current package..."))
            print(_("Press Ctrl+C again to force abort (may leave system inconsistent)"))

    signal.signal(signal.SIGINT, sigint_handler)

    # Erase packages (all from resolution, including reverse deps and orphans)
    packages_to_erase = [action.name for action in all_actions]
    _header_text = ngettext(
        "Erasing {count} package...",
        "Erasing {count} packages...",
        len(all_actions)).format(count=len(all_actions))
    print(colors.info("\n" + _header_text))

    # Check if another operation is in progress
    # Use root path for lock file when operating on chroot
    install_root = getattr(args, 'root', None) or getattr(args, 'urpm_root', None)
    lock = InstallLock(root=install_root)
    if not lock.acquire(blocking=False):
        print(colors.warning(_("  RPM database is locked by another process.")))
        print(colors.dim(_("  Waiting for lock... (Ctrl+C to cancel)")))
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
            use_userns=bool(getattr(args, 'allow_no_root', False) and rpm_root),
        )

        # Smart sync (default): parent returns when triggers start.
        # Full sync (--sync): parent waits for everything including triggers.
        full_sync = getattr(args, 'sync', False)

        from ...core.transaction_queue import TransactionProgress, TransactionPhase
        from ...core.triggers import describe_trigger

        try:
            _term_width = os.get_terminal_size().columns - 1
        except OSError:
            _term_width = 79

        _total = len(packages_to_erase)
        _dw = len(str(_total))
        _count_suffix_width = 1 + _dw + 1 + _dw + 1 + 4
        _bar_width = max(_term_width - _count_suffix_width - 2, 10)
        _progress_started = [False]

        last_erase_current = [None]

        def queue_progress(progress: TransactionProgress):
            """Two-line progress display for erase.

            Line 1: header (left) + package/trigger info (right-aligned)
            Line 2: [████░░░░░░░░░░░░░░░░░░░░░░░] XX/XX 100%
            """
            if progress.phase in (TransactionPhase.VERIFY,
                                  TransactionPhase.PREPARE):
                return

            done = progress.packages_done
            total = progress.packages_total
            name = progress.package_name

            if done == last_erase_current[0] and name == last_erase_shown[0]:
                return
            last_erase_current[0] = done
            last_erase_shown[0] = name

            pct = int(done * 100 / total) if total else 0

            if progress.phase == TransactionPhase.SCRIPT:
                info = describe_trigger(progress.script_name) if progress.script_name else name
            else:
                info = name

            # Truncate info so header + space + info fits in terminal width
            max_info = _term_width - len(_header_text) - 2
            if len(info) > max_info:
                info = info[:max_info - 1] + "…"

            padding = _term_width - len(_header_text) - len(info)
            header_line = f"{_header_text}{' ' * max(padding, 1)}{info}"
            if len(header_line) > _term_width:
                header_line = header_line[:_term_width]

            filled = int(_bar_width * pct / 100)
            count_suffix = f" {done:>{_dw}}/{total} {pct:>3}%"
            bar_line = f"[{'█' * filled}{'░' * (_bar_width - filled)}]{count_suffix}"
            if len(bar_line) > _term_width:
                bar_line = bar_line[:_term_width]

            if not _progress_started[0]:
                _progress_started[0] = True
            print(f"\033[A\r\033[K{header_line}\n\033[K{bar_line}",
                  end='', flush=True)

        queue_result = ops.execute_erase(
            packages_to_erase, options=erase_opts,
            full_sync=full_sync, progress_callback=queue_progress,
        )

        # Clear 2-line progress and print done
        print(f"\033[A\r\033[K{_header_text}\n\033[K  [{_total}/{_total}] " + _("done"))

        if not queue_result.success:
            print(colors.error("\n" + _("Erase failed:")))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            if not erase_opts.force:
                print(colors.dim(_("  Use --force to ignore dependency problems")))
            ops.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning("\n  " + _("Erase interrupted")))
            ops.abort_transaction(transaction_id)
            return 130

        erased_count = queue_result.operations[0].count if queue_result.operations else len(packages_to_erase)
        print(colors.success("  " + ngettext(
            "{count} package erased",
            "{count} packages erased",
            erased_count).format(count=erased_count)))

        # Persist and display captured scriptlet output
        ops.record_scriptlet_output(transaction_id, queue_result)
        from ..helpers.progress import display_scriptlet_output
        display_scriptlet_output(queue_result, verbose=getattr(args, 'verbose', False),
                                 transaction_id=transaction_id)

        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        erased_packages = [action.name for action in all_actions]
        resolver.unmark_packages(erased_packages)

        return 0

    except Exception as e:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


