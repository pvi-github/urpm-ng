"""Tests du mesh §2 de SPEC_DISTUPGRADE.

Vérifie que les commandes write refusent quand ``distupgrade.state``
(SQLite) est présent ou quand un lock live est tenu par un vrai
distupgrade, et que l'échappatoire install/erase ponctuelle est
autorisée avec warning.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from urpm.cli.helpers.distupgrade_mesh import (
    DISTUPGRADE_LOCK_PATH,
    DistupgradeMeshRefusal,
    check_distupgrade_mesh,
)
from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.state import write_state


@pytest.fixture
def redirect_mesh_paths(tmp_path, monkeypatch):
    """Redirect the lock path + state DB into a tmp dir for isolation."""
    fake_lock = tmp_path / "distupgrade.lock"
    monkeypatch.setattr(
        "urpm.cli.helpers.distupgrade_mesh.DISTUPGRADE_LOCK_PATH", fake_lock)
    # State backing DB (SQLite) : isolated per test.
    state_db = PackageDatabase(db_path=tmp_path / "packages.db")
    yield state_db, fake_lock
    state_db.close()


def _write_state(db: PackageDatabase, stage: str = "pre_check_done") -> None:
    write_state({
        "version_from": "10", "version_to": "11",
        "started_at": "2026-08-05T00:00:00Z",
        "stage": stage,
    }, db)


def _write_lock(path: Path, pid: int) -> None:
    path.write_text(f"{pid}\n")


class TestNoDistupgradeInProgress:
    """No .state, no lock → all commands pass."""

    def test_write_command_passes(self, redirect_mesh_paths):
        state_db, _lock = redirect_mesh_paths
        check_distupgrade_mesh("upgrade", state_db)

    def test_read_only_passes(self, redirect_mesh_paths):
        state_db, _lock = redirect_mesh_paths
        check_distupgrade_mesh("search", state_db, is_read_only=True)


class TestStateFilePresent:
    """.state on disk = interrupted distupgrade → refuse writes."""

    def test_upgrade_refused_with_actionable_message(self, redirect_mesh_paths):
        state_db, _lock = redirect_mesh_paths
        _write_state(state_db, stage="tx_a_committing")
        with pytest.raises(DistupgradeMeshRefusal) as exc:
            check_distupgrade_mesh("upgrade", state_db)
        assert "tx_a_committing" in exc.value.message
        assert "--resume" in exc.value.message
        assert "--abort" in exc.value.message

    def test_read_only_still_passes(self, redirect_mesh_paths):
        state_db, _lock = redirect_mesh_paths
        _write_state(state_db)
        # Search / show / etc. still work.
        check_distupgrade_mesh("search", state_db, is_read_only=True)

    def test_escape_hatch_allowed_with_warning(self, redirect_mesh_paths,
                                               capsys):
        state_db, _lock = redirect_mesh_paths
        _write_state(state_db, stage="tx_a_committing")
        check_distupgrade_mesh("install", state_db, is_escape_hatch=True)
        out = capsys.readouterr().out
        assert "ATTENTION" in out
        assert "suspendu" in out


class TestLiveLock:
    """Lock file with a live distupgrade PID → refuse everything
    including the escape hatch (would race with live process)."""

    def test_refused_when_live_distupgrade_holds_lock(
            self, redirect_mesh_paths):
        state_db, lock = redirect_mesh_paths
        _write_lock(lock, 12345)
        with patch("urpm.cli.helpers.distupgrade_mesh._pid_alive",
                   return_value=True), \
             patch("urpm.cli.helpers.distupgrade_mesh._pid_is_distupgrade",
                   return_value=True):
            with pytest.raises(DistupgradeMeshRefusal) as exc:
                check_distupgrade_mesh("upgrade", state_db)
        assert "12345" in exc.value.message
        assert "--abort" in exc.value.message

    def test_escape_hatch_refused_when_live(self, redirect_mesh_paths):
        state_db, lock = redirect_mesh_paths
        _write_lock(lock, 12345)
        with patch("urpm.cli.helpers.distupgrade_mesh._pid_alive",
                   return_value=True), \
             patch("urpm.cli.helpers.distupgrade_mesh._pid_is_distupgrade",
                   return_value=True):
            with pytest.raises(DistupgradeMeshRefusal):
                check_distupgrade_mesh("install", state_db, is_escape_hatch=True)

    def test_orphan_lock_ignored(self, redirect_mesh_paths):
        """PID dead → lock is orphan, treated as absent."""
        state_db, lock = redirect_mesh_paths
        _write_lock(lock, 12345)
        with patch("urpm.cli.helpers.distupgrade_mesh._pid_alive",
                   return_value=False):
            # No refusal — orphan lock is silently ignored (Stage 0
            # will clean it up next distupgrade run).
            check_distupgrade_mesh("upgrade", state_db)

    def test_pid_reuse_not_distupgrade_ignored(self, redirect_mesh_paths):
        """PID alive but cmdline is not urpm distupgrade → not our
        process, lock is stale (post-reboot PID reuse on non-tmpfs)."""
        state_db, lock = redirect_mesh_paths
        _write_lock(lock, 12345)
        with patch("urpm.cli.helpers.distupgrade_mesh._pid_alive",
                   return_value=True), \
             patch("urpm.cli.helpers.distupgrade_mesh._pid_is_distupgrade",
                   return_value=False):
            check_distupgrade_mesh("upgrade", state_db)


class TestLockFileEdgeCases:
    """PID-write in-flight window : `.state` may be empty or
    partially written before terminator ``\n``."""

    def test_empty_lock_file_treated_as_not_held(self, redirect_mesh_paths):
        state_db, lock = redirect_mesh_paths
        lock.write_bytes(b"")
        # Empty = writer hasn't finished — treat as not held.
        check_distupgrade_mesh("upgrade", state_db)

    def test_missing_terminator_treated_as_not_held(self, redirect_mesh_paths):
        state_db, lock = redirect_mesh_paths
        lock.write_bytes(b"12345")  # no \n
        check_distupgrade_mesh("upgrade", state_db)

    def test_corrupt_lock_content_treated_as_not_held(self,
                                                     redirect_mesh_paths):
        state_db, lock = redirect_mesh_paths
        lock.write_bytes(b"not-a-pid\n")
        check_distupgrade_mesh("upgrade", state_db)
