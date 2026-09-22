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


def privileged_command_options(cmdline: str) -> list[str]:
    """Ways to run ``cmdline`` as root on *this* machine, best first.

    Never assume ``sudo``.  Mageia does not install it by default and
    does not add the first user to a sudoer group, so a message that
    says "sudo ..." hands a third of our users a command that fails,
    while hiding ``su -``, which works everywhere.  The order below is
    what the machine can actually offer, not a habit:

    1. ``sudo`` when it is installed **and** the caller belongs to a
       sudoer-conventional group: most likely to just work.
    2. ``su -c``: the universal fallback, ``su`` ships in coreutils.
    3. ``sudo`` when installed but membership was not detected: the
       caller may know a configuration we cannot see.
    4. ``pkexec``, only when our polkit policy file is installed.

    Args:
        cmdline: the command to elevate, already shell-quoted if it
            needs to be (use :func:`shlex.join` on an argv list).

    Returns:
        Non-empty list of ready-to-paste command lines.
    """
    options: list[str] = []

    have_sudo = bool(shutil.which('sudo'))
    have_pkexec = bool(shutil.which('pkexec')) and _polkit_policy_installed()
    likely_sudoer = _is_likely_sudoer()

    if likely_sudoer and have_sudo:
        options.append(f"sudo {cmdline}")
    options.append(f"su -c {shlex.quote(cmdline)}")
    if have_sudo and not likely_sudoer:
        options.append(f"sudo {cmdline}")
    if have_pkexec:
        options.append(f"pkexec {cmdline}")

    return options


def privileged_command(cmdline: str) -> str:
    """The single best way to run ``cmdline`` as root on this machine.

    For messages that show one command rather than a menu, such as the
    quick-start guide.  See :func:`privileged_command_options` for how
    "best" is decided.
    """
    return privileged_command_options(cmdline)[0]


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

    options = privileged_command_options(shlex.join(sys.argv))

    sys.stderr.write(_("This operation requires root privileges.") + "\n")
    if len(options) == 1:
        sys.stderr.write(_("Run: {cmd}").format(cmd=options[0]) + "\n")
    else:
        sys.stderr.write(_("Try one of:") + "\n")
        for opt in options:
            sys.stderr.write(f"  {opt}\n")
    sys.exit(77)
