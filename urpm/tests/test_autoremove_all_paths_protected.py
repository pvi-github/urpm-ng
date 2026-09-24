"""Three paths remove orphans; the protections covered one of them.

A tester's interactive triage kept 40 packages and queued 123, among
them the three pcre libraries that `pcre`, one of the 40, requires
exactly.  rpm refused the transaction after the confirmation:

    pcre-8.45-5.mga10 requires libpcreposix.so.1()(64bit),
                      which this operation would remove

The rescue added for `cmd_autoremove` was wired into that one function
only.  Worse, an audit of every ``add_erase`` site showed the blacklist
and the redlist were read in exactly one place too, so
``--interactive`` and ``cleandeps`` were free to offer a package the
classic path refuses to touch: a list meant to keep a system bootable
did not apply to the flow most likely to be used right after a
distupgrade, which is when the orphans appear.

These pin the shared helpers both paths now go through.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from urpm.cli.commands import cleanup


@pytest.fixture
def lists(monkeypatch):
    monkeypatch.setattr(cleanup, "_get_blacklist", lambda: {"glibc", "rpm"})
    monkeypatch.setattr(cleanup, "_get_redlist", lambda: {"dhcp-client"})


class TestTheProtectionListsApplyEverywhere:
    """``_split_by_protection`` is the single reading of both lists."""

    def test_a_blacklisted_package_is_held_back(self, lists):
        blocked, warned, safe = cleanup._split_by_protection(
            ["glibc", "lib64foo1", "dhcp-client"])
        assert blocked == ["glibc"]

    def test_a_redlisted_package_is_set_aside_for_a_question(self, lists):
        blocked, warned, safe = cleanup._split_by_protection(
            ["glibc", "lib64foo1", "dhcp-client"])
        assert warned == ["dhcp-client"]

    def test_everything_else_goes_through(self, lists):
        blocked, warned, safe = cleanup._split_by_protection(
            ["glibc", "lib64foo1", "dhcp-client"])
        assert safe == ["lib64foo1"]

    def test_the_input_order_is_kept(self, lists):
        """The caller prints these lists; alphabetical reordering would
        make a long one harder to check against the screen above."""
        _b, _w, safe = cleanup._split_by_protection(["zlib1", "alib1"])
        assert safe == ["zlib1", "alib1"]


class TestTheRescueIsReportedBeforeTheTransaction:

    def _resolver(self, rescued):
        r = MagicMock()
        r.needed_by_kept.return_value = set(rescued)
        return r

    def test_what_a_survivor_needs_leaves_the_removal_list(self, capsys):
        kept = cleanup._report_rescued(
            self._resolver({"lib64pcreposix1"}),
            ["lib64pcreposix1", "lib64unrelated1"],
            {"pcre"},
            cleanup_colors())
        assert kept == ["lib64unrelated1"]

    def test_the_operator_is_told_before_confirming(self, capsys):
        """The whole point: this reaches the screen at planning time,
        not as an rpm rejection after the operator said yes."""
        cleanup._report_rescued(
            self._resolver({"lib64pcreposix1"}),
            ["lib64pcreposix1"], {"pcre"}, cleanup_colors())
        assert "lib64pcreposix1" in capsys.readouterr().out

    def test_nothing_rescued_says_nothing(self, capsys):
        kept = cleanup._report_rescued(
            self._resolver(set()), ["lib64foo1"], {"bar"}, cleanup_colors())
        assert kept == ["lib64foo1"]
        assert capsys.readouterr().out == ""


def cleanup_colors():
    from urpm.cli import colors
    return colors


class TestTheFailureReportIsNotTruncated:
    """36 unsatisfied dependencies cut to three is a puzzle, not an
    answer: each line names a package that has to be kept."""

    def test_limit_zero_prints_every_line(self, capsys):
        from urpm.cli.helpers.failure_report import print_errors
        errors = [f"pkg{i} requires libfoo{i}.so.1()(64bit)"
                  for i in range(36)]
        withheld = print_errors(errors, limit=0)
        out = capsys.readouterr().out
        assert withheld == 0
        assert "and" not in out.split("pkg35")[-1]
        for i in range(36):
            assert f"pkg{i} " in out

    def test_a_positive_limit_still_truncates(self, capsys):
        from urpm.cli.helpers.failure_report import print_errors
        assert print_errors(["a", "b", "c", "d"], limit=2) == 2

    def test_the_removal_paths_ask_for_everything(self):
        """Source-level: the erase call sites must not reintroduce a
        cap, which is how this regressed in the first place."""
        import pathlib
        import re
        root = pathlib.Path(cleanup.__file__).resolve().parent
        for name in ("cleanup.py", "remove.py"):
            src = (root / name).read_text(encoding="utf-8")
            capped = re.findall(
                r"print_errors\(\w+\.collect_errors\(\), limit=([1-9]\d*)",
                src)
            assert not capped, f"{name}: capped at {capped}"


class TestEveryRemovalPathIsWired:
    """The ratchet for this whole round.

    The rescue was written once and wired into one of the three
    functions that remove orphans; the protection lists were read in
    one of them too.  Nothing said so, so the gap was found by a
    tester's failed transaction rather than by the suite.  This asserts
    the wiring itself, per function, so a fourth path cannot be added
    without it and an existing one cannot quietly lose it.
    """

    #: function name -> helpers it must call
    EXPECTED = {
        "cmd_autoremove": {"_split_by_protection", "_report_rescued"},
        "_cmd_autoremove_interactive": {"_split_by_protection",
                                        "_report_rescued"},
        "cmd_cleandeps": {"_split_by_protection", "_report_rescued"},
    }

    @staticmethod
    def _calls_in(func_node):
        import ast
        names = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names.add(node.func.id)
        return names

    def _functions(self):
        import ast
        import pathlib
        src = pathlib.Path(cleanup.__file__).resolve().with_suffix(".py")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        return {n.name: n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)}

    def test_the_three_removal_paths_exist_under_these_names(self):
        """Guards the test itself: a rename must not silently turn the
        checks below into no-ops."""
        found = self._functions()
        missing = [n for n in self.EXPECTED if n not in found]
        assert not missing, f"renamed or gone: {missing}"

    @pytest.mark.parametrize("func_name", sorted(EXPECTED))
    def test_the_path_applies_both_helpers(self, func_name):
        funcs = self._functions()
        if func_name not in funcs:
            pytest.skip(f"{func_name} not found; covered by the test above")
        calls = self._calls_in(funcs[func_name])
        missing = self.EXPECTED[func_name] - calls
        assert not missing, (
            f"{func_name} does not call {sorted(missing)}: a removal path "
            "without the protection lists can offer a blacklisted package, "
            "and one without the rescue hands rpm a transaction it will "
            "refuse after the operator confirmed it")
