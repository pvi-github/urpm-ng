"""Tests for :func:`urpm.cli.helpers.media.parse_custom_media_url`.

Regression harness for the file:// duplication bug — when the
custom-URL parser and the server-split heuristic disagreed on where
base_path ends, the reconstructed download URL doubled every pre-
version segment (e.g. ``.../mgabiz/home/superadmin/mgabiz/9/...``).
"""

from __future__ import annotations

from urpm.cli.helpers.media import parse_custom_media_url


class TestFileURL:
    def test_mageia_style_layout_splits_base_and_relative(self):
        """file:// URL with a `<version>/<arch>` segment pair splits.

        server.base_path holds everything BEFORE `9/`, media.relative_path
        holds the tail — no overlap when the two are concatenated
        later, so ``server + relative + media_info/synthesis.hdlist.cz``
        stays sane.
        """
        parsed = parse_custom_media_url(
            "file:///home/superadmin/mgabiz/9/x86_64/media/urpm/testing")
        assert parsed is not None
        assert parsed["protocol"] == "file"
        assert parsed["host"] == ""
        assert parsed["base_path"] == "/home/superadmin/mgabiz"
        assert parsed["relative_path"] == "9/x86_64/media/urpm/testing"
        assert parsed["version"] == "9"
        assert parsed["arch"] == "x86_64"

    def test_no_mageia_pattern_falls_back_to_empty_base(self):
        """Without `<version>/<arch>`, base stays empty (legacy path)."""
        parsed = parse_custom_media_url(
            "file:///opt/some/weird/tree")
        assert parsed is not None
        assert parsed["base_path"] == ""
        assert parsed["relative_path"] == "opt/some/weird/tree"
        assert parsed["version"] is None

    def test_at_root_no_prefix(self):
        parsed = parse_custom_media_url(
            "file:///9/x86_64/media/urpm/testing")
        assert parsed is not None
        assert parsed["base_path"] == ""
        assert parsed["relative_path"] == "9/x86_64/media/urpm/testing"


class TestHTTPURL:
    def test_https_with_mageia_layout(self):
        parsed = parse_custom_media_url(
            "https://mirror.example.org/mageia/9/x86_64/media/urpm/testing")
        assert parsed is not None
        assert parsed["protocol"] == "https"
        assert parsed["host"] == "mirror.example.org"
        assert parsed["base_path"] == "/mageia"
        assert parsed["relative_path"] == "9/x86_64/media/urpm/testing"

    def test_https_without_mageia_layout(self):
        parsed = parse_custom_media_url(
            "https://example.com/repo/x86_64/")
        assert parsed is not None
        assert parsed["base_path"] == ""
        assert parsed["relative_path"] == "repo/x86_64"


class TestReconstructionInvariant:
    """server.base_path + '/' + media.relative_path == URL path."""

    def test_file_roundtrip(self):
        original = "file:///home/superadmin/mgabiz/9/x86_64/media/urpm/testing"
        parsed = parse_custom_media_url(original)
        rebuilt_path = (
            parsed["base_path"] + "/" + parsed["relative_path"]
        ).replace("//", "/")
        assert rebuilt_path == original[len("file://"):]

    def test_https_roundtrip(self):
        original = "https://mirror.example.org/mageia/9/x86_64/media/core/release"
        parsed = parse_custom_media_url(original)
        rebuilt = (
            f"{parsed['protocol']}://{parsed['host']}"
            f"{parsed['base_path']}/{parsed['relative_path']}"
        )
        assert rebuilt == original
