"""The completion has to describe the CLI that exists.

Two ways it drifts, and both were live at once: ``urpm cache flush``
was added and never offered, while ``rebuild-fts`` was offered and had
no parser behind it.  Neither shows up in normal use, a missing verb
just fails to complete and an extra one completes into an error, so
nothing catches them but a test that reads both sides.

This file used to compare the verb lists only, on the grounds that a
flag is cheaper to get wrong than a subcommand.  An audit of the whole
surface proved that wrong: 45 subcommands were offering
``--json --flat --show-all``, which exist on the *parent* of a
subcommand group and not on its subcommands, so a TAB handed the
operator a flag that answers "unrecognized arguments".  Three verbs of
``urpm server`` were never offered, three commands were routed
nowhere, ``--dev`` was offered and does not exist, and ``update``
offered ``--files`` where the flag is ``--force``.  So options are
compared too now.

Reading the completion means reading bash with regular expressions,
which is only sound because of what is being looked for: the words in
a ``compgen -W`` list and the patterns of a ``case``, both of which
are flat text.  Comment lines are dropped first, since the handlers
document the flags a command does *not* take and reading those as
offers would invent drift that is not there.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from urpm.cli.main import create_parser

COMPLETION = Path(__file__).resolve().parents[2] / "completion" / "urpm.bash"

#: Line continuations joined: several lists span three or four lines,
#: and reading only the first would silently compare a fragment.
TEXT = COMPLETION.read_text().replace("\\\n", " ")

#: ``-h`` / ``--help`` exist on every parser argparse builds and are
#: offered once, among the global flags, rather than on all 90 of them.
UNIVERSAL = {"-h", "--help"}


# ── Reading the CLI ─────────────────────────────────────────────────────


def _subparsers(parser):
    """The subcommand action of *parser*, or None if it takes no verb."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _walk(parser, path=()):
    """Yield ``(path, verbs, flags)`` for every node of the parser tree.

    ``verbs`` holds every spelling accepted below the node, aliases
    included, because that is what the completion lists.  ``flags``
    holds the node's own option strings.
    """
    action = _subparsers(parser)
    yield (path,
           set(action.choices) if action else set(),
           {s for a in parser._actions for s in a.option_strings} - UNIVERSAL)
    if action is None:
        return
    seen = set()
    for name, sub in action.choices.items():
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        yield from _walk(sub, path + (name,))


CLI = {path: (verbs, flags) for path, verbs, flags in _walk(create_parser())}


# ── Reading the completion ──────────────────────────────────────────────


def _without_comments(text: str) -> str:
    """Drop comments, whole-line and trailing alike."""
    kept = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept.append(re.sub(r'\s#.*$', "", line))
    return "\n".join(kept)


CONSTANTS = dict(re.findall(r'^(_URPM_[A-Z_]+)="([^"]*)"', TEXT, re.M))


def _expanded(text: str) -> str:
    """Substitute the ``_URPM_*`` constants a handler interpolates."""
    for name, value in CONSTANTS.items():
        text = text.replace(f"${name}", value)
    return text


def _offered_flags(text: str) -> set:
    """Every option string *text* claims the command accepts.

    Both what a ``compgen -W`` list offers and what a ``case "$prev"``
    recognises, since completing the *value* of a flag is as much a
    claim that the flag exists as offering its name.
    """
    text = _expanded(_without_comments(text))
    # The completion's own grammar, not the command's flags.
    text = text.replace("compgen -W", " ").replace("-eq", " ")
    text = re.sub(r'_filedir -[a-z]', " ", text)
    text = re.sub(r'\[\[ -[a-z] ', " ", text)
    return set(re.findall(r'(?<![\w$/-])--?[A-Za-z][A-Za-z0-9-]*',
                          text)) - UNIVERSAL


def _handler_bodies() -> dict:
    """``name -> body`` for every ``_urpm_<name>()`` function."""
    bodies = {}
    starts = [(m.start(), m.group(1))
              for m in re.finditer(r'^(_urpm_[a-z_]+)\(\) \{', TEXT, re.M)]
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(TEXT)
        bodies[name] = TEXT[pos:end]
    return bodies


BODIES = _handler_bodies()

