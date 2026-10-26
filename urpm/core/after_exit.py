"""Carrying out a post-operation action once urpm itself is gone.

SPEC_POST_TRANSACTION_HOOKS §5.1.  The rules of
:mod:`urpm.core.hooks` normally act at the end of an operation, inside
the urpm process, and that is enough to aim at a service, even the one
running the transaction.  Two things escape it, and both are about the
process rather than the action:

* it cannot see the code that has just replaced its own.  Its modules
  were imported before rpm wrote the new files, so its own idea of
  what the schema should be is the old one;
* it cannot let go of the database it holds for the length of the
  operation.  Measured the 2026-10-25: a migration attempted from the
  ``%post`` of ``urpm-ng-core`` added over four minutes to an upgrade,
  waiting out 30-second busy timeouts against the very urpm that was
  running the scriptlet.

Hence this module.  A fork alone would not do either: a child inherits
the same stale modules.  So the child waits for the parent to die and
then **replaces itself** with a fresh interpreter, which is the one
that reads the installed code.  :func:`spawn` is the waiting half,
:func:`main` is what the fresh interpreter runs.

The verbs are the closed vocabulary of :class:`urpm.core.hooks.Action`,
for the reason given in §7: this runs as root with nobody watching, so
it names known operations and never a command line.  Every outcome is
written back to the waiting line in the history, which is the only
account the operator will get of something that happened after their
command returned.
"""

from __future__ import annotations

import os
import select
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .hooks import Action

#: How long the child waits before giving up on a parent that will not
#: die.  A urpm operation can be long, but not this long, and an
#: orphan waiting for ever is worse than an action not carried out: the
#: lazy path and the pending-upgrade warning both remain.
_WAIT_TIMEOUT_S = 6 * 3600


def _migrate_db(database, subject: str) -> str:
    """Bring the package database's schema up to the installed code.

    Opening the database is what applies the migrations, so by the time
    this is called the work is already done; what is left is to say so.
    The caller opened it with the *new* code, which is the whole reason
    this verb only exists at the deferred moment.
    """
    version = database.conn.execute(
        "SELECT version FROM schema_info").fetchone()[0]
    return f"schema at v{version}"


#: ``verb -> implementation``.  Keyed on the closed vocabulary; a verb
#: the loader accepts and this cannot perform would be a rule that
#: fires into nothing, which a test checks for.
VERBS: Dict[str, Callable[[object, str], str]] = {
    Action.MIGRATE_DB: _migrate_db,
}


def spawn(waiting: Sequence[Tuple[int, str, str]],
          pids: Optional[Sequence[int]] = None,
          db_path: Optional[str] = None) -> Optional[int]:
    """Fork a child that will act once every process in *pids* is gone.

    Returns the child's pid, or ``None`` when there is nothing to do or
    the fork is refused, which is never fatal: a deferred action is one
    whose loss is recoverable, by the invariant of §5.1.

    Args:
        waiting: ``(history line id, verb, subject)`` for each action,
            already recorded by
            :meth:`~urpm.core.db.history.HistoryMixin.record_deferred_action`.
        pids: Processes to outlive.  Defaults to this one alone, which
            is right only when it is the last to let go; the caller
            adds the children it delegated the operation to and then
            released, since those keep working, and keep the database
            open, after urpm has answered the operator.  See
            :func:`urpm.core.transaction_queue.released_children`.
        db_path: Database to act upon, for a caller that is not on the
            system path (tests, a chroot).  ``None`` means the default.
    """
    if not waiting:
        return None
    watch = tuple(pids) if pids else (os.getpid(),)
    try:
        pid = os.fork()
    except OSError:
        return None  # out of processes: the lazy paths still apply
    if pid:
        return pid

    # ── child ───────────────────────────────────────────────────────
    # Nothing below may raise back into the parent's code: this process
    # shares its memory image and must never run any of it again.
    #
    # The replacement happens *before* the wait, not after.  Waiting
    # first would mean holding the parent's open descriptors on the
    # database -- measured at three, the file, its WAL and its shared
    # memory -- for as long as the wait lasts, which blocks the WAL
    # from being consolidated; a distupgrade is exactly where that
    # costs the most.  SQLite opens them ``O_CLOEXEC``, so the exec
    # closes all three, and the fresh interpreter then waits with
    # nothing of ours in hand and nothing of ours in memory.
    # Nothing is imported here, and that is a rule rather than a
    # preference: this child shares the parent's ``sys.modules``, whose
    # entries were loaded before rpm replaced the files on disk, while
    # anything imported fresh is read from the new ones.  Mixing the two
    # is the very hazard this module exists to avoid, and it bit once,
    # the 2026-10-26, from a module-level import added right here.
    try:
        os.setsid()  # survive the session the caller was started from
        devnull = os.open(os.devnull, os.O_RDWR)
        for stream in (0, 1, 2):
            os.dup2(devnull, stream)
        os.execv(sys.executable, _argv_for(waiting, watch, db_path))
    except BaseException:
        pass
    os._exit(0)


