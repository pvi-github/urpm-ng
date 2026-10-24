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

    def test_with_include(self, spec_dir, monkeypatch):
        """A spec can include another file.  This proves the include is

        1. found when it sits in the ``SOURCES`` alongside ``SPECS``,
        2. not taken from a file of the same name in the generic
           ``~/rpmbuild/SOURCES``.

        The second half is the one that matters at build time: the
        sources of the package being built are the ones next to it, and
        a stale namesake in the caller's home must not stand in.
        """
        from urpm.core.buildrequires import parse_buildrequires_from_spec

        # Keep the generic rpmbuild/SOURCES used below inside tmp_path:
        # rpmspec derives %_sourcedir from $HOME just as Path.home() does.
        monkeypatch.setenv("HOME", str(spec_dir))

        # Create tree like rpmbuild
        (spec_dir / "SPECS").mkdir(exist_ok=True)
        spec = _write_spec(
            spec_dir / "SPECS",
            "BuildRequires: gcc make\n"
            "Source1: test.spec.include\n"
            "%include %{SOURCE1}\n"
        )
        # rpmspec fails to find included file
        with pytest.raises(RuntimeError):
            parse_buildrequires_from_spec(spec)

        # when included file is found
        (spec_dir / "SOURCES").mkdir(exist_ok=True)
        include = (spec_dir / "SOURCES" / "test.spec.include")
        include.write_text("BuildRequires: gdb\n")
        got = parse_buildrequires_from_spec(spec)
        assert "gdb" in got
        assert "make" in got
        assert "gcc" in got

        # check that the command still fails when the included file is
        # in rpmbuild generic place
        sources_dir = Path.home() / "rpmbuild/SOURCES/"
        sources_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(include, sources_dir / "test.spec.include")
        include.unlink()
        with pytest.raises(RuntimeError):
            parse_buildrequires_from_spec(spec)

    def test_with_include_outside_a_rpmbuild_tree(self, spec_dir, monkeypatch):
        """A spec sitting on its own resolves its include from %_sourcedir.

        ``find_spec_in_workdir`` accepts a spec in the current
        directory as well as one under ``SPECS/``, and that layout has
        no sibling ``SOURCES`` to point at: the include can only come
        from the default ``%_sourcedir``, which is where it is put
        here.
        """
        from urpm.core.buildrequires import parse_buildrequires_from_spec

        monkeypatch.setenv("HOME", str(spec_dir))
        default_sources = spec_dir / "rpmbuild" / "SOURCES"
        default_sources.mkdir(parents=True)
        (default_sources / "sut.inc").write_text("BuildRequires: gdb\n")

        flat = spec_dir / "work"
        flat.mkdir()
        spec = _write_spec(
            flat,
            "BuildRequires: gcc\n"
            "Source1: sut.inc\n"
            "%include %{SOURCE1}\n"
        )

        got = parse_buildrequires_from_spec(spec)
        assert "gdb" in got
        assert "gcc" in got

    def test_a_spec_named_from_inside_specs_finds_its_sources(
            self, spec_dir, monkeypatch):
        """``cd SPECS && urpm build foo.spec`` is the same build tree.

        The name alone says nothing about where it sits, so the path is
        made absolute before the tree is recognised; otherwise this
        invocation loses the include while the equivalent one from the
        tree root finds it.
        """
        from urpm.core.buildrequires import parse_buildrequires_from_spec

        monkeypatch.setenv("HOME", str(spec_dir))
        (spec_dir / "SPECS").mkdir()
        (spec_dir / "SOURCES").mkdir()
        (spec_dir / "SOURCES" / "sut.inc").write_text("BuildRequires: gdb\n")
        _write_spec(
            spec_dir / "SPECS",
            "BuildRequires: gcc\n"
            "Source1: sut.inc\n"
            "%include %{SOURCE1}\n"
        )

        monkeypatch.chdir(spec_dir / "SPECS")
        got = parse_buildrequires_from_spec(Path("sut.spec"))
        assert "gdb" in got
        assert "gcc" in got

    def test_a_spec_linked_into_specs_takes_the_trees_sources(
            self, spec_dir, monkeypatch):
        """A spec symlinked into ``SPECS/`` belongs to the tree it is in.

        Packagers keep the spec in a checkout and link it into the
        build tree; the sources it names are the tree's, so the link is
        deliberately not followed when deciding where %_sourcedir
        points.
        """
        from urpm.core.buildrequires import parse_buildrequires_from_spec

        monkeypatch.setenv("HOME", str(spec_dir))
        checkout = spec_dir / "checkout"
        checkout.mkdir()
        real_spec = _write_spec(
            checkout,
            "BuildRequires: gcc\n"
            "Source1: sut.inc\n"
            "%include %{SOURCE1}\n"
        )
        (spec_dir / "SPECS").mkdir()
        (spec_dir / "SOURCES").mkdir()
        (spec_dir / "SOURCES" / "sut.inc").write_text("BuildRequires: gdb\n")
        link = spec_dir / "SPECS" / "sut.spec"
        link.symlink_to(real_spec)

        got = parse_buildrequires_from_spec(link)
        assert "gdb" in got
        assert "gcc" in got
