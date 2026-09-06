"""``--abort`` must not silently wreck a half-migrated machine.

``_rollback_stage1`` puts the media database back exactly as it was
before the distupgrade — target media removed, source media restored —
and ``delete_state`` drops the Tx B plan, the NEVRA→path map and the
undo journal.  Neither touches the rpmdb.

Before Tx A that is a clean undo: nothing has been installed, so
rewinding the bookkeeping rewinds everything.  From ``tx_a_committing``
on it is the opposite — packages have moved, and the same two calls
leave a system whose halves disagree, pointing at the previous
release's repositories, with the plan needed to finish now deleted.

The command made no distinction and asked nothing.  A tester whose Tx B
failed on an error they could not read, then whose ``--resume`` failed
for an unrelated reason, reached for ``--abort`` as the only remaining
idea — and that is how the machine ended up unrecoverable.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pytest

from urpm.cli.commands.distupgrade import _ABORT_UNSAFE_STAGES, _cmd_abort


@pytest.fixture
def db():
    from urpm.core.database import PackageDatabase
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    database = PackageDatabase(path)
    yield database
    database.close()
    path.unlink(missing_ok=True)


def _at_stage(db, stage: str) -> None:
    from urpm.core.distupgrade.state import write_state
    write_state({"stage": stage, "version_from": "9", "version_to": "10"}, db)


def _args(**kw) -> argparse.Namespace:
    base = {"yes": False, "auto": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _state_exists(db) -> bool:
    from urpm.core.distupgrade.state import read_state
    return read_state(db) is not None


SAFE_STAGES = [
    "pre_check_done", "stage1_running", "media_swapped",
    "stage2_running", "downloaded",
]


class TestRefusesPastTxA:

    @pytest.mark.parametrize("stage", sorted(_ABORT_UNSAFE_STAGES))
    def test_refused_without_confirmation(self, db, stage, capsys):
        _at_stage(db, stage)
        assert _cmd_abort(db, _args()) == 1
        assert _state_exists(db), (
            "state must survive a refused abort — deleting it is what "
            "removes the way forward"
        )

    def test_message_names_the_stage_and_the_way_out(self, db, capsys):
        """A refusal that does not say what to do instead just moves the
        dead end one step later."""
        _at_stage(db, "tx_b_running")
        _cmd_abort(db, _args())
        out = capsys.readouterr().out
        assert "tx_b_running" in out
        assert "--resume" in out
        assert "--abort --yes" in out

    def test_explains_the_consequence(self, db, capsys):
        """The operator has to be able to tell this apart from an
        ordinary « are you sure »."""
        _at_stage(db, "tx_a_done")
        _cmd_abort(db, _args())
        out = capsys.readouterr().out.lower()
        assert "half-migrated" in out


class TestAllowedBeforeTxA:
    """Nothing is installed yet, so the rollback really is complete."""

    @pytest.mark.parametrize("stage", SAFE_STAGES)
    def test_proceeds_without_confirmation(self, db, stage):
        _at_stage(db, stage)
        assert _cmd_abort(db, _args()) == 0
        assert not _state_exists(db)

    def test_no_state_at_all_is_fine(self, db):
        """``--abort`` is safe to type blindly when nothing is running."""
        assert _cmd_abort(db, _args()) == 0


class TestForcedAbort:
    """The escape hatch stays, it just stops being the default."""

    def test_yes_forces_it_through(self, db):
        _at_stage(db, "tx_b_running")
        assert _cmd_abort(db, _args(yes=True)) == 0
        assert not _state_exists(db)

    def test_auto_forces_it_through(self, db):
        """``--auto`` is the same flag under another name."""
        _at_stage(db, "tx_b_running")
        assert _cmd_abort(db, _args(auto=True)) == 0

    def test_forced_abort_still_warns(self, db, capsys):
        """Going through deliberately should still leave a trace of what
        the machine now is."""
        _at_stage(db, "tx_b_running")
        _cmd_abort(db, _args(yes=True))
        assert "half-migrated" in capsys.readouterr().out.lower()

    def test_no_args_keeps_the_old_signature_working(self, db):
        """``_cmd_abort(db)`` is called without args elsewhere ; that
        path must not raise, and must apply the guard."""
        _at_stage(db, "tx_b_running")
        assert _cmd_abort(db) == 1
