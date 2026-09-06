"""The test media must be regenerated when their inputs change.

``test_install.py`` runs against RPMs built by ``gen_test_rpms``.  The
old check asked only whether ``urpm/tests/media/`` existed, so a media
set produced by an older ``data/`` satisfied it forever: whole media
added since were never built, and media that did survive kept whatever
packages they had when they were made.

That is not a cosmetic gap.  The same commit, checked out twice, gave
117 passed in one tree and 6 failed in the other -- the only difference
being how old each ``media/`` happened to be.  The failures pointed at
``rpm -i`` and read like code regressions.

Timestamps cannot express this.  git sets mtimes to checkout time, so a
fresh clone of unchanged data looks modified; and a ``media/`` touched
during a test run looks newer than the specs it is missing.  Both were
observed.  The generator therefore stamps ``media/`` with a digest of
what it read, and freshness is that digest matching.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from urpm.tests import gen_test_rpms
from urpm.tests.gen_test_rpms import (
    STAMP_NAME,
    input_digest,
    media_are_current,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal stand-in for ``urpm/tests/`` with generated media."""
    (tmp_path / "data" / "SPECS" / "conflicts").mkdir(parents=True)
    (tmp_path / "data" / "SPECS" / "conflicts" / "a.spec").write_text("Name: a\n")
    (tmp_path / "data" / "SPECS" / "solo.spec").write_text("Name: solo\n")
    (tmp_path / "media").mkdir()
    _stamp(tmp_path)
    return tmp_path


def _stamp(base: Path) -> None:
    (base / "media" / STAMP_NAME).write_text(input_digest(base) + "\n")


class TestFreshMediaAreAccepted:

    def test_untouched_tree_is_current(self, tree):
        assert media_are_current(tree)

    def test_touching_media_does_not_make_it_stale(self, tree):
        """A test run writing inside ``media/`` must not be mistaken for
        a regeneration -- nor for an invalidation."""
        (tree / "media" / "conflicts").mkdir()
        (tree / "media" / "conflicts" / "a-1-1.noarch.rpm").write_bytes(b"x")
        assert media_are_current(tree)

    def test_mtimes_are_irrelevant(self, tree):
        """git assigns checkout time to every file, so a clone of
        unchanged data must still count as current."""
        for path in tree.rglob("*"):
            if path.is_file():
                path.touch()
        assert media_are_current(tree)


class TestStaleMediaAreRejected:

    def test_new_medium_in_data(self, tree):
        """The case that broke: ``supplements-1`` existed in ``data/``
        and had never been built."""
        (tree / "data" / "SPECS" / "supplements-1").mkdir()
        (tree / "data" / "SPECS" / "supplements-1" / "s.spec").write_text("Name: s\n")
        assert not media_are_current(tree)

    def test_new_spec_inside_an_existing_medium(self, tree):
        """The subtler half: the medium survived, its packages did not.
        ``file-conflicts`` held 10 RPMs for 15 specs."""
        (tree / "data" / "SPECS" / "conflicts" / "fa.spec").write_text("Name: fa\n")
        assert not media_are_current(tree)

    def test_edited_spec(self, tree):
        (tree / "data" / "SPECS" / "solo.spec").write_text("Name: solo\nEpoch: 1\n")
        assert not media_are_current(tree)

    def test_removed_spec(self, tree):
        (tree / "data" / "SPECS" / "solo.spec").unlink()
        assert not media_are_current(tree)

    def test_non_spec_data_counts_too(self, tree):
        """``rpm-i586-to-i686``, ``reconfig`` and ``media_info`` are
        copied from ``data/`` rather than built from a spec."""
        (tree / "data" / "reconfig.urpmi").write_text("...\n")
        assert not media_are_current(tree)


class TestAbsentOrUnvouchedMedia:

    def test_no_media_directory(self, tmp_path):
        (tmp_path / "data").mkdir()
        assert not media_are_current(tmp_path)

    def test_media_without_a_stamp(self, tree):
        """Every media set built before this mechanism existed.  Absence
        of a stamp means unknown, and unknown must not read as fresh."""
        (tree / "media" / STAMP_NAME).unlink()
        assert not media_are_current(tree)

    def test_truncated_stamp(self, tree):
        """A generator killed midway leaves no usable stamp."""
        (tree / "media" / STAMP_NAME).write_text("")
        assert not media_are_current(tree)

    def test_stamp_is_a_directory(self, tree):
        """Must answer, not raise: this runs before every test."""
        (tree / "media" / STAMP_NAME).unlink()
        (tree / "media" / STAMP_NAME).mkdir()
        assert not media_are_current(tree)


class TestDigest:

    def test_stable_across_calls(self, tree):
        assert input_digest(tree) == input_digest(tree)

    def test_independent_of_the_tree_location(self, tree, tmp_path):
        """Paths enter the digest relative to the base, so the same data
        under a different prefix -- a worktree, a clone -- agrees."""
        elsewhere = tmp_path / "moved"
        shutil.copytree(tree, elsewhere)
        assert input_digest(elsewhere) == input_digest(tree)

    def test_content_moved_between_files_is_detected(self, tree):
        """Names are hashed alongside contents, so swapping two specs is
        not a no-op."""
        before = input_digest(tree)
        a = tree / "data" / "SPECS" / "conflicts" / "a.spec"
        b = tree / "data" / "SPECS" / "solo.spec"
        a_text, b_text = a.read_text(), b.read_text()
        a.write_text(b_text)
        b.write_text(a_text)
        assert input_digest(tree) != before


class TestGendistribAvailability:
    """``gendistrib`` (package ``rpmtools``) is optional, and its absence
    silently shrinks the media set: the ``media_info`` medium is not
    built and the tests needing it skip.

    That makes it an input, not an environment detail.  Generating
    without it and then installing ``rpmtools`` leaves ``data/``
    untouched, so a digest over ``data/`` alone would keep certifying
    the incomplete set as current -- the exact false "up to date" this
    stamp exists to prevent.  Observed here: the first real run of
    ``make test-media`` produced 53 media and vouched for them.
    """

    def test_gaining_gendistrib_invalidates_the_stamp(self, tree, monkeypatch):
        monkeypatch.setattr(gen_test_rpms, "find_gendistrib", lambda base: "")
        _stamp(tree)
        assert media_are_current(tree)

        monkeypatch.setattr(
            gen_test_rpms, "find_gendistrib", lambda base: "/usr/bin/gendistrib")
        assert not media_are_current(tree)

    def test_losing_gendistrib_invalidates_the_stamp(self, tree, monkeypatch):
        """Symmetric, and it matters: a media set built *with* it holds a
        medium the degraded generator cannot reproduce."""
        monkeypatch.setattr(
            gen_test_rpms, "find_gendistrib", lambda base: "/usr/bin/gendistrib")
        _stamp(tree)
        assert media_are_current(tree)

        monkeypatch.setattr(gen_test_rpms, "find_gendistrib", lambda base: "")
        assert not media_are_current(tree)

    def test_only_presence_matters_not_the_path(self, tree, monkeypatch):
        """Two machines with rpmtools installed under different prefixes
        must agree -- otherwise every such machine regenerates forever."""
        monkeypatch.setattr(
            gen_test_rpms, "find_gendistrib", lambda base: "/usr/bin/gendistrib")
        _stamp(tree)
        monkeypatch.setattr(
            gen_test_rpms, "find_gendistrib", lambda base: "/opt/rpmtools/bin/gendistrib")
        assert media_are_current(tree)