def _argv_for(waiting: Sequence[Tuple[int, str, str]],
              pids: Sequence[int],
              db_path: Optional[str]) -> List[str]:
    """The fresh interpreter's command line.

    ``-m`` rather than a path: the module is found in the *installed*
    package, which is the point, and no new file has to be shipped or
    looked up.  Everything the wait and the actions need travels as
    arguments, so the new process needs nothing from the old one.
    """
    argv = [sys.executable, "-m", "urpm.core.after_exit", "run",
            db_path or "", ",".join(str(pid) for pid in pids)]
    for action_id, verb, subject in waiting:
        argv += [str(action_id), verb, subject or ""]
    return argv


def _wait_for_exit(pids: Sequence[int]) -> None:
    """Block until every process in *pids* has terminated.

    ``os.pidfd_open`` is the primitive this needs, and the only one
    that fits: the processes to outlive include the child urpm
    delegated the operation to, which is this watcher's **sibling**, so
    neither ``waitpid`` nor ``PR_SET_PDEATHSIG`` can see it go.  A
    pidfd works on any process of our own uid, becomes readable the
    moment that process terminates whether or not anyone reaps it, and
    carries no race: opening it fails outright when the process is
    already gone, which is the answer we want.

    Bounded by :data:`_WAIT_TIMEOUT_S`: an orphan waiting for ever is
    worse than an action not carried out, the lazy paths of §5.1 being
    what makes that tolerable.
    """
    watching = {}
    for pid in pids:
        try:
            watching[os.pidfd_open(pid)] = pid
        except (OSError, ValueError):
            continue  # already gone, or a pid we cannot open
    if not watching:
        return

    poller = select.poll()
    for fd in watching:
        poller.register(fd, select.POLLIN)

    deadline = time.monotonic() + _WAIT_TIMEOUT_S
    try:
        while watching:
            left_ms = int((deadline - time.monotonic()) * 1000)
            if left_ms <= 0:
                return
            for fd, _event in poller.poll(left_ms):
                poller.unregister(fd)
                watching.pop(fd, None)
                os.close(fd)
    finally:
        for fd in watching:
            os.close(fd)


def _perform(database, action_id: int, verb: str, subject: str) -> bool:
    """Run one verb and complete its line.  True when it went through."""
    implementation = VERBS.get(verb)
    if implementation is None:
        database.complete_deferred_action(
            action_id, "failed", f"unknown action {verb!r}")
        return False
    try:
        detail = implementation(database, subject)
    except Exception as exc:  # the operator's only account of this
        database.complete_deferred_action(action_id, "failed", str(exc))
        return False
    database.complete_deferred_action(action_id, "done", detail)
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point of the fresh interpreter.

    ``run <db> <pids> <id> <verb> <subject> [<id> <verb> <subject>…]``,
    where ``db`` may be empty for the default one and ``pids`` is a
    comma-separated list, possibly empty.  The wait happens here, in
    this process, so the one that forked us keeps nothing open while
    it lasts.

    Returns 0 when every action went through, 1 when one failed, 2 on a
    command line this does not understand.  The exit code is for a
    human reading a log: nothing waits on this process.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "run" or len(argv) < 6:
        return 2
    db_path, pids_raw, triples = argv[1], argv[2], argv[3:]
    if len(triples) % 3:
        return 2
    try:
        parsed = [(int(triples[i]), triples[i + 1], triples[i + 2])
                  for i in range(0, len(triples), 3)]
        pids = [int(pid) for pid in pids_raw.split(",") if pid]
    except ValueError:
        return 2

    # Out of the cgroup this was forked into, before the wait.  Under a
    # systemd unit, stopping it signals every process in its cgroup by
    # default, and what this process waits for *is* that unit going
    # away: it would be killed at the exact moment it is meant to act.
    # Measured the 2026-10-26, see
    # :func:`urpm.core.txresources.leave_unit_cgroup`.
    from .txresources import leave_unit_cgroup

    leave_unit_cgroup()

    _wait_for_exit(pids)

    from .database import PackageDatabase

    database = PackageDatabase(db_path=Path(db_path) if db_path else None)
    try:
        known = {row["id"] for row in database.list_deferred_actions()}
        failed = False
        for action_id, verb, subject in parsed:
            if action_id not in known:
                continue  # history pruned since; not an error
            if not _perform(database, action_id, verb, subject):
                failed = True
        return 1 if failed else 0
    finally:
        database.close()


if __name__ == "__main__":  # pragma: no cover - exec'd entry point
    sys.exit(main())
