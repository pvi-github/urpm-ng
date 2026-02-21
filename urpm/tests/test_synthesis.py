"""Tests for synthesis parser"""

from urpm.core.synthesis import parse_nevra, parse_dependency


class TestParseNevra:
    """Tests for NEVRA parsing."""

    def test_simple_nevra(self):
        name, version, release, arch = parse_nevra("firefox-120.0-1.mga9.x86_64")
        assert name == "firefox"
        assert version == "120.0"
        assert release == "1.mga9"
        assert arch == "x86_64"

    def test_name_with_dashes(self):
        name, version, release, arch = parse_nevra("lib64-foo-bar-1.2.3-4.mga9.x86_64")
        assert name == "lib64-foo-bar"
        assert version == "1.2.3"
        assert release == "4.mga9"
        assert arch == "x86_64"

    def test_noarch(self):
        name, version, release, arch = parse_nevra("python-docs-3.11-1.mga9.noarch")
        assert name == "python-docs"
        assert arch == "noarch"

    def test_i586(self):
        name, version, release, arch = parse_nevra("glibc-2.38-1.mga9.i586")
        assert arch == "i586"


class TestParseDependency:
    """Tests for dependency string parsing."""

    def test_simple_dep(self):
        name, op, ver = parse_dependency("libfoo")
        assert name == "libfoo"
        assert op == ""
        assert ver == ""

    def test_dep_with_bracket_version(self):
        name, op, ver = parse_dependency("libfoo[>= 1.0]")
        assert name == "libfoo"
        assert op == ">="
        assert ver == "1.0"

    def test_dep_with_inline_version(self):
        name, op, ver = parse_dependency("libbar>=2.5")
        assert name == "libbar"
        assert op == ">="
        assert ver == "2.5"

    def test_dep_with_equals(self):
        name, op, ver = parse_dependency("python[== 3.11]")
        assert name == "python"
        assert op == "=="
        assert ver == "3.11"
