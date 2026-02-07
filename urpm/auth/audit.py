"""Structured audit logging for urpm operations.

Logs all privileged operations (install, remove, upgrade, media changes)
to /var/log/urpm/audit.log in JSON format, one event per line.

The audit log is append-only and intended for security review.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, List

from .context import AuthContext

logger = logging.getLogger(__name__)

# Default audit log location
AUDIT_LOG_DIR = Path("/var/log/urpm")
AUDIT_LOG_FILE = AUDIT_LOG_DIR / "audit.log"


class AuditLogger:
    """Append-only JSON audit logger for privileged operations."""

    def __init__(self, log_path: Path = None):
        self._log_path = log_path or AUDIT_LOG_FILE
        self._fd = None

    def _ensure_open(self) -> bool:
        """Open log file, creating directory if needed. Returns True on success."""
        if self._fd is not None:
            return True
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(self._log_path, 'a')
            return True
        except (OSError, PermissionError) as e:
            logger.debug(f"Cannot open audit log {self._log_path}: {e}")
            return False

    def _write(self, event: dict):
        """Write a single event to the audit log."""
        if not self._ensure_open():
            return
        try:
            self._fd.write(json.dumps(event, ensure_ascii=False) + '\n')
            self._fd.flush()
        except OSError as e:
            logger.debug(f"Cannot write to audit log: {e}")

    def close(self):
        """Close the audit log file."""
        if self._fd:
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None

    # --- Event methods ---

    def log_operation_start(
        self,
        context: AuthContext,
        action: str,
        packages: List[str],
        command: str = ""
    ):
        """Log the start of a privileged operation."""
        self._write({
            'timestamp': time.time(),
            'event': f'{action}_start',
            'user': context.user_name,
            'uid': context.user_id,
            'pid': context.pid,
            'source': context.source,
            'packages': packages,
            'command': command,
        })

    def log_operation_complete(
        self,
        context: AuthContext,
        action: str,
        packages: List[str],
        success: bool,
        error: str = ""
    ):
        """Log the completion of a privileged operation."""
        event = {
            'timestamp': time.time(),
            'event': f'{action}_complete',
            'user': context.user_name,
            'uid': context.user_id,
            'source': context.source,
            'packages': packages,
            'success': success,
        }
        if error:
            event['error'] = error
        self._write(event)

    def log_auth_denied(
        self,
        context: AuthContext,
        action: str
    ):
        """Log a denied authorization attempt."""
        self._write({
            'timestamp': time.time(),
            'event': 'auth_denied',
            'user': context.user_name,
            'uid': context.user_id,
            'pid': context.pid,
            'source': context.source,
            'action': action,
        })

    def log_media_change(
        self,
        context: AuthContext,
        change_type: str,
        media_name: str,
        details: str = ""
    ):
        """Log a media configuration change."""
        event = {
            'timestamp': time.time(),
            'event': 'media_change',
            'user': context.user_name,
            'uid': context.user_id,
            'source': context.source,
            'change_type': change_type,
            'media_name': media_name,
        }
        if details:
            event['details'] = details
        self._write(event)
