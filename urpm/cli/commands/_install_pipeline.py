"""Shared post-resolution install/upgrade pipeline.

``cmd_install`` (install.py) and ``cmd_upgrade`` (upgrade.py) used to
carry ~120 lines of near-identical code from "begin_transaction is
done" through "complete_transaction".  This module consolidates that
shared work into :func:`run_install_transaction` so the two commands
agree on locking, signal handling, progress display, error
classification and transaction completion.

What stays in each caller
-------------------------

* Resolution (each command resolves its own package set).
* ``ops.begin_transaction`` — the caller owns the ``transaction_id``
  so the helper can simply pass it to ``ops.complete_transaction`` /
  ``ops.abort_transaction``.
* Pre-resolved ``rpm_paths`` and ``download_items``.
* Upgrade-only pre-processing: orphan-on-arrival detection
  (mutates ``result.actions``), ``cancelled_new_versions`` filter on
  ``rpm_paths``, reason overwriting on actions.
* Install-only post-processing: README display after
  ``complete_transaction``, promotion of user-requested already-
  installed packages to explicit.
* Upgrade-only post-processing: ``resolver.unmark_packages`` for
  orphans and removes.
* Command-specific exit-code mapping (e.g. upgrade maps ``0`` →
  ``partial_exit`` when packages were skipped at resolution time).

Returned exit codes
-------------------

The helper follows the **Unix convention** the user picked when the
diff between ``cmd_install`` and ``cmd_upgrade`` surfaced their
divergence (``doc/REFACTOR_INSTALL_UPGRADE_DIFF.md`` §"Points
d'inquiétude" #4):

* ``0``  — transaction succeeded (or was a no-op),
* ``1``  — transaction failed (``ops.abort_transaction`` already
  called),
* ``130`` — interrupted by SIGINT (``ops.abort_transaction`` already
  called).

A caller is free to translate ``0`` to its own command-specific
success code; the helper only commits to never returning a negative
result on a happy path.
"""

from __future__ import annotations

import signal
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .. import colors
from ...i18n import _, ngettext

if TYPE_CHECKING:
    from ...core.install import InstallResult
    from ...core.operations import PackageOperations, InstallOptions
    from ...core.resolver import Resolution, Resolver


