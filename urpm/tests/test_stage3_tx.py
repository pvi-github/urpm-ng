"""Tests for TS3 Stage 3 primitives (SPEC_DISTUPGRADE §4.3).

Stage 3 delegates the actual rpm work to
:meth:`PackageOperations.execute_install` — we mock ``PackageOperations``
at the boundary and assert wiring + state bookkeeping rather than
re-testing the transaction queue (already covered by its own suite).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.stage3 import (
    SmokeTestFailure,
    Stage3Error,
    bump_stage,
    execvp_to_continue,
    persist_tx_a_plan,
    persist_tx_b_plan,
    run_stage3_tx_a,
    run_stage3_tx_b,
    smoke_test_target_stack,
)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = PackageDatabase(db_path=tmp_path / "packages.db")
    yield db
    db.close()


# ── State bookkeeping ─────────────────────────────────────────────


class TestPersistPlans:
    def test_tx_a_plan_written(self, state_db):
        from urpm.core.distupgrade.state import read_state
        persist_tx_a_plan(
            state_db,
            ["foo-1-1.mga11.x86_64", "bar-2-1.mga11.x86_64"],
            version_from="10", version_to="11",
        )
        state = read_state(state_db)
        assert state["stage"] == "tx_a_committing"
        assert state["tx_a_plan_ordered"] == [
            "foo-1-1.mga11.x86_64", "bar-2-1.mga11.x86_64"]

    def test_tx_b_plan_written(self, state_db):
        from urpm.core.distupgrade.state import read_state, write_state
        write_state({"version_from": "10", "version_to": "11",
                     "stage": "tx_a_done"}, state_db)
        persist_tx_b_plan(
            state_db,
            ["baz-3-1.mga11.x86_64"],
            version_from="10", version_to="11",
        )
        state = read_state(state_db)
        assert state["stage"] == "tx_b_running"
        assert state["tx_b_plan_ordered"] == ["baz-3-1.mga11.x86_64"]

    def test_bump_stage_preserves_other_fields(self, state_db):
        from urpm.core.distupgrade.state import read_state, write_state
        write_state({
            "version_from": "10", "version_to": "11",
            "started_at": "2026-08-05T00:00:00Z",
            "stage": "tx_a_committing",
        }, state_db)
        bump_stage("tx_a_done", state_db)
        state = read_state(state_db)
        assert state["stage"] == "tx_a_done"
        assert state["started_at"] == "2026-08-05T00:00:00Z"


# ── Smoke test ────────────────────────────────────────────────────


class TestSmokeTest:
    def test_ok_on_success(self):
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="ok\n",
                                          stderr="")
            smoke_test_target_stack()  # must not raise

    def test_raises_on_non_zero(self):
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="",
                                          stderr="ImportError")
            with pytest.raises(SmokeTestFailure):
                smoke_test_target_stack()

    def test_raises_on_missing_ok_marker(self):
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0,
                                          stdout="silent\n", stderr="")
            with pytest.raises(SmokeTestFailure):
                smoke_test_target_stack()

    def test_raises_on_missing_python(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(SmokeTestFailure):
                smoke_test_target_stack()


# ── execvp ────────────────────────────────────────────────────────


class TestExecvp:
    def test_calls_execvp_with_pinned_path(self):
        with patch("os.execvp") as execvp:
            with pytest.raises(Stage3Error):
                execvp_to_continue()
        execvp.assert_called_once_with(
            "/usr/bin/urpm",
            ["urpm", "distupgrade", "--continue"],
        )


# ── Tx orchestrator ───────────────────────────────────────────────


def _fake_queue_result(*, success=True, errors=(),
                       rpmnew=(), scriptlet_json="{}",
                       script_errs=()):
    """A QueueResult-like object the stage3 wiring can consume."""
    op = SimpleNamespace(rpmnew_files=list(rpmnew))
    return SimpleNamespace(
        success=success,
        errors=list(errors),
        operations=[op],
        scriptlet_output=scriptlet_json,
        script_error_packages=set(script_errs),
    )


class TestRunStage3TxA:
    def test_happy_path_persists_state_and_calls_smoke(self, state_db):
        from urpm.core.distupgrade.state import read_state, write_state
        write_state({"version_from": "10", "version_to": "11"},
                    state_db)

        ops = MagicMock()
        ops.begin_transaction.return_value = 100
        ops.execute_install.return_value = _fake_queue_result(
            rpmnew=["/etc/foo.rpmnew"])
        smoke = MagicMock()

        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            run_stage3_tx_a(
                state_db,
                tx_a_plan=["foo-1-1.mga11.x86_64"],
                rpm_paths_by_nevra={
                    "foo-1-1.mga11.x86_64": "/cache/foo.rpm"},
                version_from="10", version_to="11",
                smoke=smoke,
            )

        ops.begin_transaction.assert_called_once()
        ops.execute_install.assert_called_once()
        # Force + nodeps applied per §4.3 étape 5.
        _, kwargs = ops.execute_install.call_args
        options = kwargs["options"]
        assert options.force is True
        assert options.nodeps is True
        assert kwargs["full_sync"] is True
        assert kwargs["rpm_paths"] == ["/cache/foo.rpm"]

        ops.record_scriptlet_output.assert_called_once_with(
            100, ops.execute_install.return_value)
        ops.complete_transaction.assert_called_once_with(100)
        smoke.assert_called_once()

        # rpmnew + transaction_id persisted for Stage 4.
        state = read_state(state_db)
        assert state["stage"] == "tx_a_done"
        assert state["tx_a_transaction_id"] == 100
        assert state["rpmnew_files_tx_a"] == ["/etc/foo.rpmnew"]

    def test_missing_rpm_path_raises(self, state_db):
        with pytest.raises(Stage3Error, match="cannot locate"):
            run_stage3_tx_a(
                state_db,
                tx_a_plan=["foo-1-1.mga11.x86_64"],
                rpm_paths_by_nevra={},
                version_from="10", version_to="11",
                smoke=lambda: None,
            )

    def test_execute_failure_aborts_transaction(self, state_db):
        ops = MagicMock()
        ops.begin_transaction.return_value = 100
        ops.execute_install.side_effect = RuntimeError("rpm boom")
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            with pytest.raises(Stage3Error, match="commit failed"):
                run_stage3_tx_a(
                    state_db,
                    tx_a_plan=["foo-1-1.mga11.x86_64"],
                    rpm_paths_by_nevra={
                        "foo-1-1.mga11.x86_64": "/cache/foo.rpm"},
                    version_from="10", version_to="11",
                    smoke=lambda: None,
                )
        ops.abort_transaction.assert_called_once_with(100)
        ops.complete_transaction.assert_not_called()

    def test_queue_result_unsuccessful_aborts(self, state_db):
        ops = MagicMock()
        ops.begin_transaction.return_value = 100
        ops.execute_install.return_value = _fake_queue_result(
            success=False, errors=["boom"])
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            with pytest.raises(Stage3Error, match="did not converge"):
                run_stage3_tx_a(
                    state_db,
                    tx_a_plan=["foo-1-1.mga11.x86_64"],
                    rpm_paths_by_nevra={
                        "foo-1-1.mga11.x86_64": "/cache/foo.rpm"},
                    version_from="10", version_to="11",
                    smoke=lambda: None,
                )
        ops.abort_transaction.assert_called_once_with(100)


class TestRunStage3TxB:
    def test_happy_path_persists_state(self, state_db, monkeypatch):
        from urpm.core.distupgrade.state import read_state
        from urpm.core.distupgrade import stage3

        ops = MagicMock()
        ops.begin_transaction.return_value = 200
        ops.execute_install.return_value = _fake_queue_result(
            rpmnew=["/etc/bar.rpmnew"])
        # Retry pass probes the rpmdb : pretend the planned NEVRA is
        # already installed so the retry short-circuits.  Testing the
        # retry path itself lives in TestRetryMissingInstalls below.
        canon = stage3._canonical_nevra("bar-2-1.mga11.x86_64")
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda root="/": {canon},
        )
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            run_stage3_tx_b(
                state_db,
                tx_b_plan=["bar-2-1.mga11.x86_64"],
                rpm_paths_by_nevra={
                    "bar-2-1.mga11.x86_64": "/cache/bar.rpm"},
                version_from="10", version_to="11",
            )

        ops.begin_transaction.assert_called_once()
        ops.complete_transaction.assert_called_once_with(200)

        state = read_state(state_db)
        assert state["stage"] == "transactions_done"
        assert state["tx_b_transaction_id"] == 200
        assert state["rpmnew_files_tx_b"] == ["/etc/bar.rpmnew"]


class TestCanonicalNevra:
    """Canonicalisation used by the Tx B retry pass to compare
    plan NEVRAs against rpmdb rows regardless of epoch presence."""

    def test_with_epoch(self):
        from urpm.core.distupgrade.stage3 import _canonical_nevra
        assert _canonical_nevra(
            "menu-messages-1:1-8.mga10.noarch") == \
            "menu-messages|1|1|8.mga10|noarch"

    def test_without_epoch_normalises_to_zero(self):
        from urpm.core.distupgrade.stage3 import _canonical_nevra
        assert _canonical_nevra(
            "menu-messages-1-8.mga10.noarch") == \
            "menu-messages|0|1|8.mga10|noarch"

    def test_epoch_vs_no_epoch_match_when_epoch_zero(self):
        from urpm.core.distupgrade.stage3 import _canonical_nevra
        a = _canonical_nevra("foo-0:2.7.0-1.mga10.x86_64")
        b = _canonical_nevra("foo-2.7.0-1.mga10.x86_64")
        assert a == b

    def test_malformed_returns_none(self):
        from urpm.core.distupgrade.stage3 import _canonical_nevra
        assert _canonical_nevra("not-a-nevra") is None


class TestRetryMissingInstalls:
    """Retry pass fires exactly when the rpmdb probe reveals that
    packages from ``tx_b_plan`` are absent post-commit."""

    def test_no_missing_no_retry(self, state_db, monkeypatch):
        from urpm.core.distupgrade import stage3
        ops = MagicMock()
        # All planned NEVRAs already in rpmdb → nothing to retry.
        canon = stage3._canonical_nevra("foo-1-1.mga11.x86_64")
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda root="/": {canon},
        )
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            result = stage3._retry_missing_installs(
                state_db,
                planned_nevras=["foo-1-1.mga11.x86_64"],
                rpm_paths_by_nevra={
                    "foo-1-1.mga11.x86_64": "/cache/foo.rpm"},
                version_from="10", version_to="11",
            )
        assert result["missing_before_retry"] == []
        assert result["retry_recovered"] == []
        ops.begin_transaction.assert_not_called()

    def test_missing_triggers_retry_and_recovers(self, state_db, monkeypatch):
        from urpm.core.distupgrade import stage3

        planned = ["foo-1-1.mga11.x86_64", "bar-2-1.mga11.x86_64"]
        # Before retry : bar is installed, foo is missing.  After
        # retry : both are installed (successful recovery).
        canon_foo = stage3._canonical_nevra("foo-1-1.mga11.x86_64")
        canon_bar = stage3._canonical_nevra("bar-2-1.mga11.x86_64")
        probe_calls = [0]

        def _probe(root="/"):
            probe_calls[0] += 1
            if probe_calls[0] == 1:
                return {canon_bar}
            return {canon_foo, canon_bar}

        monkeypatch.setattr(stage3, "_installed_nevras_canonical", _probe)

        ops = MagicMock()
        ops.begin_transaction.return_value = 999
        ops.execute_install.return_value = _fake_queue_result(rpmnew=[])
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            result = stage3._retry_missing_installs(
                state_db,
                planned_nevras=planned,
                rpm_paths_by_nevra={
                    "foo-1-1.mga11.x86_64": "/cache/foo.rpm",
                    "bar-2-1.mga11.x86_64": "/cache/bar.rpm",
                },
                version_from="10", version_to="11",
            )
        assert result["missing_before_retry"] == ["foo-1-1.mga11.x86_64"]
        assert result["retry_recovered"] == ["foo-1-1.mga11.x86_64"]
        assert result["still_missing"] == []
        ops.begin_transaction.assert_called_once()
        ops.execute_install.assert_called_once()
        # Only the missing package should be in the retry batch.
        args = ops.execute_install.call_args
        assert args.kwargs["rpm_paths"] == ["/cache/foo.rpm"]

    def test_missing_but_no_rpm_path_stays_missing(self, state_db, monkeypatch):
        from urpm.core.distupgrade import stage3
        # Probe reports SOME installed pkg (so probe worked) but our
        # planned foo is absent.  No path for foo in nevra_to_path so
        # nothing to retry with -- it stays in still_missing.
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda root="/": {stage3._canonical_nevra("other-1-1.mga11.x86_64")},
        )
        ops = MagicMock()
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            result = stage3._retry_missing_installs(
                state_db,
                planned_nevras=["foo-1-1.mga11.x86_64"],
                rpm_paths_by_nevra={},  # no path known
                version_from="10", version_to="11",
            )
        assert "foo-1-1.mga11.x86_64" in result["still_missing"]
        ops.begin_transaction.assert_not_called()


class TestSplitPlanBySize:
    """Batch slicing preserves topological order and respects size cap."""

    def test_empty_plan(self):
        from urpm.core.distupgrade.stage3 import _split_plan_by_size
        assert _split_plan_by_size([], {}, 200 * 1024 * 1024) == []

    def test_all_small_one_batch(self, tmp_path):
        from urpm.core.distupgrade.stage3 import _split_plan_by_size
        paths = {}
        plan = []
        for i in range(5):
            p = tmp_path / f"pkg{i}-1-1.mga10.x86_64.rpm"
            p.write_bytes(b"x" * (10 * 1024 * 1024))  # 10 MB each
            nevra = f"pkg{i}-1-1.mga10.x86_64"
            plan.append(nevra)
            paths[nevra] = str(p)
        batches = _split_plan_by_size(plan, paths, 200 * 1024 * 1024)
        assert len(batches) == 1
        assert batches[0] == plan

    def test_size_cap_splits(self, tmp_path):
        from urpm.core.distupgrade.stage3 import _split_plan_by_size
        paths = {}
        plan = []
        for i in range(5):
            p = tmp_path / f"pkg{i}-1-1.mga10.x86_64.rpm"
            p.write_bytes(b"x" * (60 * 1024 * 1024))  # 60 MB each
            nevra = f"pkg{i}-1-1.mga10.x86_64"
            plan.append(nevra)
            paths[nevra] = str(p)
        # 60 MB × 5 = 300 MB total, cap 200 MB → 3 batches (60+60+60=180, next
        # 60 makes 240 > 200 → new batch)
        batches = _split_plan_by_size(plan, paths, 200 * 1024 * 1024)
        assert len(batches) == 2
        # First batch : as many 60 MB pkgs as fit under 200 MB → 3
        assert batches[0] == plan[:3]
        assert batches[1] == plan[3:]

    def test_oversize_single_pkg_gets_own_batch(self, tmp_path):
        from urpm.core.distupgrade.stage3 import _split_plan_by_size
        big = tmp_path / "big-1-1.mga10.x86_64.rpm"
        big.write_bytes(b"x" * (300 * 1024 * 1024))  # 300 MB
        plan = ["big-1-1.mga10.x86_64"]
        batches = _split_plan_by_size(
            plan, {"big-1-1.mga10.x86_64": str(big)},
            200 * 1024 * 1024)
        assert batches == [plan]

    def test_missing_rpm_counts_as_zero(self, tmp_path):
        """A .rpm unlinked by earlier cleanup mid-Tx still fits its
        batch : size accounting treats missing files as 0 rather than
        raising."""
        from urpm.core.distupgrade.stage3 import _split_plan_by_size
        plan = ["missing-1-1.mga10.x86_64", "also-missing-1-1.mga10.noarch"]
        batches = _split_plan_by_size(
            plan,
            {"missing-1-1.mga10.x86_64": "/nonexistent/foo.rpm",
             "also-missing-1-1.mga10.noarch": "/nonexistent/bar.rpm"},
            200 * 1024 * 1024)
        assert batches == [plan]


class TestPurgeInstalledBatchRpms:
    def test_installed_get_unlinked_missing_stay(self, tmp_path, monkeypatch):
        from urpm.core.distupgrade import stage3
        installed_rpm = tmp_path / "installed-1-1.mga10.x86_64.rpm"
        installed_rpm.write_bytes(b"x" * 1024)
        failed_rpm = tmp_path / "failed-1-1.mga10.x86_64.rpm"
        failed_rpm.write_bytes(b"x" * 1024)
        canon_installed = stage3._canonical_nevra(
            "installed-1-1.mga10.x86_64")
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda root="/": {canon_installed},
        )
        freed_files, freed_bytes = stage3._purge_installed_batch_rpms(
            ["installed-1-1.mga10.x86_64", "failed-1-1.mga10.x86_64"],
            {"installed-1-1.mga10.x86_64": str(installed_rpm),
             "failed-1-1.mga10.x86_64": str(failed_rpm)},
        )
        assert freed_files == 1
        assert freed_bytes == 1024
        assert not installed_rpm.exists()
        assert failed_rpm.exists()

    def test_empty_installed_probe_no_purge(self, tmp_path, monkeypatch):
        from urpm.core.distupgrade import stage3
        rpm_file = tmp_path / "foo-1-1.mga10.x86_64.rpm"
        rpm_file.write_bytes(b"x")
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda root="/": set(),
        )
        freed_files, freed_bytes = stage3._purge_installed_batch_rpms(
            ["foo-1-1.mga10.x86_64"],
            {"foo-1-1.mga10.x86_64": str(rpm_file)},
        )
        assert freed_files == 0
        assert rpm_file.exists()
