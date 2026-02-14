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

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Tuple

from .database import PackageDatabase
from .download import Downloader, DownloadItem
from .transaction_queue import TransactionQueue

logger = logging.getLogger(__name__)

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
    use_peers: bool = True
    only_peers: bool = False
    root: str = "/"
    use_userns: bool = False
    sync: bool = False


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

        for action in actions:
            if action.action == TransactionType.REMOVE:
                continue

            media_name = action.media_name

            # Local RPMs don't need download
            if media_name == '@LocalRPMs':
                pkg_info = resolver._solvable_to_pkg.get(action.nevra)
                if not pkg_info:
                    for sid, info in resolver._solvable_to_pkg.items():
                        if (info.get('name') == action.name and
                            info.get('evr') == action.evr and
                            info.get('arch') == action.arch and
                            info.get('media_name') == '@LocalRPMs'):
                            pkg_info = info
                            break
                if not pkg_info and local_rpm_infos:
                    for info in local_rpm_infos:
                        if info.get('name') == action.name:
                            pkg_info = info
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

        return download_items, local_action_paths

    def download_packages(
        self,
        download_items: List[DownloadItem],
        options: InstallOptions = None,
        progress_callback: Callable = None,
        urpm_root: str = None
    ) -> Tuple[list, int, int, dict]:
        """Download packages.

        Args:
            download_items: Items to download
            options: Install options (peers config)
            progress_callback: Download progress callback
            urpm_root: Override base dir for cache

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
            db=self.db
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
        progress_callback: Callable[[str, str, int, int], None] = None,
        auth_context=None
    ) -> Any:
        """Execute RPM installation via TransactionQueue.

        Args:
            rpm_paths: List of RPM file paths to install
            options: Install options
            progress_callback: Called with (op_id, name, current, total)
            auth_context: Optional AuthContext for permission check + audit

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
            use_userns=options.use_userns
        )
        queue.add_install(
            rpm_paths,
            operation_id="install",
            verify_signatures=options.verify_signatures,
            force=options.force,
            test=options.test,
            reinstall=options.reinstall,
            noscripts=options.noscripts
        )

        result = queue.execute(
            progress_callback=progress_callback,
            sync=options.sync
        )
        self._audit_complete(auth_context, "install", pkg_names, success=True)
        return result

    def execute_erase(
        self,
        package_names: List[str],
        options: InstallOptions = None,
        progress_callback: Callable[[str, str, int, int], None] = None,
        auth_context=None
    ) -> Any:
        """Execute RPM removal via TransactionQueue.

        Args:
            package_names: Package names to remove
            options: Install options
            progress_callback: Called with (op_id, name, current, total)
            auth_context: Optional AuthContext for permission check + audit

        Returns:
            TransactionQueue result
        """
        if _HAS_AUTH:
            self._check_auth(auth_context, Permission.REMOVE, "remove")

        if options is None:
            options = InstallOptions()

        self._audit_start(auth_context, "remove", package_names)

        queue = TransactionQueue(
            root=options.root,
            use_userns=options.use_userns
        )
        queue.add_erase(
            package_names,
            operation_id="erase",
            force=options.force,
            test=options.test,
        )

        result = queue.execute(
            progress_callback=progress_callback,
            sync=options.sync
        )
        self._audit_complete(auth_context, "remove", package_names, success=True)
        return result

    def execute_upgrade(
        self,
        rpm_paths: List[str],
        erase_names: List[str] = None,
        orphan_names: List[str] = None,
        options: InstallOptions = None,
        progress_callback: Callable[[str, str, int, int], None] = None,
        auth_context=None
    ) -> Any:
        """Execute upgrade via TransactionQueue.

        Combines install (with optional erase of obsoleted packages)
        and orphan cleanup in a single queue.

        Args:
            rpm_paths: RPM file paths to install/upgrade
            erase_names: Package names to remove (obsoleted)
            orphan_names: Orphaned deps to remove in background
            options: Install options
            progress_callback: Called with (op_id, name, current, total)
            auth_context: Optional AuthContext for permission check + audit

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
            use_userns=options.use_userns
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
            sync=options.sync
        )
        self._audit_complete(auth_context, "upgrade", pkg_names, success=True)
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

    def complete_transaction(self, transaction_id: int):
        """Mark a transaction as successfully completed."""
        self.db.complete_transaction(transaction_id)

    def abort_transaction(self, transaction_id: int):
        """Mark a transaction as interrupted/failed."""
        self.db.abort_transaction(transaction_id)

    def mark_dependencies(self, resolver, actions: list):
        """Mark packages as dependencies or explicit in the deps list.

        Args:
            resolver: Resolver instance
            actions: List of PackageAction from resolver
        """
        from .resolver import InstallReason

        dep_packages = [a.name for a in actions
                        if a.reason != InstallReason.EXPLICIT]
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
        """Search for files matching a pattern.

        Args:
            pattern: File path pattern (glob-style)
            limit: Maximum results

        Returns:
            List of dicts with file_path, pkg_nevra, media_name
        """
        return self.db.search_files(pattern, limit=limit)

    def get_package_files(self, nevra: str) -> List[str]:
        """Get list of files for a package.

        Args:
            nevra: Package NEVRA

        Returns:
            List of file paths
        """
        return self.db.get_package_files(nevra)

    def get_installed_packages(self) -> List[Dict]:
        """Get list of all installed packages.

        Returns:
            List of dicts with name, version, release, arch, summary
        """
        import subprocess

        result = subprocess.run(
            ['rpm', '-qa', '--qf', '%{NAME}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}\\t%{SUMMARY}\\n'],
            capture_output=True,
            timeout=60
        )

        packages = []
        for line in result.stdout.decode(errors='replace').splitlines():
            parts = line.split('\t', 4)
            if len(parts) >= 4:
                packages.append({
                    'name': parts[0],
                    'version': parts[1],
                    'release': parts[2],
                    'arch': parts[3],
                    'summary': parts[4] if len(parts) > 4 else '',
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
        try:
            import urllib.request
            from .config import get_port
            port = get_port()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/invalidate-cache",
                method='POST',
                data=b''
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass  # urpmd may not be running
