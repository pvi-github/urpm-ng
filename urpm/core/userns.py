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

A second question lives here, and it is a matter of *width* rather
than of presence : a package build is faithful only if the container
can represent every uid the spec may name.  A ``%check`` is ordinary
code and may chown to anything the kernel accepts — python's
``test_posix`` goes to 2**31 on purpose, to exercise large values.
Past the delegated range the kernel answers EINVAL, and the build
dies eleven thousand log lines in, on an error that says nothing
about identifier delegation.  :func:`format_build_range_warning`
turns that into something a packager can act on before the build
starts.  See :data:`BUILD_RANGE_MIN`.

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

# Smallest delegated range that lets a container represent every uid a
# spec is known to name.  python's ``test_posix`` chowns to 2**31 on
# purpose ("Try an amusingly large uid/gid to make sure we handle large
# unsigned values"), and podman maps container uids 1..N onto the
# delegated range, so uid 2**31 is reachable exactly when N >= 2**31.
BUILD_RANGE_MIN = 2 ** 31

# What to recommend: one id of headroom above the minimum, and the
# value verified end to end — python 2.7.18 builds with its full test
# suite passing, in a rootless container, spec untouched.
RECOMMENDED_RANGE = 2 ** 31 + 1

# Where shadow-utils starts subordinate ranges by default.  Used only
# to shape the recommendation when the user has no entry at all.
DEFAULT_RANGE_START = 524288


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
        subuid_entries: The ``(start, count)`` pairs read from
            ``/etc/subuid`` for this user, in file order.  Kept so the
            recommendation can name the exact line to replace instead
            of describing it.
        subgid_entries: Same for ``/etc/subgid``.
    """

    username: str
    subuid_count: int
    subgid_count: int
    is_root: bool
    subuid_entries: tuple = ()
    subgid_entries: tuple = ()

    @property
    def wide_enough_for_build(self) -> bool:
        """True when a container can represent the uids a build may name.

        Root passes: it owns the whole space, there is no namespace to
        translate through.  Otherwise both files must delegate at least
        :data:`BUILD_RANGE_MIN` ids — a gid is as translatable as a
        uid, and a ``chown`` names both.
        """
        if self.is_root:
            return True
        return min(self.subuid_count,
                   self.subgid_count) >= BUILD_RANGE_MIN

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
    subuid_entries = _read_entries(Path("/etc/subuid"), username)
    subgid_entries = _read_entries(Path("/etc/subgid"), username)
    return UserNamespaceCapability(
        username=username,
        subuid_count=sum(count for _s, count in subuid_entries),
        subgid_count=sum(count for _s, count in subgid_entries),
        is_root=is_root,
        subuid_entries=subuid_entries,
        subgid_entries=subgid_entries,
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


def _read_entries(path: Path, username: str) -> tuple:
    """Every ``(start, count)`` delegated to *username* in *path*.

    The format shipped by shadow-utils is one entry per line,
    ``login:first_uid:count`` — several entries per user are legal.
    Malformed lines (wrong field count, non-integer fields) are
    skipped silently ; we return whatever valid entries we found
    rather than raise.  Missing or unreadable file returns ``()``.
    """
    if not path.exists():
        return ()
    try:
        text = path.read_text()
    except OSError:
        return ()
    entries = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or parts[0] != username:
            continue
        try:
            entries.append((int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return tuple(entries)


def _count_delegated(path: Path, username: str) -> int:
    """Total number of ids delegated to *username* in *path*.

    Sum over every entry : several ranges per user are legal and all
    of them count.
    """
    return sum(count for _start, count in _read_entries(path, username))


def _widen_commands(cap: UserNamespaceCapability) -> str:
    """The shell block that widens the delegation, or a description.

    An exact ``sed`` is only honest when there is exactly one entry in
    each file and both agree : that is the shape shadow-utils creates,
    and the only one where we can name the line to replace without
    guessing.  Anything else (several ranges, uid and gid delegated
    differently) gets prose — a wrong ``sed`` on ``/etc/subuid`` is a
    good way to lock a user out of rootless containers entirely.
    """
    single = (len(cap.subuid_entries) == 1
              and cap.subuid_entries == cap.subgid_entries)
    if not single:
        return _(
            "    give '{user}' a single range of at least {needed} ids in "
            "/etc/subuid and /etc/subgid,\n"
            "    keeping the start it has today"
        ).format(user=cap.username, needed=BUILD_RANGE_MIN)

    start, count = cap.subuid_entries[0]
    return (
        "    cp -a /etc/subuid /etc/subuid.bak\n"
        "    cp -a /etc/subgid /etc/subgid.bak\n"
        f"    sed -i 's/^{cap.username}:{start}:{count}$/"
        f"{cap.username}:{start}:{RECOMMENDED_RANGE}/' \\\n"
        "        /etc/subuid /etc/subgid"
    )


def format_build_range_warning(cap: UserNamespaceCapability) -> str:
    """Explain a too-narrow delegation, and how to widen it.

    Addressed to a packager about to start a build that may well die
    on it.  Names the observed numbers, the command to run, and why
    the range start must not move.

    The commands are given for a **root shell**, not ``sudo`` : the
    ``podman system migrate`` that follows has to run back as the
    packager, and mixing the two in one copy-paste block is how people
    end up migrating root's containers instead of their own.
    """
    return _(
        "The delegated id range is too narrow for a faithful build.\n"
        "\n"
        "'{user}' has {count} delegated ids. A spec's %check is ordinary "
        "code and may name any uid the kernel accepts: python's "
        "test_posix chowns to {needed} on purpose, to exercise large "
        "values. Past the delegated range the kernel answers EINVAL, "
        "and the build dies in the middle of the test suite on an error "
        "that never mentions identifier delegation.\n"
        "\n"
        "To widen it, in a root shell (not sudo):\n"
        "{commands}\n"
        "\n"
        "Leave the range start where it is: the first ids keep the same "
        "translation, so images and layers already on disk stay valid. "
        "Then, back as '{user}' and with no container running:\n"
        "\n"
        "    podman system migrate\n"
        "\n"
        "The pause process holds the old map alive until then, which is "
        "why the change looks ignored if a container is up."
    ).format(
        user=cap.username,
        count=min(cap.subuid_count, cap.subgid_count),
        needed=BUILD_RANGE_MIN,
        commands=_widen_commands(cap),
    )


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
