"""Tests for resolver NEVRA-aware helpers."""

import pytest

from urpm.core.resolver import _is_nevra, _name_from_nevra


class TestIsNevra:
    """Tests for NEVRA detection."""

    @pytest.mark.parametrize("input_str", [
        "firefox",
        "lib64-foo",
        "gdk-pixbuf2.0",
    ])
    def test_simple_names_are_not_nevra(self, input_str):
        assert _is_nevra(input_str) is False

    @pytest.mark.parametrize("input_str", [
        "firefox*",
        "lib64*",
        "gstreamer1.0-plugins-?",
    ])
    def test_globs_are_not_nevra(self, input_str):
        assert _is_nevra(input_str) is False

    @pytest.mark.parametrize("input_str", [
        "firefox >= 130",
        "python-docs == 3.11",
    ])
    def test_version_constraints_are_not_nevra(self, input_str):
        assert _is_nevra(input_str) is False

    @pytest.mark.parametrize("input_str,expected_name", [
        ("firefox-130.0-1.mga10.x86_64", "firefox"),
        ("lib64pipewire0.3_0-1.6.2-2.mga10.x86_64", "lib64pipewire0.3_0"),
        ("gstreamer1.0-plugins-bad-1.26.11-2.mga10.tainted.x86_64", "gstreamer1.0-plugins-bad"),
        ("python-docs-3.11-1.mga9.noarch", "python-docs"),
        ("glibc-2.38-1.mga9.i586", "glibc"),
        ("task-xfce-1.0-1.mga10.x86_64", "task-xfce"),
        ("a-1.0-1.mga10.x86_64", "a"),
    ])
    def test_valid_nevras(self, input_str, expected_name):
        assert _is_nevra(input_str) is True
        assert _name_from_nevra(input_str) == expected_name


class TestNameFromNevra:
    """Tests for NEVRA name extraction."""

    @pytest.mark.parametrize("input_str,expected", [
        ("firefox-130.0-1.mga10.x86_64", "firefox"),
        ("lib64pipewire0.3_0-1.6.2-2.mga10.x86_64", "lib64pipewire0.3_0"),
        ("gstreamer1.0-plugins-bad-1.26.11-2.mga10.tainted.x86_64", "gstreamer1.0-plugins-bad"),
        ("php8.4-fpm-8.4.5-1.mga10.x86_64", "php8.4-fpm"),
        ("faad2-2.11.2-2.mga10.tainted.x86_64", "faad2"),
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
        result = self._extract_explicit_names(["firefox-130.0-1.mga10.x86_64"])
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
            "firefox-130.0-1.mga10.x86_64",
            "vim",
            "python-docs >= 3.11",
        ])
        assert result == {"firefox", "vim", "python-docs"}
