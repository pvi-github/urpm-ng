"""URL variant handling — bare vs prefixed version segments.

Covers the three real-world layouts urpm-ng meets in the wild :

* **Officiels** — ``.../<version>/<arch>/media/<class>/<type>`` with
  bare numeric version and arch consecutive.
* **Blogdrake plate** — ``.../mageia<version>/<channel>/<arch>``
  with a prefixed version segment separated from the arch by the
  channel.
* **Blogdrake via media.cfg** — ``.../mageia<version>/<arch>/media``
  with the prefixed version and consecutive arch (closer to the
  officiel shape but still prefixed).

Every helper the media pipeline exposes must handle all three ; a
regression on any variant re-triggers the silent-drop-style
symptoms we hit at 0.9.4 (community media never quite added, never
correctly transposed through distupgrade).
"""

from __future__ import annotations

from urpm.cli.helpers.media import (
    _match_version_token as cli_match_version_token,
    parse_custom_media_url,
)
from urpm.core.distupgrade.stage1 import _try_transpose_string
from urpm.core.media_cfg import decompose_url
from urpm.core.media_pipeline import (
    _extract_version_from_url,
    _match_version_token as core_match_version_token,
    _split_url,
)


# ── version token detection ──────────────────────────────────────────


class TestMatchVersionToken:
    def test_bare_numeric(self):
        assert core_match_version_token("10") == "10"

    def test_bare_cauldron(self):
        assert core_match_version_token("cauldron") == "cauldron"

    def test_mageia_prefix(self):
        assert core_match_version_token("mageia10") == "10"

    def test_mga_prefix(self):
        assert core_match_version_token("mga9") == "9"

    def test_prefix_case_insensitive(self):
        assert core_match_version_token("Mageia10") == "10"
        assert core_match_version_token("MGA9") == "9"

    def test_random_word_rejected(self):
        assert core_match_version_token("free") is None
        assert core_match_version_token("distrib") is None

    def test_empty_rejected(self):
        assert core_match_version_token("") is None

    def test_cli_helper_filters_unknown_versions(self):
        # The CLI-side helper additionally guards against random
        # numeric segments (KNOWN_VERSIONS filter) so a stray
        # ``mageia42`` does not slip through as a version.
        assert cli_match_version_token("mageia42") is None
        assert cli_match_version_token("mageia10") == "10"


# ── _split_url (core) ────────────────────────────────────────────────


class TestSplitUrl:
    def test_officiel_layout(self):
        proto, host, base = _split_url(
            "https://mirror.example.org/pub/mageia/distrib/10/x86_64/"
            "media/core/release"
        )
        assert (proto, host, base) == (
            "https", "mirror.example.org", "/pub/mageia/distrib",
        )

    def test_blogdrake_plate(self):
        proto, host, base = _split_url(
            "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64"
        )
        # Pivot is the first of {version_idx, arch_idx} — here the
        # prefixed version at index 1 comes before the arch at 3.
        assert (proto, host, base) == (
            "https", "ftp.blogdrake.org", "/mageia",
        )

    def test_blogdrake_via_media_cfg(self):
        proto, host, base = _split_url(
            "https://ftp.blogdrake.org/mageia/mageia10/x86_64/media"
        )
        assert (proto, host, base) == (
            "https", "ftp.blogdrake.org", "/mageia",
        )

    def test_no_pattern_leaves_base_empty(self):
        proto, host, base = _split_url(
            "https://example.org/some/random/path"
        )
        assert base == ""

    def test_multiple_version_segments_bail_out(self):
        # Two version-looking segments — the guard bails out rather
        # than guessing.  Made-up path but exercises the safety net.
        proto, host, base = _split_url(
            "https://example.org/mageia10/9/x86_64/media"
        )
        assert base == ""


# ── _extract_version_from_url (core) ─────────────────────────────────


class TestExtractVersionFromUrl:
    def test_officiel(self):
        assert _extract_version_from_url(
            "https://mirror.example.org/distrib/10/x86_64/media/core/release"
        ) == "10"

    def test_blogdrake_plate(self):
        assert _extract_version_from_url(
            "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64"
        ) == "10"

    def test_blogdrake_via_media_cfg(self):
        assert _extract_version_from_url(
            "https://ftp.blogdrake.org/mageia/mageia10/x86_64/media"
        ) == "10"

    def test_cauldron_bare(self):
        assert _extract_version_from_url(
            "https://mirror.example.org/distrib/cauldron/x86_64/media/core/release"
        ) == "cauldron"


# ── parse_custom_media_url (cli) ─────────────────────────────────────


class TestParseCustomMediaUrl:
    def test_blogdrake_plate_detects_both(self):
        parsed = parse_custom_media_url(
            "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64"
        )
        assert parsed is not None
        assert parsed["version"] == "10"
        assert parsed["arch"] == "x86_64"
        assert parsed["base_path"] == "/mageia"
        assert parsed["relative_path"] == "mageia10/free/x86_64"
        assert parsed["is_official"] is False

    def test_blogdrake_via_media_cfg_detects_both(self):
        parsed = parse_custom_media_url(
            "https://ftp.blogdrake.org/mageia/mageia10/x86_64/media"
        )
        assert parsed["version"] == "10"
        assert parsed["arch"] == "x86_64"
        assert parsed["base_path"] == "/mageia"
        assert parsed["relative_path"] == "mageia10/x86_64/media"

    def test_official_style_still_works(self):
        parsed = parse_custom_media_url(
            "https://mirror.example.org/distrib/10/x86_64/media/core/release"
        )
        assert parsed["version"] == "10"
        assert parsed["arch"] == "x86_64"
        assert parsed["base_path"] == "/distrib"
        assert parsed["relative_path"] == "10/x86_64/media/core/release"

    def test_no_hint_leaves_version_and_arch_none(self):
        parsed = parse_custom_media_url("file:///home/user/local/rpms")
        assert parsed["version"] is None
        assert parsed["arch"] is None
        assert parsed["base_path"] == ""


