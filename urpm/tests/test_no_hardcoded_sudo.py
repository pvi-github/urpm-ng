"""Mageia neither installs ``sudo`` by default nor puts the first user
in a sudoer group.  A message that spells out ``sudo <command>`` hands
those users a line that fails, and hides ``su -``, which works on every
Unix because ``su`` ships in coreutils.

:mod:`urpm.auth.privileges` already resolves this per machine.  Every
user-facing string must therefore either name the bare command, or go
through that module.  This file is the ratchet: the six sites fixed on
2026-09-23 stay fixed, and a new one cannot appear unnoticed.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent

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
