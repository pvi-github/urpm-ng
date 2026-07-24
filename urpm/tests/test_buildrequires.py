"""Tests for urpm.core.buildrequires — spec BuildRequires extraction.

The critical property under test: a ``BuildRequires:`` sitting inside a
falsy ``%if`` block MUST NOT appear in the returned list.  A previous
regex-based implementation extracted every literal ``BuildRequires:``
line regardless of the surrounding conditional block, dragging packages
the spec author had explicitly guarded away and blowing up transactions
with unrelated deps of those unwanted packages.
"""

import shutil
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("rpmspec") is None,
    reason="rpmspec unavailable (rpm-build not installed)",
)


@pytest.fixture
def spec_dir(tmp_path):
    return tmp_path


def _write_spec(path: Path, body: str) -> Path:
    """Write a minimal-header spec followed by ``body`` and return the path.

    rpmspec refuses to parse a spec without at least Name/Version/Release/
    Summary/License, so every fixture spec grows the same header.
    """
    spec = path / "sut.spec"
    spec.write_text(
        "Name:      sut\n"
        "Version:   0\n"
        "Release:   1\n"
        "Summary:   test subject\n"
        "License:   Public Domain\n"
        f"{body}\n"
        "\n"
        "%description\n"
        "%prep\n"
        "%build\n"
        "%install\n"
        "%files\n",
        encoding="utf-8",
    )
    return spec


class TestParseBuildRequiresFromSpec:
    def test_plain_buildrequires_are_returned(self, spec_dir):
        from urpm.core.buildrequires import parse_buildrequires_from_spec
        spec = _write_spec(
            spec_dir,
            "BuildRequires: python3-devel\n"
            "BuildRequires: gcc",
        )
        got = parse_buildrequires_from_spec(spec)
        assert "python3-devel" in got
        assert "gcc" in got

    def test_falsy_if_block_is_ignored(self, spec_dir):
        """The exact bug case from BUG_buildrequires_parser_ignores_conditionals.md.

        A ``BuildRequires:`` guarded behind ``%if 0`` must be silently
        dropped — same behaviour as ``rpmspec -q --buildrequires`` and
        as rpmbuild itself at build time.
        """
        from urpm.core.buildrequires import parse_buildrequires_from_spec
        spec = _write_spec(
            spec_dir,
            "%global with_selinux 0\n"
            "%if 0%{?with_selinux}\n"
            "BuildRequires: selinux-policy-devel\n"
            "%endif",
        )
        got = parse_buildrequires_from_spec(spec)
        assert "selinux-policy-devel" not in got, (
            f"selinux-policy-devel leaked out of an %if 0 block: {got}")

    def test_truthy_if_block_keeps_the_br(self, spec_dir):
        """Symmetric of the falsy case: a BR under ``%if 1`` must appear."""
        from urpm.core.buildrequires import parse_buildrequires_from_spec
        spec = _write_spec(
            spec_dir,
            "%global with_selinux 1\n"
            "%if 0%{?with_selinux}\n"
            "BuildRequires: selinux-policy-devel\n"
            "%endif",
        )
        got = parse_buildrequires_from_spec(spec)
        assert "selinux-policy-devel" in got, (
            f"selinux-policy-devel dropped from an %if 1 block: {got}")

    def test_inline_flag_conditional_is_evaluated(self, spec_dir):
        """``%{?flag:BuildRequires: X}`` inline form: expanded only when
        ``flag`` is set.  Historically the safe workaround spec authors
        used to sneak past the buggy regex — must still work now that
        the parser is proper."""
        from urpm.core.buildrequires import parse_buildrequires_from_spec

        # Flag unset — BR must be absent.
        (spec_dir / "unset").mkdir(exist_ok=True)
        spec = _write_spec(
            spec_dir / "unset",
            "%{?with_selinux:BuildRequires: selinux-policy-devel}",
        )
        got_unset = parse_buildrequires_from_spec(spec)
        assert "selinux-policy-devel" not in got_unset

        # Flag set — BR must be present.
        (spec_dir / "set").mkdir(exist_ok=True)
        spec2 = _write_spec(
            spec_dir / "set",
            "%global with_selinux 1\n"
            "%{?with_selinux:BuildRequires: selinux-policy-devel}",
        )
        got_set = parse_buildrequires_from_spec(spec2)
        assert "selinux-policy-devel" in got_set

    def test_missing_file_raises(self, spec_dir):
        from urpm.core.buildrequires import parse_buildrequires_from_spec
        with pytest.raises(FileNotFoundError):
            parse_buildrequires_from_spec(spec_dir / "does-not-exist.spec")
