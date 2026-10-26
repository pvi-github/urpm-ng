"""The trace of an action carried out after urpm is gone.

SPEC_POST_TRANSACTION_HOOKS §5.1.  A rule at the ``after-exit`` moment
acts once the urpm process has died, which is the only way to replace
urpm's own code or to touch the database it holds.  By then there is no
screen left to write to: the command has returned to the shell.

So the parent writes a waiting line before it goes, and whoever carries
the action out completes it.  The history is where it lands, next to
``history_packages`` and ``history_scriptlets``, because that is the
durable record an operator can go back to -- and because §7 asks for
every action taken as root to leave a trace.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file."""
    with tempfile.TemporaryDirectory(prefix="deferred_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            yield database
        finally:
            database.close()


@pytest.fixture
def transaction(db):
    """One recorded operation to hang the actions off."""
    return db.begin_transaction("upgrade", "urpm upgrade urpm-ng-core")


class TestTheWaitingLine:
    def test_it_reads_back_as_deferred(self, db, transaction):
        row_id = db.record_deferred_action(
            transaction, "db-schema", "migrate-db")

        rows = db.list_deferred_actions(transaction)
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert rows[0]["rule"] == "db-schema"
        assert rows[0]["action"] == "migrate-db"
        assert rows[0]["status"] == "deferred"
        assert rows[0]["finished_at"] is None

    def test_the_subject_travels_when_the_rule_has_one(self, db, transaction):
        db.record_deferred_action(transaction, "late-restart",
                                  "restart-service", subject="urpmd")

        assert db.list_deferred_actions(transaction)[0]["subject"] == "urpmd"

    def test_a_rule_without_a_subject_leaves_it_empty(self, db, transaction):
        db.record_deferred_action(transaction, "db-schema", "migrate-db")

        assert db.list_deferred_actions(transaction)[0]["subject"] is None

    def test_two_rules_both_get_their_line(self, db, transaction):
        db.record_deferred_action(transaction, "zulu", "migrate-db")
        db.record_deferred_action(transaction, "alpha", "restart-service",
                                  subject="urpmd")

        assert {r["rule"] for r in db.list_deferred_actions(transaction)} == {
            "zulu", "alpha"}


class TestCompletingIt:
    def test_a_success_is_recorded(self, db, transaction):
        row_id = db.record_deferred_action(
            transaction, "db-schema", "migrate-db")

        db.complete_deferred_action(row_id, "done", "schema v39 -> v40")

        row = db.list_deferred_actions(transaction)[0]
        assert row["status"] == "done"
        assert row["detail"] == "schema v39 -> v40"
        assert row["finished_at"] is not None

    def test_a_failure_keeps_its_reason(self, db, transaction):
        # The operator never saw this happen, so the reason is the only
        # thing they will have.
        row_id = db.record_deferred_action(
            transaction, "db-schema", "migrate-db")

        db.complete_deferred_action(row_id, "failed", "database is locked")

        row = db.list_deferred_actions(transaction)[0]
        assert row["status"] == "failed"
        assert row["detail"] == "database is locked"

    def test_completing_an_unknown_line_is_answered_not_raised(self, db):
        # The child may outlive a history the operator has pruned.
        assert db.complete_deferred_action(999999, "done") is False

    def test_completing_twice_is_harmless(self, db, transaction):
        row_id = db.record_deferred_action(
            transaction, "db-schema", "migrate-db")

        assert db.complete_deferred_action(row_id, "done")
        assert db.complete_deferred_action(row_id, "done")


class TestWhatTheNextCommandLooksFor:
    """A failure has to surface somewhere, since nobody was watching."""

    def test_the_failures_can_be_found_across_transactions(self, db):
        first = db.begin_transaction("upgrade")
        second = db.begin_transaction("install")
        ok = db.record_deferred_action(first, "a", "migrate-db")
        bad = db.record_deferred_action(second, "b", "migrate-db")
        db.complete_deferred_action(ok, "done")
        db.complete_deferred_action(bad, "failed", "locked")

        failed = db.list_deferred_actions(status="failed")

        assert [r["id"] for r in failed] == [bad]

    def test_a_line_still_waiting_is_not_a_failure(self, db, transaction):
        db.record_deferred_action(transaction, "db-schema", "migrate-db")

        assert db.list_deferred_actions(status="failed") == []

    def test_nothing_deferred_is_an_empty_list(self, db):
        assert db.list_deferred_actions() == []


class TestItBelongsToItsTransaction:
    def test_the_lines_go_when_the_transaction_goes(self, db, transaction):
        db.record_deferred_action(transaction, "db-schema", "migrate-db")

        with db._conn_write() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM history WHERE id = ?", (transaction,))
            conn.commit()

        assert db.list_deferred_actions(transaction) == []


class TestTheSchemaMoved:
    def test_a_v39_database_gains_the_table(self, tmp_path):
        """An operator's history survives the upgrade that adds it."""
        path = tmp_path / "packages.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE schema_info (version INTEGER);
            INSERT INTO schema_info (version) VALUES (39);
            CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT DEFAULT 'running'
            );
            INSERT INTO history (timestamp, action) VALUES (1, 'upgrade');
        """)
        conn.commit()
        conn.close()

        database = PackageDatabase(db_path=path)
        try:
            assert database.list_deferred_actions() == []
            row_id = database.record_deferred_action(1, "r", "migrate-db")
            assert database.list_deferred_actions(1)[0]["id"] == row_id
        finally:
            database.close()
