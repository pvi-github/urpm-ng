"""Rootless user-namespace capability probe and preflight.

Anywhere urpm-ng steps out of the operator's account into a rootless
kernel user namespace — ``podman unshare``, ``podman run`` (rootless
mode), rootless docker, future bubblewrap/lxc adapters — the kernel
needs a range of « delegated » UIDs and GIDs on the host to map into
the namespace.  Without that range (``/etc/subuid`` and
``/etc/subgid`` unpopulated for the current user) the tooling
silently falls back to a « single-uid mapping » in which only the
caller's own UID is mapped and every other UID inside the namespace
becomes unmappable.  Concretely : ``chown`` to system users
(``lp``, ``mail``, ``nobody``, …) fails, ``rpm-cpio`` dies
mid-extraction with the confusing « chown failed – Directory not
empty », and half the transaction succeeds silently.

This module is the single source of truth on « can the current user
enter a rootless userns safely ? ».  Callers that intend to enter
one — every ``Container`` construction against podman when we are
not root, every direct ``podman unshare`` invocation — MUST gate on
:func:`require_userns` first.  The rest of the codebase stays
oblivious : the preflight lives at the boundary that concerns it.

The check is intentionally cheap — two small text files read once,
no ``podman`` call, no subprocess — so gating it at construction has
no measurable cost.

The bypass is an environment variable, not a CLI flag, on purpose :
it is an ops-side emergency contract for atypical setups (custom
kernel patches, experimental rootless configs, mock CI environments
that arrange userns access outside ``/etc/subuid``), not a user
option that would normalise the broken state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..i18n import _


# Undocumented escape hatch : set to ``1`` in the environment to skip
# the preflight even when the current user has no delegated range.
# Used by ops in atypical setups where userns access is arranged
# outside ``/etc/subuid`` (custom kernel, mock CI, …).  Not a CLI
# flag on purpose — surfacing it as a user-facing option would
# normalise the broken state we are trying to catch.
BYPASS_ENV = "URPM_ALLOW_BROKEN_USERNS"


@dataclass(frozen=True)
class UserNamespaceCapability:
    """What ``/etc/subuid`` and ``/etc/subgid`` actually delegate to
    the current user.

    Attributes:
        username: Login name of the current euid, or ``"root"`` when
            euid=0.
        subuid_count: Sum of the third field over every
            ``username:*:count`` entry in ``/etc/subuid``.  ``0``
            when the file is absent or lists no entry for the user.
        subgid_count: Same for ``/etc/subgid``.
        is_root: True when the current euid is 0 — root does not need
            delegated ranges (owns the whole UID space already).
    """

    username: str
    subuid_count: int
    subgid_count: int
    is_root: bool

    @property
    def usable(self) -> bool:
        """True when a real (>1) range is delegated in both files.

        Root passes unconditionally.  A count of exactly 1 is treated
        as unusable : that is the « single-uid mapping » fallback
        which is precisely the broken state we are guarding against.
        """
        if self.is_root:
            return True
        return self.subuid_count > 1 and self.subgid_count > 1


class UserNamespaceUnusableError(RuntimeError):
    """Raised when the current user lacks a real subuid/subgid range.

    The message is user-facing — it names the current user, states
    what's actually delegated, and prints the exact ``usermod`` /
    ``podman system migrate`` commands to run as root.  Caller code
    should let it propagate to the CLI top-level exception handler
    which will display it verbatim.
    """


def probe_current_user() -> UserNamespaceCapability:
    """Read ``/etc/subuid`` and ``/etc/subgid`` for the current euid.

    Never raises — a missing file, an unreadable file, or an entry
    with a non-integer count all count as « nothing delegated » (0).
    The dataclass returned makes the exact state observable to
    callers that want to log or display it, even when the state is
    usable.
    """
    is_root = os.geteuid() == 0
    username = _current_username()
    return UserNamespaceCapability(
        username=username,
        subuid_count=_count_delegated(Path("/etc/subuid"), username),
        subgid_count=_count_delegated(Path("/etc/subgid"), username),
        is_root=is_root,
    )


def require_userns() -> UserNamespaceCapability:
    """Preflight — return the capability, or raise
    :class:`UserNamespaceUnusableError` when it is unusable.

    Contract for callers : call this at the entry of any operation
    that will spawn a subprocess under a rootless user namespace
    (``Container.__init__`` for podman ; direct ``podman unshare``
    sites in :mod:`urpm.core.transaction_queue`).  The return value
    can be ignored — the exception is what matters — but callers
    that want to log the delegated range are welcome to hold on to
    it.

    Root always passes.  The :data:`BYPASS_ENV` variable set to
    ``"1"`` also passes, after probing (so logs still show the
    actual state).
    """
    cap = probe_current_user()
    if cap.usable:
        return cap
    if os.environ.get(BYPASS_ENV) == "1":
        return cap
    raise UserNamespaceUnusableError(_format_error_message(cap))


def _current_username() -> str:
    """Login name of the current euid, robust to a shallow rpmdb
    where ``pwd`` cannot resolve the entry (defensive : happens
    inside minimal chroots that urpm-ng itself may run against)."""
    import pwd
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return f"uid={os.geteuid()}"


def _count_delegated(path: Path, username: str) -> int:
    """Sum every ``username:start:count`` line for *username* in
    the file at *path*.

    The format shipped by shadow-utils is one entry per line,
    ``login:first_uid:count`` — several entries per user are legal
    and are all summed.  Malformed lines (wrong field count,
    non-integer count) are skipped silently ; we return whatever
    valid entries we found rather than raise.  Missing or unreadable
    file returns 0.
    """
    if not path.exists():
        return 0
    total = 0
    try:
        text = path.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or parts[0] != username:
            continue
        try:
            total += int(parts[2])
        except ValueError:
            continue
    return total


def _format_error_message(cap: UserNamespaceCapability) -> str:
    """Compose the actionable diagnostic for
    :class:`UserNamespaceUnusableError`.

    The message names the exact user, prints the observed counts,
    and gives copy-pasteable commands.  Kept as a single ``_()``
    template so translators see the whole context in one string ;
    the interpolated values (username, counts) are neutral tokens
    that stay identical across every language.
    """
    return _(
        "User '{user}' has no usable subuid/subgid range "
        "(subuid_count={subuid}, subgid_count={subgid}).\n"
        "Rootless container operations (podman, podman unshare, rpm "
        "chroot install) will fail silently or with confusing cpio "
        "errors like 'chown failed - Directory not empty'.\n"
        "\n"
        "Fix — as root :\n"
        "    usermod --add-subuids 100000-165535 "
        "--add-subgids 100000-165535 {user}\n"
        "    podman system migrate\n"
        "\n"
        "To force through anyway (not recommended, silent failures "
        "expected) :\n"
        "    {bypass}=1 <your command>"
    ).format(
        user=cap.username,
        subuid=cap.subuid_count,
        subgid=cap.subgid_count,
        bypass=BYPASS_ENV,
    )
