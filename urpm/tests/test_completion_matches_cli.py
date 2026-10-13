"""The completion has to describe the CLI that exists.

Two ways it drifts, and both were live at once: ``urpm cache flush``
was added and never offered, while ``rebuild-fts`` was offered and had
no parser behind it.  Neither shows up in normal use — a missing verb
just fails to complete, an extra one completes into an error — so
nothing catches them but a test that reads both sides.

Only the verb lists are compared.  Option flags drift too, but they
are far more numerous and far less costly to get wrong: a flag that
fails to complete is a nuisance, a subcommand that does not exist is a
lie about what the tool can do.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from urpm.cli.main import create_parser

COMPLETION = Path(__file__).resolve().parents[2] / "completion" / "urpm.bash"


def _completion_verbs(variable: str) -> set:
    """Words listed in a ``local <variable>="..."`` assignment.

    Line continuations are joined first : several of these lists span
    three or four lines, and reading only the first would silently
    compare against a fragment.
    """
    text = COMPLETION.read_text().replace("\\\n", " ")
    match = re.search(rf'{variable}="([^"]+)"', text)
    assert match, f"{variable} not found in {COMPLETION}"
    return set(match.group(1).split())


def _parser_verbs(*path: str) -> set:
    """Subcommands argparse really accepts under *path*.

    ``path`` walks the subparser tree, so ``("cache",)`` yields what
    ``urpm cache`` takes.
    """
    parser = create_parser()
    for name in path:
        action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
        parser = action.choices[name]
    action = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
    return set(action.choices)


class TestCacheVerbs:
    """The list this session touched, and the one that was already
    wrong when it was touched."""

    def test_flush_is_among_them(self):
        """The one that went missing, named so a regression says why."""
        assert "flush" in _completion_verbs("cache_subcmds")
        assert "flush" in _parser_verbs("cache")


#: ``(completion variable, path through the parser tree)`` for every
#: verb list the completion keeps.  The top level is in here because
#: that is where the drift was worst and least visible: three real
#: verbs (``audit``, ``genmedia``, ``system``) were never offered, and
#: ``up`` was offered although ``update`` never declared it as an
#: alias, so completing it led straight to a usage error.
VERB_LISTS = [
    ("_URPM_COMMANDS", ()),
    ("cache_subcmds", ("cache",)),
    ("hold_subcmds", ("hold",)),
    ("skip_subcmds", ("skip",)),
    ("mark_subcmds", ("mark",)),
]


@pytest.mark.parametrize("variable,path", VERB_LISTS)
def test_every_verb_the_cli_takes_is_offered(variable, path):
    missing = _parser_verbs(*path) - _completion_verbs(variable)
    assert not missing, (
        f"the CLI accepts {sorted(missing)} under {' '.join(('urpm',) + path)} "
        f"and {variable} does not offer them")


@pytest.mark.parametrize("variable,path", VERB_LISTS)
def test_nothing_is_offered_that_does_not_exist(variable, path):
    extra = _completion_verbs(variable) - _parser_verbs(*path)
    assert not extra, (
        f"{variable} offers {sorted(extra)}, which no parser accepts "
        f"under {' '.join(('urpm',) + path)} — it completes straight "
        f"into an error")


class TestTheHelperReadsWhatItThinksItReads:
    """The comparison is only worth anything if both sides are read
    correctly; a regex that silently matched nothing would make every
    assertion above pass."""

    def test_a_missing_variable_is_an_error_not_an_empty_set(self):
        with pytest.raises(AssertionError):
            _completion_verbs("no_such_variable_here")

    def test_the_parser_side_is_not_empty(self):
        assert _parser_verbs("cache"), "introspection returned nothing"
