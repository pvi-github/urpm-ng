"""Every source that translates has to be declared for extraction.

``xgettext`` reads the list in ``po/POTFILES.in``, nothing else. A
module that calls ``_()`` without being listed looks perfectly
translated in the source and ships untranslated in all six languages,
and nothing says so: ``msgfmt --statistics`` reports 100 % because the
strings were never extracted to begin with, and the untranslated count
stays at zero.

That is how ``urpm skip`` was written: its whole output was invisible
to the translators, and the measurement that was supposed to catch it
said everything was fine. One file list, read from both ends.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
POTFILES = REPO / "po" / "POTFILES.in"

#: The gettext entry points the Makefile passes to xgettext
#: (``--keyword=_ --keyword=N_ --keyword=ngettext:1,2``).
KEYWORDS = {"_", "N_", "ngettext"}


def listed_files() -> set:
    """What ``POTFILES.in`` declares, as repo-relative paths."""
    return {line.strip() for line in POTFILES.read_text().splitlines()
            if line.strip() and not line.startswith("#")}


def translates(path: pathlib.Path) -> bool:
    """Whether *path* passes a literal to a gettext entry point.

    A literal is what xgettext can extract; ``_(variable)`` is a
    lookup at run time of a string translated somewhere else, and does
    not require the file to be listed.
    """
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if getattr(node.func, "id", None) not in KEYWORDS:
            continue
        if isinstance(node.args[0], (ast.Constant, ast.JoinedStr)):
            return True
    return False


def source_files():
    """The shipped Python sources, tests excluded."""
    for path in sorted(REPO.glob("urpm/**/*.py")):
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in f"/{rel}" or "__pycache__" in rel:
            continue
        yield rel, path


def test_potfiles_in_exists():
    """A missing list would make every assertion below vacuous."""
    assert POTFILES.is_file()
    assert listed_files(), "POTFILES.in is empty"


def test_every_translating_source_is_listed():
    listed = listed_files()
    missing = sorted(rel for rel, path in source_files()
                     if translates(path) and rel not in listed)
    assert not missing, (
        "these files call _() or ngettext() with a literal and are not in "
        "po/POTFILES.in, so their strings are never extracted and ship "
        "untranslated: " + ", ".join(missing))


def test_nothing_listed_has_disappeared():
    """A stale entry makes `make pot` fail on a missing file."""
    gone = sorted(rel for rel in listed_files()
                  if not (REPO / rel).exists())
    assert not gone, (
        "po/POTFILES.in names files that no longer exist: " + ", ".join(gone))


def test_the_detector_sees_a_real_case():
    """The scan is only worth something if it can say yes.

    A ``translates()`` that always answered no would make the coverage
    test pass whatever happens.
    """
    assert translates(REPO / "urpm/cli/display.py")


def test_the_detector_ignores_a_module_without_strings(tmp_path):
    plain = tmp_path / "plain.py"
    plain.write_text("def f(x):\n    return x + 1\n")
    assert not translates(plain)


def test_a_runtime_lookup_does_not_require_listing(tmp_path):
    """``_(variable)`` extracts nothing, so it demands nothing."""
    indirect = tmp_path / "indirect.py"
    indirect.write_text("def f(msg):\n    return _(msg)\n")
    assert not translates(indirect)