#: Which handler answers which top-level command, read from the
#: dispatcher rather than guessed from the name.
DISPATCH = {}
for _match in re.finditer(r'^        ([a-z|_-]+\))\s*\n?\s*(_urpm_[a-z_]+) ;;',
                          TEXT, re.M):
    for _name in _match.group(1).rstrip(")").split("|"):
        DISPATCH[_name] = _match.group(2)


def _case_start(body: str) -> int:
    """Where a group handler's subcommand ``case`` opens, or -1."""
    for needle in ('case "$sub" in', 'case "${words[2]}" in'):
        found = body.find(needle)
        if found >= 0:
            return found
    return -1


def _branches(body: str) -> dict:
    """``subcommand -> branch text`` inside a group handler's ``case``.

    A branch runs to the next label at the same indentation, not to the
    first ``;;``: several of them open a nested ``case "$prev"`` whose
    own ``;;`` would cut the body in half and hide most of what it
    offers.
    """
    start = _case_start(body)
    if start < 0:
        return {}
    region = body[start:]
    labels = [(m.start(), m.group(1))
              for m in re.finditer(r'^        ([^\s()]+)\)\s*$',
                                   region, re.M)]
    found = {}
    for i, (pos, label) in enumerate(labels):
        end = labels[i + 1][0] if i + 1 < len(labels) else len(region)
        for name in label.split("|"):
            if name != "*":
                found[name] = region[pos:end]
    return found


def _completion_verbs(variable: str) -> set:
    """Words listed in a ``<variable>="..."`` assignment."""
    match = re.search(rf'{variable}="([^"]+)"', TEXT)
    assert match, f"{variable} not found in {COMPLETION}"
    return set(match.group(1).split())


def _parser_verbs(*path: str) -> set:
    """Subcommands argparse really accepts under *path*."""
    assert path in CLI, f"urpm {' '.join(path)} has no parser"
    return CLI[path][0]


# ── Verbs ───────────────────────────────────────────────────────────────


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
    ("media_subcmds", ("media",)),
    ("server_subcmds", ("server",)),
    ("mirror_subcmds", ("mirror",)),
    ("config_subcmds", ("config",)),
    ("config_list_subcmds", ("config", "blacklist")),
    ("config_list_subcmds", ("config", "redlist")),
    ("key_subcmds", ("key",)),
    ("peer_subcmds", ("peer",)),
    ("appstream_subcmds", ("appstream",)),
    ("image_subcmds", ("image",)),
    ("system_subcmds", ("system",)),
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


def test_every_list_of_verbs_is_compared():
    """A verb list the table above forgets is a blind spot.

    The table used to cover five of them, and the drift had settled in
    the other eight.
    """
    declared = set(re.findall(r'local ([a-z_]+_subcmds)=', TEXT))
    declared.add("_URPM_COMMANDS")
    compared = {variable for variable, _path in VERB_LISTS}
    assert declared <= compared, (
        f"{sorted(declared - compared)} is a verb list nothing compares")


def test_every_command_with_verbs_has_a_list():
    """A subcommand group whose verbs are hard-coded is unreachable."""
    lacking = [" ".join(path) for path, (verbs, _flags) in CLI.items()
               if verbs and path
               and path not in {p for _v, p in VERB_LISTS}]
    assert not lacking, (
        f"urpm {lacking} take verbs and no named list holds them")


# ── Options ─────────────────────────────────────────────────────────────


def _top_level_commands():
    """Canonical name of every top-level command, with its handler."""
    for path, (_verbs, _flags) in sorted(CLI.items()):
        if len(path) == 1:
            yield path[0]


@pytest.mark.parametrize("command", sorted(_top_level_commands()))
def test_every_command_is_routed_to_a_handler(command):
    """``audit``, ``genmedia`` and ``system`` were routed nowhere, so a
    TAB after them completed filenames."""
    assert command in DISPATCH, (
        f"urpm {command} is offered as a verb and the dispatcher sends it "
        f"nowhere")


