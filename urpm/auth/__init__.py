"""Authentication and authorization for urpm.

Provides:
- AuthContext: caller identity and permissions
- Permission: fine-grained permission flags
- AuditLogger: structured audit logging
"""

from .context import AuthContext, Permission
from .audit import AuditLogger

__all__ = ['AuthContext', 'Permission', 'AuditLogger']
