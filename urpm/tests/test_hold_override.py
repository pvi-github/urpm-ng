"""Bypassing a hold is confirmed, never assumed.

Naming a held package on the command line overrides its hold: the
resolver leaves it out of the locks, the way urpmi's ``select.pm``
clears the skip flag for an explicit selection.  That override is the
only way a hold can be undone by a command that does not mention
holds at all, so it is confirmed first, and the recorded reason is
printed back.

Four situations are pinned here, plus the wiring: a question that is
never asked protects nothing, and every behavioural test below builds
its own call rather than going through the CLI.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from urpm.cli.helpers import hold as hold_helper
from urpm.cli.helpers.hold import confirm_hold_override, requested_holds

REPO = pathlib.Path(__file__).resolve().parents[2]


class FakeDB:
    """Just the two hold queries the helper uses."""

    def __init__(self, holds=None, broken=False):
        #: ``{name: reason}``
        self._holds = dict(holds or {})
        self._broken = broken

    def get_held_packages_set(self):
        if self._broken:
            raise RuntimeError("database is locked")
        return set(self._holds)

    def list_holds(self):
        if self._broken:
            raise RuntimeError("database is locked")
        return [{"package_name": n, "reason": r, "added_timestamp": 0}
                for n, r in sorted(self._holds.items())]


@pytest.fixture
def tty(monkeypatch):
    """A terminal is attached, so a question can be asked."""
    monkeypatch.setattr(hold_helper.sys.stdin, "isatty", lambda: True,
                        raising=False)


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(hold_helper.sys.stdin, "isatty", lambda: False,
                        raising=False)


def answer(monkeypatch, text):
    """Feed one answer to the prompt."""
    monkeypatch.setattr("builtins.input", lambda *_a: text)


class TestRequestedHolds:
    """Which of the names the user typed carry a hold."""

    def test_nothing_held_is_the_cheap_path(self):
        assert requested_holds(FakeDB(), ["firefox"]) == []

    def test_a_held_name_is_found_with_its_reason(self):
        db = FakeDB({"firefox": "waiting for an extension"})
        assert requested_holds(db, ["firefox"]) == [
            ("firefox", "waiting for an extension")]

    def test_an_unheld_name_is_not_reported(self):
        db = FakeDB({"firefox": ""})
        assert requested_holds(db, ["thunderbird"]) == []

    def test_a_nevra_is_recognised_as_its_package(self):
        """``urpm upgrade firefox-153.3.0-1.mga10.x86_64`` names firefox."""
        db = FakeDB({"firefox": "r"})
        got = requested_holds(db, ["firefox-153.3.0-1.mga10.x86_64"])
        assert [name for name, _r in got] == ["firefox"]

    def test_the_users_order_is_kept(self):
        db = FakeDB({"aaa": "", "zzz": ""})
        got = requested_holds(db, ["zzz", "aaa"])
        assert [name for name, _r in got] == ["zzz", "aaa"]

    def test_a_name_typed_twice_is_reported_once(self):
        db = FakeDB({"firefox": ""})
        assert len(requested_holds(db, ["firefox", "firefox"])) == 1

    def test_an_unreadable_database_reports_nothing(self):
        """A broken query costs the question, never the command."""
        assert requested_holds(FakeDB(broken=True), ["firefox"]) == []


class TestConfirmHoldOverride:
    """The four situations the question can be in."""

    def test_no_hold_asks_nothing(self, monkeypatch, capsys):
        def forbidden(*_a):
            raise AssertionError("asked a question with no hold in play")

        monkeypatch.setattr("builtins.input", forbidden)
        assert confirm_hold_override(FakeDB(), ["firefox"]) is True
        assert capsys.readouterr().out == ""

    def test_yes_goes_on(self, monkeypatch, tty, capsys):
        answer(monkeypatch, "y")
        db = FakeDB({"firefox": "waiting for an extension"})
        assert confirm_hold_override(db, ["firefox"]) is True
        out = capsys.readouterr().out
        assert "firefox" in out
        # The reason was recorded to be shown at exactly this moment.
        assert "waiting for an extension" in out

    def test_no_aborts(self, monkeypatch, tty, capsys):
        answer(monkeypatch, "n")
        db = FakeDB({"firefox": ""})
        assert confirm_hold_override(db, ["firefox"]) is False
        assert "Aborted." in capsys.readouterr().out

    def test_an_empty_answer_aborts(self, monkeypatch, tty):
        """The prompt reads [y/N]; the default has to be no."""
        answer(monkeypatch, "")
        assert confirm_hold_override(FakeDB({"firefox": ""}),
                                     ["firefox"]) is False

    def test_auto_is_a_yes_given_in_advance(self, monkeypatch, tty, capsys):
        def forbidden(*_a):
            raise AssertionError("asked a question although -y was given")

        monkeypatch.setattr("builtins.input", forbidden)
        db = FakeDB({"firefox": ""})
        assert confirm_hold_override(db, ["firefox"], auto=True) is True
        # Still said out loud, so -y does not hide what it decided.
        assert "firefox" in capsys.readouterr().out

    def test_without_a_terminal_the_answer_is_no(self, monkeypatch, no_tty,
                                                 capsys):
        """A script naming a held package has not been told about it."""
        def forbidden(*_a):
            raise AssertionError("prompted with no terminal attached")

        monkeypatch.setattr("builtins.input", forbidden)
        db = FakeDB({"firefox": ""})
        assert confirm_hold_override(db, ["firefox"]) is False
        out = capsys.readouterr().out
        assert "urpm hold remove" in out

    def test_an_interrupted_question_aborts(self, monkeypatch, tty):
        def interrupt(*_a):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", interrupt)
        assert confirm_hold_override(FakeDB({"firefox": ""}),
                                     ["firefox"]) is False

    def test_the_install_wording_says_install(self, monkeypatch, tty, capsys):
        prompts = []
        monkeypatch.setattr("builtins.input",
                            lambda p="": prompts.append(p) or "y")
        db = FakeDB({"firefox": ""})
        confirm_hold_override(db, ["firefox"], operation="install")
        assert "Install" in prompts[0]
        assert "Upgrade" not in prompts[0]

    def test_the_upgrade_wording_says_upgrade(self, monkeypatch, tty):
        prompts = []
        monkeypatch.setattr("builtins.input",
                            lambda p="": prompts.append(p) or "y")
        db = FakeDB({"firefox": ""})
        confirm_hold_override(db, ["firefox"], operation="upgrade")
        assert "Upgrade" in prompts[0]


class TestTheQuestionIsAsked:
    """Both commands that can bypass a hold have to call it.

    Every test above builds its own call, so all of them would keep
    passing with the two call sites deleted and the hold silently
    bypassed again.
    """

    SITES = {
        "urpm/cli/commands/upgrade.py": "cmd_upgrade",
        "urpm/cli/commands/install.py": "cmd_install",
    }

    @staticmethod
    def _function(relpath, name):
        tree = ast.parse((REPO / relpath).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found in {relpath}")

    @pytest.mark.parametrize("relpath,func", sorted(SITES.items()))
    def test_the_command_asks(self, relpath, func):
        body = self._function(relpath, func)
        calls = [c for c in ast.walk(body) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", None) == "confirm_hold_override"]
        assert calls, (
            f"{func} never calls confirm_hold_override, so naming a held "
            f"package bypasses the hold without a word")

    @pytest.mark.parametrize("relpath,func", sorted(SITES.items()))
    def test_a_refusal_stops_the_command(self, relpath, func):
        """The answer has to be read, negated, and returned on.

        Two ways to get this wrong, both of which leave the question on
        screen and look fine in a casual read. Calling it and ignoring
        the answer carries on regardless, which is worse than not
        asking. Dropping the ``not`` inverts the whole thing: the
        command would abort precisely when the user said yes. So the
        shape is pinned, not just the presence of the call.
        """
        body = self._function(relpath, func)

        def negated_call(expr):
            """``not confirm_hold_override(...)`` somewhere in *expr*."""
            return any(
                isinstance(node, ast.UnaryOp)
                and isinstance(node.op, ast.Not)
                and isinstance(node.operand, ast.Call)
                and getattr(node.operand.func, "id", None)
                == "confirm_hold_override"
                for node in ast.walk(expr))

        guarded = [
            node for node in ast.walk(body)
            if isinstance(node, ast.If)
            and negated_call(node.test)
            and any(isinstance(s, ast.Return) for s in node.body)
        ]
        assert guarded, (
            f"{func} must return when confirm_hold_override says no, "
            f"which means testing `not confirm_hold_override(...)`")
