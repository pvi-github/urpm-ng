"""Validation tests for the shared media.cfg fixture kit.

These tests load every fixture defined under ``urpm/tests/fixtures/media_cfg/``
and assert that:

* the fixture parses without error (or, for ``malformed.cfg``, that it raises);
* the resulting ``MediaCfgInfo`` and ``DiscoveredMedia`` records expose
  the expected shape (version, arch, branch, number of media, classification
  flags).

The kit is the input domain for :mod:`urpm.core.media_pipeline` tests
(phase 1b).  Keeping the parse contract stable is essential: a regression
in :func:`urpm.core.media_cfg.parse_media_cfg` that broke any of these
catalogues would break the upstream primitive too.
"""

from __future__ import annotations

import configparser

import pytest

from urpm.core.media_cfg import parse_media_cfg
from urpm.tests.fixtures import load_media_cfg


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse(name: str, media_root: str = "9/x86_64/media"):
    """Load a fixture and parse it with the production parser."""
    content = load_media_cfg(name)
    return parse_media_cfg(content, media_root)


# ── Official Mageia catalogue ────────────────────────────────────────────


class TestOfficialMageia9x86_64:
    """The canonical mirror layout: core/nonfree/tainted × release/updates."""

    def test_media_info_section_matches(self):
        info, _ = _parse("official_mageia_9_x86_64")
        assert info.version == "9"
        assert info.arch == "x86_64"
        assert info.branch == "Official"

    def test_eight_media_sections_discovered(self):
        # core/release, core/updates, nonfree/release, nonfree/updates,
        # tainted/release, tainted/updates, core/backports,
        # core/backports_testing
        _, media = _parse("official_mageia_9_x86_64")
        assert len(media) == 8

    def test_section_names_preserved_in_media_records(self):
        _, media = _parse("official_mageia_9_x86_64")
        section_set = {m.section for m in media}
        assert "core/release" in section_set
        assert "nonfree/updates" in section_set
        assert "tainted/release" in section_set
        assert "core/backports" in section_set

    def test_display_names_carried_from_name_field(self):
        _, media = _parse("official_mageia_9_x86_64")
        names = {m.section: m.name for m in media}
        assert names["core/release"] == "Core Release"
        assert names["nonfree/updates"] == "Nonfree Updates"
        assert names["tainted/release"] == "Tainted Release"

    def test_update_flag_set_only_on_updates_sections(self):
        _, media = _parse("official_mageia_9_x86_64")
        updates = {m.section for m in media if m.is_update}
        assert updates == {"core/updates", "nonfree/updates", "tainted/updates"}

    def test_noauto_marks_backports(self):
        _, media = _parse("official_mageia_9_x86_64")
        noauto = {m.section for m in media if m.noauto}
        assert "core/backports" in noauto
        assert "core/backports_testing" in noauto

    def test_is_official_inferred_from_media_type(self):
        _, media = _parse("official_mageia_9_x86_64")
        assert all(m.is_official for m in media)


# ── Custom community repo (mgabiz-like) ──────────────────────────────────


class TestCustomSignedMgabiz:
    """Single-media community catalogue."""

    def test_one_media_discovered(self):
        _, media = _parse("custom_signed_mgabiz")
        assert len(media) == 1

    def test_branch_is_mgabiz(self):
        info, _ = _parse("custom_signed_mgabiz")
        assert info.branch == "mgabiz"

    def test_not_marked_official(self):
        _, media = _parse("custom_signed_mgabiz")
        assert media[0].is_official is False


# ── MLO catalogue with empty arch= in [media_info] ───────────────────────


