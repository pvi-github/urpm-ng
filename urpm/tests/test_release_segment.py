"""Reading a release out of a URL, and writing one back in.

The knowledge under test used to live in five modules at once, and
the copies disagreed: the distupgrade knew a community mirror writes
``/mageia9/`` where the official tree writes ``/9/``, the urpmi.cfg
sync did not, and a blogdrake medium came out of a release upgrade
still pointed at the release the machine had left.

Two properties carry everything else here.  A prefixed segment
identifies itself, so it needs no context and gets none.  A bare
number identifies nothing, so position decides, which is what keeps a
mirror's own ``/mirrors/10/`` directory from being renamed on an
upgrade.
"""

from __future__ import annotations

import pytest

from urpm.core.release_segment import (
    BIT32_ARCHES,
    BIT32_SLOT,
    BUILD_ARCHES,
    CAULDRON,
    RELEASE_PREFIXES,
    SOURCES_SEGMENT,
    URL_ARCH_SEGMENTS,
    arch_slot,
    is_32bit_arch,
    is_arch_segment,
    is_release_position,
    is_sources_segment,
    layout_anchors,
    parse_release_token,
    release_tokens,
)


def path(url_path: str) -> list:
    """The segment list a caller passes in, leading empty included."""
    return url_path.split("/")


# ── What reads as a release ─────────────────────────────────────────────


class TestTheSpellings:
    @pytest.mark.parametrize("segment,identity", [
        ("10", "10"),
        ("11.5", "11.5"),
        ("cauldron", "cauldron"),
        ("mageia10", "10"),
        ("mga9", "9"),
    ])
    def test_a_release_is_recognised(self, segment, identity):
        token = parse_release_token(segment)

        assert token is not None
        assert token.identity == identity

    def test_the_prefix_comes_back_as_found(self):
        # It is going back into a URL that has to keep working, so it
        # is not normalised on the way through.
        assert parse_release_token("Mageia10").prefix == "Mageia"
        assert parse_release_token("10").prefix == ""

    def test_the_identity_is_canonical(self):
        # The rest of the codebase names releases in lowercase.
        assert parse_release_token("CAULDRON").identity == CAULDRON

    @pytest.mark.parametrize("segment", [
        "", "free", "distrib", "media", "x86_64", "SRPMS",
        "mageia", "mga", "mageia-10", "10a", "mga10-rpms",
    ])
    def test_everything_else_is_not_a_release(self, segment):
        assert parse_release_token(segment) is None

    def test_every_prefix_the_module_declares_actually_parses(self):
        for prefix in RELEASE_PREFIXES:
            token = parse_release_token(f"{prefix}10")
            assert token is not None, prefix
            assert token.prefix == prefix

    def test_the_segments_of_a_path_come_back_with_their_index(self):
        found = release_tokens(path("/pub/mageia10/free/x86_64"))

        assert [(i, t.identity) for i, t in found] == [(2, "10")]


# ── Respelling ──────────────────────────────────────────────────────────


class TestRespelling:
    def test_a_bare_release_stays_bare(self):
        assert parse_release_token("9").respelled("10") == "10"

    def test_a_prefixed_release_keeps_its_prefix(self):
        # Rewriting ``mageia9`` as ``10`` would point the URL at a
        # directory the mirror does not have.
        assert parse_release_token("mageia9").respelled("10") == "mageia10"
        assert parse_release_token("mga9").respelled("10") == "mga10"

    def test_a_name_is_as_good_a_target_as_a_number(self):
        assert parse_release_token("10").respelled(CAULDRON) == "cauldron"

    def test_the_text_reads_back_as_the_segment(self):
        assert parse_release_token("mga9").text == "mga9"


# ── Release, or a number that looks like one ────────────────────────────


