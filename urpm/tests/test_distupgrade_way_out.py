"""A failed distupgrade must say what to try next, not just what broke.

The migration runs as two transactions.  When the first fails, the
message ends with "run ``urpm recover`` once the underlying issue is
fixed" -- the operator has somewhere to go.

When the second failed, there was nothing.  The error was printed and
the command returned 1.  That is the wall one tester hit: Tx B failed on
a message they could not read, ``--resume`` then failed for an unrelated
reason, and with no third suggestion they reached for ``--abort``, which
rolled the media back and left the machine half-migrated.

Two handlers were in that state, not one.  Besides Tx B, the *retry* of
Tx A (``_resume_from_tx_a``) also printed its error and returned bare,
though the primary Tx A handler a few hundred lines below has pointed at
``urpm recover`` all along.

Both now reuse the wording that already existed rather than inventing a
variant: the same advice should read identically wherever the operator
meets it, and reusing the msgid means no untranslated string appears in
the one place a user lands when things have gone wrong.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pytest

from urpm.cli.commands.distupgrade import (
    _cmd_continue_after_execvp,
    _resume_from_tx_a,
)


@pytest.fixture
def db():
    from urpm.core.database import PackageDatabase
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = Path(handle.name)
    database = PackageDatabase(path)
    yield database
    database.close()
    path.unlink(missing_ok=True)


def _write_state(db, **extra) -> dict:
    from urpm.core.distupgrade.state import write_state
    state = {
        "stage": "tx_b_running",
        "version_from": "9",
        "version_to": "10",
        "tx_b_plan_ordered": ["foo-1.0-1.mga10.x86_64"],
        "nevra_to_path": {"foo-1.0-1.mga10.x86_64": "/cache/foo.rpm"},
        "erase_names": [],
    }
    state.update(extra)
    write_state(state, db)
    return state


@pytest.fixture
def failing_tx_b(monkeypatch):
    """Make Tx B fail the way it failed for the tester: out of space."""
    from urpm.core import distupgrade as core

    def boom(*args, **kwargs):
        raise core.Stage3Error("No space left on device")

    monkeypatch.setattr(core, "run_stage3_tx_b", boom)
    return boom


class TestTxBOffersAWayOut:

    def test_points_at_resume(self, db, failing_tx_b, capsys):
        _write_state(db)
        rc = _cmd_continue_after_execvp(argparse.Namespace(auto=False), db)
        out = capsys.readouterr().out
        assert rc == 1
        assert "--resume" in out, (
            "a Tx B failure with no suggested next step is what led one "
            "tester to --abort"
        )

    def test_still_reports_the_underlying_error(self, db, failing_tx_b,
                                                capsys):
        """Advice must not replace the diagnosis -- the operator needs
        both, and needs the cause first to know whether resuming is
        even sensible."""
        _write_state(db)
        _cmd_continue_after_execvp(argparse.Namespace(auto=False), db)
        out = capsys.readouterr().out
        assert "No space left on device" in out

    def test_state_is_preserved(self, db, failing_tx_b):
        """Suggesting --resume would be a lie if the plan were dropped
        on the way out."""
        from urpm.core.distupgrade.state import read_state
        _write_state(db)
        _cmd_continue_after_execvp(argparse.Namespace(auto=False), db)
        state = read_state(db)
        assert state is not None
        assert state.get("tx_b_plan_ordered")


class TestTxARetryOffersAWayOut:

    def test_points_at_recover(self, db, monkeypatch, capsys):
        from urpm.core import distupgrade as core

        def boom(*args, **kwargs):
            raise core.Stage3Error("scriptlet failed")

        monkeypatch.setattr(core, "run_stage3_tx_a", boom, raising=False)
        state = _write_state(
            db, stage="tx_a_committing",
            tx_a_plan_ordered=["rpm-4.20-1.mga10.x86_64"])
        rc = _resume_from_tx_a(db, state)
        out = capsys.readouterr().out
        assert rc == 1
        assert "recover" in out, (
            "the retry path was as silent as Tx B, though the "
            "first-attempt handler has pointed at urpm recover all along"
        )

    def test_still_reports_the_underlying_error(self, db, monkeypatch,
                                                capsys):
        from urpm.core import distupgrade as core

        def boom(*args, **kwargs):
            raise core.Stage3Error("scriptlet failed")

        monkeypatch.setattr(core, "run_stage3_tx_a", boom, raising=False)
        state = _write_state(
            db, stage="tx_a_committing",
            tx_a_plan_ordered=["rpm-4.20-1.mga10.x86_64"])
        _resume_from_tx_a(db, state)
        assert "scriptlet failed" in capsys.readouterr().out