# ── _try_transpose_string (distupgrade Stage 1) ──────────────────────


class TestTryTransposeString:
    def test_mageia_prefix_bumped(self):
        assert _try_transpose_string(
            "mageia/mageia9/free/x86_64", "9", "10",
        ) == "mageia/mageia10/free/x86_64"

    def test_mga_disttag_still_bumped(self):
        assert _try_transpose_string(
            "some-media-mga9-thing", "9", "10",
        ) == "some-media-mga10-thing"

    def test_bare_segment_still_bumped(self):
        assert _try_transpose_string(
            "distrib/9/x86_64/media/core/release", "9", "10",
        ) == "distrib/10/x86_64/media/core/release"

    def test_mageia_prefix_wins_over_mga_substring(self):
        # Belt-and-braces : substring ``mga9`` appears inside
        # ``mageia9``.  We want the prefixed rewrite ``mageia9`` →
        # ``mageia10``, not a partial ``mga9`` → ``mga10`` that
        # would leave ``mageia10`` looking like ``maga10``.
        result = _try_transpose_string("path/mageia9/free", "9", "10")
        assert result == "path/mageia10/free"
        assert "maga" not in result

    def test_no_marker_returns_none(self):
        assert _try_transpose_string(
            "no-version-here", "9", "10",
        ) is None


# ── decompose_url (media discover) ───────────────────────────────────


class TestDecomposeUrl:
    """``decompose_url`` splits an officiel-shaped media root URL into
    ``(scheme, host, base_path, media_root)``.  The version segment
    in the URL may be prefixed (``mageia10``) — the split must
    preserve that token verbatim so downstream ``relative_path``
    reconstruction from ``media.cfg`` sections
    (``[../../free/x86_64]`` → ``mageia10/free/x86_64``) keeps the
    correct URL prefix and the follow-up ``urpm media update``
    finds the synthesis at the right place.
    """

    def test_officiel_layout(self):
        assert decompose_url(
            "https://mirror.example.org/pub/Mageia/distrib/10/x86_64/media",
            "10", "x86_64",
        ) == ("https", "mirror.example.org",
              "/pub/Mageia/distrib", "10/x86_64/media")

    def test_blogdrake_via_media_cfg_prefixed_version(self):
        # Regression : the substring match used to hit ``10`` inside
        # ``mageia10``, producing base_path ``/mageia/mageia`` and
        # media_root ``10/x86_64/media`` — which then rebuilt every
        # media URL without the ``mageia`` prefix and 404'd at update
        # time.  We now match segment-wise on {version,
        # mageia<version>, mga<version>} and keep the actual URL
        # token.
        assert decompose_url(
            "https://ftp.blogdrake.org/mageia/mageia10/x86_64/media",
            "10", "x86_64",
        ) == ("https", "ftp.blogdrake.org",
              "/mageia", "mageia10/x86_64/media")

    def test_blogdrake_trailing_slash(self):
        assert decompose_url(
            "https://ftp.blogdrake.org/mageia/mageia10/x86_64/media/",
            "10", "x86_64",
        ) == ("https", "ftp.blogdrake.org",
              "/mageia", "mageia10/x86_64/media")

    def test_no_pattern_falls_back_to_full_path(self):
        # When the URL doesn't carry the expected shape, base_path
        # stays empty and media_root captures the whole path so the
        # caller can decide.
        scheme, host, base, root = decompose_url(
            "https://example.org/some/random", "10", "x86_64",
        )
        assert base == ""
        assert root == "some/random"

    def test_mgabiz_cauldron_during_freeze(self):
        # Regression : mgabiz publishes at ``.../cauldron/x86_64/media/``
        # but its media.cfg carries the target numeric ``version=11``
        # (freeze period).  Before the fix, ``decompose_url`` looked
        # for {11, mageia11, mga11} in the path, missed ``cauldron``,
        # left base_path empty and folded ``/repo/Mageia/mgabiz`` into
        # every media's relative_path — every URL reconstruction then
        # doubled it.  The ``cauldron`` candidate is always added so
        # the anchor lands on the right segment.
        assert decompose_url(
            "https://www.mageia.biz/repo/Mageia/mgabiz/cauldron/x86_64/media/",
            "11", "x86_64",
        ) == ("https", "www.mageia.biz",
              "/repo/Mageia/mgabiz", "cauldron/x86_64/media")

    def test_official_cauldron_when_version_matches(self):
        # Sanity : the classic cauldron URL where media.cfg also
        # says ``version=cauldron`` keeps working — the ``cauldron``
        # candidate would match anyway.
        assert decompose_url(
            "https://mirror.example.org/pub/Mageia/distrib/cauldron/x86_64/media",
            "cauldron", "x86_64",
        ) == ("https", "mirror.example.org",
              "/pub/Mageia/distrib", "cauldron/x86_64/media")