def run_install_transaction(
    args,
    ops: "PackageOperations",
    resolver: "Resolver",
    *,
    mode: str,
    result: "Resolution",
    rpm_paths: List[str],
    download_items: list,
    transaction_id: int,
    install_opts: "InstallOptions",
    full_sync: bool,
    restart_info: Dict[str, List[str]],
    header_template: str,
    success_message_kind: str,
    install_actions: Optional[list] = None,
    erase_names: Optional[List[str]] = None,
    orphan_names: Optional[List[str]] = None,
    after_complete: Optional[Callable[[Any], None]] = None,
) -> int:
    """Run the shared install/upgrade post-resolution pipeline.

    Args:
        args: Parsed argparse namespace; consulted for ``root``,
            ``urpm_root`` and ``verbose`` only.
        ops: ``PackageOperations`` bound to the right database.
        resolver: Live resolver (used for ``mark_dependencies`` after
            success).
        mode: ``"install"`` or ``"upgrade"`` — selects the right
            keyword set for :meth:`PackageOperations.resilient_install`
            and the progress display total.
        result: Resolution backing the transaction (its ``actions``
            are passed to ``mark_dependencies`` on success).
        rpm_paths: RPMs to feed the resilient pipeline; already
            filtered by the caller (e.g. ``cancelled_new_versions``).
        download_items: Original download items, used by the resilient
            pipeline for retry-from-alternate-mirror.
        transaction_id: Identifier returned by
            ``ops.begin_transaction`` — the helper will call
            ``ops.complete_transaction`` or ``ops.abort_transaction``
            with this value.
        install_opts: Pre-built :class:`InstallOptions`.  The caller
            sets command-specific fields (``reinstall``, ``noscripts``,
            ``config_policy``, …).
        full_sync: Already-computed full-sync decision.
        restart_info: Already-computed ``{component: [pkg names]}``
            from :func:`check_needs_restart_from_actions`.  Used to
            print the "you should reboot" advice after success.
        header_template: Template (``"Installing {count} package..."``
            or ``"Upgrading {count} package..."``).
        success_message_kind: ``"installed"`` or ``"upgraded"`` —
            selects the success line wording.
        install_actions: Passed as ``actions=`` to
            ``resilient_install`` in install mode only.  Ignored in
            upgrade mode.
        erase_names: Names to erase in this transaction.
        orphan_names: Orphan names to remove in background (upgrade
            mode only).
        after_complete: Optional callable invoked after
            ``ops.complete_transaction`` succeeds, with the
            ``queue_result`` as argument.  Used by ``cmd_install`` to
            display README messages between commit and scriptlet
            output, by ``cmd_upgrade`` for orphan-unmark.

    Returns:
        ``0`` on success, ``1`` on failure (``abort_transaction`` has
        been called), ``130`` on SIGINT interrupt (also aborted).
    """
    from ..helpers.progress import (
        display_scriptlet_output, make_progress_callback,
    )
    from ...core.background_install import InstallLock
    from .install import _apply_config_policy

    # ── SIGINT handler: first Ctrl+C asks for graceful stop,
    # second one force-aborts.
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(_signum, _frame):
        if interrupted[0]:
            print(_("\n\nForce abort!"))
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        interrupted[0] = True
        print(_("\n\nInterrupt requested - finishing current package..."))

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # ── Acquire the install lock just to wait for any peer
        # transaction in progress; release immediately so the child
        # rpm process can re-acquire its own lock.
        install_root = (getattr(args, 'root', None)
                        or getattr(args, 'urpm_root', None))
        lock = InstallLock(root=install_root)
        if not lock.acquire(blocking=False):
            print(colors.warning(_(
                "  RPM database is locked by another process.")))
            print(colors.dim(_(
                "  Waiting for lock... (Ctrl+C to cancel)")))
            lock.acquire(blocking=True)
        lock.release()

        queue_progress = make_progress_callback(
            header_template=header_template,
            total=len(rpm_paths) if mode == "install" else None,
            full_sync=full_sync,
        )

        # ── Build resilient_install kwargs from mode ──
        kwargs: Dict[str, Any] = {
            'rpm_paths': rpm_paths,
            'download_items': download_items,
            'options': install_opts,
            'progress_callback': queue_progress,
            'root': install_opts.root,
            'urpm_root': getattr(args, 'urpm_root', None),
            'full_sync': full_sync,
        }
        if mode == "install":
            kwargs['actions'] = install_actions
            kwargs['erase_names'] = erase_names or None
        elif mode == "upgrade":
            kwargs['erase_names'] = erase_names or None
            kwargs['orphan_names'] = orphan_names
            kwargs['mode'] = "upgrade"
        else:
            raise ValueError(f"mode must be 'install' or 'upgrade', got {mode!r}")

        resilient_result = ops.resilient_install(**kwargs)

        # ── Clean up the live progress region ──
        queue_progress.cleanup()
        header = queue_progress.state.get('header') or (
            header_template.format(count=len(rpm_paths))
        )
        print(
            f"\033[2A\r\033[K{header}\n\033[K  " + _("done") + "\n\033[K",
            end='', flush=True,
        )

        # ── Show excluded packages warning ──
        if resilient_result.excluded_packages:
            excluded_count = len(resilient_result.excluded_packages)
            if mode == "install":
                msg = ngettext(
                    "{count} package could not be installed due to verification errors:",
                    "{count} packages could not be installed due to verification errors:",
                    excluded_count,
                )
            else:
                msg = ngettext(
                    "{count} package could not be upgraded due to verification errors:",
                    "{count} packages could not be upgraded due to verification errors:",
                    excluded_count,
                )
            print(colors.warning("\n" + msg.format(count=excluded_count)))
            for name, reason in resilient_result.excluded_packages[:10]:
                print(f"  {colors.warning(name)}: {reason}")
            print(colors.warning(_(
                "These packages will be retried on the next update.")))

        # ── Interrupt check (Ctrl+C during the resilient pipeline) ──
        if interrupted[0]:
            print(colors.warning("\n  " + _("Operation interrupted")))
            ops.abort_transaction(transaction_id)
            return 130

        # ── Failure path ──
        if not resilient_result.success:
            verb = _("Installation failed:") if mode == "install" else _("Upgrade failed:")
            print(colors.error("\n" + verb))
            for err in resilient_result.errors[:3]:
                print(f"  {colors.error(str(err))}")
            ops.abort_transaction(transaction_id)
            return 1

        # ── Success message ──
        installed_count = resilient_result.installed
        if success_message_kind == "installed":
            print(colors.success("  " + ngettext(
                "{count} package installed",
                "{count} packages installed",
                installed_count,
            ).format(count=installed_count)))
        else:  # "upgraded"
            print(colors.success("  " + ngettext(
                "{count} package upgraded",
                "{count} packages upgraded",
                installed_count,
            ).format(count=installed_count)))

        # ── Apply config policy on rpmnew files + persist scriptlet ──
        qr = resilient_result.queue_result
        if qr is not None and qr.operations:
            rpmnew_files = qr.operations[0].rpmnew_files
            if rpmnew_files:
                _apply_config_policy(rpmnew_files, install_opts.config_policy)
        if qr is not None:
            ops.record_scriptlet_output(transaction_id, qr)

        # ── Commit the transaction ──
        ops.complete_transaction(transaction_id)

        # ── Caller hook (README display for install; orphan/remove
        # unmark for upgrade) ──
        if after_complete is not None:
            after_complete(qr)

        # ── Display captured scriptlet output ──
        display_scriptlet_output(
            qr, verbose=getattr(args, 'verbose', False),
            transaction_id=transaction_id,
        )

        # ── Restart recommendations (kernel / libc / systemd-class
        # provides triggered ``should-restart:*``) ──
        if restart_info:
            from ...core.needs_restart import format_restart_messages
            for msg in format_restart_messages(restart_info):
                print(colors.warning(f"  ⚠ {msg}"))

        # ── Update installed-through-deps.list for urpmi compat ──
        ops.mark_dependencies(resolver, result.actions)

        return 0

    except Exception:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)
