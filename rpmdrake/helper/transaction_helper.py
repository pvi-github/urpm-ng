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
import sys
import signal
from pathlib import Path
from typing import List

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
        """Main loop: read commands from stdin."""
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
            self._execute(cmd)
        elif cmd_type == "cancel":
            self.cancelled = True
            self._send({"type": "cancelled"})
        else:
            self._error(f"Unknown command: {cmd_type}")

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
                problems = "; ".join(resolution.problems) if resolution.problems else "Resolution failed"
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
                problems = "; ".join(resolution.problems) if resolution.problems else "Échec de la résolution"
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
            resolution = resolver.resolve_install(packages, choices=choices or {})

            if not resolution.success:
                problems = "; ".join(resolution.problems) if resolution.problems else "Resolution failed"
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
                problems = "; ".join(resolution.problems) if resolution.problems else "Resolution failed"
                self._error(f"Resolution failed: {problems}")
                return

            if not resolution.actions:
                self._send({"type": "done", "success": True, "count": 0, "message": "System is up to date"})
                return

            # Download and upgrade
            self._download_and_install(resolution, resolver, "upgrade")

        except Exception as e:
            self._error(f"System upgrade failed: {e}")

    def _download_and_install(self, resolution, resolver, operation_id: str, requested_packages: List[str] = None):
        """Download packages and run install/upgrade transaction."""
        # Build download items
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
                self._send({
                    "type": "download_progress",
                    "name": name,
                    "current": pkg_num,
                    "total": pkg_total
                })
                return True

            dl_results, downloaded, cached, peer_stats = self.ops.download_packages(
                download_items, progress_callback=progress_callback
            )

            # Check for failures
            failed = [r for r in dl_results if not r.success]
            if failed:
                self._error(f"Download failed: {failed[0].error}")
                return

            # Collect downloaded paths
            rpm_paths.extend([str(r.path) for r in dl_results if r.success and r.path])

        if not rpm_paths:
            self._send({"type": "done", "success": True, "count": 0, "message": "Nothing to install"})
            return

        # Get packages to erase (for obsoletes)
        erase_names = [a.name for a in resolution.actions if a.action.name == 'REMOVE']

        # Run transaction
        self._status(f"Installing {len(rpm_paths)} package(s)...")

        def install_progress(op_id, name, current, total):
            self._send({
                "type": "install_progress",
                "name": name,
                "current": current,
                "total": total
            })

        options = InstallOptions()
        options.sync = True  # Wait for full rpmdb sync before returning
        result = self.ops.execute_upgrade(
            rpm_paths,
            erase_names=erase_names,
            options=options,
            progress_callback=install_progress
        )

        if result.success:
            total_count = sum(op.count for op in result.operations)

            # Mark dependency packages (not explicitly requested)
            if requested_packages:
                requested_lower = {p.lower() for p in requested_packages}
                dep_packages = []
                for a in resolution.actions:
                    if a.action.name in ('INSTALL', 'UPGRADE'):
                        if a.name.lower() not in requested_lower:
                            dep_packages.append(a.name)
                if dep_packages:
                    try:
                        resolver.mark_as_dependency(dep_packages)
                    except Exception:
                        pass  # Non-critical, don't fail the transaction

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
            self._error("; ".join(errors) if errors else "Transaction failed")

    def _run_erase_transaction(self, resolution):
        """Run erase transaction."""
        erase_names = [a.name for a in resolution.actions if a.action.name == 'REMOVE']

        if not erase_names:
            self._send({"type": "done", "success": True, "count": 0})
            return

        self._status(f"Removing {len(erase_names)} package(s)...")

        def progress_callback(op_id, name, current, total):
            self._send({
                "type": "erase_progress",
                "name": name,
                "current": current,
                "total": total
            })

        options = InstallOptions()
        options.sync = True  # Wait for full rpmdb sync before returning
        result = self.ops.execute_erase(
            erase_names,
            options=options,
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
            self._error("; ".join(errors) if errors else "Transaction failed")


def run_helper():
    """Entry point for the helper."""
    helper = TransactionHelper()
    helper.run()


if __name__ == "__main__":
    run_helper()
