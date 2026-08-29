"""Tests for the Phase B per-action history lifecycle
(SPEC_DISTUPGRADE §3.B)."""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

import pytest

from urpm.core.database import PackageDatabase


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urpm.core.config.get_system_version", lambda root=None: "10")
    db_path = tmp_path / "packages.db"
    d = PackageDatabase(db_path=db_path)
    yield d
    d.close()


class TestBeginTransactionCarriesPid:
    def test_pid_running_populated_at_begin(self, db):
        tx = db.begin_transaction("install", command="urpm install foo")
        conn = db._get_connection()
        row = conn.execute(
            "SELECT pid_running, status FROM history WHERE id = ?",
            (tx,),
        ).fetchone()
        assert row["pid_running"] == os.getpid()
        assert row["status"] == "running"


class TestTransactionTerminalStateClearsPid:
    def test_complete_transaction_clears_pid_running(self, db):
        tx = db.begin_transaction("upgrade")
        db.complete_transaction(tx, return_code=0)
        conn = db._get_connection()
        row = conn.execute(
            "SELECT pid_running, status FROM history WHERE id = ?",
            (tx,),
        ).fetchone()
        assert row["pid_running"] is None
        assert row["status"] == "complete"

    def test_abort_transaction_clears_pid_running(self, db):
        tx = db.begin_transaction("upgrade")
        db.abort_transaction(tx)
        conn = db._get_connection()
        row = conn.execute(
            "SELECT pid_running, status FROM history WHERE id = ?",
            (tx,),
        ).fetchone()
        assert row["pid_running"] is None
        assert row["status"] == "interrupted"


class TestPerActionLifecycle:
    """Cover TB.1 record_action_start + TB.2 record_action_end."""

    def _plan_one_row(self, db, tx, nevra="foo-1.0-1.mga11.x86_64"):
        db.record_package(tx, nevra, "foo", "install", "explicit")
        db.conn.commit()
        return nevra

    def test_record_planted_row_has_planned_status(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        row = db.conn.execute(
            "SELECT status, started_at, finished_at, error_message "
            "FROM history_packages "
            "WHERE history_id = ? AND pkg_nevra = ?",
            (tx, nevra),
        ).fetchone()
        assert row["status"] == "planned"
        assert row["started_at"] is None
        assert row["finished_at"] is None
        assert row["error_message"] is None

    def test_record_action_start_populates_started_at(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        db.record_action_start(tx, nevra)
        db.conn.commit()
        row = db.conn.execute(
            "SELECT status, started_at FROM history_packages "
            "WHERE history_id = ? AND pkg_nevra = ?",
            (tx, nevra),
        ).fetchone()
        # start does NOT touch status — that's the end's job.
        assert row["status"] == "planned"
        assert row["started_at"] is not None
        assert row["started_at"] > 0

    def test_record_action_end_done(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        db.record_action_start(tx, nevra)
        db.record_action_end(tx, nevra, status="done")
        db.conn.commit()
        row = db.conn.execute(
            "SELECT status, finished_at, error_message "
            "FROM history_packages "
            "WHERE history_id = ? AND pkg_nevra = ?",
            (tx, nevra),
        ).fetchone()
        assert row["status"] == "done"
        assert row["finished_at"] is not None
        assert row["error_message"] is None

    def test_record_action_end_failed_stores_message(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        db.record_action_end(tx, nevra, status="failed",
                             error_message="prein_scriptlet_exit_1")
        db.conn.commit()
        row = db.conn.execute(
            "SELECT status, error_message FROM history_packages "
            "WHERE history_id = ? AND pkg_nevra = ?",
            (tx, nevra),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "prein_scriptlet_exit_1"

    def test_record_action_end_skipped(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        db.record_action_end(tx, nevra, status="skipped")
        db.conn.commit()
        row = db.conn.execute(
            "SELECT status FROM history_packages "
            "WHERE history_id = ? AND pkg_nevra = ?",
            (tx, nevra),
        ).fetchone()
        assert row["status"] == "skipped"

    def test_record_action_end_rejects_planned(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        with pytest.raises(ValueError, match="invalid terminal status"):
            db.record_action_end(tx, nevra, status="planned")

    def test_record_action_end_rejects_bogus(self, db):
        tx = db.begin_transaction("install")
        nevra = self._plan_one_row(db, tx)
        with pytest.raises(ValueError, match="invalid terminal status"):
            db.record_action_end(tx, nevra, status="running")


class TestResetAfterFork:
    """TB.3 : reset_after_fork drops inherited connections without
    closing the underlying fds (parent still owns them)."""

    def test_reset_clears_thread_local(self, db):
        # Prime the connection cache
        _ = db._get_connection()
        assert hasattr(db._local, "conn") and db._local.conn is not None
        db.reset_after_fork()
        assert getattr(db._local, "conn", None) is None
        assert db.conn is None

    def test_reset_clears_all_conns_list(self, db):
        _ = db._get_connection()
        assert len(db._all_conns) >= 1
        db.reset_after_fork()
        assert db._all_conns == []

    def test_reset_does_not_close_conns(self, db):
        """The parent still owns the fds — closing would break it.
        Just drop our tracking."""
        conn = db._get_connection()
        db.reset_after_fork()
        # Underlying connection should still be usable from the parent
        row = conn.execute("SELECT 1").fetchone()
        assert row[0] == 1

    def test_get_connection_after_reset_returns_fresh(self, db):
        conn1 = db._get_connection()
        db.reset_after_fork()
        conn2 = db._get_connection()
        assert conn2 is not conn1

    def test_registered_in_forkable_dbs(self, db):
        from urpm.core.database import _all_forkable_dbs
        assert db in _all_forkable_dbs
