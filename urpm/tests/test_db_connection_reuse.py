"""Per-thread SQLite connections must not accumulate descriptors.

``urpmd`` answers every HTTP request in a fresh thread, and a thread
that touches the database gets its own connection.  Those connections
used to be kept in a registry for the lifetime of the process, so a
daemon leaked descriptors at the rate it served requests: a tester's
reached the 1024 soft limit, after which it could open neither the
database, nor ``/proc/loadavg``, nor its own lock file, and spun a
whole core in an accept loop with no descriptor left to accept with.

What makes this a reuse problem rather than a closing problem:
``conn.close()`` does not hand the descriptors back.  SQLite's unix VFS
defers closing the main database file while the process holds another
connection to the same inode, because that close would drop every POSIX
lock the process holds on it.  A daemon always keeps one connection
open, so closing the others frees their ``-wal`` and nothing else.  The
fix therefore recycles a finished thread's connection instead of
opening another one, and these tests pin the descriptor count, which is
the property that failed in the field.
"""

from __future__ import annotations

import collections
import os
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase


pytestmark = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="descriptor accounting needs /proc/self/fd",
)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="conn_reuse_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            database.list_media()      # prime the main-thread connection
            yield database
        finally:
            database.close()


def open_descriptors(db) -> int:
    """How many descriptors this process holds on *db*'s files.

    Counts the database, its ``-wal`` and its ``-shm``, which is what a
    leak actually consumes and what the ``ulimit`` applies to.

    Matched on the full path and not on the file name : every database
    in this suite is called ``packages.db``, and a name match would
    count the ones other test modules hold open in the same process.
    """
    prefix = str(db.db_path)
    total = 0
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except OSError:
            continue                   # closed while we looked
        if target.startswith(prefix):
            total += 1
    return total


def in_a_thread(fn, *args):
    """Run *fn* in a thread and wait for that thread to be gone."""
    thread = threading.Thread(target=fn, args=args)
    thread.start()
    thread.join()


# ── The property that failed in the field ───────────────────────────────


def test_sequential_requests_do_not_accumulate_descriptors(db):
    """The daemon's shape: many short-lived threads, one after another."""
    baseline = open_descriptors(db)

    for _ in range(200):
        in_a_thread(db.list_media)

    assert open_descriptors(db) <= baseline + 2, (
        "descriptors grow with the number of requests served")


def test_a_finished_thread_hands_its_connection_back(db):
    """Reuse is observable, not just inferred from the count."""
    seen = []
    in_a_thread(lambda: seen.append(db._get_connection()))
    assert len(db._all_conns) == 2          # main thread + the dead one
    in_a_thread(lambda: seen.append(db._get_connection()))

    assert seen[1] is seen[0], "a second thread opened a new connection"
    assert len(db._all_conns) == 2


def test_concurrent_threads_still_get_their_own(db):
    """Reuse must not serialise live threads onto one connection.

    Each of them runs a statement while the others are in flight, so
    sharing a single handle would be the SQLITE_MISUSE window the
    per-thread design exists to avoid.
    """
    together = threading.Barrier(8)
    connections = []
    guard = threading.Lock()

    def hold():
        conn = db._get_connection()
        with guard:
            connections.append(conn)
        together.wait(timeout=10)       # nobody leaves before all arrived

    threads = [threading.Thread(target=hold) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(conn) for conn in connections}) == 8


def test_the_main_thread_keeps_its_connection(db):
    """It is alive, so it is never a reclamation candidate."""
    main = db._get_connection()
    for _ in range(5):
        in_a_thread(db.list_media)

    assert db._get_connection() is main
    assert threading.current_thread() in db._all_conns


# ── Handing a connection on safely ──────────────────────────────────────


def test_an_uncommitted_transaction_is_not_passed_on(db):
    """A thread can end mid-transaction; the next one must not inherit it."""
    media_id = db.add_media(
        name="Core Release", short_name="core_release",
        mageia_version="10", architecture="x86_64",
        relative_path="10/x86_64/media/core/release",
    )

    def leave_a_write_open():
        conn = db._get_connection()
        conn.execute("UPDATE media SET name = 'half written' WHERE id = ?",
                     (media_id,))
        # no commit, and the thread ends here

    in_a_thread(leave_a_write_open)

    def read_it_back():
        conn = db._get_connection()
        row = conn.execute("SELECT name FROM media WHERE id = ?",
                           (media_id,)).fetchone()
        assert row["name"] == "Core Release"
        # And the connection is usable for a write of our own.
        conn.execute("UPDATE media SET priority = 5 WHERE id = ?",
                     (media_id,))
        conn.commit()

    in_a_thread(read_it_back)


def test_an_unusable_connection_is_dropped_not_handed_out(db):
    """A connection that will not roll back has no business being reused."""
    in_a_thread(db.list_media)
    assert db._all_conns

    # Close the idle connection behind the pool's back, the way a
    # caller holding a stale reference might.
    (dead_thread,) = [t for t in db._all_conns
                      if t is not threading.current_thread()]
    db._all_conns[dead_thread].close()

    got = []
    in_a_thread(lambda: got.append(db._get_connection()))
    assert got[0].execute("SELECT 1").fetchone()[0] == 1


# ── Shutdown and fork ───────────────────────────────────────────────────


def test_close_releases_everything_including_the_idle_pool(db):
    for _ in range(5):
        in_a_thread(db.list_media)
    assert db._idle_conns or db._all_conns

    db.close()
    assert open_descriptors(db) == 0


def test_reset_after_fork_drops_the_idle_pool_too(db):
    """A child must not reuse a connection inherited from its parent."""
    in_a_thread(db.list_media)
    db.reset_after_fork()

    assert db._all_conns == {}
    assert db._idle_conns == []
    # And the parent's connections still work, since reset does not close.
    assert db._get_connection().execute("SELECT 1").fetchone()[0] == 1
