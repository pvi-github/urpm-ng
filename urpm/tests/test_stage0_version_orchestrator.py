"""Tests for TS0.5 version detection + Stage 0 orchestrator.

SPEC_DISTUPGRADE §4.0 + §6.4.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from urpm.core.distupgrade.version import (
    ReleaseIdentity,
    VersionDetectionError,
    detect_target_release,
    parse_release_arg,
    read_current_release,
)


# ── version parsing ────────────────────────────────────────────────


class TestParseReleaseArg:
    def test_numeric(self):
        r = parse_release_arg("11")
        assert r == ReleaseIdentity(identity="11", numeric="11")

    def test_cauldron_bare(self):
        r = parse_release_arg("cauldron")
        assert r == ReleaseIdentity(identity="cauldron", numeric=None)

    def test_cauldron_colon(self):
        r = parse_release_arg("cauldron:12")
        assert r == ReleaseIdentity(identity="cauldron", numeric="12")

    def test_display_matches_input(self):
        assert parse_release_arg("11").display() == "11"
        assert parse_release_arg("cauldron").display() == "cauldron"
        assert parse_release_arg("cauldron:12").display() == "cauldron:12"

    def test_uppercase_normalized(self):
        assert parse_release_arg("Cauldron").identity == "cauldron"

    def test_empty_raises(self):
        with pytest.raises(VersionDetectionError):
            parse_release_arg("")

    def test_missing_numeric_raises(self):
        with pytest.raises(VersionDetectionError):
            parse_release_arg("cauldron:")

    def test_non_numeric_after_colon_raises(self):
        with pytest.raises(VersionDetectionError):
            parse_release_arg("cauldron:beta")


class TestReadCurrentRelease:
    def test_delegates_to_get_system_version(self, monkeypatch):
        """Thin wrapper — behaviour test lives on get_system_version."""
        monkeypatch.setattr(
            "urpm.core.config.get_system_version", lambda: "10")
        assert read_current_release() == "10"

    def test_returns_none_when_system_version_absent(self, monkeypatch):
        monkeypatch.setattr(
            "urpm.core.config.get_system_version", lambda: None)
        assert read_current_release() is None


class TestDetectTargetRelease:
    def test_user_supplied_wins(self):
        r = detect_target_release(current="10", user_supplied="11")
        assert r.identity == "11"

    def test_user_supplied_same_as_current_refused(self):
        with pytest.raises(VersionDetectionError, match="already"):
            detect_target_release(current="10", user_supplied="10")

    def test_numeric_current_auto_bumps(self):
        # N → N+1 heuristic : covers the standard upgrade window.
        assert detect_target_release(current="9").identity == "10"
        assert detect_target_release(current="10").identity == "11"

    def test_cauldron_current_refuses_auto(self):
        with pytest.raises(VersionDetectionError, match="rolling"):
            detect_target_release(current="cauldron")

    def test_no_current_refuses_auto(self):
        with pytest.raises(VersionDetectionError,
                           match="could not read"):
            detect_target_release(current=None)


# ── orchestrator ───────────────────────────────────────────────────


class TestRunStage0:
    def _mock_env(self, monkeypatch, tmp_path):
        """Redirect state + stub subsystems used by Stage 0."""
        from urpm.core.database import PackageDatabase
        from urpm.core.distupgrade import lock as lock_mod

        state_db = PackageDatabase(db_path=tmp_path / "packages.db")

        monkeypatch.setattr(
            lock_mod, "_audit_parent", lambda _fd: None)

        # Point mageia-release to a real file so clock gate passes
        release = tmp_path / "mageia-release"
        release.write_text("Mageia release 10 (Official) for x86_64\n")
        monkeypatch.setattr(
            "urpm.core.distupgrade.stage0.MAGEIA_RELEASE_PATH",
            release)
        monkeypatch.setattr(
            "urpm.core.distupgrade.orchestrator.read_current_release",
            lambda: "10")

        # Stub the mutant phase
        monkeypatch.setattr(
            "urpm.core.distupgrade.orchestrator.run_phase_a_refresh",
            lambda db: [])
        return state_db, tmp_path

    def test_happy_path_dry_run(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from urpm.core.distupgrade.orchestrator import run_stage0
        from urpm.core.distupgrade.lock import (
            acquire_distupgrade_lock, release_distupgrade_lock,
        )
        from urpm.core.distupgrade.state import read_state
        state_db, root = self._mock_env(monkeypatch, tmp_path)
        # Redirect lock dir too
        lock_dir = root / "run" / "urpm"
        lock_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "urpm.core.distupgrade.orchestrator.acquire_distupgrade_lock",
            lambda: acquire_distupgrade_lock(dir_path=lock_dir))

        result = run_stage0(
            state_db,
            user_supplied_target="11",
            skip_phase_a_upgrade=True,
        )
        assert result.current == "10"
        assert result.target.identity == "11"

        state = read_state(state_db)
        assert state["stage"] == "pre_check_done"
        assert state["version_to"] == "11"

        release_distupgrade_lock(result.lock_fd, dir_path=lock_dir)

    def test_clock_gate_aborts(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from urpm.core.distupgrade.orchestrator import (
            Stage0Error, run_stage0,
        )
        release = tmp_path / "mageia-release"
        release.write_text("Mageia release 10\n")
        import os
        future = 3_000_000_000  # ~2065
        os.utime(release, (future, future))
        monkeypatch.setattr(
            "urpm.core.distupgrade.stage0.MAGEIA_RELEASE_PATH",
            release)
        with pytest.raises(Stage0Error, match="chrony"):
            run_stage0(MagicMock(), user_supplied_target="11",
                       skip_phase_a_upgrade=True)

    def test_missing_target_auto_bumps_from_current(self, tmp_path,
                                                    monkeypatch):
        """Without ``--to``, run_stage0 auto-detects target = N+1
        from the current release identity."""
        state_db, root = self._mock_env(monkeypatch, tmp_path)
        lock_dir = root / "run" / "urpm"
        lock_dir.mkdir(parents=True)
        from urpm.core.distupgrade.lock import (
            acquire_distupgrade_lock,
        )
        monkeypatch.setattr(
            "urpm.core.distupgrade.orchestrator.acquire_distupgrade_lock",
            lambda: acquire_distupgrade_lock(dir_path=lock_dir))
        from urpm.core.distupgrade.orchestrator import run_stage0
        result = run_stage0(
            state_db,
            user_supplied_target=None,   # auto-detect
            skip_phase_a_upgrade=True,
        )
        assert result.current == "10"
        assert result.target.identity == "11"
