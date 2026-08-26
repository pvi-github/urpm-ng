"""Unit tests for :mod:`urpm.cli.helpers.orphans_triage`.

Every scenario scripts a stdin transcript of number keys (the TUI is
number-based across every menu — see the module docstring for the
rationale), runs the session, and inspects the resulting
:class:`TriageResult` plus a coarse check of the captured stdout when
it matters.

Shortcut cheat-sheet (mirror of the module) :

* **Welcome**  1=prev-release  2=sublib  3=userland  4=all  5=filter
              6=summary+apply  7=quit
* **Package**  1=keep  2=remove  3=skip  4=next  5=prev  6=batch
              7=filter  8=quit  9=details
* **Filter menu**  1=add  2=reset  3=back
* **Batch confirm**  1=yes  2=list first  3=no
* **Summary**  1=apply  2=back  3=cancel
* **Quit confirm**  1=confirm & quit  2=back
"""

from __future__ import annotations

import io
from typing import List

import pytest

from urpm.cli.helpers.orphans_triage import (
    DECISION_KEEP,
    DECISION_REMOVE,
    TriageResult,
    TriageSession,
)
from urpm.core.resolution.orphan_classify import OrphanInfo


# --- Helpers --------------------------------------------------------------


def _mk(
    name: str,
    evr: str = "1.0-1.mga10",
    arch: str = "x86_64",
    provides: list = None,
    size: int = 1024,
    install_time: int = 1_600_000_000,
    summary: str = "test package",
    group: str = "System/Base",
) -> OrphanInfo:
    return OrphanInfo(
        name=name,
        evr=evr,
        arch=arch,
        nevra=f"{name}-{evr}.{arch}",
        provides=provides or [name],
        size=size,
        install_time=install_time,
        summary=summary,
        group=group,
    )


def _script(*lines: str) -> io.StringIO:
    """Build a fake stdin transcript ; each element becomes one input line."""
    return io.StringIO("".join(l + "\n" for l in lines))


def _run(
    orphans: List[OrphanInfo],
    script: io.StringIO,
    *,
    current_major=10,
    initial_filters=None,
) -> tuple:
    stdout = io.StringIO()
    session = TriageSession(
        orphans=orphans,
        current_major=current_major,
        initial_filters=initial_filters,
        stdin=script,
        stdout=stdout,
        now_ts=1_700_000_000,
    )
    result = session.run()
    return result, stdout.getvalue()


# --- Basic flows ----------------------------------------------------------


class TestQuitPaths:
    def test_quit_from_welcome(self):
        # welcome: 7=quit (no pending decisions → immediate exit)
        result, _ = _run([_mk("a")], _script("7"))
        assert isinstance(result, TriageResult)
        assert result.to_remove == []
        assert result.to_keep == []

    def test_quit_with_pending_decisions_confirmed(self):
        # welcome 3=userland  → pkg 1=keep  → pkg 8=quit  → welcome 7=quit
        # → confirm 1=confirm and quit  (decisions kept because quit-confirm 1)
        result, _ = _run(
            [_mk("a")],
            _script("3", "1", "8", "7", "1"),
        )
        assert result.to_keep == ["a"]
        assert result.to_remove == []

    def test_quit_with_pending_decisions_cancelled_via_summary(self):
        # welcome 3=userland  → pkg 1=keep  → pkg 8=quit
        # → welcome 7=quit  → confirm 2=go back
        # → welcome 6=summary  → summary 3=cancel  (drops decisions)
        # → welcome 7=quit (nothing pending now → immediate exit)
        result, _ = _run(
            [_mk("a")],
            _script("3", "1", "8", "7", "2", "6", "3", "7"),
        )
        assert result.to_keep == []


class TestPerPackageActions:
    def test_keep_and_remove(self):
        orphans = [_mk("keeper"), _mk("removed"), _mk("skipped")]
        # welcome 4=all  →  1=keep  →  2=remove  →  3=skip  →  8=quit
        # → welcome 7=quit  → confirm 1
        result, _ = _run(
            orphans,
            _script("4", "1", "2", "3", "8", "7", "1"),
        )
        assert result.to_keep == ["keeper"]
        assert result.to_remove == ["removed"]

    def test_next_and_prev(self):
        orphans = [_mk("a"), _mk("b"), _mk("c")]
        # a → 4=next → b → 4=next → c → 5=prev → back to b → 2=remove
        # → 8=quit  → welcome 7=quit  → confirm 1
        result, _ = _run(
            orphans,
            _script("4", "4", "4", "5", "2", "8", "7", "1"),
        )
        assert result.to_remove == ["b"]
        assert result.to_keep == []

    def test_details_and_continue(self):
        # 4=all  → 9=details  → (enter)  → 1=keep  → 8=quit
        # → welcome 7=quit → confirm 1
        result, stdout = _run(
            [_mk("a", provides=["a", "config(a)", "libfoo.so.1"])],
            _script("4", "9", "", "1", "8", "7", "1"),
        )
        assert "Details" in stdout
        assert result.to_keep == ["a"]


