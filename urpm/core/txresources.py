"""Yielding the machine while a long operation runs.

A tester's mga9 → mga10 upgrade froze the whole GNOME desktop for
minutes: no console on Ctrl-Alt-F4, and the disk working without
pause.  The post-mortem ruled out the two usual suspects — the swap
was untouched (``0B`` of 3 GB) and no ``oom-kill`` was logged, so not
memory; 1.5 GB were still free and no ``No space left`` appeared, so
not space.  What was left is the device being saturated, and the
session starving behind it: the errors in that boot's journal are a
``GetManagedObjects() failed: Did not receive a reply`` and a handful
of ``Failed to start app-gnome-*.scope``, which is what a session
looks like when its own small writes cannot get through.

``ionice -c3``, which :func:`urpm.core.transaction_queue.
_apply_transaction_niceness` has applied since the first distupgrade,
could not prevent it, and that is not a surprise once stated:
**an I/O priority does not govern writeback**.  It applies to the
reads and synchronous writes a process issues; the gigabytes of dirty
pages a package transaction leaves behind are flushed later by kernel
threads that do not inherit it.  Only per-cgroup accounting follows
those pages, because the kernel charges writeback to the cgroup that
owns the page — which is why both the ``io`` and the ``memory``
controller have to be enabled, and why enabling ``io`` alone would be
a more complicated ``ionice``.

The lever is deliberately **relative**.  urpm-ng runs on a 2 GB
laptop and on a 128 GB workstation and cannot know which, so any
absolute figure — a dirty-page ceiling, a parallelism cut below some
amount of RAM — would cripple one end to protect the other.  A weight
says « when someone else wants the device, yield », which means the
same thing at both ends, and it is only consulted *under contention*:
a machine installing packages with nothing else running loses nothing
by carrying a low weight, since there is nobody to yield to.

Two limits, stated rather than discovered later.  On a very small
machine the operation will be slow whatever we do; the goal here is
« slow but usable », not fast.  And where cgroup v2 is out of reach
(an old kernel, a constrained container, a non-root caller) the
fallback is today's ``nice`` + ``ionice``, which does *not* cover
writeback: that case is reported, not papered over.

Sysfs only, no ``systemd-run`` and no ``.slice`` — the project carries
a zero-systemd-dependency rule, and cgroup v2 is a kernel interface
that sysvinit and OpenRC expose just as well.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


#: Where cgroup v2 is mounted.  Resolved at call time, never bound as
#: a default argument, so a test can point the module elsewhere and
#: actually constrain what it believes it constrains.
CGROUP_ROOT = Path("/sys/fs/cgroup")

#: Our own sub-cgroup.  One for the whole machine: two concurrent urpm
#: operations share it, which is what we want (they are both bulk I/O),
#: and the directory is only removed once the last one has left.
CGROUP_NAME = "urpm-transaction"

#: ``io.weight`` and ``cpu.weight`` both default to 100 on a 1..10000
#: scale.  10 reads as « yield widely » without starving ourselves to
#: a halt.  No setting exposed yet: a first figure to adjust against
#: real-world experience, not a knob to tune blind.
IO_WEIGHT = 10
CPU_WEIGHT = 10

#: Controllers that must be enabled on the *parent* for our weights to
#: mean anything.  ``memory`` is not optional: writeback is charged
#: through the owning memory cgroup, and without it the io weight
#: would govern reads and synchronous writes only.
REQUIRED_CONTROLLERS = ("io", "memory")

#: What :func:`lower_priority` managed to obtain.
BY_CGROUP = "cgroup"
BY_NICE = "nice"
BY_NOTHING = "nothing"


def _own_cgroup_path() -> Optional[str]:
    """The cgroup v2 path of the current process, as in ``/proc``.

    Needed to put the process back where it came from.  The v2 entry
    is the one with an empty controller list (``0::<path>``).
    """
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hierarchy, controllers, path = line.split(":", 2)
            if hierarchy == "0" and not controllers:
                return path
    except (OSError, ValueError):
        pass
    return None


def _controllers_available(root: Path) -> bool:
    """Whether the parent delegates what we need to its children."""
    try:
        enabled = set(
            (root / "cgroup.subtree_control").read_text().split())
    except OSError:
        return False
    missing = [c for c in REQUIRED_CONTROLLERS if c not in enabled]
    if missing:
        logger.debug(
            "cgroup v2 present but %s not enabled on %s",
            " and ".join(missing), root)
        return False
    return True


def _join_cgroup() -> Optional[Path]:
    """Create the sub-cgroup, set the weights, move ourselves in.

    Returns the cgroup directory on success, ``None`` when cgroup v2
    is unusable here — which covers an unmounted v2 hierarchy, a
    parent that does not delegate ``io``/``memory``, and the ordinary
    case of not being root.
    """
    root = CGROUP_ROOT
    if not (root / "cgroup.controllers").exists():
        return None
    if not _controllers_available(root):
        return None

    group = root / CGROUP_NAME
    try:
        group.mkdir(exist_ok=True)
    except OSError as exc:
        logger.debug("cannot create %s : %s", group, exc)
        return None

    # Weights before membership, so the first page written is already
    # accounted at the right weight.
    for name, value in (("io.weight", IO_WEIGHT),
                        ("cpu.weight", CPU_WEIGHT)):
        try:
            (group / name).write_text(str(value))
        except OSError as exc:
            # A missing io.weight means the controller is not really
            # there: nothing to gain by staying.
            logger.debug("cannot set %s : %s", name, exc)
            if name == "io.weight":
                _remove_cgroup(group)
                return None

    try:
        (group / "cgroup.procs").write_text(str(os.getpid()))
    except OSError as exc:
        logger.debug("cannot move pid into %s : %s", group, exc)
        _remove_cgroup(group)
        return None
    return group


def _remove_cgroup(group: Path) -> None:
    """Drop the cgroup if nobody is left in it.

    ``rmdir`` fails with ``EBUSY`` while a process remains, which is
    exactly the arbitration we want between two concurrent urpm
    operations: whoever leaves last removes it.
    """
    try:
        group.rmdir()
    except OSError:
        pass


#: Where a process we mean to outlive urpm goes to escape being swept
#: up with it.  A sibling of :data:`CGROUP_NAME` rather than the same
#: directory: this one carries no weights, so a deferred action is not
#: throttled like bulk I/O, and it leaves the transaction's cgroup free
#: to be removed by whoever leaves it last.
DETACHED_CGROUP_NAME = "urpm-detached"


def leave_unit_cgroup(name: str = DETACHED_CGROUP_NAME) -> Optional[Path]:
    """Move the calling process out of whatever cgroup it was started in.

    A process forked from a systemd unit stays in that unit's cgroup:
    ``fork`` inherits it, ``setsid`` detaches the session and the
    controlling terminal but changes no cgroup, and ``exec`` changes
    only the program.  So when the unit stops, a ``KillMode`` of
    ``control-group`` -- the default, and what ``urpm-dbus.service``
    has -- signals every process in that cgroup, ours included.

    Measured the 2026-10-26: a deferred action handed over by the D-Bus
    service recorded its waiting line and was then killed before it
    could act, at the very instant it was meant to, since what it waits
    for is that service going away.  The same action from the command
    line completed every time.

    Membership is all we need here, so unlike :func:`_join_cgroup` this
    asks for no controller and sets no weight; a cgroup at the root of
    the hierarchy is outside any unit's subtree, which is the whole
    point.  Best-effort by design: on a machine with no cgroup v2, or
    no unit to be swept up with, there is nothing to escape.

    The directory is left behind once empty, unlike the transaction's:
    a process cannot remove the cgroup it is in, and here there is
    nobody outside to do it, every process concerned being one that
    deliberately outlived its parent.  An empty cgroup costs nothing
    and the next deferred action moves into the same one.

    Returns the cgroup joined, or ``None`` if we stayed put.
    """
    root = CGROUP_ROOT
    if not (root / "cgroup.controllers").exists():
        return None

    group = root / name
    try:
        group.mkdir(exist_ok=True)
        (group / "cgroup.procs").write_text(str(os.getpid()))
    except OSError as exc:
        logger.debug("cannot leave the unit cgroup for %s : %s", group, exc)
        return None
    return group


def _nice_and_ioprio() -> str:
    """The fallback: what the transaction has always done.

    Kept verbatim in effect — ``ionice -c3`` on ourselves plus
    ``nice(5)`` — so a machine without cgroup v2 is no worse off than
    before.  It does not cover writeback; the caller says so.
    """
    obtained = BY_NOTHING
    if shutil.which("ionice"):
        result = subprocess.run(
            ["ionice", "-c3", "-p", str(os.getpid())],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            obtained = BY_NICE
    try:
        os.nice(5)
        obtained = BY_NICE
    except OSError:
        # Can fail on a restricted kernel — no reason to abort a
        # legitimate operation over scheduling politeness.
        pass
    return obtained


def lower_priority() -> str:
    """Make the current process yield the machine to interactive work.

    Tries the cgroup first because it is the only one of the two that
    governs writeback, and falls back to ``nice``/``ionice``.

    Returns:
        :data:`BY_CGROUP`, :data:`BY_NICE` or :data:`BY_NOTHING`.
        Callers use it for the log line, not for control flow: there
        is nothing to do differently, only something to be honest
        about.
    """
    if _join_cgroup() is not None:
        logger.info(
            "I/O budget : running in cgroup %s (io.weight=%d, "
            "cpu.weight=%d), writeback included",
            CGROUP_NAME, IO_WEIGHT, CPU_WEIGHT)
        return BY_CGROUP

    obtained = _nice_and_ioprio()
    if obtained == BY_NICE:
        logger.info(
            "I/O budget : nice+ionice only (no usable cgroup v2) — "
            "writeback is not covered, a desktop may still stutter")
    else:
        logger.info("I/O budget : none obtained")
    return obtained


@contextmanager
def yielding_the_machine():
    """Lower the priority for a phase, then put it back.

    For a process that outlives the operation — the download runs in
    the main process, where staying in the cgroup afterwards would
    quietly deprioritise everything that follows.  The forked
    transaction child has no use for this: it exits, and exiting is
    the cleanest way out of a cgroup.

    Never raises.  A failure to restore is logged and leaves the
    process in the cgroup, which is a slow operation rather than a
    broken one.
    """
    origin = _own_cgroup_path()
    group = _join_cgroup()
    if group is None:
        # No cgroup to leave; the fallback is a per-process setting
        # that would survive the phase, so it is applied only where
        # the process does not outlive the work.
        yield BY_NOTHING
        return
    logger.info(
        "I/O budget : this phase runs in cgroup %s (io.weight=%d)",
        CGROUP_NAME, IO_WEIGHT)
    try:
        yield BY_CGROUP
    finally:
        if origin:
            try:
                target = CGROUP_ROOT / origin.lstrip("/") / "cgroup.procs"
                target.write_text(str(os.getpid()))
            except OSError as exc:
                logger.warning(
                    "could not leave cgroup %s : %s — this process stays "
                    "deprioritised", CGROUP_NAME, exc)
        _remove_cgroup(group)
