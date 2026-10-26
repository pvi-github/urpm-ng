"""Carrying out an action once urpm is gone.

SPEC_POST_TRANSACTION_HOOKS §5.1.  Two properties make this the only
way to do certain things, and both are about the process rather than
about the action: a process cannot see the code that has just replaced
its own, having imported its modules before rpm wrote them, and it
cannot let go of the database it holds for the length of the operation.

So a fork is not enough.  The child inherits the same stale modules, so
it waits for the parent to die and then replaces itself with a fresh
interpreter, which is the one that reads the new code.  ``main`` is
that interpreter's entry point and does the work; the waiting half is
what has to be forked.

The verbs stay a closed vocabulary here too: this runs as root with
nobody watching, and §7 asks for every such action to leave a trace.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from urpm.core import after_exit
from urpm.core.database import PackageDatabase
from urpm.core.hooks import Action


@pytest.fixture
def db_path(tmp_path):
    """A database with one recorded operation, closed again."""
    path = tmp_path / "packages.db"
    database = PackageDatabase(db_path=path)
    database.begin_transaction("upgrade", "urpm upgrade urpm-ng-core")
    database.close()
    return path


def _line(path, action=Action.MIGRATE_DB, subject=None):
    """Record a waiting line and give back its id."""
    database = PackageDatabase(db_path=path)
    try:
        return database.record_deferred_action(1, "db-schema", action,
                                               subject=subject)
    finally:
        database.close()


def _row(path, action_id):
    database = PackageDatabase(db_path=path)
    try:
        for row in database.list_deferred_actions():
            if row["id"] == action_id:
                return row
        return None
    finally:
        database.close()


class TestTheEntryPoint:
    """``main`` is what the fresh interpreter runs."""

    def test_a_migration_is_carried_out_and_recorded(self, db_path):
        action_id = _line(db_path)

        code = after_exit.main(["run", str(db_path), "",
                                str(action_id), Action.MIGRATE_DB, ""])

        assert code == 0
        row = _row(db_path, action_id)
        assert row["status"] == "done"
        assert row["detail"]

    def test_an_unknown_verb_fails_the_line_rather_than_raising(self, db_path):
        # A rule file from a newer urpm, read by this one: refusing is
        # the loader's job, but nothing may crash here either.
        action_id = _line(db_path, action="no-such-verb")

        code = after_exit.main(["run", str(db_path), "",
                                str(action_id), "no-such-verb", ""])

        assert code == 1
        assert _row(db_path, action_id)["status"] == "failed"

    def test_a_line_that_is_gone_is_not_an_error(self, db_path):
        # The operator may have pruned the history meanwhile.
        assert after_exit.main(["run", str(db_path), "",
                                "999999", Action.MIGRATE_DB, ""]) == 0

    def test_a_failing_action_is_recorded_as_failed(self, db_path,
                                                    monkeypatch):
        action_id = _line(db_path)
        monkeypatch.setitem(
            after_exit.VERBS, Action.MIGRATE_DB,
            lambda database, subject: (_ for _ in ()).throw(
                RuntimeError("database is locked")))

        code = after_exit.main(["run", str(db_path), "",
                                str(action_id), Action.MIGRATE_DB, ""])

        assert code == 1
        row = _row(db_path, action_id)
        assert row["status"] == "failed"
        assert "locked" in row["detail"]

    def test_garbage_arguments_are_answered_not_raised(self):
        assert after_exit.main([]) == 2
        assert after_exit.main(["run"]) == 2
        assert after_exit.main(["run", "/tmp/x", "", "not-a-number", "x",
                                ""]) == 2
        assert after_exit.main(["run", "/tmp/x", "not-a-pid", "1", "x",
                                ""]) == 2


class TestWaitingForEveryProcessThatMatters:
    """Which processes have to be gone, and how we notice.

    Not the direct parent alone.  Under smart sync urpm hands the
    operation to a forked child and returns to the shell as soon as
    the extractions are over, by design: the child goes on running
    scriptlets and triggers, and keeps the database open while it
    does.  So an action that needs the database free, or needs the
    code on disk to be the one in use, has to wait for *that* child
    too.

    A watcher forked by urpm is that child's sibling, so neither
    ``waitpid`` nor ``PR_SET_PDEATHSIG`` can see it die.
    ``os.pidfd_open`` works on any process of our own uid, with no
    parent relationship and no race: it fails outright when the
    process is already gone.
    """

    def _sleeper(self, seconds=30):
        return subprocess.Popen([sys.executable, "-c",
                                 f"import time; time.sleep({seconds})"])

    def test_a_process_already_gone_is_not_waited_for(self):
        dead = self._sleeper(0)
        dead.wait()

        start = time.monotonic()
        after_exit._wait_for_exit([dead.pid])

        assert time.monotonic() - start < 2

    def test_nothing_to_watch_returns_at_once(self):
        start = time.monotonic()
        after_exit._wait_for_exit([])

        assert time.monotonic() - start < 2

    def test_it_waits_for_a_live_process_and_notices_its_end(self):
        sleeper = self._sleeper(30)
        start = time.monotonic()

        import threading
        threading.Timer(1.0, sleeper.terminate).start()
        after_exit._wait_for_exit([sleeper.pid])
        waited = time.monotonic() - start
        sleeper.wait()

        assert 0.5 < waited < 20, f"returned after {waited:.1f}s"

    def test_it_waits_for_the_last_of_them(self):
        quick, slow = self._sleeper(0), self._sleeper(30)
        quick.wait()
        import threading
        threading.Timer(1.0, slow.terminate).start()

        start = time.monotonic()
        after_exit._wait_for_exit([quick.pid, slow.pid])
        waited = time.monotonic() - start
        slow.wait()

        assert waited > 0.5, "returned before the slow one was gone"

    def test_a_pid_nobody_owns_is_skipped(self):
        # A delegated child reaped between the snapshot and here.
        start = time.monotonic()
        after_exit._wait_for_exit([999999])

        assert time.monotonic() - start < 2


class TestTheDelegatedChildrenAreKnown:
    """urpm records the children it released rather than waiting."""

    def test_the_snapshot_is_a_tuple_of_pids(self):
        from urpm.core import transaction_queue

        with transaction_queue._UNREAPED_LOCK:
            transaction_queue._UNREAPED_CHILDREN.add(4242)
        try:
            assert 4242 in transaction_queue.released_children()
        finally:
            with transaction_queue._UNREAPED_LOCK:
                transaction_queue._UNREAPED_CHILDREN.discard(4242)

    def test_it_does_not_hand_out_its_own_set(self):
        from urpm.core import transaction_queue

        snapshot = transaction_queue.released_children()

        assert isinstance(snapshot, tuple)


class TestWaitingForTheParent:
    """The half that has to be forked, exercised with a real death."""

    def test_the_action_runs_once_the_parent_is_gone(self, db_path):
        action_id = _line(db_path)
        # A parent that registers the action and exits at once.  Its
        # child must outlive it, notice, and complete the line.
        parent = (
            "import sys; sys.path.insert(0, %r)\n"
            "from urpm.core import after_exit\n"
            "after_exit.spawn([(%d, %r, '')], db_path=%r)\n"
            % (str(Path(after_exit.__file__).parents[2]),
               action_id, Action.MIGRATE_DB, str(db_path))
        )
        subprocess.run([sys.executable, "-c", parent], check=True,
                       timeout=60)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = _row(db_path, action_id)
            if row["status"] != "deferred":
                break
            time.sleep(0.2)

        assert row["status"] == "done", (
            "the detached child never completed the line")

    def test_nothing_is_forked_when_there_is_nothing_to_do(self, db_path):
        before = len(os.listdir("/proc/self/task"))

        assert after_exit.spawn([], db_path=str(db_path)) is None

        assert len(os.listdir("/proc/self/task")) == before


class TestTheWiring:
    """What the end of an operation hands to the child.

    The line goes into the history before the fork, because once the
    command has returned there is no screen left to report on.  And
    nothing here may raise: the operation is committed and recorded, so
    a rule that cannot be handed over is dropped, not escalated.
    """

    class _Hook:
        def __init__(self, identifier, action, subjects=()):
            self.identifier = identifier
            self.action = action
            self.subjects = frozenset(subjects)

    class _Triggered:
        def __init__(self, hook):
            self.hook = hook
            self.subjects = hook.subjects

    class _Ops:
        def __init__(self, db, triggered=(), raises=False):
            self.db = db
            self._triggered = list(triggered)
            self._raises = raises
            self.asked = []

        def hooks_triggered_by(self, transaction_id, operation="",
                               moment=""):
            self.asked.append(moment)
            if self._raises:
                raise RuntimeError("a rule file nobody can parse")
            return self._triggered

    def _defer(self, ops, monkeypatch, spawned=4242):
        from urpm.cli.helpers import hook_report

        calls = []
        monkeypatch.setattr("urpm.core.after_exit.spawn",
                            lambda waiting, **kw: calls.append(waiting)
                            or spawned)
        lines = hook_report.defer_post_exit_actions(ops, 1, "upgrade")
        return lines, calls

    def test_the_line_is_recorded_and_the_child_spawned(self, db_path,
                                                        monkeypatch):
        database = PackageDatabase(db_path=db_path)
        hook = self._Hook("db-schema", Action.MIGRATE_DB, {"40"})
        ops = self._Ops(database, [self._Triggered(hook)])
        try:
            lines, calls = self._defer(ops, monkeypatch)
            recorded = database.list_deferred_actions(1)
        finally:
            database.close()

        assert len(recorded) == 1
        assert recorded[0]["rule"] == "db-schema"
        assert recorded[0]["action"] == Action.MIGRATE_DB
        assert recorded[0]["subject"] == "40"
        assert calls == [[(recorded[0]["id"], Action.MIGRATE_DB, "40")]]
        assert any(Action.MIGRATE_DB in line for line in lines)

    def test_it_asks_for_the_deferred_phase_only(self, db_path, monkeypatch):
        from urpm.core.hooks import Moment

        database = PackageDatabase(db_path=db_path)
        ops = self._Ops(database)
        try:
            self._defer(ops, monkeypatch)
        finally:
            database.close()

        assert ops.asked == [Moment.AFTER_EXIT]

    def test_nothing_deferred_says_nothing(self, db_path, monkeypatch):
        database = PackageDatabase(db_path=db_path)
        ops = self._Ops(database)
        try:
            lines, calls = self._defer(ops, monkeypatch)
        finally:
            database.close()

        assert lines == []
        assert calls == []

    def test_a_rule_file_that_explodes_is_dropped(self, db_path,
                                                  monkeypatch):
        database = PackageDatabase(db_path=db_path)
        ops = self._Ops(database, raises=True)
        try:
            lines, calls = self._defer(ops, monkeypatch)
        finally:
            database.close()

        assert lines == []
        assert calls == []

    def test_a_refused_fork_promises_nothing(self, db_path, monkeypatch):
        # Announcing an action that will not happen would be worse than
        # silence: the line stays in the history as the record.
        database = PackageDatabase(db_path=db_path)
        hook = self._Hook("db-schema", Action.MIGRATE_DB)
        ops = self._Ops(database, [self._Triggered(hook)])
        try:
            lines, _calls = self._defer(ops, monkeypatch, spawned=None)
        finally:
            database.close()

        assert lines == []


class TestTheWatcherHoldsNothing:
    """It replaces itself before waiting, not after.

    Waiting first would mean keeping the parent's open descriptors on
    the database -- the file, its WAL and its shared memory -- for as
    long as the wait lasts, which stops the WAL from being
    consolidated.  A distupgrade is where that costs the most, being
    both the longest operation and the one with the largest WAL.
    SQLite opens those ``O_CLOEXEC``, so the exec is what frees them.
    """

    def test_the_wait_happens_in_the_new_process(self):
        # The pids travel as arguments, which is only useful if the
        # waiting is done downstream of the exec.
        argv = after_exit._argv_for([(1, Action.MIGRATE_DB, "")],
                                    [111, 222], "/tmp/p.db")

        assert argv[3:6] == ["run", "/tmp/p.db", "111,222"]

    def test_it_keeps_no_descriptor_on_the_database(self, db_path):
        database = PackageDatabase(db_path=db_path)
        sleeper = subprocess.Popen([sys.executable, "-c",
                                    "import time; time.sleep(30)"])
        try:
            action_id = database.record_deferred_action(1, "r",
                                                        Action.MIGRATE_DB)
            child = after_exit.spawn([(action_id, Action.MIGRATE_DB, "")],
                                     pids=[sleeper.pid],
                                     db_path=str(db_path))
            assert child, "no watcher was forked"

            deadline = time.monotonic() + 10
            held = None
            while time.monotonic() < deadline:
                try:
                    with open(f"/proc/{child}/cmdline", "rb") as handle:
                        cmdline = handle.read().decode("utf-8", "replace")
                    # The module path, not the bare name: before
                    # the exec the child still carries the test
                    # runner's own command line.
                    if "urpm.core.after_exit" in cmdline:
                        held = [name for name in os.listdir(f"/proc/{child}/fd")
                                if "packages.db" in _target(child, name)]
                        break
                except OSError:
                    pass
                time.sleep(0.2)

            assert held is not None, "the watcher never reached the exec"
            assert held == [], f"still holding {held} on the database"
        finally:
            sleeper.terminate()
            sleeper.wait()
            database.close()


def _target(pid, name):
    try:
        return os.readlink(f"/proc/{pid}/fd/{name}")
    except OSError:
        return ""


class TestTheClosedVocabulary:
    def test_every_deferred_only_verb_has_an_implementation(self):
        # A verb the loader accepts and the runner cannot perform would
        # be a rule that fires into nothing.
        for verb in Action.DEFERRED_ONLY:
            assert verb in after_exit.VERBS

    def test_nothing_outside_the_vocabulary_is_runnable(self):
        assert set(after_exit.VERBS) <= set(Action.ALL)
