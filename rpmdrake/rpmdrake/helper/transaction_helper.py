#!/usr/bin/env python3
"""Transaction helper for rpmdrake-ng.

This script runs as root via pkexec to execute package operations.
Communication with the GUI is via JSON on stdin/stdout.

Protocol:
    GUI -> Helper (stdin):
        {"cmd": "execute", "action": "install", "packages": ["firefox", "vlc"]}
        {"cmd": "execute", "action": "erase", "packages": ["foo"]}
        {"cmd": "execute", "action": "upgrade", "packages": ["firefox"]}
        {"cmd": "execute", "action": "upgrade_all"}
        {"cmd": "cancel"}

    Helper -> GUI (stdout):
        {"type": "status", "message": "Resolving dependencies..."}
        {"type": "download_progress", "name": "firefox.rpm", "current": 1000, "total": 5000}
        {"type": "install_progress", "name": "firefox", "current": 1, "total": 3}
        {"type": "error", "message": "Dependency error: ..."}
        {"type": "done", "success": true, "count": 3}
"""

import json
import logging
import sys
import signal
import threading
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from urpm.core.database import PackageDatabase
from urpm.core.resolver import Resolver
from urpm.core.operations import PackageOperations, InstallOptions


class TransactionHelper:
    """Helper class to execute package transactions."""

    def __init__(self):
        self.db = PackageDatabase()
        self.ops = PackageOperations(self.db)
        self.cancelled = False
        self._stdin_watcher: threading.Thread | None = None
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful cancellation."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        """Handle termination signals."""
        self.cancelled = True
        self._send({"type": "cancelled"})
        sys.exit(0)

    def _send(self, msg: dict):
        """Send JSON message to stdout."""
        print(json.dumps(msg), flush=True)

    def _status(self, message: str):
        """Send status message."""
        self._send({"type": "status", "message": message})

    def _error(self, message: str):
        """Send error message."""
        self._send({"type": "error", "message": message})

    def run(self):
        """Main loop: read commands from stdin.

        Reads one command at a time. During long operations (download/install),
        a background stdin watcher thread reads cancel commands in parallel.
        """
        for line in sys.stdin:
            if self.cancelled:
                break

            line = line.strip()
            if not line:
                continue

            try:
                cmd = json.loads(line)
            except json.JSONDecodeError as e:
                self._error(f"Invalid JSON: {e}")
                continue

            self._handle_command(cmd)

    def _handle_command(self, cmd: dict):
        """Handle a single command."""
        cmd_type = cmd.get("cmd")

        if cmd_type == "execute":
            try:
                self._execute(cmd)
            finally:
                self._stop_stdin_watcher()
        elif cmd_type == "cancel":
            self.cancelled = True
            self._send({"type": "cancelled"})
        else:
            self._error(f"Unknown command: {cmd_type}")

    def _wait_response(self, timeout: float = 300) -> dict | None:
        """Wait for a single JSON response from stdin (synchronous).

        Called before the stdin watcher is started (e.g. for README
        confirmation), so there is no contention on stdin.

        Returns the parsed JSON dict, or None on timeout / error.
        """
        import select
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            line = sys.stdin.readline().strip()
            if not line:
                return None
            return json.loads(line)
        except (json.JSONDecodeError, OSError):
            return None

    def _start_stdin_watcher(self):
        """Start a daemon thread that reads stdin for cancel commands.

        While _execute is running (blocking the main loop), this thread
        listens for {"cmd": "cancel"} and sets self.cancelled = True.
        The download callback checks this flag and stops cleanly.
        """
        self._stdin_watcher_stop = threading.Event()

        def _watch():
            for line in sys.stdin:
                if self._stdin_watcher_stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("cmd") == "cancel":
                    self.cancelled = True
                    break

        self._stdin_watcher = threading.Thread(target=_watch, daemon=True)
        self._stdin_watcher.start()

    def _stop_stdin_watcher(self):
        """Stop the stdin watcher thread."""
        if self._stdin_watcher:
            self._stdin_watcher_stop.set()
            self._stdin_watcher = None

    def _execute(self, cmd: dict):
        """Execute a package operation."""
        action = cmd.get("action")
        packages = cmd.get("packages", [])
        choices = cmd.get("choices", {})  # capability -> chosen package

        if action == "install":
            self._do_install(packages, choices)
        elif action == "erase":
            self._do_erase(packages)
        elif action == "upgrade":
            self._do_upgrade(packages, choices)
        elif action == "upgrade_all":
            self._do_upgrade_all(choices)
        else:
            self._error(f"Unknown action: {action}")

    def _do_install(self, packages: List[str], choices: dict = None):
        """Install packages."""
        if not packages:
            self._error("No packages specified")
            return

        self._status(f"Resolving dependencies for {len(packages)} package(s)...")

        try:
            resolver = Resolver(self.db)
            resolution = resolver.resolve_install(packages, choices=choices or {})

            if not resolution.success:
                problems = "\n".join(resolution.problems) if resolution.problems else "Resolution failed"
                self._error(f"Resolution failed: {problems}")
                return

            if not resolution.actions:
                self._send({"type": "done", "success": True, "count": 0, "message": "Nothing to do"})
                return

            # Download and install
            self._download_and_install(resolution, resolver, "install", requested_packages=packages)

        except Exception as e:
            self._error(f"Install failed: {e}")

    def _do_erase(self, packages: List[str]):
        """Erase packages."""
        if not packages:
            self._error("No packages specified")
            return

        self._status(f"Résolution des dépendances pour {len(packages)} paquet(s)...")

        try:
            resolver = Resolver(self.db)
            resolution = resolver.resolve_remove(packages)

            if not resolution.success:
                problems = "\n".join(resolution.problems) if resolution.problems else "Échec de la résolution"
                self._error(f"Échec de la résolution: {problems}")
                return

            if not resolution.actions:
                self._send({"type": "done", "success": True, "count": 0, "message": "Rien à faire"})
                return

            # Erase (no download needed)
            self._run_erase_transaction(resolution)

        except Exception as e:
            self._error(f"Échec de la suppression: {e}")

    def _do_upgrade(self, packages: List[str], choices: dict = None):
        """Upgrade specific packages."""
        if not packages:
            self._error("No packages specified")
            return

        self._status(f"Resolving upgrades for {len(packages)} package(s)...")

        try:
            resolver = Resolver(self.db)
            resolution = resolver.resolve_upgrade(package_names=packages)

            if not resolution.success:
                problems = "\n".join(resolution.problems) if resolution.problems else "Resolution failed"
                self._error(f"Resolution failed: {problems}")
                return

            if not resolution.actions:
                self._send({"type": "done", "success": True, "count": 0, "message": "Packages already up to date"})
                return

            # Download and upgrade
            self._download_and_install(resolution, resolver, "upgrade")

        except Exception as e:
            self._error(f"Upgrade failed: {e}")

    def _do_upgrade_all(self, choices: dict = None):
        """Upgrade all packages with available updates."""
        self._status("Resolving system upgrade...")

        try:
            resolver = Resolver(self.db)
            resolution = resolver.resolve_upgrade()

            if not resolution.success:
                problems = "\n".join(resolution.problems) if resolution.problems else "Resolution failed"
                self._error(f"Resolution failed: {problems}")
                return

            if not resolution.actions:
                self._send({"type": "done", "success": True, "count": 0, "message": "System is up to date"})
                return

            # Download and upgrade
            self._download_and_install(resolution, resolver, "upgrade")

        except Exception as e:
            self._error(f"System upgrade failed: {e}")

    @staticmethod
    def _name_from_nevra(nevra: str) -> str:
        """Extract package name from a NEVRA string (name-version-release.arch).

        Falls back to returning the string as-is if it doesn't look like a NEVRA.
        """
        dot_pos = nevra.rfind('.')
        if dot_pos < 0:
            return nevra
        without_arch = nevra[:dot_pos]
        dash_pos = without_arch.rfind('-')
        if dash_pos < 0:
            return nevra
        without_release = without_arch[:dash_pos]
        dash_pos = without_release.rfind('-')
        if dash_pos < 0:
            return nevra
        return without_release[:dash_pos]

    def _download_and_install(self, resolution, resolver, operation_id: str, requested_packages: List[str] = None):
        """Download packages and run install/upgrade transaction.

        Uses the resilient install pipeline which pre-verifies GPG
        signatures, retries failed downloads from alternate mirrors, and
        excludes unrecoverable packages before executing the transaction.
        """
        # Build download items (kept for retry in resilient_install)
        download_items, local_paths = self.ops.build_download_items(
            resolution.actions, resolver, []
        )

        rpm_paths = list(local_paths)

        # Download if needed
        if download_items:
            self._status(f"Downloading {len(download_items)} package(s)...")

            def progress_callback(name, pkg_num, pkg_total, bytes_done, bytes_total,
                                  item_bytes=None, item_total=None, slots_status=None):
                if self.cancelled:
                    return False

                # Build slots info for parallel display
                slots = []
                if slots_status:
                    for slot, prog in slots_status:
                        if prog is not None:
                            slots.append({
                                "slot": slot,
                                "name": prog.name,
                                "bytes_done": prog.bytes_done,
                                "bytes_total": prog.bytes_total,
                                "source": prog.source,
                                "source_type": prog.source_type,
                            })
                        else:
                            slots.append({"slot": slot, "name": None})

                self._send({
                    "type": "download_progress",
                    "name": name,
                    "current": pkg_num,
                    "total": pkg_total,
                    "bytes_done": bytes_done,
                    "bytes_total": bytes_total,
                    "slots": slots,
                })
                return True

            dl_results, downloaded, cached, peer_stats = self.ops.download_packages(
                download_items, progress_callback=progress_callback
            )

            # Cancel check: if cancelled during download, abort before install
            if self.cancelled:
                self._send({"type": "cancelled"})
                return

            # Check for failures
            failed = [r for r in dl_results if not r.success]
            if failed:
                self._error(f"Download failed: {failed[0].error}")
                return

            # Collect downloaded paths
            rpm_paths.extend([str(r.path) for r in dl_results if r.success and r.path])

        # Cancel check before install phase
        if self.cancelled:
            self._send({"type": "cancelled"})
            return

        if not rpm_paths:
            self._send({"type": "done", "success": True, "count": 0, "message": "Nothing to install"})
            return

        # Get packages to erase (for obsoletes)
        erase_names = [a.name for a in resolution.actions if a.action.name == 'REMOVE']

        # README.urpmi messages are collected post-install from the filesystem
        # by the child process (same approach as the CLI).  They are available
        # in result.queue_result.operations[0].readme_messages after full_sync.

        # Run transaction via resilient pipeline (signature pre-check,
        # retry from alternate mirrors, exclusion of bad packages)
        self._status(f"Installing {len(rpm_paths)} package(s)...")

        def install_progress(tp):
            data = {
                "type": "install_progress",
                "name": tp.package_name,
                "current": tp.packages_done,
                "total": tp.packages_total,
                "phase": tp.phase.value,
                "bytes_done": tp.bytes_done,
                "bytes_total": tp.bytes_total,
            }
            if tp.script_name:
                data["script"] = tp.script_name
            self._send(data)

        # Check if any package provides should-restart:system — force full sync
        from urpm.core.needs_restart import check_needs_restart_from_provides
        restart_info = {}
        for a in resolution.actions:
            if a.action.name in ('INSTALL', 'UPGRADE'):
                pkg_info = self.db.get_package(a.name)
                if pkg_info and pkg_info.get('provides'):
                    restart_info[a.name] = pkg_info['provides']
        needs_restart = check_needs_restart_from_provides(restart_info)

        # Smart sync (default): return after extraction + per-package scripts.
        # Generic triggers (shared-mime-info, ldconfig, etc.) run in background.
        # READMEs are collected and sent before triggers (phase='install_done').
        # Full sync only when a system restart is required.
        full_sync = 'system' in needs_restart if needs_restart else False

        options = InstallOptions()
        result = self.ops.resilient_install(
            rpm_paths,
            download_items=download_items,
            options=options,
            actions=resolution.actions,
            progress_callback=install_progress,
            erase_names=erase_names,
            mode="upgrade" if operation_id == "upgrade" else "install",
            full_sync=full_sync,
        )

        if result.success:
            # Mark dependency packages (not explicitly requested)
            if requested_packages:
                # Extract names from NEVRAs for comparison (handles both names and NEVRAs)
                requested_names = {self._name_from_nevra(p).lower() for p in requested_packages}
                dep_packages = []
                for a in resolution.actions:
                    if a.action.name in ('INSTALL', 'UPGRADE'):
                        if a.name.lower() not in requested_names:
                            dep_packages.append(a.name)
                if dep_packages:
                    try:
                        resolver.mark_as_dependency(dep_packages)
                    except Exception as exc:
                        # Non-critical: the install itself succeeded, only
                        # the explicit/dependency bookkeeping is degraded.
                        # Surface it in the log so we notice if it becomes
                        # systematic (e.g. DB schema drift).
                        logger.warning(
                            "mark_as_dependency failed for %d package(s): %s",
                            len(dep_packages), exc, exc_info=True,
                        )

            # Collect post-install README messages from the transaction result
            readme_messages = []
            if result.queue_result and result.queue_result.operations:
                readme_messages = result.queue_result.operations[0].readme_messages or []

            done_msg = {
                "type": "done",
                "success": True,
                "count": result.installed,
                "readme_messages": readme_messages,
            }
            # Report restart requirements to the GUI
            if needs_restart:
                from urpm.core.needs_restart import format_restart_messages
                done_msg["restart_messages"] = format_restart_messages(needs_restart)
            # Report excluded packages so the GUI can inform the user
            if result.excluded_packages:
                done_msg["excluded_packages"] = [
                    {"name": name, "reason": reason}
                    for name, reason in result.excluded_packages
                ]
                done_msg["reduced_transaction"] = True
            self._send(done_msg)
        else:
            error_msg = "\n".join(result.errors) if result.errors else "Transaction failed"
            self._error(error_msg)

    def _run_erase_transaction(self, resolution):
        """Run erase transaction."""
        erase_names = [a.name for a in resolution.actions if a.action.name == 'REMOVE']

        if not erase_names:
            self._send({"type": "done", "success": True, "count": 0})
            return

        self._status(f"Removing {len(erase_names)} package(s)...")

        def progress_callback(tp):
            self._send({
                "type": "erase_progress",
                "name": tp.package_name,
                "current": tp.packages_done,
                "total": tp.packages_total,
                "phase": tp.phase.value,
            })

        options = InstallOptions()
        result = self.ops.execute_erase(
            erase_names,
            options=options,
            full_sync=False,
            progress_callback=progress_callback
        )

        if result.success:
            total_count = sum(op.count for op in result.operations)
            self._send({
                "type": "done",
                "success": True,
                "count": total_count
            })
        else:
            errors = []
            for op in result.operations:
                errors.extend(op.errors)
            if result.overall_error:
                errors.append(result.overall_error)
            self._error("\n".join(errors) if errors else "Transaction failed")


def run_helper():
    """Entry point for the helper."""
    helper = TransactionHelper()
    helper.run()


if __name__ == "__main__":
    run_helper()
