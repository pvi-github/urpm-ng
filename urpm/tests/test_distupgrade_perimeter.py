"""Tests for what « this distupgrade » covers, seen from Stage 4.

Tx B does not commit once.  On a real mga9 to mga10 it commits in 22
batches, each its own transaction, plus a retry pass for the packages
rpm silently failed to extract.  The state used to keep one
transaction id per side, overwritten at every batch, so Stage 4 only
ever saw the last one.

Measured on the test VM: transactions #15 to #37, urpmi installed in
#31, Stage 4 asking about #37.  The rule that keeps urpmi's media on
the installed release therefore never fired, and the scriptlet and
``.rpmnew`` reports had been dropping 21 batches out of 22 since well
before that.

The perimeter is now read from the history, which already tags these
transactions ``distupgrade``; the state only marks where the operation
begins.  Batching stops leaking into what Stage 4 has to know.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.state import (
    note_first_transaction, read_state, transaction_ids, write_state,
)
from urpm.core.operations import PackageOperations

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path):
    instance = PackageDatabase(db_path=tmp_path / "packages.db")
    yield instance
    instance.close()


class TestTheBoundary:
    """Where the operation starts, written once."""

    def test_it_is_written_by_the_first_transaction(self, db):
        write_state({"stage": "tx_a_committing"}, db)
        tx = db.begin_transaction("distupgrade", "tx-a")

        note_first_transaction(db, tx)

        assert read_state(db)["first_transaction_id"] == tx

    def test_a_later_transaction_does_not_move_it(self, db):
        write_state({"stage": "tx_a_committing"}, db)
        first = db.begin_transaction("distupgrade", "tx-a")
        note_first_transaction(db, first)

        for i in range(3):
            later = db.begin_transaction("distupgrade", f"batch {i}")
            note_first_transaction(db, later)

        # A resume reopens transactions with higher ids; the boundary
        # set on the original run has to hold or the earlier batches
        # drop out of the perimeter.
        assert read_state(db)["first_transaction_id"] == first


class TestThePerimeter:
    """Which transactions the operation is made of."""

    def test_it_spans_every_batch(self, db):
        write_state({"stage": "tx_b_running"}, db)
        ids = []
        for cmd in ("tx-a", "tx-b batch 1/3", "tx-b batch 2/3",
                    "tx-b batch 3/3", "tx-b retry"):
            tx = db.begin_transaction("distupgrade", cmd)
            note_first_transaction(db, tx)
            ids.append(tx)

        assert transaction_ids(db) == ids

    def test_an_earlier_distupgrade_is_left_out(self, db):
        old = db.begin_transaction("distupgrade", "last year's upgrade")
        write_state({"stage": "tx_a_committing"}, db)
        current = db.begin_transaction("distupgrade", "tx-a")
        note_first_transaction(db, current)

        got = transaction_ids(db)

        assert old not in got
        assert got == [current]

    def test_phase_a_is_left_out(self, db):
        # Phase A is a plain upgrade with its own perimeter and its own
        # rules, so it carries the ``upgrade`` action and does not
        # belong to the distupgrade's own accounting.
        write_state({"stage": "tx_a_committing"}, db)
        tx_a = db.begin_transaction("distupgrade", "tx-a")
        note_first_transaction(db, tx_a)
        phase_a = db.begin_transaction("upgrade", "urpm distupgrade phase A")

        assert phase_a not in transaction_ids(db)

    def test_nothing_without_a_distupgrade_under_way(self, db):
        db.begin_transaction("distupgrade", "orphan")

        assert transaction_ids(db) == []

    def test_nothing_when_the_boundary_is_absent(self, db):
        write_state({"stage": "tx_a_committing"}, db)
        db.begin_transaction("distupgrade", "tx-a")

        assert transaction_ids(db) == []


class TestTheRpmnewList:
    """Not in the history, so it still accumulates in the state."""

    def test_batches_add_up(self, db):
        from urpm.core.distupgrade.stage3 import _persist_tx_result

        write_state({"stage": "tx_b_running"}, db)
        _persist_tx_result(db, side="b", transaction_id=1,
                           rpmnew_files=["/etc/one.rpmnew"])
        _persist_tx_result(db, side="b", transaction_id=2,
                           rpmnew_files=["/etc/two.rpmnew"])

        assert read_state(db)["rpmnew_files_tx_b"] == [
            "/etc/one.rpmnew", "/etc/two.rpmnew"]

    def test_the_same_file_twice_is_listed_once(self, db):
        from urpm.core.distupgrade.stage3 import _persist_tx_result

        write_state({"stage": "tx_b_running"}, db)
        _persist_tx_result(db, side="b", transaction_id=1,
                           rpmnew_files=["/etc/same.rpmnew"])
        _persist_tx_result(db, side="b", transaction_id=2,
                           rpmnew_files=["/etc/same.rpmnew"])

        assert read_state(db)["rpmnew_files_tx_b"] == ["/etc/same.rpmnew"]

    def test_the_two_sides_stay_apart(self, db):
        from urpm.core.distupgrade.stage3 import _persist_tx_result

        write_state({"stage": "tx_b_running"}, db)
        _persist_tx_result(db, side="a", transaction_id=1,
                           rpmnew_files=["/etc/a.rpmnew"])
        _persist_tx_result(db, side="b", transaction_id=2,
                           rpmnew_files=["/etc/b.rpmnew"])

        state = read_state(db)
        assert state["rpmnew_files_tx_a"] == ["/etc/a.rpmnew"]
        assert state["rpmnew_files_tx_b"] == ["/etc/b.rpmnew"]


class TestBookkeepingNeverStopsTheUpgrade:
    """Both writes run between two committed batches."""

    def test_persisting_the_artefacts_swallows_a_failure(self, db,
                                                         monkeypatch):
        from urpm.core.distupgrade import stage3

        def explode(*args, **kwargs):
            raise RuntimeError("state unwritable")

        monkeypatch.setattr(stage3, "_persist_tx_result",
                            stage3._persist_tx_result)
        monkeypatch.setattr("urpm.core.distupgrade.state.write_state",
                            explode)

        # No exception: an upgrade must not stop because its report
        # lost a filename.
        stage3._persist_tx_result(db, side="b", transaction_id=1,
                                  rpmnew_files=["/etc/one.rpmnew"])

    def test_recording_the_plan_swallows_a_failure(self, db, monkeypatch):
        from urpm.core.distupgrade import stage3

        def explode(*args, **kwargs):
            raise RuntimeError("state unreadable")

        monkeypatch.setattr("urpm.core.distupgrade.state.read_state", explode)

        stage3._record_plan_rows(db, 1, ["foo-1-1.mga10.x86_64"], [])


class TestTheRulesSeeEveryBatch:
    """The failure measured on the VM, as a test."""

    @pytest.fixture
    def ops(self):
        self.by_id = {}
        instance = PackageOperations.__new__(PackageOperations)
        instance.db = SimpleNamespace(
            get_transaction=lambda tid: self.by_id.get(tid))
        instance.audit = None
        return instance

    def _rows(self, *names):
        return {"packages": [
            {"pkg_name": n, "pkg_nevra": f"{n}-1-1.mga10.x86_64",
             "action": "install", "status": "done"} for n in names]}

    def test_a_package_from_an_intermediate_batch_is_seen(self, ops,
                                                          monkeypatch):
        # urpmi landed in batch 16 of 22 on the test VM.  Asking about
        # the last batch alone is what made the rule miss it.
        self.by_id = {16: self._rows("urpmi"), 22: self._rows("zsh")}
        monkeypatch.setattr("urpm.core.rpmdb.provides_of",
                            lambda names, root="/": {n: set() for n in names})

        outcome = ops._operation_outcome([16, 22])

        # Both ends of the range, so a truncated union fails here
        # rather than passing because the one package looked at
        # happened to be in the batch that was kept.
        assert "urpmi" in outcome.provides
        assert "zsh" in outcome.provides

    def test_a_single_id_still_works(self, ops, monkeypatch):
        # Every other caller passes one transaction and must be
        # unaffected.
        self.by_id = {16: self._rows("urpmi")}
        monkeypatch.setattr("urpm.core.rpmdb.provides_of",
                            lambda names, root="/": {n: set() for n in names})

        assert "urpmi" in ops._operation_outcome(16).provides

    def test_one_failed_batch_marks_the_operation_incomplete(self, ops,
                                                             monkeypatch):
        # ``only-on-full-success`` reasons about the whole operation,
        # so a batch that lost a package has to weigh on the verdict
        # even when the last one went through.
        # The failure is in the *last* batch and the first went
        # through, so reading only the first would report a clean
        # operation.
        rows = self._rows("zsh")
        rows["packages"][0]["status"] = "failed"
        self.by_id = {16: self._rows("urpmi"), 22: rows}
        monkeypatch.setattr("urpm.core.rpmdb.provides_of",
                            lambda names, root="/": {n: set() for n in names})

        assert ops._operation_outcome([16, 22]).fully_successful is False


class TestPhaseAIsAnUpgradeLikeAnyOther:
    """Its own perimeter, its own rules, its own bookkeeping."""

    def _calls(self):
        tree = ast.parse(
            (REPO / "urpm/core/distupgrade/phase_a.py").read_text(
                encoding="utf-8"))
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (getattr(node.func, "attr", None)
                        or getattr(node.func, "id", None))
                if name:
                    found.setdefault(name, []).append(
                        [ast.unparse(a) for a in node.args])
        return found

    def test_it_records_its_scriptlets(self):
        # It waits for them with ``full_sync=True`` because Stage 1
        # needs a settled state, then used to throw the output away.
        assert "record_scriptlet_output" in self._calls()

    def test_it_records_its_verdicts(self):
        assert "record_action_outcomes" in self._calls()

    def test_it_evaluates_its_rules_as_an_upgrade(self):
        calls = self._calls()
        assert "run_post_operation_hooks" in calls
        named = {arg for args in calls["run_post_operation_hooks"]
                 for arg in args}
        assert "Operation.UPGRADE" in named


class TestNobodyReadsThePerSideFieldAnyMore:
    """A leftover reader would silently see one batch out of 22."""

    def test_the_field_is_gone_from_production_code(self):
        offenders = []
        for path in (REPO / "urpm").rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            if "tx_a_transaction_id" in text or "tx_b_transaction_id" in text:
                offenders.append(str(path.relative_to(REPO)))

        assert offenders == [], (
            "these still read a single transaction id per side: "
            + ", ".join(offenders))