def _flag_cases():
    """``(label, cli flags, offered flags)`` for every comparable node.

    A group handler is compared branch by branch, and its prologue
    against the group's own flags: that distinction is the whole point,
    since the reporting flags live on the parent and not below it.  A
    handler that serves a group without splitting into branches mixes
    the two levels on purpose, so it is compared against the union,
    which is a looser check and the honest one for that shape.
    """
    for path, (verbs, flags) in sorted(CLI.items()):
        if len(path) != 1:
            continue
        handler = DISPATCH.get(path[0])
        if handler is None:
            continue  # reported by its own test
        body = BODIES[handler]
        branches = _branches(body)
        if not branches:
            union = set(flags)
            for verb in verbs:
                union |= CLI.get(path + (verb,), (set(), set()))[1]
            yield f"urpm {path[0]}", union, _offered_flags(body)
            continue
        yield (f"urpm {path[0]} (before a subcommand)", flags,
               _offered_flags(body[:_case_start(body)]))
        for verb in sorted(verbs):
            key = path + (verb,)
            if key not in CLI or verb not in branches:
                continue
            yield (f"urpm {path[0]} {verb}", CLI[key][1],
                   _offered_flags(branches[verb]))


FLAG_CASES = list(_flag_cases())


@pytest.mark.parametrize("label,cli,offered",
                         FLAG_CASES, ids=[c[0] for c in FLAG_CASES])
def test_no_flag_is_offered_that_does_not_exist(label, cli, offered):
    """The costly direction: a flag that completes into a usage error."""
    assert not offered - cli, (
        f"{label} offers {sorted(offered - cli)}, which its parser "
        f"refuses — a TAB here hands out a command that fails")


@pytest.mark.parametrize("label,cli,offered",
                         FLAG_CASES, ids=[c[0] for c in FLAG_CASES])
def test_no_flag_the_cli_takes_is_left_out(label, cli, offered):
    """The cheap direction, worth holding anyway: a flag nobody can
    discover is a flag nobody uses."""
    assert not cli - offered, (
        f"{label} accepts {sorted(cli - offered)} and the completion "
        f"never offers them")


def test_a_subcommand_with_flags_has_a_branch():
    """A subcommand that takes flags and has no branch offers none."""
    orphans = []
    for path, (verbs, _flags) in sorted(CLI.items()):
        if len(path) != 1 or path[0] not in DISPATCH:
            continue
        body = BODIES[DISPATCH[path[0]]]
        branches = _branches(body)
        if not branches:
            continue
        for verb in sorted(verbs):
            key = path + (verb,)
            if key in CLI and CLI[key][1] and verb not in branches:
                orphans.append(f"urpm {path[0]} {verb}")
    assert not orphans, f"{orphans} take flags and have no branch"


# ── The reading itself ──────────────────────────────────────────────────


class TestTheHelperReadsWhatItThinksItReads:
    """The comparison is only worth anything if both sides are read
    correctly; a regex that silently matched nothing would make every
    assertion above pass."""

    def test_a_missing_variable_is_an_error_not_an_empty_set(self):
        with pytest.raises(AssertionError):
            _completion_verbs("no_such_variable_here")

    def test_the_parser_side_is_not_empty(self):
        assert _parser_verbs("cache"), "introspection returned nothing"

    def test_the_dispatcher_was_read(self):
        assert DISPATCH.get("install") == "_urpm_install"
        assert len(DISPATCH) > 40, "the dispatcher came back nearly empty"

    def test_a_handler_body_was_read(self):
        assert "--reinstall" in _offered_flags(BODIES["_urpm_install"])

    def test_a_comment_is_not_read_as_an_offer(self):
        # _urpm_upgrade documents the install-only flags it does *not*
        # take; reading that list as offers invented eight defects.
        assert "--reinstall" not in _offered_flags(BODIES["_urpm_upgrade"])

    def test_a_branch_is_not_cut_at_a_nested_case(self):
        # `image make` opens a `case "$prev"` whose first ;; used to
        # end the branch, hiding the flag list that follows it.
        made = _branches(BODIES["_urpm_image"])["make"]
        assert "--keep-chroot" in _offered_flags(made)

    def test_a_constant_is_expanded(self):
        assert "--json" in _offered_flags('"$_URPM_DISPLAY_FLAGS"')

    def test_the_completion_registers_itself(self):
        assert re.search(r'^complete -F _urpm urpm$', TEXT, re.M)
