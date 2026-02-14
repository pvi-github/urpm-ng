"""Authentication context and permissions for urpm operations.

Defines who is calling and what they're allowed to do.
Used by PackageOperations to enforce access control when called
via D-Bus/PolicyKit. The CLI running as root bypasses checks.
"""

import os
import pwd
from dataclasses import dataclass
from enum import Flag, auto
from typing import Optional


class Permission(Flag):
    """Fine-grained permission flags for package operations."""
    QUERY = auto()          # Search, info, list
    REFRESH = auto()        # Refresh metadata
    INSTALL = auto()        # Install packages
    REMOVE = auto()         # Remove packages
    UPGRADE = auto()        # System upgrade
    MEDIA_MANAGE = auto()   # Add/remove/configure media

    # Convenience combinations
    ALL_READ = QUERY
    ALL_WRITE = INSTALL | REMOVE | UPGRADE | REFRESH | MEDIA_MANAGE
    ALL = QUERY | REFRESH | INSTALL | REMOVE | UPGRADE | MEDIA_MANAGE


class AuthError(Exception):
    """Raised when an operation is denied by auth policy."""

    def __init__(self, action: str, context: 'AuthContext'):
        self.action = action
        self.context = context
        super().__init__(
            f"Permission denied: {action} "
            f"(user={context.user_name}, source={context.source})"
        )


@dataclass(frozen=True)
class AuthContext:
    """Identity and permissions of the caller.

    Created by the transport layer (CLI, D-Bus, API) and passed
    to PackageOperations for authorization decisions.
    """
    user_id: int
    user_name: str
    permissions: Permission
    source: str     # "cli", "polkit", "token"
    pid: int = 0    # Caller PID (for audit)

    # --- Permission checks ---

    def can_query(self) -> bool:
        return bool(self.permissions & Permission.QUERY)

    def can_refresh(self) -> bool:
        return bool(self.permissions & Permission.REFRESH)

    def can_install(self) -> bool:
        return bool(self.permissions & Permission.INSTALL)

    def can_remove(self) -> bool:
        return bool(self.permissions & Permission.REMOVE)

    def can_upgrade(self) -> bool:
        return bool(self.permissions & Permission.UPGRADE)

    def can_manage_media(self) -> bool:
        return bool(self.permissions & Permission.MEDIA_MANAGE)

    def require(self, perm: Permission, action: str = ""):
        """Raise AuthError if permission is not granted."""
        if not (self.permissions & perm):
            raise AuthError(action or perm.name.lower(), self)

    # --- Factory methods ---

    @classmethod
    def from_root_cli(cls) -> 'AuthContext':
        """Create context for CLI running as root (full permissions)."""
        uid = os.getuid()
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        return cls(
            user_id=uid,
            user_name=name,
            permissions=Permission.ALL,
            source="cli",
            pid=os.getpid(),
        )

    @classmethod
    def from_cli_user(cls) -> 'AuthContext':
        """Create context for CLI running as unprivileged user (read-only)."""
        uid = os.getuid()
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        return cls(
            user_id=uid,
            user_name=name,
            permissions=Permission.QUERY,
            source="cli",
            pid=os.getpid(),
        )

    @classmethod
    def for_polkit(
        cls,
        uid: int,
        pid: int,
        permissions: Permission
    ) -> 'AuthContext':
        """Create context from PolicyKit authorization result."""
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        return cls(
            user_id=uid,
            user_name=name,
            permissions=permissions,
            source="polkit",
            pid=pid,
        )