class TestBatchAction:
    def test_batch_after_remove(self):
        orphans = [_mk(f"pkg{i}") for i in range(5)]
        # 4=all  → 2=remove first  → 6=batch  → confirm 1=yes  → all removed
        result, _ = _run(
            orphans,
            _script("4", "2", "6", "1"),
        )
        assert set(result.to_remove) == {f"pkg{i}" for i in range(5)}

    def test_batch_with_listing(self):
        orphans = [_mk("a"), _mk("b")]
        # 4=all  → 1=keep a  → 6=batch  → confirm 2=list  → 1=confirm
        result, stdout = _run(
            orphans,
            _script("4", "1", "6", "2", "1"),
        )
        assert "a-1.0-1.mga10.x86_64" in stdout
        assert set(result.to_keep) == {"a", "b"}

    def test_batch_declined(self):
        # 4=all → 2=remove a → 6=batch → confirm 3=no → 3=skip b → 8=quit
        # → welcome 7=quit → confirm 1 (a was decided before batch decline)
        result, _ = _run(
            [_mk("a"), _mk("b")],
            _script("4", "2", "6", "3", "3", "8", "7", "1"),
        )
        assert result.to_remove == ["a"]


class TestCategoriesAndFilters:
    def test_category_mga9_relic(self):
        orphans = [
            _mk("relic", evr="1.0-1.mga9"),
            _mk("modern", evr="1.0-1.mga10"),
        ]
        # 1=previous-release  → 2=remove  → 8=quit  → welcome 7=quit → 1
        result, _ = _run(
            orphans,
            _script("1", "2", "8", "7", "1"),
        )
        assert result.to_remove == ["relic"]

    def test_category_sublib(self):
        orphans = [
            _mk("lib64foo1",
                provides=["lib64foo1", "libfoo.so.1()(64bit)"]),
            _mk("mgatools", provides=["mgatools"]),
        ]
        # 2=sublib  → 2=remove  → 8=quit  → welcome 7=quit  → 1
        result, _ = _run(
            orphans,
            _script("2", "2", "8", "7", "1"),
        )
        assert result.to_remove == ["lib64foo1"]

    def test_initial_filter_applied(self):
        orphans = [
            _mk("mga9pkg", evr="1.0-1.mga9"),
            _mk("mga10pkg", evr="1.0-1.mga10"),
        ]
        # 4=all (with initial filter disttag=mga9 pre-applied)
        # → 2=remove  → 2=remove  → 8=quit  → welcome 7 → 1
        result, _ = _run(
            orphans,
            _script("4", "2", "2", "8", "7", "1"),
            initial_filters=["disttag=mga9"],
        )
        assert result.to_remove == ["mga9pkg"]

    def test_filter_menu_add_and_apply(self):
        orphans = [
            _mk("a", evr="1.0-1.mga9"),
            _mk("b", evr="1.0-1.mga10"),
        ]
        # welcome 5=filter  → filter 1=add  → criterion "disttag=mga9"
        # → filter 3=back  → welcome 4=all  → pkg 2=remove
        # → 8=quit  → welcome 6=summary  → 1=apply
        result, _ = _run(
            orphans,
            _script("5", "1", "disttag=mga9", "3",
                    "4", "2", "8", "6", "1"),
        )
        assert result.to_remove == ["a"]

    def test_filter_menu_reset(self):
        orphans = [_mk("a", evr="1.0-1.mga9")]
        # 5=filter  → 2=reset  (back to welcome auto)  → 7=quit
        result, _ = _run(
            orphans,
            _script("5", "2", "7"),
            initial_filters=["disttag=mga10"],
        )
        assert result.to_remove == []


class TestSummaryScreen:
    def test_summary_apply_yes(self):
        # 4=all  → 1=keep a  → 2=remove b  → 8=quit
        # → welcome 6=summary  → 1=apply
        result, stdout = _run(
            [_mk("a"), _mk("b")],
            _script("4", "1", "2", "8", "6", "1"),
        )
        assert "Triage summary" in stdout
        assert result.to_keep == ["a"]
        assert result.to_remove == ["b"]

    def test_summary_apply_no_wipes(self):
        # 4=all → 1=keep → 8=quit → welcome 6=summary → 3=cancel
        # → welcome 7=quit → nothing pending (wiped) → immediate exit
        result, _ = _run(
            [_mk("a")],
            _script("4", "1", "8", "6", "3", "7"),
        )
        assert result.to_keep == []

    def test_summary_edit_returns_to_welcome(self):
        # 4=all → 1=keep → 8=quit → welcome 6=summary → 2=back
        # → welcome 7=quit → confirm 1=confirm (decisions kept)
        result, _ = _run(
            [_mk("a"), _mk("b")],
            _script("4", "1", "8", "6", "2", "7", "1"),
        )
        assert result.to_keep == ["a"]


class TestEmptyOrPathological:
    def test_no_orphans(self):
        # welcome 7=quit
        result, _ = _run([], _script("7"))
        assert result.to_remove == []
        assert result.to_keep == []

    def test_eof(self):
        result, _ = _run([_mk("a")], io.StringIO(""))
        assert result.quit_reason == "eof"

    def test_unknown_choice_recovers(self):
        # invalid key "z" then 7=quit
        result, _ = _run(
            [_mk("a")],
            _script("z", "7"),
        )
        assert result.to_keep == []
