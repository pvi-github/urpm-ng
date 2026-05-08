"""Privilege escalation helpers for CLI commands.

This module provides a single entry point, :func:`require_privileges`, used
by CLI commands to enforce that a privileged operation is being run as
root.  When the running process is *not* root, the helper prints an
elevation hint listing the most relevant escalation method first
(based on group membership) plus alternatives, and exits with code 77
(``EX_NOPERM``) so that scripts can distinguish a permission failure
from a generic error.

The detection of "is the user a likely sudoer?" is performed without
spawning any subprocess: we read the process group list via the stdlib
:mod:`grp` module and compare it against
:data:`urpm.core.config.SUDOER_GROUPS`.  This keeps the check
instantaneous and dependency-free.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys

from ..core.config import SUDOER_GROUPS
from ..i18n import _


def _is_likely_sudoer() -> bool:
    """Rapid check: is the user a member of a sudoers-conventional group?

    Reads the group list once via stdlib (no subprocess, no
    ``/etc/sudoers`` parsing).  The list of "sudoer-conventional" groups
    lives in :data:`urpm.core.config.SUDOER_GROUPS`; update it there if
    a downstream distribution uses a different convention.

    Returns:
        True when at least one of the caller's supplementary groups is
        named in :data:`SUDOER_GROUPS`, False otherwise.  Returns False
        on platforms where :mod:`grp` is unavailable (non-Unix).
    """
    try:
        import grp
    except ImportError:
        return False
    for gid in os.getgroups():
        try:
            if grp.getgrgid(gid).gr_name in SUDOER_GROUPS:
                return True
        except KeyError:
            continue
    return False


def _polkit_policy_installed() -> bool:
    """True when the urpm polkit policy file is installed system-wide."""
    return os.path.exists(
        '/usr/share/polkit-1/actions/org.mageia.urpm.policy'
    )


def require_privileges(action_id: str | None = None,
                       *,
                       allow_skip: bool = False) -> None:
    """Ensure the running process has root privileges.

    On ``euid != 0`` and ``allow_skip`` is False, prints a one-shot
    elevation hint listing the most relevant escalation method first
    (based on group membership) plus alternatives, and exits with code
    77 (``EX_NOPERM``, distinct from the generic 1 so scripts can
    detect permission failures).

    Args:
        action_id: optional polkit action id (e.g.
            ``"org.mageia.urpm.install"``) for future hooks. Currently
            unused — accepted for forward compatibility with the
            migration of existing call sites in commit B.
        allow_skip: when True, return without check (used for chroot
            operations on a foreign root that don't require host root).
    """
    if os.geteuid() == 0:
        return
    if allow_skip:
        return

    cmdline = shlex.join(sys.argv)
    options: list[str] = []

    have_sudo = bool(shutil.which('sudo'))
    have_pkexec = bool(shutil.which('pkexec')) and _polkit_policy_installed()
    likely_sudoer = _is_likely_sudoer()

    # Order of presentation:
    # 1. sudo if the user is a member of a sudoer-conventional group AND
    #    sudo is installed — most likely to succeed without surprise.
    # 2. su -c — always available (su is in coreutils), the universal
    #    fallback.
    # 3. sudo (when installed but membership not detected) — last resort,
    #    the user knows their own configuration.
    # 4. pkexec — listed only when the polkit policy file is installed.
    if likely_sudoer and have_sudo:
        options.append(f"sudo {cmdline}")
    options.append(f"su -c {shlex.quote(cmdline)}")
    if have_sudo and not likely_sudoer:
        options.append(f"sudo {cmdline}")
    if have_pkexec:
        options.append(f"pkexec {cmdline}")

    sys.stderr.write(_("This operation requires root privileges.") + "\n")
    if len(options) == 1:
        sys.stderr.write(_("Run: {cmd}").format(cmd=options[0]) + "\n")
    else:
        sys.stderr.write(_("Try one of:") + "\n")
        for opt in options:
            sys.stderr.write(f"  {opt}\n")
    sys.exit(77)
