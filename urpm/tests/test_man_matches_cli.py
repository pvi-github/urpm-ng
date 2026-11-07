"""The manual pages must describe the CLI that exists, in every language.

Same reasoning as :mod:`test_completion_matches_cli`, applied to the
seven hand-maintained manual trees.  Nothing generates them, nothing
checks them, and an option added to the parser stays undocumented until
somebody happens to look: the audit of the 2026-10-26 found eleven
options missing from the English page, and the German, Spanish, Dutch
and Portuguese pages twenty-three behind it.

Two distinct properties, and the second is the one that scales.

The English page is compared against the parser, option by option.  The
comparison is deliberately *global*: a single file documents ninety
parsers, and attributing each option to its own section would mean
parsing seven hand-written pages' sectioning, which is brittle for no
gain.  So this answers "is it documented anywhere", not "is it in the
right place"; a reader is the only judge of the second.

The six translations are compared against the English page.  An option
is not translated, so the sets must be equal, which turns a question
about translation into one about mechanics, and catches a page left
behind by a cycle without anyone having to read German.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from urpm.cli.main import create_parser

MAN = Path(__file__).resolve().parents[2] / "man"

#: Languages the manual is maintained in, English excluded: it is the
#: reference the others are compared against.
TRANSLATIONS = ("de", "es", "fr", "it", "nl", "pt")

#: ``-h`` / ``--help`` exist on every parser argparse builds, and no
#: page documents them ninety times.  ``--json`` and ``--flat`` are set
#: aside on purpose: no command produces either output yet, so
#: documenting them would promise what urpm does not do.
NOT_DOCUMENTED = {"--help", "--json", "--flat"}


# ── Reading the CLI ─────────────────────────────────────────────────────


def _subparsers(parser):
    """The subcommand action of *parser*, or None if it takes no verb."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _walk(parser, path=()):
    """Yield ``(path, long options)`` for every node of the parser tree.

    An option whose help is :data:`argparse.SUPPRESS` is left out: it is
    hidden from ``--help`` on purpose, being an internal handover rather
    than something an operator is meant to reach for, and a manual page
    that documented it would be advertising it.
    """
    yield path, {option
                 for a in parser._actions if a.help is not argparse.SUPPRESS
                 for option in a.option_strings
                 if option.startswith("--")} - NOT_DOCUMENTED
    action = _subparsers(parser)
    if action is None:
        return
    seen = set()
    for name, sub in action.choices.items():
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        yield from _walk(sub, path + (name,))


CLI = dict(_walk(create_parser()))


# ── Reading a manual page ───────────────────────────────────────────────


def _man_text(language: str) -> str:
    """The page with its comments dropped and its groff escapes undone.

    A hyphen is written ``\\-`` in a manual source, so a naive search
    for ``--option`` finds almost nothing; the audit script made that
    mistake first and reported a page as empty of options.  Font
    switches are dropped for the same reason: ``\\fB\\-\\-auto\\fR`` has
    to read as one word.
    """
    text = (MAN / language / "man1" / "urpm.1").read_text()
    text = "\n".join(line for line in text.splitlines()
                     if not line.startswith('.\\"'))
    text = re.sub(r"\\f[BIRP]|\\fB|\\s[+-]?\d", "", text)
    return text.replace("\\-", "-")


#: Options of *other* programs, named in prose because the page
#: explains what ``urpm build`` does with them: ``--build-memory`` is
#: enforced as ``podman --memory``, parallelism reaches rpmbuild as
#: ``--define '_smp_mflags -jN'``.  Listed one by one rather than
#: guessed from the surrounding words: a sentence is not a structure to
#: parse, and five options the page describes correctly were reported
#: as lies by an attempt to infer it.  urpm's own spellings differ
#: (``--build-memory``, ``--net-isolation``), so none of these can
#: collide with a real one.
FOREIGN = {"--cpus", "--define", "--memory", "--memory-swap", "--net"}


def _man_options(language: str) -> set:
    """The options a page names, foreign ones excluded.

    The trailing hyphen is dropped: German and Dutch glue a word onto
    the option in prose, as in ``\\fB--release\\fR-Angabe``, and the
    option named there is ``--release``.  No option ends in a hyphen,
    so nothing true is lost.
    """
    found = re.findall(r"--[a-z][a-z0-9-]*", _man_text(language))
    return {option.rstrip("-") for option in found} - FOREIGN


EN = _man_options("en")


# ── The English page against the parser ─────────────────────────────────


@pytest.mark.parametrize(
    "path", sorted(path for path, options in CLI.items() if options),
    ids=lambda path: " ".join(path) or "urpm")
def test_every_option_the_cli_takes_is_documented(path):
    """What the audit of the 2026-10-26 counted, as a standing check."""
    undocumented = sorted(CLI[path] - EN)

    assert not undocumented, (
        f"`urpm {' '.join(path)}` takes {', '.join(undocumented)}, "
        f"absent from man/en/man1/urpm.1")


def test_nothing_is_documented_that_the_cli_refuses():
    """The other direction, which is how a page comes to lie.

    An option removed from the parser leaves its paragraph behind, and
    the paragraph is what an operator reads.  The completion pass of
    this cycle found forty-five such claims.
    """
    offered = {option for options in CLI.values() for option in options}
    offered |= NOT_DOCUMENTED

    assert not sorted(EN - offered), sorted(EN - offered)


def _declared_names(language: str) -> set:
    """The words a page declares in a header position.

    A command is declared by a ``.B`` or ``.BR`` line, alone or ahead of
    its arguments: ``.B i|install \\fIpackage\\fR``.  Option names and
    argument placeholders are dropped, aliases are kept, since the page
    spells several commands as ``alias|name``.
    """
    names = set()
    for line in _man_text(language).splitlines():
        if not re.match(r"^\.B[R]? ", line):
            continue
        head = re.sub(r'\\f[BIR]|"|,', " ", line[3:]).strip()
        for word in re.split(r"[ |]+", head):
            # Hyphens belong to the name: add-cdrom, ack-blacklist.
            if re.fullmatch(r"[a-z][a-z0-9-]*", word):
                names.add(word)
    return names


def _cli_commands() -> set:
    """Every command name the CLI answers to, aliases excluded.

    Aliases are left out on purpose: ``urpm --help`` does not list them
    either, and a page that spelled all ninety of them would be a
    worse page.
    """
    names = set()

    def walk(parser):
        action = _subparsers(parser)
        if action is None:
            return
        for choice in action._choices_actions:
            names.add(choice.dest)
        for sub in action.choices.values():
            walk(sub)

    walk(create_parser())
    return names


def test_every_command_the_cli_takes_is_documented():
    """A whole command can go missing, and the option check will not say.

    The options are compared against the page as a whole, so an option
    of an undocumented command counts as documented the moment another
    command shares its name.  That is how ``urpm system``, with its
    export, its import and five options, sat entirely absent from the
    page while every option test passed.
    """
    documented = _declared_names("en")
    absent = sorted(name for name in _cli_commands()
                    if name not in documented)

    assert not absent, f"absent from man/en/man1/urpm.1: {absent}"


# ── The translations against the English page ───────────────────────────


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_a_translation_documents_the_same_options(language):
    """An option is not translated, so the two sets must be equal."""
    theirs = _man_options(language)
    missing = sorted(EN - theirs)
    extra = sorted(theirs - EN)

    assert not missing and not extra, (
        f"man/{language} is missing {missing} and carries {extra} "
        f"that man/en does not")