class TestMloArchEmpty:
    """Real-world catalogue where ``[media_info].arch`` is empty.

    Verifies the parser does NOT explode on the empty value — the
    arch fallback is the caller's responsibility (extracted from the
    URL or another source per the audit's cascade rule).
    """

    def test_parses_without_error(self):
        info, media = _parse("mlo_arch_empty")
        assert info.version == "9"
        assert info.arch == ""  # the well-documented MLO quirk
        assert info.branch == "MLO"

    def test_five_media_discovered(self):
        # core, nonfree, tainted, backport, testing
        _, media = _parse("mlo_arch_empty")
        assert len(media) == 5

    def test_display_names_carry_mlo_prefix(self):
        _, media = _parse("mlo_arch_empty")
        names = {m.section: m.name for m in media}
        assert names["core"] == "MLO_core"
        assert names["nonfree"] == "MLO_nonfree"
        assert names["tainted"] == "MLO_tainted"

    def test_backport_and_testing_marked_noauto(self):
        _, media = _parse("mlo_arch_empty")
        noauto_sections = {m.section for m in media if m.noauto}
        assert noauto_sections == {"backport", "testing"}

    def test_not_marked_official(self):
        _, media = _parse("mlo_arch_empty")
        assert all(m.is_official is False for m in media)


# ── Cross-architecture catalogue ─────────────────────────────────────────


class TestMultiArch:
    """Catalogue with ``../../<arch>/media/<...>`` cross-arch references."""

    def test_five_media_discovered(self):
        _, media = _parse("multi_arch", media_root="10/x86_64/media")
        assert len(media) == 5

    def test_native_x86_64_arch(self):
        _, media = _parse("multi_arch", media_root="10/x86_64/media")
        native = [m for m in media if m.section == "core/release"]
        assert len(native) == 1
        assert native[0].architecture == "x86_64"

    def test_cross_arch_i586_detected_as_32bit(self):
        _, media = _parse("multi_arch", media_root="10/x86_64/media")
        cross = [m for m in media
                 if m.section == "../../i586/media/core/release"]
        assert len(cross) == 1
        assert cross[0].architecture == "i586"
        assert cross[0].is_32bit is True

    def test_cross_arch_aarch64(self):
        _, media = _parse("multi_arch", media_root="10/x86_64/media")
        cross = [m for m in media
                 if m.section == "../../aarch64/media/core/release"]
        assert len(cross) == 1
        assert cross[0].architecture == "aarch64"
        assert cross[0].is_32bit is False


# ── Catalogue without name= fields ───────────────────────────────────────


class TestNoNameField:
    """Sections without ``name=`` must still produce a non-empty display name
    via the parser's Title-Cased fallback (``_make_display_name``).
    """

    def test_all_media_carry_a_non_empty_name(self):
        _, media = _parse("no_name_field")
        for m in media:
            assert m.name, (
                f"section {m.section!r} has empty name; the parser's "
                f"_make_display_name fallback should always produce one"
            )

    def test_names_are_not_ugly(self):
        # The Title-Cased fallback should produce things like
        # "Core Release", not "core/release" or "core_release".
        _, media = _parse("no_name_field")
        for m in media:
            assert "/" not in m.name
            assert not m.name.islower()


# ── Degenerate catalogue ─────────────────────────────────────────────────


class TestEmptyCatalog:
    """Valid catalogue with only ``[media_info]``."""

    def test_parses_with_zero_media(self):
        info, media = _parse("empty_catalog")
        assert info.version == "10"
        assert info.arch == "x86_64"
        assert info.branch == "Empty"
        assert media == []


# ── Malformed input ──────────────────────────────────────────────────────


class TestMalformedCatalog:
    """Non-INI input must raise rather than silently produce nothing."""

    def test_raises_configparser_error(self):
        content = load_media_cfg("malformed")
        with pytest.raises(configparser.Error):
            parse_media_cfg(content, "9/x86_64/media")


# ── Loader-level sanity ──────────────────────────────────────────────────


def test_load_media_cfg_unknown_name_raises():
    """``load_media_cfg('nonsense')`` must surface a FileNotFoundError
    with a useful listing of available fixtures."""
    with pytest.raises(FileNotFoundError) as exc_info:
        load_media_cfg("nonsense_does_not_exist")
    # The error message lists what's available — useful when a typo
    # creeps into a test.
    assert "official_mageia_9_x86_64" in str(exc_info.value)
