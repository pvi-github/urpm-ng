"""Telling the release identity from the release number.

A mirror serves the development branch under ``cauldron/<arch>/…`` for
the whole cycle and only renames the tree to the number on release day.
A cauldron, meanwhile, already announces in ``os-release`` the version
it is *becoming*.

Reading ``VERSION_ID`` to build a mirror path therefore produced
``11/x86_64/media/core/release`` on a fresh cauldron: a path that exists
on no mirror, so no server could be attached to it and every medium was
listed with none.  Reported from a fresh cauldron install.

``/etc/version``, from ``mageia-release-common``, carries the branch as
its last field and is the only file that tells the two apart::

    10 4 official
    11 0.0.5 cauldron
"""

from pathlib import Path

import pytest

from urpm.core.config import (
    get_system_branch, get_system_identity, get_system_version,
)


@pytest.fixture
def system(tmp_path):
    """Build a fake system root; omit a file by passing None."""
    def _make(version_line=None, version_id=None):
        etc = tmp_path / "etc"
        etc.mkdir(exist_ok=True)
        if version_line is not None:
            (etc / "version").write_text(version_line + "\n", encoding="utf-8")
        if version_id is not None:
            (etc / "os-release").write_text(
                f'VERSION_ID={version_id}\n', encoding="utf-8")
        return str(tmp_path)
    return _make


class TestBranch:
    """What /etc/version says about the branch."""

    def test_a_released_system_reads_official(self, system):
        assert get_system_branch(system("10 4 official", "10")) == "official"

    def test_a_cauldron_reads_cauldron(self, system):
        assert get_system_branch(system("11 0.0.5 cauldron", "11")) == "cauldron"

    def test_case_does_not_matter(self, system):
        assert get_system_branch(system("11 0.0.5 Cauldron", "11")) == "cauldron"

    def test_a_missing_file_is_not_an_error(self, system):
        assert get_system_branch(system(None, "10")) is None

    def test_a_line_without_a_branch_field_yields_nothing(self, system):
        """Better no answer than mistaking the version for a branch."""
        assert get_system_branch(system("10", "10")) is None


class TestIdentity:
    """What urpm should address mirrors with."""

    def test_a_released_system_is_addressed_by_its_number(self, system):
        assert get_system_identity(system("10 4 official", "10")) == "10"

    def test_a_cauldron_is_addressed_as_cauldron(self, system):
        """The whole point: os-release says 11, mirrors serve cauldron."""
        root = system("11 0.0.5 cauldron", "11")

        assert get_system_version(root) == "11"
        assert get_system_identity(root) == "cauldron"

    def test_without_etc_version_the_number_is_used(self, system):
        """The agreed fallback: absent branch means not cauldron.

        Getting this wrong on a real cauldron gives a visible 404 on a
        path that does not exist, rather than a silently wrong tree.
        """
        assert get_system_identity(system(None, "11")) == "11"

    def test_a_system_telling_nothing_yields_nothing(self, system):
        assert get_system_identity(system(None, None)) is None

    def test_the_branch_wins_over_a_disagreeing_number(self, system):
        """A freeze window is exactly when the two disagree."""
        root = system("11 0.0.5 cauldron", "10")

        assert get_system_identity(root) == "cauldron"


def test_this_host_agrees_with_its_own_files():
    """Read the running system rather than assert a hardcoded answer.

    The suite runs on releases and on cauldrons alike, so the expected
    value is whatever this machine's own files say.
    """
    branch = get_system_branch()
    identity = get_system_identity()

    if branch == "cauldron":
        assert identity == "cauldron"
    else:
        assert identity == get_system_version()


def test_a_chroot_is_read_instead_of_the_host(tmp_path, system):
    """Cross-version init must not inherit the host's branch."""
    root = system("11 0.0.5 cauldron", "11")

    assert get_system_identity(root) == "cauldron"
    # The host is a release; had root been ignored we would see its value.
    assert get_system_identity(root) != get_system_identity()
