"""Tests for Phase C security (SPEC_DISTUPGRADE §3.C).

- TC.1 sanitize_scriptlet_output — 5 layers.
- TC.2 record_scriptlet_event — output sanitized before INSERT.
- TC.3 get_scriptlet_output — union of legacy + v34, sorted.
- TC.4 cmd_cleanup --legacy-scriptlets — atomic migration + drop.
- TC.5 SanitisingFilter — 4 fields mutated.
"""

from __future__ import annotations

import logging
from io import StringIO

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.security import (
    SanitisingFilter,
    install_sanitising_factory,
    install_sanitising_filter,
    sanitize_scriptlet_output,
    uninstall_sanitising_factory,
)


# ── TC.1 ─────────────────────────────────────────────────────────────


class TestSanitizeLayers:
    def test_none_becomes_empty_string(self):
        assert sanitize_scriptlet_output(None) == ""

    def test_bytes_coerced_via_str(self):
        assert sanitize_scriptlet_output(b"hello") == "b'hello'"

    def test_safe_ascii_unchanged(self):
        s = "hello world\n\tOK.\n"
        assert sanitize_scriptlet_output(s) == s

    def test_ascii_escape_stripped(self):
        # \x1b (ESC) begins ANSI escape sequences — must be stripped.
        s = "prompt\x1b]0;pwned\x07\x1b[2J"
        clean = sanitize_scriptlet_output(s)
        assert "\x1b" not in clean
        assert "\x07" not in clean

    def test_null_byte_stripped(self):
        assert "\x00" not in sanitize_scriptlet_output("a\x00b")

    def test_tab_and_newline_preserved(self):
        assert sanitize_scriptlet_output("a\tb\nc") == "a\tb\nc"

    def test_bidi_override_stripped(self):
        # U+202E RIGHT-TO-LEFT OVERRIDE — Trojan Source.
        s = "hello ‮evil"
        clean = sanitize_scriptlet_output(s)
        assert "‮" not in clean

    def test_zero_width_space_stripped(self):
        s = "hidden​text"
        clean = sanitize_scriptlet_output(s)
        assert "​" not in clean

    def test_unicode_tag_stripped(self):
        # U+E0041 = tag 'A' — invisible payload carrier.
        s = "hello\U000e0041there"
        assert "\U000e0041" not in sanitize_scriptlet_output(s)

    def test_variation_selector_stripped(self):
        s = "a️b"  # VS-16
        assert "️" not in sanitize_scriptlet_output(s)

    def test_combining_run_bounded(self):
        # Zalgo attack : 20 combining acutes stacked on 'e'.
        acute = "́"
        s = "e" + acute * 20
        clean = sanitize_scriptlet_output(s)
        # 1 base + up to 4 combining marks
        assert len(clean) <= 5
        assert clean.startswith("e")

    def test_short_combining_run_preserved(self):
        # 3 combining diacritics — legit.
        s = "ẹ́̈"
        assert sanitize_scriptlet_output(s) == s

    def test_idempotent(self):
        s = "\x1bhello‮world​"
        once = sanitize_scriptlet_output(s)
        twice = sanitize_scriptlet_output(once)
        assert once == twice


# ── TC.2 + TC.3 ─────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urpm.core.config.get_system_version", lambda root=None: "10")
    d = PackageDatabase(db_path=tmp_path / "packages.db")
    yield d
    d.close()


class TestRecordScriptletEvent:
    def test_output_sanitized_before_insert(self, db):
        tx = db.begin_transaction("install")
        db.record_scriptlet_event(
            tx, pkg_name="foo",
            script_type="post", status="failed",
            started_at=1000, finished_at=1001, exit_code=1,
            output="prompt\x1b]0;pwned\x07\x1b[2J",
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT output FROM history_scriptlets WHERE history_id = ?",
            (tx,),
        ).fetchone()
        assert "\x1b" not in row["output"]
        assert "\x07" not in row["output"]

    def test_none_output_stays_none(self, db):
        tx = db.begin_transaction("upgrade")
        db.record_scriptlet_event(
            tx, "foo", "pretrans", "started",
            started_at=1000,
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT output FROM history_scriptlets WHERE history_id = ?",
            (tx,),
        ).fetchone()
        assert row["output"] is None


class TestGetScriptletOutputUnion:
    def test_only_legacy_rows(self, db):
        tx = db.begin_transaction("install")
        db.record_scriptlet_output(tx, "foo", "old-output", is_error=False)
        db.record_scriptlet_output(tx, "bar", "old-err", is_error=True)
        db.conn.commit()
        rows = db.get_scriptlet_output(tx)
        assert [r["source"] for r in rows] == ["legacy", "legacy"]
        assert rows[0]["status"] == "ok"
        assert rows[1]["status"] == "failed"

    def test_v34_rows_come_first_by_started_at(self, db):
        tx = db.begin_transaction("install")
        db.record_scriptlet_output(tx, "z", "late-legacy", is_error=False)
        db.record_scriptlet_event(
            tx, "a", "post", "ok", started_at=100, finished_at=101,
            exit_code=0, output="clean")
        db.record_scriptlet_event(
            tx, "b", "posttrans", "ok", started_at=200, finished_at=201,
            exit_code=0, output="clean-b")
        db.conn.commit()
        rows = db.get_scriptlet_output(tx)
        # Legacy row (started_at=None → 0) precedes started_at=100.
        assert [r["source"] for r in rows] == ["legacy", "v34", "v34"]
        assert rows[1]["pkg_name"] == "a"
        assert rows[2]["pkg_name"] == "b"

    def test_v34_row_shape(self, db):
        tx = db.begin_transaction("install")
        db.record_scriptlet_event(
            tx, "foo", "post", "ok", started_at=100,
            finished_at=110, exit_code=0, output="all good")
        db.conn.commit()
        row = db.get_scriptlet_output(tx)[0]
        assert row == {
            "pkg_name": "foo",
            "script_type": "post",
            "status": "ok",
            "started_at": 100,
            "finished_at": 110,
            "exit_code": 0,
            "output": "all good",
            "source": "v34",
        }