class TestWhichSegmentIsTheRelease:
    def test_the_official_shape_is_recognised(self):
        segments = path("/pub/Mageia/distrib/10/x86_64/media/core/release")

        assert is_release_position(segments, 4)

    def test_a_source_tree_is_recognised(self):
        segments = path("/pub/Mageia/distrib/10/SRPMS/core/release")

        assert is_release_position(segments, 4)

    def test_a_mirrors_own_numbered_directory_is_not_a_release(self):
        # ``/mirrors/10/`` is that mirror's layout.  Nothing with the
        # Mageia shape follows it, and renaming it on an upgrade
        # leaves the operator pointing at nothing.
        segments = path("/mirrors/10/mageia/distrib/9/x86_64/media/core")

        assert not is_release_position(segments, 2)
        assert is_release_position(segments, 5)

    def test_a_prefixed_segment_needs_no_context(self):
        # This is the whole reason a community layout can be
        # recognised: nothing in what follows ``mageia10`` has the
        # official shape.
        segments = path("/mageia/mageia10/free/x86_64")

        assert is_release_position(segments, 2)

    def test_a_prefixed_segment_at_the_very_end_still_counts(self):
        assert is_release_position(path("/pub/mga10"), 2)

    def test_a_bare_segment_at_the_very_end_does_not(self):
        assert not is_release_position(path("/pub/10"), 2)

    def test_a_segment_that_is_not_a_release_is_never_a_position(self):
        assert not is_release_position(path("/pub/free/x86_64/media"), 2)

    @pytest.mark.parametrize("index", [-1, 99])
    def test_an_index_outside_the_path_is_answered_not_raised(self, index):
        assert not is_release_position(path("/pub/10/x86_64/media"), index)


# ── The layout around it ────────────────────────────────────────────────


class TestTheAnchors:
    def test_an_architecture_is_an_anchor(self):
        assert is_arch_segment("x86_64")
        assert is_arch_segment("noarch")

    def test_the_arches_mageia_builds_for_are_all_there(self):
        # The list used to exist in three copies that disagreed, and a
        # missing architecture is a URL silently not recognised as a
        # media tree.
        assert {"armv5tl", "ppc64le"} <= BUILD_ARCHES
        assert "noarch" not in BUILD_ARCHES
        assert URL_ARCH_SEGMENTS == BUILD_ARCHES | {"noarch"}

    def test_a_source_tree_is_an_anchor_but_not_an_architecture(self):
        # ``SRPMS`` sits where an arch sits, which is why it anchors a
        # URL; it is never a value for ``media.architecture``.
        assert is_sources_segment(SOURCES_SEGMENT)
        assert not is_arch_segment(SOURCES_SEGMENT)

    def test_the_32_bit_tree_is_one_slot_under_two_names(self):
        # Release 9 publishes i586, release 10 publishes i686 and
        # serves i586 as an alias of it.  Anything comparing a medium
        # across a release change has to see one place, not two.
        assert arch_slot("i586") == arch_slot("i686") == BIT32_SLOT
        assert BIT32_ARCHES <= BUILD_ARCHES

    def test_every_other_architecture_is_its_own_slot(self):
        assert arch_slot("x86_64") == "x86_64"
        assert arch_slot("aarch64") == "aarch64"
        assert arch_slot("noarch") == "noarch"

    def test_a_slot_is_never_mistaken_for_a_directory(self):
        # The slot is a name for comparing; no mirror serves it.
        assert not is_arch_segment(BIT32_SLOT)

    def test_a_missing_architecture_is_answered_not_raised(self):
        assert arch_slot("") == ""
        assert arch_slot(None) == ""
        assert not is_32bit_arch(None)

    def test_the_case_of_the_answer_does_not_matter(self):
        assert is_32bit_arch("I586")
        assert arch_slot("I686") == BIT32_SLOT

    def test_both_kinds_come_back_from_a_path(self):
        assert layout_anchors(path("/mageia/mageia10/free/x86_64")) == [
            (4, "x86_64")]
        assert layout_anchors(path("/mageia/mageia10/free/SRPMS")) == [
            (4, "SRPMS")]
