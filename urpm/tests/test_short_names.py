"""Unit tests for :mod:`urpm.cli.helpers.short_names`.

Verifies the blgrk compression rule on the canonical examples
agreed with the user, the ambiguous-host disambiguation table, and
the end-to-end name generation for the four real-world layouts
urpm-ng meets (blogdrake, MLO, mageia.org / .biz, offi mirrors).
"""

from __future__ import annotations

from urpm.cli.helpers.short_names import (
    AMBIGUOUS_HOSTS,
    blgrk,
    extract_channel,
    extract_server_names,
    generate_media_names,
)


# ── blgrk ────────────────────────────────────────────────────────────


class TestBlgrk:
    def test_blogdrake(self):
        assert blgrk("blogdrake") == "blgrk"

    def test_mageialinux(self):
        assert blgrk("mageialinux") == "mglnx"

    def test_online(self):
        assert blgrk("online") == "nln"

    def test_coffee(self):
        assert blgrk("coffee") == "cff"

    def test_distrib(self):
        assert blgrk("distrib") == "dsrb"

    def test_mageia(self):
        assert blgrk("mageia") == "mg"

    def test_dashed_segments_kept_separate(self):
        assert blgrk("mageialinux-online") == "mglnx-nln"
        assert blgrk("distrib-coffee") == "dsrb-cff"

    def test_empty(self):
        assert blgrk("") == ""

    def test_case_insensitive(self):
        assert blgrk("Blogdrake") == blgrk("blogdrake") == "blgrk"


# ── extract_server_names ─────────────────────────────────────────────


class TestExtractServerNames:
    def test_ftp_prefix_stripped(self):
        assert extract_server_names("ftp.blogdrake.org") == (
            "Blogdrake", "blgrk",
        )

    def test_www_prefix_stripped(self):
        assert extract_server_names("www.mageialinux-online.org") == (
            "Mageialinux-Online", "mglnx-nln",
        )

    def test_no_prefix_kept_as_is(self):
        assert extract_server_names("mageialinux-online.org") == (
            "Mageialinux-Online", "mglnx-nln",
        )

    def test_ambiguous_mageia_org_canonical(self):
        long_, short = extract_server_names("mageia.org")
        assert long_ == "Mageia.Org"
        # canonical TLD dropped from short form
        assert short == "mga"

    def test_ambiguous_mageia_biz_disambiguated(self):
        long_, short = extract_server_names("mageia.biz")
        assert long_ == "Mageia.Biz"
        assert short == "mgabiz"

    def test_distrib_coffee_first_segment(self):
        assert extract_server_names(
            "distrib-coffee.ipsl.jussieu.fr",
        ) == ("Distrib-Coffee", "dsrb-cff")

    def test_empty_hostname(self):
        assert extract_server_names("") == ("", "")

    def test_ambiguous_hosts_table_is_extensible(self):
        # Guard : the entry shape must expose both keys the caller
        # relies on, so future additions can't accidentally omit one.
        for entry in AMBIGUOUS_HOSTS.values():
            assert "canonical_tld" in entry
            assert "short_mnemonic" in entry


# ── extract_channel ──────────────────────────────────────────────────


class TestExtractChannel:
    def test_blogdrake_plate(self):
        assert extract_channel(
            "mageia10/free/x86_64", version="10", arch="x86_64",
        ) == "free"

    def test_blogdrake_multi_channel(self):
        assert extract_channel(
            "mageia10/nonfree/updates/x86_64", "10", "x86_64",
        ) == "nonfree-updates"

    def test_official_after_media_marker(self):
        assert extract_channel(
            "10/x86_64/media/core/release", "10", "x86_64",
        ) == "core-release"

    def test_official_nonfree_updates(self):
        assert extract_channel(
            "10/x86_64/media/nonfree/updates", "10", "x86_64",
        ) == "nonfree-updates"

    def test_no_channel_returns_empty(self):
        # Only version + arch, no channel segment.
        assert extract_channel(
            "mageia10/x86_64", "10", "x86_64",
        ) == ""

    def test_empty_input(self):
        assert extract_channel("", None, None) == ""


# ── generate_media_names ─────────────────────────────────────────────


class TestGenerateMediaNames:
    def test_blogdrake_current_release_and_arch(self):
        result = generate_media_names(
            "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64/",
            current_release="10",
            primary_arch="x86_64",
        )
        assert result["name"] == "Blogdrake_Free"
        assert result["short_name"] == "blgrk_free"
        assert result["version"] == "10"
        assert result["arch"] == "x86_64"
        assert result["channel"] == "free"

    def test_blogdrake_secondary_arch(self):
        result = generate_media_names(
            "https://ftp.blogdrake.org/mageia/mageia10/free/i586/",
            current_release="10",
            primary_arch="x86_64",
        )
        assert result["short_name"] == "blgrk_i586_free"
        assert result["name"] == "Blogdrake_i586_Free"

    def test_blogdrake_different_release(self):
        result = generate_media_names(
            "https://ftp.blogdrake.org/mageia/mageia9/free/i586/",
            current_release="10",
            primary_arch="x86_64",
        )
        assert result["short_name"] == "blgrk_9_i586_free"
        assert result["name"] == "Blogdrake_9_i586_Free"

    def test_mageia_org_direct(self):
        result = generate_media_names(
            "https://mageia.org/some/10/x86_64/media/core/release",
            current_release="10",
            primary_arch="x86_64",
        )
        # Dash inside a channel block is preserved ; the ``_`` is
        # only the inter-block separator.
        assert result["short_name"] == "mga_core-release"
        assert result["name"] == "Mageia.Org_Core-Release"

    def test_mageia_biz_disambiguates_tld(self):
        result = generate_media_names(
            "https://mageia.biz/repo/10/x86_64/media/core/release",
            current_release="10",
            primary_arch="x86_64",
        )
        assert result["short_name"] == "mgabiz_core-release"
        assert result["name"] == "Mageia.Biz_Core-Release"

    def test_override_name(self):
        result = generate_media_names(
            "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64/",
            current_release="10", primary_arch="x86_64",
            override_name="My Custom Name",
        )
        assert result["name"] == "My Custom Name"
        assert result["short_name"] == "blgrk_free"  # not overridden

    def test_override_shortname(self):
        result = generate_media_names(
            "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64/",
            current_release="10", primary_arch="x86_64",
            override_shortname="bdk",
        )
        assert result["name"] == "Blogdrake_Free"
        assert result["short_name"] == "bdk"

    def test_url_without_version_arch_still_produces_server(self):
        # Fallback : no version/arch detected in path — name still
        # carries the server identity, callers can decide what to do
        # from there (probably error out, but the module doesn't).
        result = generate_media_names(
            "file:///home/user/local/rpms",
            current_release="10", primary_arch="x86_64",
        )
        assert result["version"] is None
        assert result["arch"] is None
