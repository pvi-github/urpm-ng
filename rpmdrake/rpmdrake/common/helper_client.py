"""Client for communicating with the transaction helper.

Spawns the helper via pkexec and communicates via JSON on stdin/stdout.
"""

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

__all__ = ["HelperClient", "TransactionResult", "DownloadSlotInfo"]


@dataclass
class DownloadSlotInfo:
    """Info about a single download slot."""
    slot: int
    name: Optional[str] = None
    bytes_done: int = 0
    bytes_total: int = 0
    source: str = ""
    source_type: str = ""  # 'server', 'peer', 'cache'


@dataclass
class TransactionResult:
    """Result of a transaction."""
    success: bool
    count: int = 0
    error: str = ""
    message: str = ""


class HelperClient:
    """Client for communicating with the privileged helper."""

    def __init__(
        self,
        on_status: Callable[[str], None] = None,
        on_download_progress: Callable[[str, int, int, int, int, List[DownloadSlotInfo]], None] = None,
        on_install_progress: Callable[[str, int, int], None] = None,
        on_error: Callable[[str], None] = None,
        on_done: Callable[[TransactionResult], None] = None,
    ):
        """Initialize helper client.

        Args:
            on_status: Called with status messages.
            on_download_progress: Called with (name, current, total, bytes_done, bytes_total, slots)
                                  during download. slots is a list of DownloadSlotInfo.
            on_install_progress: Called with (name, current, total) during install.
            on_error: Called with error messages.
            on_done: Called when transaction completes.
        """
        self.on_status = on_status
        self.on_download_progress = on_download_progress
        self.on_install_progress = on_install_progress
        self.on_error = on_error
        self.on_done = on_done

        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None

    def _get_helper_path(self) -> str:
        """Get path to the helper script."""
        # Try installed path first
        installed = "/usr/libexec/rpmdrake-ng-helper"
        if os.path.exists(installed):
            return installed

        # Development path
        dev_path = Path(__file__).parent.parent.parent / "bin" / "rpmdrake-ng-helper"
        if dev_path.exists():
            return str(dev_path)

        raise FileNotFoundError("rpmdrake-ng-helper not found")

    def _start_helper(self) -> bool:
        """Start the helper process via pkexec."""
        try:
            helper_path = self._get_helper_path()

            # Use pkexec for privilege escalation
            self._process = subprocess.Popen(
                ["pkexec", helper_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1  # Line buffered
            )

            # Start reader thread
            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()

            return True

        except FileNotFoundError as e:
            if self.on_error:
                self.on_error(f"Helper not found: {e}")
            return False
        except Exception as e:
            if self.on_error:
                self.on_error(f"Failed to start helper: {e}")
            return False

    def _read_output(self):
        """Read output from helper (runs in thread)."""
        if not self._process or not self._process.stdout:
            return

        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                    self._handle_message(msg)
                except json.JSONDecodeError:
                    pass

        except Exception as e:
            if self.on_error:
                self.on_error(f"Reader error: {e}")

    def _handle_message(self, msg: dict):
        """Handle a message from the helper."""
        msg_type = msg.get("type")

        if msg_type == "status":
            if self.on_status:
                self.on_status(msg.get("message", ""))

        elif msg_type == "download_progress":
            if self.on_download_progress:
                # Parse slots info
                slots = []
                for slot_data in msg.get("slots", []):
                    slots.append(DownloadSlotInfo(
                        slot=slot_data.get("slot", 0),
                        name=slot_data.get("name"),
                        bytes_done=slot_data.get("bytes_done", 0),
                        bytes_total=slot_data.get("bytes_total", 0),
                        source=slot_data.get("source", ""),
                        source_type=slot_data.get("source_type", ""),
                    ))
                self.on_download_progress(
                    msg.get("name", ""),
                    msg.get("current", 0),
                    msg.get("total", 0),
                    msg.get("bytes_done", 0),
                    msg.get("bytes_total", 0),
                    slots
                )

        elif msg_type == "install_progress" or msg_type == "erase_progress":
            if self.on_install_progress:
                self.on_install_progress(
                    msg.get("name", ""),
                    msg.get("current", 0),
                    msg.get("total", 0)
                )

        elif msg_type == "error":
            if self.on_error:
                self.on_error(msg.get("message", "Unknown error"))
            # Also signal done with failure
            if self.on_done:
                self.on_done(TransactionResult(
                    success=False,
                    error=msg.get("message", "Unknown error")
                ))

        elif msg_type == "done":
            if self.on_done:
                self.on_done(TransactionResult(
                    success=msg.get("success", False),
                    count=msg.get("count", 0),
                    message=msg.get("message", "")
                ))

        elif msg_type == "cancelled":
            if self.on_done:
                self.on_done(TransactionResult(
                    success=False,
                    error="Cancelled"
                ))

    def _send_command(self, cmd: dict) -> bool:
        """Send a command to the helper."""
        if not self._process or not self._process.stdin:
            return False

        try:
            self._process.stdin.write(json.dumps(cmd) + "\n")
            self._process.stdin.flush()
            return True
        except Exception as e:
            if self.on_error:
                self.on_error(f"Failed to send command: {e}")
            return False

    def install(self, packages: List[str], choices: Dict[str, str] = None) -> bool:
        """Install packages.

        Args:
            packages: List of package names to install.
            choices: Optional dict mapping capability -> chosen package name
                     for resolving alternatives.

        Returns:
            True if command was sent successfully.
        """
        if not self._start_helper():
            return False

        cmd = {
            "cmd": "execute",
            "action": "install",
            "packages": packages
        }
        if choices:
            cmd["choices"] = choices

        return self._send_command(cmd)

    def erase(self, packages: List[str]) -> bool:
        """Remove packages.

        Args:
            packages: List of package names to remove.

        Returns:
            True if command was sent successfully.
        """
        if not self._start_helper():
            return False

        return self._send_command({
            "cmd": "execute",
            "action": "erase",
            "packages": packages
        })

    def upgrade(self, packages: List[str], choices: Dict[str, str] = None) -> bool:
        """Upgrade specific packages.

        Args:
            packages: List of package names to upgrade.
            choices: Optional dict mapping capability -> chosen package name
                     for resolving alternatives.

        Returns:
            True if command was sent successfully.
        """
        if not self._start_helper():
            return False

        cmd = {
            "cmd": "execute",
            "action": "upgrade",
            "packages": packages
        }
        if choices:
            cmd["choices"] = choices

        return self._send_command(cmd)

    def upgrade_all(self, choices: Dict[str, str] = None) -> bool:
        """Upgrade all packages with available updates.

        Args:
            choices: Optional dict mapping capability -> chosen package name
                     for resolving alternatives.

        Returns:
            True if command was sent successfully.
        """
        if not self._start_helper():
            return False

        cmd = {
            "cmd": "execute",
            "action": "upgrade_all"
        }
        if choices:
            cmd["choices"] = choices

        return self._send_command(cmd)

    def cancel(self):
        """Cancel the current operation."""
        if self._process:
            self._send_command({"cmd": "cancel"})
            # Give it a moment to clean up
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
            self._process = None

    def wait(self, timeout: float = None) -> bool:
        """Wait for the helper to complete.

        Args:
            timeout: Maximum time to wait in seconds.

        Returns:
            True if helper completed, False if timeout.
        """
        if self._reader_thread:
            self._reader_thread.join(timeout=timeout)
            return not self._reader_thread.is_alive()
        return True
