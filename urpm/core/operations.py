"""
Core operations layer for urpm privileged operations.

This module provides transport-agnostic functions for package management.
Used by both the CLI (directly) and the D-Bus service (via PackageKit).

The CLI handles all user interaction (prompts, display, progress).
This module handles the business logic (resolution, download, install).

Auth integration:
- Mutating methods accept an optional auth_context parameter.
- When provided (D-Bus), permissions are checked and operations are audited.
- When absent (CLI as root), no checks are performed.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING, List, Dict, Any, Iterable, Optional, Callable, Tuple,
)

from .database import PackageDatabase
from .download import Downloader, DownloadItem
from .install import InstallResult
from .pseudo_media import LOCAL_RPMS
from .resilient_install import (
    pre_verify_signatures, purge_failed_from_cache,
    find_dependents, retry_failed_downloads, _extract_name_from_path,
)
from .transaction_queue import TransactionQueue, TransactionProgress, TransactionPhase

if TYPE_CHECKING:  # hooks are imported lazily: they cost nothing per operation
    from .hooks import Hook, OperationOutcome, TriggeredHook

logger = logging.getLogger(__name__)


def _post_to_urpmd(endpoint: str, payload: bytes = b'') -> bool:
    """POST to the local daemon, and say whether it answered.

    The verdict is the useful part.  ``urpmd`` is optional — not
    installed, not started, stopped by an operator who dislikes
    daemons — so every caller needs to know whether the work it just
    handed over will actually happen, or whether it has to do the job
    itself.  Swallowing the failure, as the cache-invalidation notifier
    used to, is fine for a hint and wrong for a hand-off.

    The timeout is short on purpose: this is a doorbell, not the work.
    An endpoint that does anything lengthy before answering would time
    out here and be read as "no daemon", and the job would be done
    twice.
    """
    try:
        import urllib.request
        from .config import get_port
        req = urllib.request.Request(
            f"http://127.0.0.1:{get_port()}{endpoint}",
            method='POST', data=payload,
            headers={'Content-Type': 'application/json'} if payload else {},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        logger.debug("urpmd did not answer on %s: %s", endpoint, exc)
        return False

# Optional auth imports - available when urpm.auth is installed
try:
    from ..auth.context import AuthContext, Permission, AuthError
    from ..auth.audit import AuditLogger
    _HAS_AUTH = True
except ImportError:
    _HAS_AUTH = False
    AuthContext = None
    AuditLogger = None


@dataclass
class InstallOptions:
    """Options for install/upgrade operations."""
    verify_signatures: bool = True
    force: bool = False
    test: bool = False
    reinstall: bool = False
    noscripts: bool = False
    nodeps: bool = False  # skip rpm-level dep verification (rpm -i --nodeps)
    use_peers: bool = True
    only_peers: bool = False
    root: str = "/"
    use_userns: bool = False
    payload_dir: str = ""
    """Override for where ``.rpm`` files are written (``--download-dir``).

    Empty falls back to the ``download.payload_dir`` config key, then
    to the default cache.  Only the payload moves — the database and
    media metadata stay in place.
    """
    # Only meaningful when ``use_userns=True`` — asks the bootstrap
    # wrapper to preserve the operator's proxy env vars.  Off by
    # default (see :mod:`urpm.core.userns_env`) ; wired by
    # ``cmd_mkimage`` from ``--forward-proxy`` / ``[image]
    # forward_proxy``.
    forward_proxy: bool = False
    config_policy: str = "keep"  # keep, replace, or ask


def _as_transaction_ids(value) -> Tuple[int, ...]:
    """Read one transaction id, or several, as a tuple.

    Most callers hold a single id.  A distupgrade holds one per Tx B
    batch plus Tx A and the retry pass, and they are one operation as
    far as a rule is concerned.  Accepting either shape keeps a single
    entry point, which is what the wiring tests enumerate.
    """
    if isinstance(value, int):
        return (value,)
    return tuple(value)


class PackageOperations:
    """Core package operations - transport agnostic.

    Provides the business logic for install/remove/upgrade without any
    UI or transport dependency. The CLI and D-Bus service call these
    methods and handle user interaction themselves.
    """

    def __init__(self, db: PackageDatabase, base_dir: Path = None,
                 audit_logger: 'AuditLogger' = None):
        """Initialize operations.

        Args:
            db: Package database instance
            base_dir: Base directory for cache (default: from config)
            audit_logger: Optional audit logger for privileged operation logging
        """
        self.db = db
        if base_dir is None:
            from .config import get_base_dir
            base_dir = get_base_dir()
        self.base_dir = base_dir
        self.audit = audit_logger

    # =========================================================================
    # Auth helpers
    # =========================================================================

    def _check_auth(self, auth_context, permission, action: str):
        """Check authorization if an auth context is provided.

        Args:
            auth_context: AuthContext or None (CLI as root skips checks)
            permission: Required Permission flag
            action: Action name for error messages and audit

        Raises:
            AuthError: If permission is denied
        """
        if auth_context is None or not _HAS_AUTH:
            return
        if not (auth_context.permissions & permission):
            if self.audit:
                self.audit.log_auth_denied(auth_context, action)
            from ..auth.context import AuthError
            raise AuthError(action, auth_context)

    def _audit_start(self, auth_context, action: str, packages: list,
                     command: str = ""):
        """Log operation start if audit logger is available."""
        if self.audit and auth_context:
            self.audit.log_operation_start(
                auth_context, action, packages, command
            )

    def _audit_complete(self, auth_context, action: str, packages: list,
                        success: bool, error: str = ""):
        """Log operation completion if audit logger is available."""
        if self.audit and auth_context:
            self.audit.log_operation_complete(
                auth_context, action, packages, success, error
            )

    # =========================================================================
    # Download
    # =========================================================================

    def build_download_items(
        self,
        actions: list,
        resolver: Any,
        local_rpm_infos: list = None
    ) -> Tuple[List[DownloadItem], List[str]]:
        """Build download items from resolution result.

        Separates remote packages (need download) from local RPMs.

        Args:
            actions: List of PackageAction from resolver
            resolver: Resolver instance (for local RPM path lookup)
            local_rpm_infos: Local RPM header infos

        Returns:
            (download_items, local_rpm_paths)
        """
        from .resolver import TransactionType

        download_items = []
        local_action_paths = []
        media_cache = {}
        servers_cache = {}

        defensive_fallback_hits = []  # collected for a single end-of-loop warn

        for action in actions:
            if action.action == TransactionType.REMOVE:
                continue

            media_name = action.media_name

            # Local RPMs don't need download.  The action carries the
            # solvable_id it came from (populated by the resolver at
            # PackageAction construction and by add_local_rpms via the
            # NEVRA→id secondary index for REINSTALL cases); we use it to
            # go straight to the LocalRPM metadata in O(1).
            if media_name == LOCAL_RPMS:
                pkg_info = None
                if action.solvable_id is not None:
                    pkg_info = resolver._solvable_to_pkg.get(action.solvable_id)
                if pkg_info is None and local_rpm_infos:
                    # Defensive safety net: if solvable_id is missing (call
                    # path that did not go through add_local_rpms) or points
                    # to a stale entry, recover by matching the header info
                    # the CLI passed in.  This should not fire in normal
                    # operation — we warn loudly so mismatches surface.
                    for info in local_rpm_infos:
                        if info.get('nevra') == action.nevra:
                            pkg_info = info
                            defensive_fallback_hits.append(action.nevra)
                            break
                    if pkg_info is None:
                        # Fall back once more on name-only match, historical
                        # behaviour — still counted as a defensive hit.
                        for info in local_rpm_infos:
                            if info.get('name') == action.name:
                                pkg_info = info
                                defensive_fallback_hits.append(action.nevra)
                                break
                if pkg_info and pkg_info.get('local_path', pkg_info.get('path')):
                    local_action_paths.append(
                        pkg_info.get('local_path') or pkg_info.get('path')
                    )
                continue

            # Look up media and servers
            if media_name not in media_cache:
                media = self.db.get_media(media_name)
                media_cache[media_name] = media
                if media and media.get('id'):
                    servers_cache[media['id']] = self.db.get_servers_for_media(
                        media['id'], enabled_only=True
                    )

            media = media_cache[media_name]
            if not media:
                logger.warning(f"Media '{media_name}' not found")
                continue

            # Parse EVR - remove epoch for filename
            evr = action.evr
            if ':' in evr:
                evr = evr.split(':', 1)[1]
            version, release = evr.rsplit('-', 1) if '-' in evr else (evr, '1')

            # New schema (servers + relative_path) or legacy (URL)
            if media.get('relative_path'):
                servers = servers_cache.get(media['id'], [])
                servers = [dict(s) for s in servers]
                download_items.append(DownloadItem(
                    name=action.name,
                    version=version,
                    release=release,
                    arch=action.arch,
                    media_id=media['id'],
                    relative_path=media['relative_path'],
                    is_official=bool(media.get('is_official', 1)),
                    servers=servers,
                    media_name=media_name,
                    size=action.filesize or action.size
                ))
            elif media.get('url'):
                download_items.append(DownloadItem(
                    name=action.name,
                    version=version,
                    release=release,
                    arch=action.arch,
                    media_url=media['url'],
                    media_name=media_name,
                    size=action.filesize or action.size
                ))
            else:
                logger.warning(f"No URL or servers for media '{media_name}'")

        if defensive_fallback_hits:
            # This is an internal-consistency alarm: an @LocalRPMs action
            # made it here without a valid solvable_id.  Normally
            # add_local_rpms() populates both _solvable_to_pkg[s.id] and
            # _localrpm_nevra_to_id[nevra], and every construction site
            # forwards s.id to the PackageAction.  If we land in the safety
            # net, one of those links is broken and future actions may drop
            # silently.  Log + orange line on stderr so the operator sees it.
            import sys
            count = len(defensive_fallback_hits)
            sample = ', '.join(defensive_fallback_hits[:3])
            if count > 3:
                sample += f', … ({count - 3} more)'
            msg = (f"LocalRPM lookup fell through the safety net for "
                   f"{count} action(s): {sample}. This indicates a broken "
                   f"solvable_id chain (add_local_rpms → PackageAction → "
                   f"operations); please report.")
            logger.warning(msg)
            # ANSI 33 = yellow/orange, bold.  No dependency on cli.colors
            # (core layer must not import CLI).
            print(f"\x1b[1;33mWarning:\x1b[0m {msg}", file=sys.stderr)

        return download_items, local_action_paths

    def refresh_item_servers(self, items: List[DownloadItem]) -> None:
        """Re-read the server list of every item, in place.

        The items are built before the confirmation prompt so the
        summary can tell the operator how much of the payload is
        already on the disk.  Enlarging the mirror pool, on the other
        hand, writes server rows and probes latencies, which has no
        business happening for a transaction that may still be
        refused — so it stays after the prompt, and the items it
        should have fed are already built by then.

        Rather than building them twice, the newly added mirrors are
        folded back in here.  One query per distinct medium, not per
        package.
        """
        by_media: dict = {}
        for item in items:
            if not item.media_id:
                continue
            if item.media_id not in by_media:
                by_media[item.media_id] = [
                    dict(s) for s in
                    self.db.get_servers_for_media(item.media_id)
                ]
            item.servers = [dict(s) for s in by_media[item.media_id]]

    def cached_payload_bytes(
        self,
        download_items: List[DownloadItem],
        options: InstallOptions = None,
        urpm_root: str = None,
    ) -> int:
        """How much of *download_items* is already on the disk.

        Resolves the payload directory exactly as
        :meth:`download_packages` does, so the figure shown before the
        prompt describes the same files the download stage will find.
        Cheap enough to run before a confirmation: a stat and four
        bytes read per package.
        """
        from .download import cached_payload_bytes as _cached_bytes
        from .download import resolve_payload_dir

        if urpm_root:
            from .config import get_base_dir
            cache_dir = get_base_dir(urpm_root=urpm_root)
        else:
            cache_dir = self.base_dir

        payload_dir = resolve_payload_dir(
            getattr(options, 'payload_dir', '') or None, default=cache_dir)
        return _cached_bytes(download_items, payload_dir)

    def download_packages(
        self,
        download_items: List[DownloadItem],
        options: InstallOptions = None,
        progress_callback: Callable = None,
        urpm_root: str = None,
        target_version: str = None,
        target_arch: str = None,
    ) -> Tuple[list, int, int, dict]:
        """Download packages.

        Args:
            download_items: Items to download
            options: Install options (peers config)
            progress_callback: Download progress callback
            urpm_root: Override base dir for cache
            target_version: Optional Mageia release identity forwarded
                to the peer discovery query so LAN peers can filter
                their inventory to the release we're actually asking
                for.  Without this, distupgrade downloads never hit
                peers because ``query_peers_have`` reaches them with
                no version filter and they can't tell mga9 apart from
                mga10.  Passing a value here ties the coordinator to
                the release you want.
            target_arch: Same rationale for arch.

        Returns:
            (dl_results, downloaded_count, cached_count, peer_stats)
        """
        if options is None:
            options = InstallOptions()

        if urpm_root:
            from .config import get_base_dir
            cache_dir = get_base_dir(urpm_root=urpm_root)
        else:
            cache_dir = self.base_dir

        downloader = Downloader(
            cache_dir=cache_dir,
            use_peers=options.use_peers,
            only_peers=options.only_peers,
            db=self.db,
            target_version=target_version,
            target_arch=target_arch,
            payload_dir=getattr(options, 'payload_dir', '') or None,
        )

        dl_results, downloaded, cached, peer_stats = downloader.download_all(
            download_items, progress_callback
        )

        return dl_results, downloaded, cached, peer_stats

    # =========================================================================
    # Installation
    # =========================================================================

    def execute_install(
        self,
        rpm_paths: List[str],
        options: InstallOptions = None,
        progress_callback: Callable[['TransactionProgress'], None] = None,
        auth_context=None,
        actions: list = None,
        erase_names: List[str] = None,
        full_sync: bool = False,
    ) -> Any:
        """Execute RPM installation via TransactionQueue.

        Args:
            rpm_paths: List of RPM file paths to install
            options: Install options
            progress_callback: Called with a TransactionProgress object
            auth_context: Optional AuthContext for permission check + audit
            actions: PackageAction list from resolver (used for README.urpmi
                     display after transaction completes in the child process)
            erase_names: Package names to remove (obsoleted/conflicting)
            full_sync: If True, wait for the entire transaction including
                triggers. If False (default), wait for extraction only and
                run triggers in the background.

        Returns:
            TransactionQueue result
        """
        if _HAS_AUTH:
            self._check_auth(auth_context, Permission.INSTALL, "install")

        if options is None:
            options = InstallOptions()

        pkg_names = [Path(p).stem for p in rpm_paths]
        self._audit_start(auth_context, "install", pkg_names)

        queue = TransactionQueue(
            root=options.root,
            use_userns=options.use_userns,
            forward_proxy=options.forward_proxy,
        )
        queue.add_install(
            rpm_paths,
            operation_id="install",
            verify_signatures=options.verify_signatures,
            force=options.force,
            test=options.test,
            reinstall=options.reinstall,
            noscripts=options.noscripts,
            nodeps=options.nodeps,
            actions=actions,
            erase_names=erase_names or [],
        )

        result = queue.execute(
            progress_callback=progress_callback,
            full_sync=full_sync,
        )
        self._audit_complete(auth_context, "install", pkg_names, success=result.success)
        return result

    def resilient_install(
        self,
        rpm_paths: List[str],
        download_items: list,
        options: "InstallOptions" = None,
        actions: list = None,
        progress_callback: Callable[['TransactionProgress'], None] = None,
        auth_context=None,
        root: str = "/",
        urpm_root: Optional[str] = None,
        erase_names: List[str] = None,
        orphan_names: List[str] = None,
        mode: str = "install",
        full_sync: bool = False,
    ) -> "InstallResult":
        """Install or upgrade packages with signature pre-check, retry, and exclusion.

        This wraps :meth:`execute_install` or :meth:`execute_upgrade` with
        the resilient pipeline:

          1. Pre-verify GPG signatures on all RPMs — **skipped** when
             ``options.verify_signatures`` is False (``--nosignature``).
          2. Retry failed downloads from alternate mirrors (one pass
             today; see :func:`retry_failed_downloads`).
          3. Exclude unrecoverable packages and their dependents from
             the transaction.
          4. Execute a (possibly reduced) transaction via the queue.

        See :class:`urpm.core.install.InstallResult` for the exact
        contract of the returned ``success`` flag and the partial-failure
        semantics.

        Args:
            rpm_paths: RPM file paths to install/upgrade.
            download_items: Original download items (for retry on failure).
            options: Install options.
            actions: Resolver actions (passed to execute_install).
            progress_callback: Progress callback for install phase.
            auth_context: Optional auth context for D-Bus path.
            root: RPM root directory.
            urpm_root: urpm state directory override.
            erase_names: Package names to remove (upgrade mode: obsoleted).
            orphan_names: Orphaned deps to remove in background (upgrade mode).
            mode: ``"install"`` (default) or ``"upgrade"`` — selects which
                execute method to call internally.
            full_sync: If True, wait for the entire transaction including
                triggers. If False (default), wait for extraction only and
                run triggers in the background.

        Returns:
            :class:`urpm.core.install.InstallResult` with install
            outcome details (resilient-specific fields populated:
            ``excluded_packages``, ``reduced_transaction``,
            ``queue_result``).
        """
        if options is None:
            options = InstallOptions()

        path_objects = [Path(p) for p in rpm_paths]

        # ── Step 1: Pre-verify signatures (skip if --nosignature) ──
        if options.verify_signatures:
            valid_paths, sig_failed = pre_verify_signatures(path_objects, root=root)
        else:
            valid_paths = path_objects
            sig_failed = []

        excluded: List[tuple] = []

        # ── Step 2: Retry failed from alternate mirrors ──
        if sig_failed:
            failed_paths = [f.path for f in sig_failed]

            # ── Bug #3 iteration B: route by category ──
            # A ``signature`` failure is a compromise indicator
            # (key/signature/digest-from-key) — the source server is
            # blacklisted from every pool until a human acts on it
            # via ``urpm server unblacklist``.  ``preflight`` and
            # ``structural`` are corruption events: they cost
            # reputation score but do not blacklist.
            for failure in sig_failed:
                source_id = self.db.get_cache_file_server_id(
                    str(failure.path),
                )
                if failure.category == "signature":
                    if source_id is not None:
                        self.db.blacklist_server(
                            source_id,
                            reason=(
                                f"served '{failure.path.name}' with "
                                f"failing signature ({failure.reason})"
                            ),
                        )
                        server_row = self.db.get_server_by_id(source_id) or {}
                        server_name = server_row.get('name') or f"#{source_id}"
                        logger.error(
                            "SECURITY ALERT: server '%s' potentially "
                            "compromised after signature failure on '%s' "
                            "(%s) — blacklisted.  Detail: "
                            "urpm server status %s",
                            server_name, failure.path.name,
                            failure.reason, server_name,
                        )
                else:
                    if source_id is not None:
                        self.db.record_server_failure(
                            source_id, category="corrupt",
                            detail=f"{failure.path.name}: {failure.reason}",
                        )

            purge_failed_from_cache(failed_paths, self.db)

            recovered, still_failed = retry_failed_downloads(
                failed_paths,
                download_items,
                ops=self,
                options=options,
                urpm_root=urpm_root,
            )

            # Add recovered files back to the valid set
            valid_paths.extend(recovered)

            if still_failed:
                # ── Step 3: Exclude bad packages and their dependents ──
                failed_names = {name for name, _reason in still_failed}
                all_excluded = find_dependents(
                    failed_names, valid_paths, root=root
                )

                # Filter valid_paths to remove excluded packages
                filtered_paths: List[Path] = []
                for p in valid_paths:
                    pkg_name = _extract_name_from_path(p)
                    if pkg_name not in all_excluded:
                        filtered_paths.append(p)
                valid_paths = filtered_paths

                # Build excluded list with reasons
                reason_map = dict(still_failed)
                for name in all_excluded:
                    reason = reason_map.get(
                        name,
                        "depends on excluded package",
                    )
                    excluded.append((name, reason))

                logger.info(
                    "Excluded %d package(s) from transaction: %s",
                    len(excluded),
                    ", ".join(n for n, _ in excluded),
                )

        # ── Step 4: Execute the (possibly reduced) transaction ──
        if not valid_paths and not (erase_names or orphan_names):
            return InstallResult(
                success=False,
                installed=0,
                excluded_packages=excluded,
                errors=["All packages failed verification"],
            )

        final_rpm_paths = [str(p) for p in valid_paths]

        if mode == "upgrade":
            queue_result = self.execute_upgrade(
                final_rpm_paths,
                erase_names=erase_names,
                orphan_names=orphan_names,
                options=options,
                progress_callback=progress_callback,
                auth_context=auth_context,
                full_sync=full_sync,
            )
        else:
            queue_result = self.execute_install(
                final_rpm_paths,
                options=options,
                progress_callback=progress_callback,
                auth_context=auth_context,
                actions=actions,
                erase_names=erase_names,
                full_sync=full_sync,
            )

        # Determine installed count from queue result
        if queue_result is not None and queue_result.operations:
            installed_count = queue_result.operations[0].count
        elif queue_result is not None:
            installed_count = len(final_rpm_paths)
        else:
            # execute_upgrade returns None when queue is empty
            installed_count = 0

        success = queue_result.success if queue_result is not None else True

        # Surface per-operation errors to the caller so the CLI can show
        # the real rpm problems (e.g. file conflicts). Fall back to the
        # queue-level overall_error only when no per-op error is available,
        # otherwise the detailed list is silently dropped and the user
        # sees "Installation failed:" with nothing after.
        #
        # The decoding moved onto QueueResult so the distupgrade path,
        # which bypasses this method and calls execute_install directly,
        # shares it instead of reinventing a broken variant.
        op_errors: List[str] = []
        if queue_result and not queue_result.success:
            op_errors = queue_result.collect_errors()

        return InstallResult(
            success=success,
            installed=installed_count,
            excluded_packages=excluded,
            errors=op_errors,
            reduced_transaction=bool(excluded),
            queue_result=queue_result,
        )

    def execute_erase(
        self,
        package_names: List[str],
        options: InstallOptions = None,
        progress_callback: Callable[['TransactionProgress'], None] = None,
        auth_context=None,
        full_sync: bool = False,
    ) -> Any:
        """Execute RPM removal via TransactionQueue.

        Args:
            package_names: Package names to remove.
            options: Install options.
            progress_callback: Called with a TransactionProgress object.
            auth_context: Optional AuthContext for permission check + audit.
            full_sync: If True, wait for the entire transaction including
                triggers. If False (default), wait for extraction only and
                run triggers in the background.

        Returns:
            TransactionQueue result.
        """
        if _HAS_AUTH:
            self._check_auth(auth_context, Permission.REMOVE, "remove")

        if options is None:
            options = InstallOptions()

        self._audit_start(auth_context, "remove", package_names)

        queue = TransactionQueue(
            root=options.root,
            use_userns=options.use_userns,
            forward_proxy=options.forward_proxy,
        )
        queue.add_erase(
            package_names,
            operation_id="erase",
            force=options.force,
            test=options.test,
        )

        result = queue.execute(
            progress_callback=progress_callback,
            full_sync=full_sync,
        )
        self._audit_complete(auth_context, "remove", package_names, success=result.success)
        return result

    def execute_upgrade(
        self,
        rpm_paths: List[str],
        erase_names: List[str] = None,
        orphan_names: List[str] = None,
        options: InstallOptions = None,
        progress_callback: Callable[['TransactionProgress'], None] = None,
        auth_context=None,
        full_sync: bool = False,
    ) -> Any:
        """Execute upgrade via TransactionQueue.

        Combines install (with optional erase of obsoleted packages)
        and orphan cleanup in a single queue.

        Args:
            rpm_paths: RPM file paths to install/upgrade
            erase_names: Package names to remove (obsoleted)
            orphan_names: Orphaned deps to remove in background
            options: Install options
            progress_callback: Called with a TransactionProgress object
            auth_context: Optional AuthContext for permission check + audit
            full_sync: If True, wait for the entire transaction including
                triggers. If False (default), wait for extraction only and
                run triggers in the background.

        Returns:
            TransactionQueue result, or None if nothing to do
        """
        if _HAS_AUTH:
            self._check_auth(auth_context, Permission.UPGRADE, "upgrade")

        if options is None:
            options = InstallOptions()

        pkg_names = [Path(p).stem for p in rpm_paths]
        self._audit_start(auth_context, "upgrade", pkg_names)

        queue = TransactionQueue(
            root=options.root,
            use_userns=options.use_userns,
            forward_proxy=options.forward_proxy,
        )

        if rpm_paths or erase_names:
            queue.add_install(
                rpm_paths,
                operation_id="upgrade",
                verify_signatures=options.verify_signatures,
                force=options.force,
                test=options.test,
                erase_names=erase_names or [],
            )

        if orphan_names:
            queue.add_erase(
                orphan_names,
                operation_id="orphan_cleanup",
                force=options.force,
                test=options.test,
                background=True,
            )

        if queue.is_empty():
            return None

        result = queue.execute(
            progress_callback=progress_callback,
            full_sync=full_sync,
        )
        self._audit_complete(auth_context, "upgrade", pkg_names, success=result.success)
        return result

    # =========================================================================
    # Transaction History
    # =========================================================================

    def begin_transaction(
        self,
        action: str,
        command: str,
        actions: list
    ) -> int:
        """Begin a transaction and record all package actions.

        Args:
            action: Transaction type ('install', 'remove', 'upgrade')
            command: Full command line
            actions: List of PackageAction from resolver

        Returns:
            Transaction ID
        """
        transaction_id = self.db.begin_transaction(action, command)

        for pkg_action in actions:
            reason = pkg_action.reason.value if hasattr(pkg_action.reason, 'value') else str(pkg_action.reason)
            action_type = pkg_action.action.value if hasattr(pkg_action.action, 'value') else str(pkg_action.action)
            self.db.record_package(
                transaction_id,
                pkg_action.nevra,
                pkg_action.name,
                action_type,
                reason
            )

        return transaction_id

    def record_scriptlet_output(self, transaction_id: int, queue_result):
        """Persist captured scriptlet outputs to history for later review."""
        import json
        raw = getattr(queue_result, 'scriptlet_output', '')
        if not raw:
            return
        try:
            script_dict = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        errors = set(getattr(queue_result, 'script_error_packages', None) or [])
        for pkg_name, output in script_dict.items():
            text = output.strip()
            if not text:
                continue
            self.db.record_scriptlet_output(
                transaction_id, pkg_name, text,
                is_error=(pkg_name in errors),
            )

    def record_action_outcomes(self, transaction_id: int, queue_result,
                               root: str = "/") -> None:
        """Give every planned package of a transaction its verdict.

        Rows are created at ``planned`` when the transaction opens and
        stay there until something says how each one ended.  Nothing
        did, which is why every condition evaluated on ``done`` was
        false and no post-operation rule could ever fire.

        The verdict comes from the rpm database, not from rpm's
        callback.  The callback reports ``INST_STOP`` even when the
        cpio payload failed to extract, so a package can be announced
        installed and be absent; asking the database afterwards is the
        only answer that cannot lie.  The callback still supplies the
        *reason* a package is missing, which is what makes a failure
        actionable.

        Nothing is written when the child reports no attempt.  That is
        what a ``--test`` looks like from here: a transaction was built
        and never committed, so judging it against the database would
        mark every planned row failed.

        Args:
            transaction_id: The transaction, as recorded when it began.
            queue_result: What the transaction queue answered.
            root: Install root, so a chroot transaction is judged
                against its own database rather than the host's.
        """
        from .rpmdb import installed_nevras

        operations = getattr(queue_result, 'operations', None) or []
        attempted_installs, attempted_erases = set(), set()
        reasons: Dict[str, str] = {}
        for op in operations:
            attempted_installs.update(getattr(op, 'attempted', None) or [])
            attempted_erases.update(getattr(op, 'attempted_erases', None) or [])
            reasons.update(getattr(op, 'callback_reasons', None) or {})

        if not attempted_installs and not attempted_erases:
            return

        transaction = self.db.get_transaction(transaction_id) or {}
        rows = transaction.get('packages', [])
        if not rows:
            return

        pairs = tuple((row.get('pkg_name') or '', row.get('pkg_nevra') or '')
                      for row in rows)
        present = installed_nevras(pairs, root=root)

        for row in rows:
            nevra = row.get('pkg_nevra') or ''
            name = row.get('pkg_name') or ''
            is_removal = row.get('action') == 'remove'
            # A removal succeeded when the build is gone, an install
            # when it is there.  Same question, opposite answer.
            if is_removal ^ (nevra in present):
                self.db.record_action_end(transaction_id, nevra, 'done')
                continue
            reason = reasons.get(name, '')
            # ``skipped`` is for what rpm never touched, ``failed`` for
            # what it touched and lost.  Without a reason we cannot
            # tell the two apart, and claiming the gentler one would
            # hide a failure.
            status = 'skipped' if reason == 'never-started' else 'failed'
            self.db.record_action_end(transaction_id, nevra, status,
                                      error_message=reason or None)

    def complete_transaction(self, transaction_id: int):
        """Mark a transaction as successfully completed."""
        self.db.complete_transaction(transaction_id)

    def abort_transaction(self, transaction_id: int):
        """Mark a transaction as interrupted/failed."""
        self.db.abort_transaction(transaction_id)

    # =========================================================================
    # Post-operation hooks
    #
    # Split in two on purpose.  ``hooks_triggered_by`` only decides and
    # is free of side effects, so a caller can report before answering
    # its own caller; ``run_hooks`` acts, and belongs strictly after that
    # answer.  Only the acting half needs to come last: that is what lets
    # a rule target the very process carrying it out, which is the case
    # this whole mechanism exists for (see
    # ``doc/SPEC_POST_TRANSACTION_HOOKS.md``).
    # =========================================================================

    def hooks_triggered_by(self, transaction_id,
                           operation: str = "") -> List["TriggeredHook"]:
        """Which rules a finished transaction satisfies. Decides, does nothing.

        The condition is matched against what the transaction **really**
        installed, read back from the history where each package carries
        its own ``done`` / ``failed`` / ``skipped`` verdict. A partly
        applied transaction therefore needs no special handling: a
        package that never reached the disk provides nothing.

        Args:
            transaction_id: The transaction as recorded by
                :meth:`begin_transaction`.

        Returns:
            The matching rules, in load order, each paired with the
            services it applies to. Empty when no rule fires, which is
            the common case.
        """
        from .hooks import hooks_for, load_hooks

        report = load_hooks()
        if not report.hooks:
            return []

        outcome = self._operation_outcome(transaction_id, operation)
        return hooks_for(outcome, report.hooks)

    def run_hooks(self, triggered: Iterable["TriggeredHook"]
                  ) -> List[Tuple["TriggeredHook", List[Any]]]:
        """Carry out the rules' actions. Call after answering the caller.

        A hook that fails never fails the operation, which is already
        committed and recorded: the failure is reported and the remaining
        hooks still run.

        Args:
            triggered: What :meth:`hooks_triggered_by` returned.

        Returns:
            One ``(triggered, outcomes)`` pair per rule, in the same
            order.  ``outcomes`` holds one
            :class:`~urpm.core.init_system.ServiceOutcome` per service
            the rule applied to, and is empty for a rule that only
            reports, or for one that asked for a restart without naming
            anything to restart.
        """
        from . import init_system
        from .hooks import Action

        from .hooks import Operation

        results: List[Tuple["TriggeredHook", List[Any]]] = []
        for entry in triggered:
            outcomes: List[Any] = []
            declined = (entry.operation == Operation.DISTUPGRADE
                        and entry.hook.action in Action.NOT_DURING_DISTUPGRADE)
            if declined:
                # The rule is not at fault and is not refused: it is out
                # of context.  Restarting a service under a session that
                # still runs the previous release is the incident this
                # mechanism was written for, and a distupgrade ends in a
                # reboot anyway.  Say so rather than act, and rather
                # than stay silent.
                for service in sorted(entry.subjects):
                    outcomes.append(init_system.ServiceOutcome(
                        service, init_system.Result.DECLINED))
            elif entry.hook.action == Action.RESTART_SERVICE:
                # Sorted so a rule covering several services behaves the
                # same way twice in a row, in the logs as on screen.
                for service in sorted(entry.subjects):
                    outcomes.append(self._restart_for_hook(entry.hook,
                                                           service))
            elif entry.hook.action == Action.SYNC_URPMI_CONFIG:
                outcomes.append(self._sync_urpmi_config_for_hook(entry.hook))
            results.append((entry, outcomes))
        return results

    def _sync_urpmi_config_for_hook(self, hook: "Hook") -> Any:
        """Move urpmi's media to the release the machine now runs.

        The release pair comes from the distupgrade state rather than
        from the rule or from the URLs: the state is written by Stage 1
        and says exactly which release we left and which we reached,
        where guessing from a URL would have to decide whether a bare
        number in a path is a release or someone's directory.

        A rule asking for this outside a distupgrade therefore finds no
        state and does nothing, which is the right answer: there is no
        release change to follow.

        Nothing here raises.  Like every other action, it runs after the
        operation is committed and answered.
        """
        from .distupgrade.state import read_state
        from .distupgrade.version import identity_of
        from .urpmi_config import sync_urpmi_config

        try:
            state = read_state(self.db) or {}
            # ``version_to`` holds what ``ReleaseIdentity.display()``
            # produced, which is ``cauldron:11`` during a freeze.  URLs
            # carry the identity alone, so both ends go through the
            # inverse before they reach a path.
            report = sync_urpmi_config(
                identity_of(str(state.get("version_from") or "")),
                identity_of(str(state.get("version_to") or "")),
            )
        except Exception as exc:  # noqa: BLE001 — never undo a done operation
            logger.exception("hook %s could not sync urpmi.cfg",
                             hook.identifier)
            from .urpmi_config import SyncReport, URPMI_CFG
            report = SyncReport(path=URPMI_CFG,
                                errors=[f"the action raised: {exc}"])
        return report

    @staticmethod
    def _restart_for_hook(hook: "Hook", service: str) -> Any:
        """Restart one service on a rule's behalf, turning a raise into news.

        Nothing here may take the operation down: it is committed and
        recorded, and the caller has already been answered.
        """
        from . import init_system

        try:
            return init_system.try_restart(service)
        except Exception:  # noqa: BLE001 — never undo a done operation
            logger.exception("hook %s failed on %s",
                             hook.identifier, service)
            return init_system.ServiceOutcome(
                service, init_system.Result.FAILED,
                "the action raised; see the log")

    def packages_behind_hooks(self, transaction_id,
                              triggered: Iterable["TriggeredHook"]
                              ) -> List[str]:
        """The installed packages that declared what those rules watch.

        Asked by the D-Bus path and not by the CLI, because PackageKit's
        ``RequireRestart`` names a *package*: Discover turns the package
        id into the name it shows the user ("… was changed and suggests
        to be restarted").  The rules themselves never work on names, so
        the question has to be asked here rather than answered by them.

        Args:
            transaction_id: The transaction, as recorded when it began.
            triggered: What :meth:`hooks_triggered_by` returned.

        Returns:
            Package names, sorted so two identical operations report
            identically.  Empty when nothing fired.
        """
        from .rpmdb import packages_providing

        watched = {entry.hook.watch for entry in triggered}
        if not watched:
            return []

        landed, _every_row_done = self._landed_packages(transaction_id)
        requesters: set = set()
        for capability in sorted(watched):
            requesters |= packages_providing(capability, landed)
        return sorted(requesters)

    def _landed_packages(self, transaction_id) -> Tuple[set, bool]:
        """What a finished operation really put on the machine.

        Only packages recorded as ``done`` count, and only those that
        came *in*: a removal takes capabilities away rather than bringing
        them, and no rule vocabulary exists for that yet.

        Args:
            transaction_id: One transaction, or several.  A distupgrade
                is made of many — Tx A, one per Tx B batch, the retry
                pass — and a rule reasons about the whole operation, so
                the union is what it must see.

        Returns:
            The package names, and whether every recorded row succeeded.
        """
        rows = []
        for one in _as_transaction_ids(transaction_id):
            transaction = self.db.get_transaction(one) or {}
            rows += transaction.get('packages', [])
        landed = {row['pkg_name'] for row in rows
                  if row.get('status') == 'done'
                  and row.get('action') != 'remove'}
        every_row_done = bool(rows) and all(
            row.get('status') == 'done' for row in rows)
        return landed, every_row_done

    def _operation_outcome(self, transaction_id,
                           operation: str = "") -> "OperationOutcome":
        """Describe what a finished transaction actually did.

        ``operation`` is what the caller knows and the transaction row
        does not: a distupgrade reaches rpm through the same
        ``execute_install`` an upgrade uses, so only the caller can
        tell the two apart.
        """
        from .hooks import OperationOutcome
        from .rpmdb import provides_of

        landed, every_row_done = self._landed_packages(transaction_id)
        brought = provides_of(landed) if landed else {}
        return OperationOutcome(
            provides={name: frozenset(values)
                      for name, values in brought.items()},
            fully_successful=every_row_done,
            operation=operation,
        )

    def mark_dependencies(self, resolver, actions: list):
        """Mark packages as dependencies or explicit in the deps list.

        Only genuinely new packages (TransactionType.INSTALL) get their
        bookkeeping status set.  Upgrades, downgrades and reinstalls
        preserve whatever status the package already had — an explicit
        package must not be demoted to dependency just because it was
        pulled in as a transitive dep of the current transaction.

        Args:
            resolver: Resolver instance
            actions: List of PackageAction from resolver
        """
        from .resolver import InstallReason, TransactionType

        dep_packages = [a.name for a in actions
                        if a.reason != InstallReason.EXPLICIT
                        and a.action == TransactionType.INSTALL]
        explicit_packages = [a.name for a in actions
                            if a.reason == InstallReason.EXPLICIT]
        if dep_packages:
            resolver.mark_as_dependency(dep_packages)
        if explicit_packages:
            resolver.mark_as_explicit(explicit_packages)

    # =========================================================================
    # Queries (read-only operations for D-Bus/PackageKit)
    # =========================================================================

    def search_packages(
        self,
        pattern: str,
        search_provides: bool = True,
        limit: int = None
    ) -> List[Dict]:
        """Search packages by name pattern.

        Args:
            pattern: Search pattern (substring match)
            search_provides: Also search in provides capabilities
            limit: Maximum results

        Returns:
            List of package dicts with name, version, release, arch, summary, etc.
        """
        return self.db.search(pattern, limit=limit, search_provides=search_provides)

    def get_package_info(self, identifier: str) -> Optional[Dict]:
        """Get detailed package information.

        Args:
            identifier: Package name or NEVRA

        Returns:
            Package dict or None
        """
        return self.db.get_package_smart(identifier)

    def resolve_packages(self, names: List[str]) -> List[Dict]:
        """Batch resolve: get info for multiple packages at once.

        Much more efficient than calling get_package_info N times.

        Args:
            names: List of package names

        Returns:
            List of package dicts with name, version, release, arch, summary, installed
        """
        return self.db.get_packages_by_names(names)

    def search_files(self, pattern: str, limit: int = 100) -> List[Dict]:
        """Search for files matching a pattern across enabled media.

        Backed by :func:`urpm.core.files_xml.iter_file_matches`, which
        streams every enabled medium's ``files.xml.lzma`` on demand —
        no SQLite cache.

        Args:
            pattern: see :func:`urpm.core.files_xml._compile_byte_matcher`
                for the historical urpm-ng matching semantics
                (basename auto-wrap, anchored glob, …).
            limit: maximum number of results (0 means unlimited).

        Returns:
            List of dicts with ``file_path``, ``pkg_nevra``,
            ``media_name``.  Empty when no enabled medium has a
            ``files.xml.lzma`` on disk yet.
        """
        from .config import get_base_dir, get_media_local_path
        from .files_xml import FILES_XML_STUB_SIZE, iter_file_matches
        from .sync import FILES_XML_PATH

        base_dir = get_base_dir()
        media_files = []
        for media in self.db.list_media():
            if not media.get('enabled', True):
                continue
            files_xml = get_media_local_path(media, base_dir) / FILES_XML_PATH
            if (files_xml.exists()
                    and files_xml.stat().st_size > FILES_XML_STUB_SIZE):
                media_files.append((files_xml, media['name']))

        if not media_files:
            return []

        matches = iter_file_matches(media_files, pattern, limit=limit)
        return [
            {
                'file_path': m.path,
                'pkg_nevra': m.nevra,
                'media_name': m.media_name,
            }
            for m in matches
        ]

    def get_package_files(self, nevra: str) -> List[str]:
        """Get the list of files shipped by ``nevra``.

        Used by the D-Bus ``GetPackageFiles`` endpoint; CLI consumers
        should prefer ``urpm show --files`` which already reads the
        rpmdb for installed packages.

        Two-stage lookup:

        1. **Try rpmdb first.**  If the package is installed on this
           machine, ``rpm -ql`` is the authoritative source and takes
           a few ms — no synthesis parsing, no case-sensitivity
           surprise between the media's ``files.xml.lzma`` NEVRA and
           the one we were handed.  Discover's ``GetFiles`` call for
           an installed application to locate its ``.desktop`` (used
           by the *Launch* button) always hit this path.
        2. **Fall back to the media** ``files.xml.lzma``:  ``xzgrep -q``
           containment check probes each medium (xz decompresses in
           parallel with grep, ~0.5 s for the 26 MB compressed Core
           Release), and only the medium that actually carries the
           package is fully parsed.  Falls back to a straight parse
           when ``xzgrep`` is missing.

        Returns:
            List of file paths shipped by ``nevra``, or empty list
            when neither the rpmdb nor any ``files.xml.lzma`` knows
            the package.
        """
        import subprocess
        from .config import get_base_dir, get_media_local_path
        from .files_xml import FILES_XML_STUB_SIZE, parse_files_xml
        from .sync import FILES_XML_PATH

        # rpmdb path: ``rpm -ql <name>`` is the fast, authoritative
        # source for an installed package.  ``rpm -q`` does not
        # accept the ``.arch`` suffix appended to our NEVRAs
        # (``foo-1.2-3.mga10.x86_64`` fails, ``foo-1.2-3.mga10``
        # works), so we extract the bare name via the standard NEVRA
        # split ``name-version-release.arch`` and query by name.
        import re as _re
        _m = _re.match(r'^(.+?)-[^-]+-[^-]+\.[^.]+$', nevra)
        name = _m.group(1) if _m else nevra
        try:
            listing = subprocess.run(
                ['rpm', '-ql', name],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if listing.returncode == 0 and listing.stdout:
                files = [ln for ln in listing.stdout.splitlines() if ln]
                # ``(contains no files)`` is what rpm prints for a
                # metadata-only package; treat as empty and fall
                # through to the media-side scan below.
                if files and not files[0].startswith('(contains'):
                    return files
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        base_dir = get_base_dir()
        # We grep for the literal ``fn="<nevra>"`` attribute, which
        # synthesis files put once and only once per package.  ``-F``
        # turns off regex parsing so an unusual NEVRA cannot inject a
        # metacharacter.
        needle = f'fn="{nevra}"'

        for media in self.db.list_media():
            if not media.get('enabled', True):
                continue
            files_xml = get_media_local_path(media, base_dir) / FILES_XML_PATH
            if (not files_xml.exists()
                    or files_xml.stat().st_size <= FILES_XML_STUB_SIZE):
                continue

            try:
                hit = subprocess.run(
                    ['xzgrep', '-aqF', needle, str(files_xml)],
                    check=False,
                ).returncode == 0
            except FileNotFoundError:
                # ``xzgrep`` unavailable — fall back to the full
                # parse.  We pay the decompression cost but the
                # behaviour stays correct.
                hit = True

            if not hit:
                continue

            try:
                for parsed_nevra, files in parse_files_xml(files_xml):
                    if parsed_nevra == nevra:
                        return list(files)
            except Exception:
                continue
        return []

    def get_installed_packages(self) -> List[Dict]:
        """Get list of all installed packages.

        Returns:
            List of dicts with name, version, release, arch, summary, group
        """
        import subprocess

        result = subprocess.run(
            ['rpm', '-qa', '--qf',
             '%{NAME}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}\\t%{EPOCH}\\t%{SUMMARY}\\t%{GROUP}\\n'],
            capture_output=True,
            timeout=60
        )

        packages = []
        for line in result.stdout.decode(errors='replace').splitlines():
            parts = line.split('\t', 6)
            if len(parts) >= 5:
                # ``gpg-pubkey`` "packages" are RPM's way of storing
                # imported GPG keys in the rpmdb.  Their ``arch`` is
                # the literal ``(none)`` which is not a valid segment
                # in a PackageKit package_id (``name;evr;arch;data``,
                # parentheses forbidden), so the PK backend emits an
                # invalid id and PackageKit silently drops the whole
                # batch — Discover ends up with zero installed
                # packages.  These entries are also not "installable"
                # in any meaningful sense (Discover would never want
                # to remove one), so we filter them at the source.
                if parts[0] == 'gpg-pubkey':
                    continue
                epoch_str = parts[4]
                packages.append({
                    'name': parts[0],
                    'version': parts[1],
                    'release': parts[2],
                    'arch': parts[3],
                    'epoch': int(epoch_str) if epoch_str not in ('', '(none)') else 0,
                    'summary': parts[5] if len(parts) > 5 else '',
                    'group': parts[6] if len(parts) > 6 else '',
                    'installed': True,
                })

        return packages

    def download_to_directory(
        self,
        package_names: List[str],
        directory: str,
        progress_callback: Callable = None
    ) -> Tuple[bool, List[str], str]:
        """Download packages to a specific directory.

        Args:
            package_names: List of package names to download
            directory: Destination directory
            progress_callback: Optional progress callback

        Returns:
            (success, list of downloaded file paths, error message)
        """
        import shutil
        from pathlib import Path

        dest_dir = Path(directory)
        if not dest_dir.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)

        # Resolve packages
        download_items, _ = self.resolve_install(package_names)
        if not download_items:
            return False, [], "No packages to download"

        # Download to cache
        dl_results, downloaded, cached, _ = self.download_packages(
            download_items, progress_callback=progress_callback
        )

        # Copy/link to destination directory
        downloaded_paths = []
        for item, result in zip(download_items, dl_results):
            if result.success and result.path:
                src = Path(result.path)
                dest = dest_dir / src.name
                try:
                    shutil.copy2(src, dest)
                    downloaded_paths.append(str(dest))
                except Exception as e:
                    return False, downloaded_paths, f"Failed to copy {src.name}: {e}"

        return True, downloaded_paths, ""

    def whatrequires(self, package_name: str) -> List[Dict]:
        """Find packages that require a given package.

        Args:
            package_name: Package name to check

        Returns:
            List of package dicts that depend on this package
        """
        return self.db.whatrequires(package_name)

    def install_local_files(
        self,
        rpm_paths: List[str],
        progress_callback: Callable = None
    ) -> Tuple[bool, str]:
        """Install local RPM files.

        Args:
            rpm_paths: List of paths to RPM files
            progress_callback: Optional progress callback

        Returns:
            (success, error message)
        """
        import subprocess
        from pathlib import Path

        # Verify files exist
        for path in rpm_paths:
            if not Path(path).exists():
                return False, f"File not found: {path}"

        # Install with rpm
        try:
            result = subprocess.run(
                ['rpm', '-Uvh', '--replacepkgs'] + rpm_paths,
                capture_output=True,
                timeout=600
            )
            if result.returncode != 0:
                return False, result.stderr.decode(errors='replace')
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "Installation timed out"
        except Exception as e:
            return False, str(e)

    def get_updates(self, arch: str = None) -> Tuple[bool, list, list]:
        """Get list of available updates.

        Args:
            arch: System architecture (default: auto-detect)

        Returns:
            (success, upgrades, problems)
            - success: True if resolution succeeded
            - upgrades: List of PackageAction for available upgrades
            - problems: List of problem strings if resolution failed
        """
        import platform
        from .resolver import Resolver

        if arch is None:
            arch = platform.machine()

        resolver = Resolver(self.db, arch=arch)
        result = resolver.resolve_upgrade()

        if not result.success:
            return False, [], result.problems

        upgrades = [a for a in result.actions if a.action.value == 'upgrade']
        return True, upgrades, []

    # =========================================================================
    # Cache management
    # =========================================================================

    @staticmethod
    def notify_urpmd_cache_invalidate():
        """Notify urpmd that cache has changed (for P2P sharing)."""
        _post_to_urpmd("/api/invalidate-cache")

    @staticmethod
    def notify_urpmd_media_tail(media_ids: List[int]) -> bool:
        """Ask urpmd to finish what a deferred media update left behind.

        Returns whether the daemon took the job.  That answer is the
        whole point: the caller has just handed the terminal back after
        syncing the synthesis, and something still has to fetch the file
        index.  A ``False`` here means nobody is listening and the
        caller must do it itself.

        The endpoint returns as soon as the work is queued, not when it
        is done — a 36 MB fetch would blow through any sane client
        timeout, and a timeout would read as "no daemon" and get the
        work done twice.
        """
        if not media_ids:
            return True
        return _post_to_urpmd(
            "/api/media-tail",
            json.dumps({"media_ids": list(media_ids)}).encode(),
        )
