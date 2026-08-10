"""Tests for :mod:`urpm.core.deferred_cleanup`."""

import fcntl
import pytest
import tempfile
from pathlib import Path

from urpm.core.database import PackageDatabase
from urpm.core.deferred_cleanup import LOCK_PATH, sweep_pending_drop


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '10')
    db_path = tmp_path / "packages.db"
    database = PackageDatabase(db_path)
    yield database
    database.close()


@pytest.fixture
def lock_in_tmp(monkeypatch, tmp_path):
    """Redirect the deferred-cleanup lock into tmp so the test doesn't
    touch /run/urpm/deferred_cleanup.lock on the real host."""
    fake_lock = tmp_path / "deferred_cleanup.lock"
    monkeypatch.setattr(
        'urpm.core.deferred_cleanup.LOCK_PATH', fake_lock)
    return fake_lock


def _seed_media(db, *, disabled_by=None, name="Core Release"):
    media_id = db.add_media(
        name=name,
        short_name=name.lower().replace(" ", "_"),
        mageia_version="10",
        architecture="x86_64",
        relative_path="core/release",
    )
    if disabled_by is not None:
        conn = db._get_connection()
        with db._lock:
            conn.execute(
                "UPDATE media SET disabled_by = ? WHERE id = ?",
                (disabled_by, media_id))
            conn.commit()
    return media_id


def _seed_pkg(db, media_id, name="foo-1.0-1.mga10.x86_64"):
    conn = db._get_connection()
    with db._lock:
        cur = conn.execute(
            "INSERT INTO packages (name, name_lower, nevra, media_id, "
            "version, release, arch, epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name.split("-")[0], name.split("-")[0].lower(),
             name, media_id, "1.0", "1.mga10", "x86_64", 0),
        )
        pkg_id = cur.lastrowid
        # Add a few deps so the pkg-scoped bulk delete has work.
        for cap in ("libc.so.6", "libfoo.so.1"):
            conn.execute(
                "INSERT INTO requires (pkg_id, capability) VALUES (?, ?)",
                (pkg_id, cap))
        conn.execute(
            "INSERT INTO provides (pkg_id, capability) VALUES (?, ?)",
            (pkg_id, name.split("-")[0]))
        conn.commit()
    return pkg_id


class TestSweepPendingDrop:

    def test_no_op_when_nothing_pending(self, db, lock_in_tmp):
        _seed_media(db, disabled_by=None)
        assert sweep_pending_drop(db) == 0
        # No lock file should be touched on the fast path.
        assert not lock_in_tmp.exists()

    def test_purges_pending_rows_and_their_deps(self, db, lock_in_tmp):
        m1 = _seed_media(db, disabled_by="pending_drop", name="Dead A")
        m2 = _seed_media(db, disabled_by="pending_drop", name="Dead B")
        _seed_media(db, disabled_by=None, name="Alive")
        p1 = _seed_pkg(db, m1, "foo-1.0-1.mga10.x86_64")
        p2 = _seed_pkg(db, m2, "bar-2.0-1.mga10.x86_64")

        assert sweep_pending_drop(db) == 2

        conn = db._get_connection()
        assert conn.execute(
            "SELECT COUNT(*) FROM media WHERE id IN (?, ?)",
            (m1, m2)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM packages WHERE id IN (?, ?)",
            (p1, p2)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM requires WHERE pkg_id IN (?, ?)",
            (p1, p2)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM provides WHERE pkg_id IN (?, ?)",
            (p1, p2)).fetchone()[0] == 0
        # The unmarked media survives.
        assert conn.execute(
            "SELECT COUNT(*) FROM media WHERE short_name = 'alive'"
        ).fetchone()[0] == 1

    def test_lock_busy_returns_zero(self, db, lock_in_tmp):
        _seed_media(db, disabled_by="pending_drop", name="Dead A")
        # Grab the lock from a competing process — a second open() +
        # LOCK_EX | LOCK_NB should fail with BlockingIOError, which
        # sweep_pending_drop must catch and return 0 for.
        lock_in_tmp.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_in_tmp, "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert sweep_pending_drop(db) == 0
            # Row is still flagged — didn't get dropped.
            assert db._get_connection().execute(
                "SELECT COUNT(*) FROM media WHERE disabled_by='pending_drop'"
            ).fetchone()[0] == 1
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

    def test_idempotent(self, db, lock_in_tmp):
        m = _seed_media(db, disabled_by="pending_drop", name="Dead X")
        _seed_pkg(db, m)
        assert sweep_pending_drop(db) == 1
        # Second call: nothing left, fast-path no-op returns 0.
        assert sweep_pending_drop(db) == 0
