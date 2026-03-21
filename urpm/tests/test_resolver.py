"""Tests for resolver NEVRA-aware helpers.

Tests pure string parsing logic — no dependency on installed packages
or real versions. All version numbers are fictional.
"""

import pytest

from urpm.core.resolver import _is_nevra, _name_from_nevra


class TestIsNevra:
    """Tests for NEVRA detection.

    A valid NEVRA must have: name-version-release.arch
    where arch is an explicit known suffix (.x86_64, .noarch, .i586, etc.)
    """

    @pytest.mark.parametrize("input_str", [
        # Simple names (no .arch suffix → never a NEVRA)
        "firefox",
        "lib64-foo",
        "python3-dbus",
        "xdg-utils",
        "task-xfce",
        # Multi-dash names that could fool a naive parser
        "urpm-ng-all",
        "urpm-ng-cli",
        "perl-File-Temp",
        "mesa-common-devel",
        # Dot in name but not an arch
        "gdk-pixbuf2.0",
        "gstreamer1.0-plugins-bad",
    ])
    def test_simple_names_are_not_nevra(self, input_str):
        assert _is_nevra(input_str) is False

    @pytest.mark.parametrize("input_str", [
        "firefox*",
        "lib64*",
        "gstreamer1.0-plugins-?",
        "python3-[abc]*",
    ])
    def test_globs_are_not_nevra(self, input_str):
        assert _is_nevra(input_str) is False

    @pytest.mark.parametrize("input_str", [
        "firefox >= 130",
        "python-docs == 3.11",
        "glibc < 3.0",
    ])
    def test_version_constraints_are_not_nevra(self, input_str):
        assert _is_nevra(input_str) is False

    @pytest.mark.parametrize("input_str,expected_name", [
        # Standard NEVRA patterns (fictional versions)
        ("firefox-1.0-1.mga10.x86_64", "firefox"),
        ("a-1.0-1.mga10.x86_64", "a"),
        # Multi-dash package names
        ("urpm-ng-all-1.0-1.mga10.x86_64", "urpm-ng-all"),
        ("perl-File-Temp-1.0-1.mga10.noarch", "perl-File-Temp"),
        ("mesa-common-devel-1.0-1.mga10.x86_64", "mesa-common-devel"),
        ("task-xfce-1.0-1.mga10.x86_64", "task-xfce"),
        ("gstreamer1.0-plugins-bad-1.0-1.mga10.x86_64", "gstreamer1.0-plugins-bad"),
        # lib64 prefix
        ("lib64pipewire0.3_0-1.0-1.mga10.x86_64", "lib64pipewire0.3_0"),
        # Tainted release tag
        ("faad2-1.0-1.mga10.tainted.x86_64", "faad2"),
        ("gstreamer1.0-plugins-bad-1.0-1.mga10.tainted.x86_64", "gstreamer1.0-plugins-bad"),
        # All supported architectures
        ("pkg-1.0-1.mga10.x86_64", "pkg"),
        ("pkg-1.0-1.mga10.i586", "pkg"),
        ("pkg-1.0-1.mga10.i686", "pkg"),
        ("pkg-1.0-1.mga10.noarch", "pkg"),
        ("pkg-1.0-1.mga10.aarch64", "pkg"),
        ("pkg-1.0-1.mga10.armv7hl", "pkg"),
    ])
    def test_valid_nevras(self, input_str, expected_name):
        assert _is_nevra(input_str) is True
        assert _name_from_nevra(input_str) == expected_name


class TestNameFromNevra:
    """Tests for NEVRA name extraction on realistic Mageia naming patterns."""

    @pytest.mark.parametrize("input_str,expected", [
        # Simple name
        ("firefox-1.0-1.mga10.x86_64", "firefox"),
        # Dots in package name
        ("php8.4-fpm-1.0-1.mga10.x86_64", "php8.4-fpm"),
        # Multi-dash name
        ("gstreamer1.0-plugins-bad-1.0-1.mga10.x86_64", "gstreamer1.0-plugins-bad"),
        # Tainted release
        ("faad2-1.0-1.mga10.tainted.x86_64", "faad2"),
        # lib64 prefix with underscore and dots
        ("lib64pipewire0.3_0-1.0-1.mga10.x86_64", "lib64pipewire0.3_0"),
    ])
    def test_name_extraction(self, input_str, expected):
        assert _name_from_nevra(input_str) == expected


class TestExplicitNamesExtraction:
    """Tests for explicit_names construction logic (same as in resolve_install)."""

    def _extract_explicit_names(self, package_names):
        """Replicate the explicit_names logic from resolve_install."""
        import re
        explicit_names = set()
        for n in package_names:
            if _is_nevra(n):
                explicit_names.add(_name_from_nevra(n).lower())
            else:
                base = re.match(r'^(.+?)\s*[\[>=<]', n)
                explicit_names.add(base.group(1).lower() if base else n.lower())
        return explicit_names

    def test_nevra_extracts_name(self):
        result = self._extract_explicit_names(["firefox-1.0-1.mga10.x86_64"])
        assert "firefox" in result

    def test_simple_name(self):
        result = self._extract_explicit_names(["firefox"])
        assert "firefox" in result

    def test_space_constraint(self):
        result = self._extract_explicit_names(["firefox >= 130"])
        assert "firefox" in result

    def test_bracket_constraint(self):
        result = self._extract_explicit_names(["firefox[>= 130]"])
        assert "firefox" in result

    def test_mixed(self):
        result = self._extract_explicit_names([
            "firefox-1.0-1.mga10.x86_64",
            "vim",
            "python-docs >= 3.11",
        ])
        assert result == {"firefox", "vim", "python-docs"}
