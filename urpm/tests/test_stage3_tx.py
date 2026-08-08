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
    def test_happy_path_persists_state(self, state_db):
        from urpm.core.distupgrade.state import read_state
        ops = MagicMock()
        ops.begin_transaction.return_value = 200
        ops.execute_install.return_value = _fake_queue_result(
            rpmnew=["/etc/bar.rpmnew"])
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
