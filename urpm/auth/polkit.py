"""PolicyKit authentication backend for urpm D-Bus service.

Checks authorization via polkitd for privileged operations.
Each urpm action maps to a PolicyKit action ID:

    org.mageia.urpm.query         -> always allowed
    org.mageia.urpm.refresh       -> auth_admin_keep
    org.mageia.urpm.install       -> auth_admin_keep
    org.mageia.urpm.remove        -> auth_admin_keep
    org.mageia.urpm.upgrade       -> auth_admin_keep
    org.mageia.urpm.media-manage  -> auth_admin

The D-Bus service calls check_authorization() before each operation.
"""

import logging
from typing import Tuple

from .context import AuthContext, Permission

logger = logging.getLogger(__name__)

# PolicyKit action IDs
POLKIT_PREFIX = "org.mageia.urpm"

ACTION_MAP = {
    Permission.QUERY:        f"{POLKIT_PREFIX}.query",
    Permission.REFRESH:      f"{POLKIT_PREFIX}.refresh",
    Permission.INSTALL:      f"{POLKIT_PREFIX}.install",
    Permission.REMOVE:       f"{POLKIT_PREFIX}.remove",
    Permission.UPGRADE:      f"{POLKIT_PREFIX}.upgrade",
    Permission.MEDIA_MANAGE: f"{POLKIT_PREFIX}.media-manage",
}

# Reverse map for permission lookup
PERMISSION_MAP = {v: k for k, v in ACTION_MAP.items()}

# Permissions that always require interactive auth (no keep)
ALWAYS_AUTH = {Permission.MEDIA_MANAGE}


class PolicyKitError(Exception):
    """PolicyKit communication error."""
    pass


class PolicyKitBackend:
    """PolicyKit authorization backend.

    Uses GLib/Gio to communicate with polkitd over the system bus.
    """

    def __init__(self):
        self._authority = None

    def _get_authority(self):
        """Get or create the Polkit authority proxy."""
        if self._authority is not None:
            return self._authority

        try:
            import gi
            gi.require_version('Polkit', '1.0')
            from gi.repository import Polkit
            self._authority = Polkit.Authority.get_sync(None)
            return self._authority
        except (ImportError, ValueError) as e:
            raise PolicyKitError(
                f"PolicyKit not available: {e}. "
                "Install python3-gobject and polkit."
            )
        except Exception as e:
            raise PolicyKitError(f"Cannot connect to polkitd: {e}")

    def check_authorization(
        self,
        pid: int,
        uid: int,
        permission: Permission,
        allow_interaction: bool = True
    ) -> bool:
        """Check if a caller is authorized for an action.

        Args:
            pid: Caller process ID
            uid: Caller user ID
            permission: Required permission
            allow_interaction: If True, polkitd may show an auth dialog

        Returns:
            True if authorized, False otherwise
        """
        action_id = ACTION_MAP.get(permission)
        if not action_id:
            logger.warning(f"No PolicyKit action for permission {permission}")
            return False

        try:
            import gi
            gi.require_version('Polkit', '1.0')
            from gi.repository import Polkit

            authority = self._get_authority()

            subject = Polkit.UnixProcess.new_for_owner(pid, 0, uid)

            flags = Polkit.CheckAuthorizationFlags.NONE
            if allow_interaction:
                flags = Polkit.CheckAuthorizationFlags.ALLOW_USER_INTERACTION

            result = authority.check_authorization_sync(
                subject,
                action_id,
                None,   # details
                flags,
                None,   # cancellable
            )

            authorized = result.get_is_authorized()
            if not authorized:
                logger.info(
                    f"PolicyKit denied {action_id} for pid={pid} uid={uid}"
                )
            return authorized

        except PolicyKitError:
            raise
        except Exception as e:
            raise PolicyKitError(f"Authorization check failed: {e}")

    def authorize_for_actions(
        self,
        pid: int,
        uid: int,
        permissions: Permission,
        allow_interaction: bool = True
    ) -> Permission:
        """Check authorization for multiple permissions at once.

        Args:
            pid: Caller process ID
            uid: Caller user ID
            permissions: Requested permission flags
            allow_interaction: If True, polkitd may show an auth dialog

        Returns:
            Permission flags that were granted
        """
        granted = Permission(0)

        for perm in Permission:
            if perm.name.startswith('ALL'):
                continue
            if not (permissions & perm):
                continue

            if self.check_authorization(pid, uid, perm, allow_interaction):
                granted |= perm

        return granted

    def create_auth_context(
        self,
        pid: int,
        uid: int,
        permissions: Permission,
        allow_interaction: bool = True
    ) -> Tuple[AuthContext, Permission]:
        """Authorize a caller and create an AuthContext.

        Args:
            pid: Caller process ID
            uid: Caller user ID
            permissions: Requested permissions
            allow_interaction: If True, polkitd may show an auth dialog

        Returns:
            (AuthContext, denied_permissions)
            - AuthContext with granted permissions
            - denied_permissions: Permission flags that were denied
        """
        granted = self.authorize_for_actions(
            pid, uid, permissions, allow_interaction
        )
        # Query is always granted (no auth needed)
        granted |= Permission.QUERY

        denied = permissions & ~granted

        context = AuthContext.for_polkit(uid, pid, granted)
        return context, denied
