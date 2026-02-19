"""Transaction history management commands."""

import signal
import platform
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def _parse_date(date_str: str) -> int:
    """Parse a date string and return timestamp.

    Supports formats:
    - DD/MM/YYYY
    - DD/MM/YYYY HH:MM
    - YYYY-MM-DD
    - YYYY-MM-DD HH:MM
    """
    formats = [
        '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {date_str}")


def cmd_history(args, db: 'PackageDatabase') -> int:
    """Handle history command."""
    from .. import colors

    def _color_action(action):
        """Color an action string."""
        action_stripped = action.strip()
        if action_stripped in ('install', 'reinstall'):
            return colors.success(action)
        elif action_stripped in ('remove', 'erase', 'autoremove'):
            return colors.error(action)
        elif action_stripped in ('upgrade', 'update'):
            return colors.info(action)
        elif action_stripped == 'downgrade':
            return colors.warning(action)
        elif action_stripped == 'undo':
            return colors.warning(action)
        elif action_stripped == 'rollback':
            return colors.warning(action)
        return action

    def _color_status(status):
        """Color a status string."""
        status_stripped = status.strip()
        if status_stripped == 'completed':
            return colors.success(status)
        elif status_stripped == 'aborted':
            return colors.error(status)
        elif status_stripped.startswith('undone'):
            return colors.dim(status)
        return status

    # Delete specific transactions
    if getattr(args, 'delete', None):
        deleted = 0
        for tid in args.delete:
            # Check if transaction exists
            trans = db.get_transaction(tid)
            if not trans:
                print(colors.error(f"Transaction #{tid} not found"))
                continue

            # Clear undone_by references to this transaction FIRST
            db.conn.execute("UPDATE history SET undone_by = NULL WHERE undone_by = ?", (tid,))
            # Delete from history_packages (foreign key)
            db.conn.execute("DELETE FROM history_packages WHERE history_id = ?", (tid,))
            # Delete from history
            db.conn.execute("DELETE FROM history WHERE id = ?", (tid,))
            deleted += 1
            print(f"Deleted transaction #{tid}")

        db.conn.commit()
        print(colors.success(f"\n{deleted} transaction(s) deleted"))
        return 0

    # Show details of specific transaction
    if args.detail:
        trans = db.get_transaction(args.detail)
        if not trans:
            print(colors.error(f"Transaction #{args.detail} not found"))
            return 1

        dt = datetime.fromtimestamp(trans['timestamp'])
        trans_id = trans['id']
        print(f"\n{colors.bold(f'Transaction #{trans_id}')} - {dt.strftime('%Y-%m-%d %H:%M')}")
        print(f"  {colors.bold('Action:')} {_color_action(trans['action'])}")
        print(f"  {colors.bold('Status:')} {_color_status(trans['status'])}")
        if trans['command']:
            print(f"  {colors.bold('Command:')} {trans['command']}")
        if trans['undone_by']:
            print(f"  {colors.bold('Undone by:')} #{trans['undone_by']}")

        if trans['explicit']:
            exp_count = len(trans['explicit'])
            print(f"\n  {colors.bold(f'Explicit ({exp_count}):')} ")
            for p in trans['explicit']:
                action = p['action']
                print(f"    {_color_action(f'{action:10}')} {p['pkg_nevra']}")

        if trans['dependencies']:
            dep_count = len(trans['dependencies'])
            print(f"\n  {colors.bold(f'Dependencies ({dep_count}):')} ")
            show_all = getattr(args, 'show_all', False)
            deps_to_show = trans['dependencies'] if show_all else trans['dependencies'][:20]
            for p in deps_to_show:
                action = p['action']
                print(f"    {_color_action(f'{action:10}')} {colors.dim(p['pkg_nevra'])}")
            if dep_count > 20 and not show_all:
                print(colors.dim(f"    ... and {dep_count - 20} more"))

        print()
        return 0

    # List history
    action_filter = None
    if args.install:
        action_filter = 'install'
    elif args.remove:
        action_filter = 'remove'

    history = db.list_history(limit=args.count, action_filter=action_filter)

    if not history:
        print(colors.info("No transaction history"))
        return 0

    print(f"\n{colors.bold('  ID')} | {colors.bold('Date      ')} | {colors.bold('Action  ')} | {colors.bold('Status     ')} | {colors.bold('Packages')}")
    print("-" * 70)

    for h in history:
        dt = datetime.fromtimestamp(h['timestamp'])
        date_str = dt.strftime('%Y-%m-%d')
        explicit = h['explicit_pkgs'] or ''
        if len(explicit) > 30:
            explicit = explicit[:27] + '...'

        status = h['status']
        if h['undone_by']:
            status = f"undone(#{h['undone_by']})"

        pkg_info = explicit
        if h['pkg_count'] > 1 and explicit:
            dep_count = h['pkg_count'] - len(explicit.split(','))
            if dep_count > 0:
                pkg_info += colors.dim(f" (+{dep_count} deps)")

        action = h['action']
        print(f"{h['id']:>4} | {date_str:10} | {_color_action(f'{action:8}')} | {_color_status(f'{status:11}')} | {pkg_info}")

    print()
    return 0


def cmd_undo(args, db: 'PackageDatabase') -> int:
    """Handle undo command - undo last or specific transaction."""
    from ...core.install import check_root
    from ...core.resolver import Resolver
    from ...core.transaction_queue import TransactionQueue
    from ...core.background_install import InstallLock
    from ...core.download import Downloader, DownloadItem
    from ...core.config import get_base_dir, get_rpm_root
    from .. import colors

    # Check root
    if not check_root():
        print(colors.error("Error: undo requires root privileges"))
        return 1

    # Determine which transaction to undo
    if args.transaction_id is None:
        # No ID: find last complete, non-undone transaction
        history = db.list_history(limit=20)
        target_id = None
        for h in history:
            trans = db.get_transaction(h['id'])
            if trans and trans['status'] == 'complete' and not trans['undone_by']:
                target_id = h['id']
                break
        if target_id is None:
            print(colors.warning("No undoable transaction found in history"))
            return 1
    else:
        target_id = args.transaction_id

    trans = db.get_transaction(target_id)
    if not trans:
        print(colors.error(f"Transaction #{target_id} not found"))
        return 1

    if trans['status'] != 'complete':
        print(colors.error(f"Transaction #{target_id} is not complete (status: {trans['status']})"))
        return 1

    if trans['undone_by']:
        print(colors.warning(f"Transaction #{target_id} was already undone by #{trans['undone_by']}"))
        return 1

    # Build reverse actions for THIS transaction only
    to_remove = []   # Names of packages to remove
    to_install = []  # NEVRAs to reinstall

    for pkg in trans['packages']:
        action = pkg['action']
        nevra = pkg['pkg_nevra']
        name = pkg['pkg_name']
        previous = pkg.get('previous_nevra')

        if action == 'install':
            to_remove.append(name)
        elif action == 'remove':
            to_install.append(nevra)
        elif action == 'upgrade':
            to_remove.append(name)
            if previous:
                to_install.append(previous)
        elif action == 'downgrade':
            to_remove.append(name)
            if previous:
                to_install.append(previous)

    # Show summary
    print(f"\n{colors.bold(f'Undo transaction #{target_id}')} ({colors.warning(trans['action'])})")

    if to_remove:
        print(f"\n{colors.warning(f'Packages to remove ({len(to_remove)}):')}")
        for name in sorted(to_remove):
            print(f"  {colors.pkg_remove('-')} {name}")

    if to_install:
        print(f"\n{colors.success(f'Packages to reinstall ({len(to_install)}):')}")
        for nevra in sorted(to_install):
            print(f"  {colors.pkg_install('+')} {nevra}")

    if not to_remove and not to_install:
        print(colors.info("Nothing to undo"))
        return 0

    if not args.auto:
        try:
            answer = input("\nProceed? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Handle Ctrl+C
    interrupted = False
    def handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
        print("\nInterrupted! Finishing current operation...")

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    # Start undo transaction
    undo_trans_id = db.begin_transaction('undo', f'urpm undo {target_id}')

    try:
        # First remove packages that were installed (all at once for dependency handling)
        if to_remove and not interrupted:
            # Check if another operation is in progress
            lock = InstallLock()
            if not lock.acquire(blocking=False):
                print(colors.warning("  RPM database is locked by another process."))
                print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
                lock.acquire(blocking=True)
            lock.release()  # Release - child will acquire its own lock

            print(colors.info(f"\nRemoving {len(to_remove)} package(s)..."))

            last_erase_shown = [None]

            # Build transaction queue
            rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
            queue = TransactionQueue(root=rpm_root or "/")
            queue.add_erase(to_remove, operation_id="undo_remove")

            # Progress callback
            def queue_progress(op_id: str, name: str, current: int, total: int):
                if last_erase_shown[0] != name:
                    print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                    last_erase_shown[0] = name

            # Execute the queue
            queue_result = queue.execute(progress_callback=queue_progress)

            # Print done
            print(f"\r\033[K  [{len(to_remove)}/{len(to_remove)}] done")

            if not queue_result.success:
                print(colors.error("\nErase failed:"))
                if queue_result.operations:
                    for err in queue_result.operations[0].errors[:10]:
                        print(f"  {colors.error(err)}")
                elif queue_result.overall_error:
                    print(f"  {colors.error(queue_result.overall_error)}")
                db.abort_transaction(undo_trans_id)
                return 1

            if interrupted:
                erased_count = queue_result.operations[0].count if queue_result.operations else 0
                db.abort_transaction(undo_trans_id)
                print(colors.warning(f"\nUndo interrupted after {erased_count} packages"))
                return 130

            # Record removed packages
            for name in to_remove:
                db.record_package(undo_trans_id, name, name, 'remove', 'explicit')

            erased_count = queue_result.operations[0].count if queue_result.operations else len(to_remove)
            print(colors.success(f"  {erased_count} packages removed"))

        # Then reinstall packages that were removed
        if to_install and not interrupted:
            print(colors.info(f"\nReinstalling {len(to_install)} package(s)..."))

            download_items = []
            not_found = []

            # Cache media and servers lookups
            media_cache = {}
            servers_cache = {}

            for nevra in to_install:
                # Parse NEVRA: name-[epoch:]version-release.arch
                if '.' in nevra:
                    name_evr, arch = nevra.rsplit('.', 1)
                else:
                    name_evr = nevra
                    arch = platform.machine()

                # Split name from evr
                match = re.match(r'^(.+)-(\d+:)?([^-]+-[^-]+)$', name_evr)
                if match:
                    name = match.group(1)
                    epoch = match.group(2).rstrip(':') if match.group(2) else None
                    ver_rel = match.group(3)
                    version, release = ver_rel.rsplit('-', 1)
                    evr = f"{epoch}:{version}-{release}" if epoch else f"{version}-{release}"
                else:
                    # Fallback: try simpler parsing
                    parts = name_evr.rsplit('-', 2)
                    if len(parts) >= 3:
                        name = parts[0]
                        version = parts[1]
                        release = parts[2]
                        evr = f"{version}-{release}"
                    else:
                        print(f"  {colors.warning('Warning:')} cannot parse NEVRA: {nevra}")
                        not_found.append(nevra)
                        continue

                # Find package in database
                pkg = db.find_package_by_nevra(name, evr, arch)
                if not pkg:
                    # Try without epoch in evr
                    if ':' in evr:
                        evr_no_epoch = evr.split(':', 1)[1]
                        pkg = db.find_package_by_nevra(name, evr_no_epoch, arch)

                if not pkg:
                    print(f"  {colors.warning('Warning:')} {nevra} not found in repositories")
                    not_found.append(nevra)
                    continue

                # Get media info
                media_id = pkg['media_id']
                if media_id not in media_cache:
                    media_cache[media_id] = db.get_media_by_id(media_id)
                media = media_cache[media_id]

                if not media:
                    print(f"  {colors.warning('Warning:')} media not found for {nevra}")
                    not_found.append(nevra)
                    continue

                # Get servers for this media
                if media_id not in servers_cache:
                    servers_cache[media_id] = [dict(s) for s in db.get_servers_for_media(media_id, enabled_only=True)]

                servers = servers_cache[media_id]

                # Build download item
                dl_evr = evr
                if ':' in dl_evr:
                    dl_evr = dl_evr.split(':', 1)[1]  # Remove epoch for filename
                dl_version, dl_release = dl_evr.rsplit('-', 1) if '-' in dl_evr else (dl_evr, '1')

                if media.get('relative_path'):
                    download_items.append(DownloadItem(
                        name=name,
                        version=dl_version,
                        release=dl_release,
                        arch=arch,
                        media_id=media_id,
                        relative_path=media['relative_path'],
                        is_official=bool(media.get('is_official', 1)),
                        servers=servers,
                        media_name=media.get('name', ''),
                        size=pkg.get('size', 0)
                    ))
                elif media.get('url'):
                    download_items.append(DownloadItem(
                        name=name,
                        version=dl_version,
                        release=dl_release,
                        arch=arch,
                        media_url=media['url'],
                        media_name=media.get('name', ''),
                        size=pkg.get('filesize', 0)
                    ))
                else:
                    print(f"  {colors.warning('Warning:')} no URL or servers for {nevra}")
                    not_found.append(nevra)

            # Download packages
            rpm_paths = []
            if download_items:
                urpm_root = getattr(args, 'urpm_root', None)
                cache_dir = get_base_dir(urpm_root=urpm_root)
                downloader = Downloader(cache_dir=cache_dir, use_peers=True, db=db)

                # Simple progress for undo
                def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                             item_bytes=None, item_total=None, slots_status=None):
                    pct = int(bytes_done * 100 / bytes_total) if bytes_total else 0
                    print(f"\r\033[K  Downloading [{pkg_num}/{pkg_total}] {name} {pct}%", end='', flush=True)

                dl_results, downloaded, cached, _ = downloader.download_all(download_items, progress)
                print(f"\r\033[K  {downloaded} downloaded, {cached} from cache")

                # Collect successful downloads
                for result in dl_results:
                    if result.success and result.path:
                        rpm_paths.append(result.path)
                    else:
                        print(f"  {colors.error('Failed:')} {result.name}: {result.error}")

            # Install downloaded packages
            if rpm_paths and not interrupted:
                print(colors.info(f"  Installing {len(rpm_paths)} package(s)..."))

                # Check lock
                lock = InstallLock()
                if not lock.acquire(blocking=False):
                    print(colors.dim("  Waiting for RPM lock..."))
                    lock.acquire(blocking=True)
                lock.release()

                # Build transaction queue for install
                rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
                install_queue = TransactionQueue(root=rpm_root or "/")
                install_queue.add_install(rpm_paths, operation_id="undo_reinstall")

                last_install_shown = [None]

                def install_progress(op_id: str, name: str, current: int, total: int):
                    if last_install_shown[0] != name:
                        print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                        last_install_shown[0] = name

                install_result = install_queue.execute(progress_callback=install_progress)
                print(f"\r\033[K  [{len(rpm_paths)}/{len(rpm_paths)}] done")

                if not install_result.success:
                    print(colors.error("  Reinstall failed:"))
                    if install_result.operations:
                        for err in install_result.operations[0].errors[:5]:
                            print(f"    {colors.error(err)}")
                    # Don't fail the whole undo - removal was successful
                else:
                    # Record reinstalled packages
                    for nevra in to_install:
                        if nevra not in not_found:
                            name = nevra.rsplit('.', 1)[0].rsplit('-', 2)[0] if '.' in nevra else nevra.rsplit('-', 2)[0]
                            db.record_package(undo_trans_id, name, nevra, 'install', 'explicit')
                    installed_count = install_result.operations[0].count if install_result.operations else len(rpm_paths)
                    print(colors.success(f"  {installed_count} packages reinstalled"))

            elif not_found and not rpm_paths:
                print(colors.warning(f"  Could not reinstall: packages not found in repositories"))

        if interrupted:
            db.abort_transaction(undo_trans_id)
            print(colors.warning("\nUndo interrupted"))
            return 130

        # Mark original transaction as undone
        db.mark_undone(target_id, undo_trans_id)

        db.complete_transaction(undo_trans_id)

        # Update installed-through-deps.list for urpmi compatibility
        if to_remove:
            arch = platform.machine()
            resolver = Resolver(db, arch=arch)
            resolver.unmark_packages(to_remove)

        print(colors.success(f"\nUndo complete (transaction #{undo_trans_id})"))
        return 0

    except Exception as e:
        db.abort_transaction(undo_trans_id)
        print(colors.error(f"\nUndo failed: {e}"))
        return 1

    finally:
        signal.signal(signal.SIGINT, old_handler)


def cmd_rollback(args, db: 'PackageDatabase') -> int:
    """Handle rollback command.

    Usage:
    - rollback         : rollback last transaction
    - rollback N       : rollback last N transactions
    - rollback to N    : rollback to state after transaction #N
    - rollback to DATE : rollback to state at DATE
    """
    from ...core.install import check_root
    from ...core.resolver import Resolver
    from ...core.transaction_queue import TransactionQueue
    from ...core.background_install import InstallLock
    from ...core.config import get_rpm_root
    from .. import colors

    # Check root
    if not check_root():
        print(colors.error("Error: rollback requires root privileges"))
        return 1

    rollback_args = args.args if hasattr(args, 'args') else []

    # Parse arguments to determine mode and target
    mode = 'count'  # 'count' or 'to'
    count = 1
    target_id = None
    target_timestamp = None

    if not rollback_args:
        # No args: rollback 1
        count = 1
    elif rollback_args[0].lower() == 'to':
        # "rollback to ..."
        mode = 'to'
        if len(rollback_args) < 2:
            print("Usage: rollback to <transaction_id|date>")
            return 1
        target_str = ' '.join(rollback_args[1:])
        # Try as transaction ID first
        try:
            target_id = int(target_str)
        except ValueError:
            # Try as date
            try:
                target_timestamp = _parse_date(target_str)
            except ValueError as e:
                print(colors.error(f"Error: {e}"))
                print("Usage: rollback to <transaction_id|date>")
                print("Date formats: DD/MM/YYYY, DD/MM/YYYY HH:MM, YYYY-MM-DD")
                return 1
    else:
        # "rollback N"
        try:
            count = int(rollback_args[0])
            if count < 1:
                print(colors.error("Count must be at least 1"))
                return 1
        except ValueError:
            print(colors.error(f"Invalid argument: {rollback_args[0]}"))
            print("Usage: rollback [N] | rollback to <id|date>")
            return 1

    # Get history
    history = db.list_history(limit=200)
    if not history:
        print(colors.warning("No transactions in history"))
        return 1

    # Determine which transactions to undo
    if mode == 'count':
        # Undo the last N transactions
        to_undo = [h for h in history if h['status'] == 'complete'][:count]
        if not to_undo:
            print(colors.info("No completed transactions to rollback"))
            return 0
        target_desc = f"last {count} transaction(s)"
    else:
        # mode == 'to'
        if target_id is not None:
            # Rollback to state after transaction #target_id
            to_undo = [h for h in history
                       if h['id'] > target_id and h['status'] == 'complete']
            if not to_undo:
                print(colors.info(f"Already at or before transaction #{target_id}"))
                return 0
            target_desc = f"state after transaction #{target_id}"
        else:
            # Rollback to state at target_timestamp
            to_undo = [h for h in history
                       if h['timestamp'] > target_timestamp and h['status'] == 'complete']
            if not to_undo:
                date_str = datetime.fromtimestamp(target_timestamp).strftime('%d/%m/%Y %H:%M')
                print(colors.info(f"No transactions after {date_str}"))
                return 0
            target_desc = f"state at {datetime.fromtimestamp(target_timestamp).strftime('%d/%m/%Y %H:%M')}"

    # Collect all actions to reverse
    to_install = []  # NEVRAs to reinstall
    to_remove = []   # Package names to remove

    for h in to_undo:
        trans_detail = db.get_transaction(h['id'])
        if not trans_detail:
            continue

        for pkg in trans_detail['packages']:
            action = pkg['action']
            nevra = pkg['pkg_nevra']
            name = pkg['pkg_name']
            previous = pkg.get('previous_nevra')

            if action == 'install':
                if name not in to_remove:
                    to_remove.append(name)
                to_install = [n for n in to_install if not n.startswith(name + '-')]

            elif action == 'remove':
                if nevra not in to_install:
                    to_install.append(nevra)
                if name in to_remove:
                    to_remove.remove(name)

            elif action == 'upgrade':
                if previous:
                    to_install.append(previous)
                    if name not in to_remove:
                        to_remove.append(name)

            elif action == 'downgrade':
                if previous:
                    to_install.append(previous)
                    if name not in to_remove:
                        to_remove.append(name)

    # Show summary
    print(f"\n{colors.bold(f'Rollback to {target_desc}')}")
    print(f"  {colors.info(f'Undoing {len(to_undo)} transaction(s):')}\n")

    for h in to_undo:
        date_str = datetime.fromtimestamp(h['timestamp']).strftime('%d/%m/%Y %H:%M')
        action = h['action']
        action_color = colors.warning(action)
        print(f"    #{h['id']} {date_str} {action_color} - {h['explicit_pkgs'] or '(deps)'}")

    if to_remove:
        print(f"\n{colors.warning(f'Packages to remove ({len(to_remove)}):')}")
        for name in sorted(to_remove):
            print(f"  {colors.pkg_remove('-')} {name}")

    if to_install:
        print(f"\n{colors.success(f'Packages to reinstall ({len(to_install)}):')}")
        for nevra in sorted(to_install):
            print(f"  {colors.pkg_install('+')} {nevra}")

    if not to_remove and not to_install:
        print(colors.info("\nNothing to do"))
        return 0

    if not args.auto:
        try:
            answer = input("\nProceed? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Handle Ctrl+C
    interrupted = False
    def handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
        print("\nInterrupted! Finishing current operation...")

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    # Start rollback transaction
    trans_id = db.begin_transaction('rollback', f'urpm rollback {" ".join(map(str, rollback_args)) or "1"}')

    try:
        # First remove packages that were installed (all at once for dependency handling)
        if to_remove and not interrupted:
            # Check if another operation is in progress
            lock = InstallLock()
            if not lock.acquire(blocking=False):
                print(colors.warning("  RPM database is locked by another process."))
                print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))
                lock.acquire(blocking=True)
            lock.release()  # Release - child will acquire its own lock

            print(colors.info(f"\nRemoving {len(to_remove)} package(s)..."))

            last_erase_shown = [None]

            # Build transaction queue
            rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
            queue = TransactionQueue(root=rpm_root or "/")
            queue.add_erase(to_remove, operation_id="rollback_remove")

            # Progress callback
            def queue_progress(op_id: str, name: str, current: int, total: int):
                if last_erase_shown[0] != name:
                    print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                    last_erase_shown[0] = name

            # Execute the queue
            queue_result = queue.execute(progress_callback=queue_progress)

            # Print done
            print(f"\r\033[K  [{len(to_remove)}/{len(to_remove)}] done")

            if not queue_result.success:
                print(colors.error("\nErase failed:"))
                if queue_result.operations:
                    for err in queue_result.operations[0].errors[:10]:
                        print(f"  {colors.error(err)}")
                elif queue_result.overall_error:
                    print(f"  {colors.error(queue_result.overall_error)}")
                db.abort_transaction(trans_id)
                return 1

            if interrupted:
                erased_count = queue_result.operations[0].count if queue_result.operations else 0
                db.abort_transaction(trans_id)
                print(colors.warning(f"\nRollback interrupted after {erased_count} packages"))
                return 130

            # Record removed packages
            for name in to_remove:
                db.record_package(trans_id, name, name, 'remove', 'explicit')

            erased_count = queue_result.operations[0].count if queue_result.operations else len(to_remove)
            print(colors.success(f"  {erased_count} packages removed"))

        # Then reinstall packages that were removed
        if to_install and not interrupted:
            print(colors.info(f"\nReinstalling {len(to_install)} package(s)..."))
            for nevra in to_install:
                if interrupted:
                    break
                print(f"  {colors.warning('Note:')} {nevra} needs to be downloaded/installed")
                # TODO: integrate with resolver/downloader for proper reinstall

        if interrupted:
            db.abort_transaction(trans_id)
            print(colors.warning("\nRollback interrupted"))
            return 130

        # Mark all undone transactions
        for h in to_undo:
            db.mark_undone(h['id'], trans_id)

        db.complete_transaction(trans_id)

        # Update installed-through-deps.list for urpmi compatibility
        if to_remove:
            arch = platform.machine()
            resolver = Resolver(db, arch=arch)
            resolver.unmark_packages(to_remove)

        print(colors.success(f"\nRollback complete (transaction #{trans_id})"))
        return 0

    except Exception as e:
        db.abort_transaction(trans_id)
        print(colors.error(f"\nRollback failed: {e}"))
        return 1

    finally:
        signal.signal(signal.SIGINT, old_handler)
