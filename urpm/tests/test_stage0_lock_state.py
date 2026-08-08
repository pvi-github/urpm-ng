"""Tests for TS0.1 (lock), TS0.2 (.state), TS0.3 (clock gate).

SPEC_DISTUPGRADE §4.0.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.lock import (
    DistupgradeLockedError,
    acquire_distupgrade_lock,
    release_distupgrade_lock,
)
from urpm.core.distupgrade.state import (
    STATE_CONFIG_KEY,
    DistupgradeStateError,
    delete_state,
    read_state,
    write_state,
)
from urpm.core.distupgrade.stage0 import (
    ClockGateError,
    check_clock_sanity,
)


@pytest.fixture
def state_db(tmp_path):
    """A short-lived :class:`PackageDatabase` for state tests.

    The distupgrade state persists in the SQLite ``config`` table, so
    tests instantiate a fresh DB per-case and pass it explicitly to
    :func:`read_state` / :func:`write_state` — no monkeypatching of a
    module-level singleton.
    """
    db = PackageDatabase(db_path=tmp_path / "packages.db")
    yield db
    db.close()


# ── TS0.2 ────────────────────────────────────────────────────────────


class TestStateAtomicWrite:
    def test_read_absent_returns_none(self, state_db):
        assert read_state(state_db) is None

    def test_write_then_read_roundtrip(self, state_db):
        payload = {
            "version_from": "10", "version_to": "11",
            "stage": "pre_check_done",
            "started_at": "2026-08-05T00:00:00Z",
        }
        write_state(payload, state_db)
        assert read_state(state_db) == payload

    def test_write_replaces_previous(self, state_db):
        write_state({"stage": "old"}, state_db)
        write_state({"stage": "new", "extra": 1}, state_db)
        assert read_state(state_db) == {"stage": "new", "extra": 1}

    def test_malformed_json_raises(self, state_db):
        # A corrupted row (direct set_config bypassing our JSON dump)
        # must surface as a DistupgradeStateError, not a silent None.
        state_db.set_config(STATE_CONFIG_KEY, "{not json")
        with pytest.raises(DistupgradeStateError):
            read_state(state_db)

    def test_delete_absent_is_silent(self, state_db):
        delete_state(state_db)  # must not raise

    def test_delete_removes(self, state_db):
        write_state({"stage": "x"}, state_db)
        assert read_state(state_db) == {"stage": "x"}
        delete_state(state_db)
        assert read_state(state_db) is None


# ── TS0.1 ────────────────────────────────────────────────────────────


class TestLockAcquireRelease:
    def test_acquire_creates_file_with_our_pid(self, tmp_path,
                                               monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        fd = acquire_distupgrade_lock(dir_path=tmp_path)
        try:
            content = (tmp_path / "distupgrade.lock").read_bytes()
            assert content == f"{os.getpid()}\n".encode()
        finally:
            release_distupgrade_lock(fd, dir_path=tmp_path)

    def test_release_removes_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        fd = acquire_distupgrade_lock(dir_path=tmp_path)
        release_distupgrade_lock(fd, dir_path=tmp_path)
        assert not (tmp_path / "distupgrade.lock").exists()

    def test_second_acquire_refuses_when_live(self, tmp_path,
                                              monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._pid_alive",
            lambda _pid: True)
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._pid_is_distupgrade",
            lambda _pid: True)
        fd = acquire_distupgrade_lock(dir_path=tmp_path)
        try:
            with pytest.raises(DistupgradeLockedError) as exc:
                acquire_distupgrade_lock(dir_path=tmp_path)
            assert exc.value.pid == os.getpid()
        finally:
            release_distupgrade_lock(fd, dir_path=tmp_path)

    def test_orphan_lock_is_taken_over(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        # Simulate a stale lockfile from a dead process.
        (tmp_path / "distupgrade.lock").write_bytes(b"999999998\n")
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._pid_alive",
            lambda _pid: False)
        fd = acquire_distupgrade_lock(dir_path=tmp_path)
        try:
            content = (tmp_path / "distupgrade.lock").read_bytes()
            assert content == f"{os.getpid()}\n".encode()
        finally:
            release_distupgrade_lock(fd, dir_path=tmp_path)

    def test_pid_reuse_but_not_urpm_is_orphan(self, tmp_path, monkeypatch):
        """Post-reboot PID reuse on non-tmpfs /run : PID is alive but
        it's now a dbus-daemon, not urpm."""
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        (tmp_path / "distupgrade.lock").write_bytes(b"12345\n")
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._pid_alive",
            lambda _pid: True)
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._pid_is_distupgrade",
            lambda _pid: False)
        fd = acquire_distupgrade_lock(dir_path=tmp_path)
        try:
            content = (tmp_path / "distupgrade.lock").read_bytes()
            assert content == f"{os.getpid()}\n".encode()
        finally:
            release_distupgrade_lock(fd, dir_path=tmp_path)

    def test_corrupt_lockfile_treated_as_orphan(self, tmp_path,
                                                monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        (tmp_path / "distupgrade.lock").write_bytes(b"not-a-pid\n")
        fd = acquire_distupgrade_lock(dir_path=tmp_path)
        release_distupgrade_lock(fd, dir_path=tmp_path)

    def test_partial_pid_write_retries(self, tmp_path, monkeypatch):
        """PID missing terminator → « still being established », loop
        retries.  Simulate by writing a partial then a full line."""
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._audit_parent",
            lambda _fd: None)
        (tmp_path / "distupgrade.lock").write_bytes(b"12345")  # no \n
        monkeypatch.setattr(
            "urpm.core.distupgrade.lock._pid_alive",
            lambda _pid: False)
        # Even with a partial write, after unlink+retry the acquire
        # succeeds.  The check verifies the loop doesn't crash on a
        # ValueError-shaped read.
        fd = acquire_distupgrade_lock(dir_path=tmp_path, max_retries=5)
        release_distupgrade_lock(fd, dir_path=tmp_path)

    def test_audit_rejects_non_root_parent(self, tmp_path, monkeypatch):
        # Simulate a parent owned by a non-root user by faking fstat.
        class FakeStat:
            st_uid = 1000
            st_mode = 0o755
        monkeypatch.setattr(os, "fstat", lambda _fd: FakeStat())
        with pytest.raises(DistupgradeLockedError):
            acquire_distupgrade_lock(dir_path=tmp_path)


# ── TS0.3 ────────────────────────────────────────────────────────────


class TestClockSanity:
    def test_now_after_release_mtime_passes(self, tmp_path):
        release = tmp_path / "mageia-release"
        release.write_text("Mageia 10\n")
        past = time.time() - 3600
        os.utime(release, (past, past))
        check_clock_sanity(release)  # must not raise

    def test_now_before_release_mtime_raises(self, tmp_path):
        release = tmp_path / "mageia-release"
        release.write_text("Mageia 10\n")
        future = time.time() + 3600 * 24 * 365 * 5
        os.utime(release, (future, future))
        with pytest.raises(ClockGateError) as exc:
            check_clock_sanity(release)
        msg = str(exc.value)
        assert "urpm install chrony" in msg
        assert "chronyc makestep" in msg

    def test_missing_release_file_is_no_op(self, tmp_path):
        release = tmp_path / "does-not-exist"
        check_clock_sanity(release)  # must not raise
