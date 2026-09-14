"""Switching version-mode must leave the system telling one story.

``/etc/version``'s last field is the only thing that tells a development
branch from a released one, and it decides whether urpm addresses
``cauldron/`` or ``<n>/`` on a mirror.

Recording the preference in the database without touching that file
would leave the switch half-applied: the media follow the new mode
while the next ``urpm init`` or ``media autoconfig`` reads the old
branch and quietly goes back to it.

Note what is *not* changed here: ``version-mode system`` still resolves
through ``get_system_version``, which answers the number even on a
cauldron.  That is what makes the switch to official possible at all,
and swapping it for the identity would take the choice away.
"""

import os
from pathlib import Path

from urpm.core.config import get_system_branch, set_system_branch

_BRANCH_CAULDRON = "cauldron"
_BRANCH_RELEASED = "official"


def _system(tmp_path, contents):
    """Build a system root holding ``/etc/version``; return the root."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "version").write_text(contents, encoding="utf-8")
    return str(tmp_path)


def _read_back(root):
    return (Path(root) / "etc" / "version").read_text(encoding="utf-8")


def test_switching_to_cauldron_writes_cauldron(tmp_path):
    root = _system(tmp_path, "10 4 official\n")

    assert set_system_branch(_BRANCH_CAULDRON, root) == _BRANCH_CAULDRON
    assert _read_back(root).split() == ["10", "4", "cauldron"]


def test_switching_to_a_release_writes_official(tmp_path):
    """The case that matters on release day."""
    root = _system(tmp_path, "11 0.0.5 cauldron\n")

    assert set_system_branch(_BRANCH_RELEASED, root) == _BRANCH_RELEASED
    assert _read_back(root).split() == ["11", "0.0.5", "official"]


def test_the_version_and_release_fields_are_left_alone(tmp_path):
    """They belong to the distribution, not to us."""
    root = _system(tmp_path, "11 0.0.5 cauldron\n")

    set_system_branch(_BRANCH_RELEASED, root)

    assert _read_back(root).split()[:2] == ["11", "0.0.5"]


def test_an_already_aligned_file_is_not_rewritten(tmp_path):
    """No answer means nothing was touched, so nothing is announced."""
    root = _system(tmp_path, "10 4 official\n")
    before = _read_back(root)

    assert set_system_branch(_BRANCH_RELEASED, root) == ""
    assert _read_back(root) == before


def test_case_is_tolerated_when_comparing(tmp_path):
    root = _system(tmp_path, "11 0.0.5 Cauldron\n")

    assert set_system_branch(_BRANCH_CAULDRON, root) == ""


def test_a_missing_file_is_not_an_error(tmp_path):
    """A preference already recorded must not fail over /etc."""
    assert set_system_branch(_BRANCH_CAULDRON, str(tmp_path)) == ""


def test_a_file_without_a_branch_field_is_left_alone(tmp_path):
    """One field could be the version; overwriting it would destroy it."""
    root = _system(tmp_path, "10\n")

    assert set_system_branch(_BRANCH_CAULDRON, root) == ""
    assert _read_back(root) == "10\n"


def test_an_unwritable_file_is_reported_as_unchanged(tmp_path):
    root = _system(tmp_path, "10 4 official\n")
    target = Path(root) / "etc" / "version"
    os.chmod(target, 0o444)
    try:
        # Running as root defeats the permission bit, so only assert the
        # contract when the write really is refused.
        if os.access(target, os.W_OK):
            return
        assert set_system_branch(_BRANCH_CAULDRON, root) == ""
        assert _read_back(root).split()[-1] == "official"
    finally:
        os.chmod(target, 0o644)


def test_the_round_trip_is_stable(tmp_path):
    """cauldron -> official -> cauldron must restore the original line."""
    root = _system(tmp_path, "11 0.0.5 cauldron\n")
    original = _read_back(root)

    set_system_branch(_BRANCH_RELEASED, root)
    set_system_branch(_BRANCH_CAULDRON, root)

    assert _read_back(root) == original


def test_writing_then_reading_agree(tmp_path):
    """The two halves share one notion of the file's layout."""
    root = _system(tmp_path, "10 4 official\n")

    set_system_branch(_BRANCH_CAULDRON, root)

    assert get_system_branch(root) == _BRANCH_CAULDRON
