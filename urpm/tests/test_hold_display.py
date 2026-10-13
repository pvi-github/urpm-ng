"""Tests for the hold block of the skipped-jobs report.

What a hold blocked is the one line in a partial-transaction report the
user can act on without further investigation, and it is their own
earlier decision coming back at them.  libsolv's own sentence never
names the hold (it is phrased around the package whose upgrade was
asked for), so the grouping and the wording here are the only place
the connection is made.
"""

import pytest

from urpm.cli.display import _group_by_hold, print_skipped_jobs
from urpm.core.resolver import SkippedJob


def skipped(name, blocked_by=(), reason="because"):
    """One dropped job, as the resolver would hand it over."""
    return SkippedJob(name=name, reason=reason, kind="implicit_upgrade",
                      request_id=f"upg:{name}", blocked_by=tuple(blocked_by))


class TestGroupByHold:
    """The grouping, keyed by the holds responsible."""

    def test_entries_without_a_hold_are_left_out(self):
        rows = [skipped("app"), skipped("mid")]
        assert _group_by_hold(rows) == []

    def test_one_hold_gathers_everything_it_blocked(self):
        rows = [skipped("app", ["lib"]), skipped("mid", ["lib"])]
        assert _group_by_hold(rows) == [(("lib",), ["app", "mid"])]

    def test_two_holds_stay_apart(self):
        rows = [skipped("app", ["lib"]), skipped("tool", ["other"])]
        assert _group_by_hold(rows) == [
            (("lib",), ["app"]),
            (("other",), ["tool"]),
        ]

    def test_a_pair_of_holds_is_its_own_group(self):
        """Two holds blocking the same package is a third key, not two."""
        rows = [skipped("app", ["lib", "other"]), skipped("mid", ["lib"])]
        assert _group_by_hold(rows) == [
            (("lib", "other"), ["app"]),
            (("lib",), ["mid"]),
        ]

    def test_duplicates_collapse(self):
        rows = [skipped("app", ["lib"]), skipped("app", ["lib"])]
        assert _group_by_hold(rows) == [(("lib",), ["app"])]

    def test_an_object_without_the_field_is_tolerated(self):
        """A caller holding an older SkippedJob must not crash the report."""
        class Old:
            name = "app"
            evr = ""
            reason = ""
            kind = ""

        assert _group_by_hold([Old()]) == []


class TestPrintSkippedJobs:
    """The rendered block."""

    def test_nothing_skipped_prints_nothing(self, capsys):
        print_skipped_jobs([])
        assert capsys.readouterr().out == ""

    def test_the_hold_sentence_names_the_hold_and_its_victims(self, capsys):
        print_skipped_jobs([skipped("app", ["lib"]), skipped("mid", ["lib"])])
        out = capsys.readouterr().out
        assert "The hold on lib blocks the upgrade of app, mid." in out

    def test_the_install_wording_says_installation(self, capsys):
        print_skipped_jobs([skipped("app", ["lib"])], operation="install")
        out = capsys.readouterr().out
        assert "The hold on lib blocks the installation of app." in out
        assert "upgrade of" not in out

    def test_two_holds_take_the_plural(self, capsys):
        print_skipped_jobs([skipped("app", ["lib", "other"])])
        out = capsys.readouterr().out
        assert "The holds on lib, other block the upgrade of app." in out

    def test_the_release_hint_uses_the_current_grammar(self, capsys):
        """``unhold`` is no longer the way in; the hint says what is."""
        print_skipped_jobs([skipped("app", ["lib"])])
        out = capsys.readouterr().out
        assert "urpm hold remove <package>" in out
        assert "urpm unhold" not in out

    def test_each_entry_carries_its_hold_inline(self, capsys):
        print_skipped_jobs([skipped("app", ["lib"])])
        out = capsys.readouterr().out
        assert "[hold on lib]" in out

    def test_an_entry_no_hold_blocked_gets_no_hold_wording(self, capsys):
        print_skipped_jobs([skipped("app")])
        out = capsys.readouterr().out
        assert "app" in out
        assert "hold" not in out

    def test_the_reason_is_still_printed(self, capsys):
        print_skipped_jobs([skipped("app", ["lib"], reason="nothing provides x")])
        out = capsys.readouterr().out
        assert "nothing provides x" in out
