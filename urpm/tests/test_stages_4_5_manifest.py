"""Tests for the distupgrade manifest + Stages 4/5.

SPEC_DISTUPGRADE §6.1 (manifest), §4.4-4.5 (post-tx + post-boot).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.manifest import (
    BOOT_CRITICAL_LOCKS_BASE,
    BOOT_CRITICAL_LOCKS_UEFI,
    TRANSACTION_A_PROVIDES,
    boot_critical_locks,
    split_plan_for_tx_a_and_b,
)
from urpm.core.distupgrade.stages_4_5 import (
    clear_postboot_marker,
    read_postboot_marker,
    run_stage4,
    run_stage5_if_pending,
    write_postboot_marker,
)


# ── manifest ───────────────────────────────────────────────────────


class TestManifestLists:
    def test_tx_a_provides_includes_urpm_ng_core(self):
        assert "urpm-ng-core" in TRANSACTION_A_PROVIDES
        assert "rpm" in TRANSACTION_A_PROVIDES
        assert "python3" in TRANSACTION_A_PROVIDES
        assert "glibc" in TRANSACTION_A_PROVIDES

    def test_boot_locks_base_covers_bootloader(self):
        assert "grub2" in BOOT_CRITICAL_LOCKS_BASE
        assert "systemd" in BOOT_CRITICAL_LOCKS_BASE
        assert "sysvinit" in BOOT_CRITICAL_LOCKS_BASE
        assert "openrc" in BOOT_CRITICAL_LOCKS_BASE

    def test_boot_locks_uefi_adds_shim(self):
        assert "shim" in BOOT_CRITICAL_LOCKS_UEFI
        assert "grub2-efi" in BOOT_CRITICAL_LOCKS_UEFI


class TestBootCriticalLocks:
    def test_no_uefi_returns_base_only(self):
        result = boot_critical_locks(uefi_present=False)
        assert "grub2" in result
        assert "shim" not in result

    def test_uefi_present_merges(self):
        result = boot_critical_locks(uefi_present=True)
        assert "grub2" in result
        assert "shim" in result


class TestSplitPlanForTxAAndB:
    def test_name_based_split_without_pool(self):
        plan = [
            "rpm-4.20-1.mga11.x86_64",
            "urpm-ng-core-0.9.0-1.mga11.noarch",
            "firefox-120-1.mga11.x86_64",
            "libreoffice-24-1.mga11.x86_64",
            "glibc-2.42-1.mga11.x86_64",
        ]
        tx_a, tx_b = split_plan_for_tx_a_and_b(plan)
        assert set(tx_a) == {
            "rpm-4.20-1.mga11.x86_64",
            "urpm-ng-core-0.9.0-1.mga11.noarch",
            "glibc-2.42-1.mga11.x86_64",
        }
        assert set(tx_b) == {
            "firefox-120-1.mga11.x86_64",
            "libreoffice-24-1.mga11.x86_64",
        }

    def test_preserves_input_order_within_each_group(self):
        plan = [
            "glibc-2.42-1.mga11.x86_64",
            "firefox-120-1.mga11.x86_64",
            "rpm-4.20-1.mga11.x86_64",
        ]
        tx_a, tx_b = split_plan_for_tx_a_and_b(plan)
        assert tx_a == [
            "glibc-2.42-1.mga11.x86_64",
            "rpm-4.20-1.mga11.x86_64",
        ]
        assert tx_b == ["firefox-120-1.mga11.x86_64"]


# ── Post-boot marker ──────────────────────────────────────────────


class TestPostbootMarker:
    def test_write_read_clear_roundtrip(self, tmp_path):
        p = tmp_path / "postboot.pending"
        assert read_postboot_marker(path=p) == []

        write_postboot_marker(["/opt/urpm/post.sh"], path=p)
        assert p.exists()
        assert read_postboot_marker(path=p) == ["/opt/urpm/post.sh"]

        clear_postboot_marker(path=p)
        assert not p.exists()

    def test_empty_payload_still_writes_file(self, tmp_path):
        p = tmp_path / "postboot.pending"
        write_postboot_marker([], path=p)
        assert p.exists()  # signal « Stage 5 due »
        assert read_postboot_marker(path=p) == []


# ── Stage 4 ──────────────────────────────────────────────────────


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = PackageDatabase(db_path=tmp_path / "packages.db")
    yield db
    db.close()


class TestRunStage4:
    def test_aggregates_rpmnew_from_state_and_scriptlets_from_history(
            self, state_db, tmp_path):
        from urpm.core.distupgrade.state import write_state

        # Three real transactions in the history, as Tx A and two Tx B
        # batches would leave: Stage 4 finds them from the boundary
        # rather than from one id per side, which could only ever name
        # the last batch.
        tx_a = state_db.begin_transaction("distupgrade",
                                          "urpm distupgrade tx-a 10->11")
        batch_1 = state_db.begin_transaction(
            "distupgrade", "urpm distupgrade tx-b batch 1/2 10->11")
        batch_2 = state_db.begin_transaction(
            "distupgrade", "urpm distupgrade tx-b batch 2/2 10->11")

        write_state({
            "version_from": "10", "version_to": "11",
            "stage": "tx_b_running",
            "rpmnew_files_tx_a": ["/etc/foo.rpmnew"],
            "rpmnew_files_tx_b": ["/etc/bar.rpmnew"],
            "first_transaction_id": tx_a,
        }, state_db)

        # The failure sits in the *first* batch, not the last: that is
        # the case the single-id read used to drop on the floor.
        def fake_get_scriptlet(tx_id):
            if tx_id == batch_1:
                return [{"pkg_name": "foo",
                         "script_type": "pre",
                         "status": "failed",
                         "output": "boom"}]
            if tx_id == batch_2:
                return [{"pkg_name": "bar",
                         "script_type": "post",
                         "status": "ok",
                         "output": "ok"}]
            return []

        state_db.get_scriptlet_output = fake_get_scriptlet

        marker = tmp_path / "postboot.pending"
        summary = run_stage4(state_db, marker_path=marker)

        assert summary["rpmnew_files"] == [
            "/etc/foo.rpmnew", "/etc/bar.rpmnew"]
        # Only the failed row surfaces.
        assert len(summary["failed_scriptlets"]) == 1
        assert summary["failed_scriptlets"][0]["pkg_name"] == "foo"
        assert marker.exists()

        from urpm.core.distupgrade.state import read_state
        # Stage 4 clears .state on success so the distupgrade mesh
        # reopens between Stage 4 and reboot ; the postboot marker
        # file alone signals « Stage 5 pending ».
        assert read_state(state_db) is None

    def test_state_without_tx_ids_returns_empty(self, state_db,
                                                tmp_path):
        from urpm.core.distupgrade.state import write_state
        write_state({
            "version_from": "10", "version_to": "11",
            "stage": "tx_a_committing",
        }, state_db)
        marker = tmp_path / "postboot.pending"
        summary = run_stage4(state_db, marker_path=marker)
        assert summary["rpmnew_files"] == []
        assert summary["failed_scriptlets"] == []


class TestRunStage5IfPending:
    def test_absent_marker_is_noop(self, state_db, tmp_path):
        marker = tmp_path / "postboot.pending"
        assert run_stage5_if_pending(state_db, marker_path=marker) is False

    def test_runs_scripts_and_clears(self, state_db, tmp_path):
        from urpm.core.distupgrade.state import (
            read_state, write_state,
        )
        marker = tmp_path / "postboot.pending"
        marker.write_text("/opt/urpm/post.sh\n")
        write_state({
            "version_from": "10", "version_to": "11",
            "stage": "stage4_running",
        }, state_db)

        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            assert run_stage5_if_pending(state_db,
                                          marker_path=marker) is True

        assert not marker.exists()
        assert read_state(state_db) is None
