"""Mageia neither installs ``sudo`` by default nor puts the first user
in a sudoer group.  A message that spells out ``sudo <command>`` hands
those users a line that fails, and hides ``su -``, which works on every
Unix because ``su`` ships in coreutils.

:mod:`urpm.auth.privileges` already resolves this per machine.  Every
user-facing string must therefore either name the bare command, or go
through that module.  Shipped documentation cannot probe anything, so
it uses ``su -c``, the form CONTRIBUTING.md already establishes.

This file is the ratchet: the sites fixed on 2026-09-23 stay fixed,
and a new one cannot appear unnoticed.  It earned its keep on the day
it was written by finding a hard-coded ``su -c`` in ``cli/main.py``
that a grep for "sudo" could never have caught.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = SOURCE_ROOT.parent

#: Where deciding between sudo, su and pkexec is the actual job, or
#: where the text deliberately tells the reader *not* to use sudo.
ALLOWED = {
    "auth/privileges.py",   # builds the suggestions
    "core/config.py",       # defines SUDOER_GROUPS
    "core/userns.py",       # says "in a root shell (not sudo)"
    "cli/helpers/kernel.py",  # 'sudo' as a package name in a keep-list
}


def _python_sources():
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(SOURCE_ROOT).as_posix()
        if rel.startswith("tests/") or rel in ALLOWED:
            continue
        yield rel, path


def _string_literals(path: pathlib.Path):
    """Every string constant that can reach the screen, with its line.

    Parsed rather than grepped, and docstrings are excluded: prose
    explaining *why* we avoid sudo is not a defect, and a grep cannot
    tell it from a message that hands the user the command.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, 'body', None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


@pytest.mark.parametrize("rel,path", list(_python_sources()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_no_string_hands_out_a_sudo_command(rel, path):
    """No shipped string may prefix a command with a privilege tool.

    ``su`` and ``pkexec`` are caught too: the choice belongs to
    :func:`urpm.auth.privileges.privileged_command`, which knows what
    this machine has, and to nobody else.
    """
    offenders = []
    for lineno, text in _string_literals(path):
        for token in ("sudo ", "pkexec ", "su -c "):
            if token in text:
                offenders.append(f"{rel}:{lineno}: {text.strip()[:70]}")
                break
    assert not offenders, (
        "hard-coded privilege escalation in a shipped string; use "
        "urpm.auth.privileges.privileged_command() or name the command "
        "bare:\n  " + "\n  ".join(offenders)
    )


#: Tracked markdown that is nonetheless out of scope.  ``CHANGELOG.md``
#: records what happened rather than telling anyone what to type, and
#: editing past entries would falsify it.  ``rpmdrake/doc/`` holds
#: design documents for the graphical front end.
SKIPPED_DOCS = ("CHANGELOG.md", "rpmdrake/doc/")


def _markdown_sources():
    """Markdown the project ships, taken from what git tracks.

    Asking git rather than walking the tree is what keeps this check
    honest: ``rpmbuild/BUILD`` unpacks released tarballs containing
    old copies of these very documents, ``.pytest_cache`` has its own
    README, and the working tree carries local drafts.  None of those
    are ours to police, and none of them should be able to fail the
    suite.
    """
    import subprocess

    try:
        out = subprocess.run(
            ['git', '-C', str(REPO_ROOT), 'ls-files', '-z', '*.md'],
            capture_output=True, check=True,
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return  # not a checkout (released tarball): nothing to police

    for rel in sorted(filter(None, out.split('\0'))):
        if rel == "CHANGELOG.md" or rel.startswith(SKIPPED_DOCS):
            continue
        yield rel, REPO_ROOT / rel


def _fenced_command_lines(path: pathlib.Path):
    """Lines inside a ``` fence, which is where commands live.

    Prose *about* sudo is the whole point of these documents and must
    not trip the check; only what a reader would copy and paste does.
    """
    inside = False
    for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        if re.match(r'^\s*```', line):
            inside = not inside
            continue
        if inside:
            yield lineno, line


@pytest.mark.parametrize("rel,path", list(_markdown_sources()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_no_document_tells_the_reader_to_type_sudo(rel, path):
    """Documentation may explain sudo; it may not hand out a sudo line.

    ``su -c`` is allowed here and nowhere else: a static document
    cannot ask the machine what it has, and ``su`` is the one tool
    guaranteed present.

    Only ``sudo`` is refused, not ``pkexec``.  In a design document
    ``pkexec`` usually describes how a graphical program elevates its
    own helper, which is the correct mechanism and not something the
    reader types.  In Python it would be a message, and the check
    above catches it there.
    """
    offenders = [
        f"{rel}:{lineno}: {line.strip()}"
        for lineno, line in _fenced_command_lines(path)
        if re.match(r'^\s*sudo\s', line)
    ]
    assert not offenders, (
        "documentation hands the reader a command that assumes sudo, "
        'which Mageia may not have; use su -c "...":\n  '
        + "\n  ".join(offenders)
    )
