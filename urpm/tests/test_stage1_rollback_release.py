"""Refusing at Stage 2 has to leave the machine usable.

Stage 1 has already disabled the mga N media and enabled the mga N+1
ones by the time Stage 2 gets a plan to judge.  A refusal that stops
there — an empty plan, or a filesystem that cannot hold the upgrade —
must put them back, or the machine stays on mga N pointing at
catalogues for a release it is not running.

``release_distupgrade_lock`` takes the descriptor ``run_stage0``
acquired.  Called with no argument it raises ``TypeError``, and since
that call sits after the media restore and before the report, the
operator was told « Rollback failed » about a rollback that had in fact
succeeded — and was sent to run ``--abort`` on an already-clean state.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from urpm.cli.commands import distupgrade as cli


class TestTheLockIsReleasedWithItsDescriptor:

    def test_the_helper_takes_one(self):
        signature = inspect.signature(cli._undo_stage1_media_swap)
        assert "lock_fd" in signature.parameters, (
            "without it the release call cannot succeed"
        )

    def test_it_is_passed_through(self):
        released = {}
        with patch.object(cli, "_rollback_stage1", return_value=(1, 2, 3)), \
             patch("urpm.core.distupgrade.delete_state"), \
             patch("urpm.core.distupgrade.release_distupgrade_lock",
                   side_effect=lambda fd: released.setdefault("fd", fd)):
            cli._undo_stage1_media_swap(MagicMock(), 42)
        assert released["fd"] == 42

    def test_every_call_site_supplies_it(self):
        """Both refusal paths unwind through the same helper."""
        source = inspect.getsource(cli)
        calls = [line for line in source.splitlines()
                 if "_undo_stage1_media_swap(" in line
                 and "def " not in line]
        assert len(calls) == 2, f"unexpected call sites: {calls}"
        for line in calls:
            assert "lock_fd" in line, (
                f"call without a descriptor would raise TypeError: {line}"
            )


class TestTheReportSurvivesAFailure:
    """The caller is already printing a refusal; a traceback on top of
    it buries the actionable part."""

    def test_a_broken_rollback_is_reported_not_raised(self, capsys):
        with patch.object(cli, "_rollback_stage1",
                          side_effect=RuntimeError("db locked")):
            cli._undo_stage1_media_swap(MagicMock(), 7)
        out = capsys.readouterr().out
        assert "db locked" in out
        assert "--abort" in out

    def test_a_successful_rollback_says_so(self, capsys):
        """The counts are the operator's proof the media came back."""
        with patch.object(cli, "_rollback_stage1", return_value=(6, 1, 45)), \
             patch("urpm.core.distupgrade.delete_state"), \
             patch("urpm.core.distupgrade.release_distupgrade_lock"):
            cli._undo_stage1_media_swap(MagicMock(), 7)
        out = capsys.readouterr().out
        assert "45" in out
        assert "--abort" not in out, (
            "sending the operator to --abort on a clean state is what "
            "the TypeError used to do"
        )


class TestTheOrderIsWhatMakesItSafe:

    def test_the_media_are_restored_before_the_lock_is_dropped(self):
        """Read from the call sequence, not from the text : the local
        import mentions the release first and would make a substring
        check pass whatever the body actually does."""
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(cli._undo_stage1_media_swap)))
        called = [node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name)]
        assert (called.index("_rollback_stage1")
                < called.index("release_distupgrade_lock")), (
            f"dropping the lock first would let a second run start "
            f"against half-restored media; order was {called}"
        )