# ── TC.4 ─────────────────────────────────────────────────────────────


class TestLegacyScriptletsMigration:
    def test_idempotent_after_drop(self, db):
        from urpm.cli.commands.build import _cleanup_legacy_scriptlets
        db.conn.execute("DROP TABLE history_scriptlet_output")
        db.conn.commit()
        assert _cleanup_legacy_scriptlets(db, dry_run=False) == 0

    def test_empty_source_drops_table(self, db):
        from urpm.cli.commands.build import _cleanup_legacy_scriptlets
        # Table exists (v34 schema keeps it) but empty.
        assert _cleanup_legacy_scriptlets(db, dry_run=False) == 0
        exists = db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='history_scriptlet_output'"
        ).fetchone()
        assert exists is None

    def test_dry_run_leaves_table(self, db):
        from urpm.cli.commands.build import _cleanup_legacy_scriptlets
        tx = db.begin_transaction("install")
        db.record_scriptlet_output(tx, "foo", "old", is_error=True)
        db.conn.commit()
        assert _cleanup_legacy_scriptlets(db, dry_run=True) == 0
        exists = db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='history_scriptlet_output'"
        ).fetchone()
        assert exists is not None
        # No rows moved.
        n = db.conn.execute(
            "SELECT COUNT(*) FROM history_scriptlets").fetchone()[0]
        assert n == 0

    def test_migration_sanitizes_output(self, db):
        from urpm.cli.commands.build import _cleanup_legacy_scriptlets
        tx = db.begin_transaction("install")
        db.record_scriptlet_output(tx, "foo",
                                   "prompt\x1b]0;pwn\x07",
                                   is_error=True)
        db.conn.commit()
        assert _cleanup_legacy_scriptlets(db, dry_run=False) == 0
        row = db.conn.execute(
            "SELECT status, output FROM history_scriptlets "
            "WHERE history_id = ?", (tx,)).fetchone()
        assert row["status"] == "failed"
        assert "\x1b" not in row["output"]

    def test_migration_drops_source_table(self, db):
        from urpm.cli.commands.build import _cleanup_legacy_scriptlets
        tx = db.begin_transaction("install")
        db.record_scriptlet_output(tx, "foo", "clean", is_error=False)
        db.conn.commit()
        _cleanup_legacy_scriptlets(db, dry_run=False)
        exists = db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='history_scriptlet_output'"
        ).fetchone()
        assert exists is None


# ── TC.5 ─────────────────────────────────────────────────────────────


@pytest.fixture
def logger_with_stream():
    """A logger + captured StringIO handler, torn down cleanly."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(
        "%(levelname)s %(message)s %(exc_text)s"))
    install_sanitising_filter(handler)
    logger = logging.getLogger("urpm.tests.phase_c")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


class TestSanitisingFilter:
    def test_msg_sanitized(self, logger_with_stream):
        logger, stream = logger_with_stream
        logger.error("prompt\x1b]0;pwn\x07 result")
        out = stream.getvalue()
        assert "\x1b" not in out
        assert "\x07" not in out

    def test_args_sanitized(self, logger_with_stream):
        logger, stream = logger_with_stream
        logger.error("pkg %s failed", "bad‮pkg")
        out = stream.getvalue()
        assert "‮" not in out

    def test_extra_pkg_stderr_sanitized(self):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(
            "%(levelname)s %(pkg_stderr)s"))
        install_sanitising_filter(handler)
        logger = logging.getLogger("urpm.tests.extra")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.error("boom", extra={"pkg_stderr": "\x1bhack"})
        assert "\x1b" not in stream.getvalue()
        logger.removeHandler(handler)

    def test_exc_info_traceback_sanitized(self, logger_with_stream):
        logger, stream = logger_with_stream
        try:
            raise ValueError("evil\x1btrace")
        except ValueError:
            logger.exception("caught")
        out = stream.getvalue()
        assert "\x1b" not in out

    def test_install_is_idempotent(self):
        handler = logging.StreamHandler(StringIO())
        install_sanitising_filter(handler)
        install_sanitising_filter(handler)
        n = sum(1 for f in handler.filters
                if isinstance(f, SanitisingFilter))
        assert n == 1


class TestLogRecordFactoryDefense:
    def test_factory_covers_orthogonal_logger(self):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        # Deliberately NOT installing the filter on the handler ; the
        # factory alone must scrub.
        handler.setFormatter(logging.Formatter("%(message)s"))
        install_sanitising_factory()
        try:
            logger = logging.getLogger("random.module")
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.error("dirty\x1bmsg")
            assert "\x1b" not in stream.getvalue()
            logger.removeHandler(handler)
        finally:
            uninstall_sanitising_factory()
