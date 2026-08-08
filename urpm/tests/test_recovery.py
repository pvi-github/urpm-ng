"""Tests for Phase D recovery (SPEC_DISTUPGRADE §3.D).

- TD.1 check_orphaned_transactions : dead pid_running → orphan.
- TD.2 startup warning printed to stderr on orphan detection.
- TD.3 reconcile_running_transactions cross-checks rpmdb and flips
  status to 'complete' or 'interrupted' based on installed NEVRA.
"""

from __future__ import annotations

import io
import os
import sys
from unittest.mock import patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.recovery import (
    _pid_alive,
    _pid_is_urpm,
    check_orphaned_transactions,
    reconcile_running_transactions,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urpm.core.config.get_system_version", lambda: "10")
    db_path = tmp_path / "packages.db"
    d = PackageDatabase(db_path=db_path)
    yield d
    d.close()


class TestPidAlive:
    def test_zero_and_negative_are_not_alive(self):
        assert not _pid_alive(0)
        assert not _pid_alive(-1)

    def test_none_is_not_alive(self):
        assert not _pid_alive(None)

    def test_self_pid_is_alive(self):
        assert _pid_alive(os.getpid())

    def test_absurd_pid_is_not_alive(self):
        # PIDs are 32 bits max, this one is guaranteed unused
        assert not _pid_alive(999_999_999)


class TestPidIsUrpm:
    def test_current_pytest_is_not_urpm(self):
        # pytest's cmdline doesn't include 'urpm'
        assert not _pid_is_urpm(os.getpid())

    def test_dead_pid_returns_false(self):
        assert not _pid_is_urpm(999_999_999)


class TestCheckOrphaned:
    def test_no_running_row_returns_empty(self, db):
        assert check_orphaned_transactions(db) == []

    def test_live_urpm_pid_is_not_orphan(self, db):
        tx = db.begin_transaction("install")  # writes our own pid
        with patch("urpm.core.recovery._pid_alive", return_value=True), \
             patch("urpm.core.recovery._pid_is_urpm", return_value=True):
            assert check_orphaned_transactions(db) == []

    def test_dead_pid_is_orphan(self, db):
        tx = db.begin_transaction("upgrade")
        with patch("urpm.core.recovery._pid_alive", return_value=False):
            orphans = check_orphaned_transactions(db)
        assert len(orphans) == 1
        assert orphans[0]["id"] == tx

    def test_live_pid_not_urpm_is_orphan_pid_reuse(self, db):
        """PID recycled by init post-reboot to a non-urpm daemon."""
        tx = db.begin_transaction("remove")
        with patch("urpm.core.recovery._pid_alive", return_value=True), \
             patch("urpm.core.recovery._pid_is_urpm", return_value=False):
            orphans = check_orphaned_transactions(db)
        assert len(orphans) == 1
        assert orphans[0]["id"] == tx

    def test_null_pid_running_treated_as_orphan(self, db):
        """Pre-v33 row currently being migrated : no pid_running column
        value, still counts as orphan to nudge the user."""
        db.conn.execute(
            "INSERT INTO history (timestamp, action, status) "
            "VALUES (?, 'install', 'running')", (0,))
        db.conn.commit()
        orphans = check_orphaned_transactions(db)
        assert len(orphans) == 1


class TestReconcile:
    def test_no_orphan_no_op(self, db):
        assert reconcile_running_transactions(db) == {}

    def test_orphan_with_all_installed_becomes_complete(self, db):
        tx = db.begin_transaction("install")
        db.record_package(tx, "foo-1-1.mga11.x86_64", "foo", "install",
                          "explicit")
        db.conn.commit()
        with patch("urpm.core.recovery._pid_alive", return_value=False), \
             patch("urpm.core.recovery._pkg_installed_at_nevra",
                   return_value=True):
            decisions = reconcile_running_transactions(db)
        assert decisions == {tx: "complete"}
        row = db.conn.execute(
            "SELECT status, pid_running FROM history WHERE id = ?",
            (tx,)).fetchone()
        assert row["status"] == "complete"
        assert row["pid_running"] is None

    def test_orphan_with_missing_pkg_becomes_interrupted(self, db):
        tx = db.begin_transaction("upgrade")
        db.record_package(tx, "foo-1-1.mga11.x86_64", "foo", "install",
                          "explicit")
        db.conn.commit()
        with patch("urpm.core.recovery._pid_alive", return_value=False), \
             patch("urpm.core.recovery._pkg_installed_at_nevra",
                   return_value=False):
            decisions = reconcile_running_transactions(db)
        assert decisions == {tx: "interrupted"}
        row = db.conn.execute(
            "SELECT status FROM history WHERE id = ?", (tx,)).fetchone()
        assert row["status"] == "interrupted"

    def test_orphan_remove_still_installed_becomes_interrupted(self, db):
        tx = db.begin_transaction("remove")
        db.record_package(tx, "foo-1-1.mga11.x86_64", "foo", "remove",
                          "explicit")
        db.conn.commit()
        with patch("urpm.core.recovery._pid_alive", return_value=False), \
             patch("urpm.core.recovery._pkg_installed_at_nevra",
                   return_value=True):
            decisions = reconcile_running_transactions(db)
        assert decisions == {tx: "interrupted"}

    def test_reconcile_is_idempotent(self, db):
        tx = db.begin_transaction("install")
        db.record_package(tx, "foo-1-1.mga11.x86_64", "foo", "install",
                          "explicit")
        db.conn.commit()
        with patch("urpm.core.recovery._pid_alive", return_value=False), \
             patch("urpm.core.recovery._pkg_installed_at_nevra",
                   return_value=True):
            first = reconcile_running_transactions(db)
            second = reconcile_running_transactions(db)
        assert first == {tx: "complete"}
        # Second pass sees the row as complete → no reconciliation.
        assert second == {}


class TestStartupWarning:
    def test_warning_printed_on_orphan(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "urpm.core.config.get_system_version", lambda: "10")
        db_path = tmp_path / "packages.db"

        # First open : bootstrap schema, no orphans.
        d = PackageDatabase(db_path=db_path)
        # Seed a running-with-dead-pid row.
        d.conn.execute(
            "INSERT INTO history (timestamp, action, status, pid_running) "
            "VALUES (?, 'install', 'running', ?)", (0, 999_999_998))
        d.conn.commit()
        d.close()

        # Second open : startup warning should fire.
        capsys.readouterr()  # drop first-open output
        d2 = PackageDatabase(db_path=db_path)
        err = capsys.readouterr().err
        d2.close()
        assert "[urpm] transaction" in err
        assert "interrupted" in err
        assert "urpm recover" in err

    def test_no_warning_when_clean(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "urpm.core.config.get_system_version", lambda: "10")
        db_path = tmp_path / "packages.db"
        d = PackageDatabase(db_path=db_path)
        err = capsys.readouterr().err
        d.close()
        assert "urpm recover" not in err
