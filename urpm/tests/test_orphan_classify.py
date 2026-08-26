"""Unit tests for :mod:`urpm.core.resolution.orphan_classify`.

Covers the pure-Python classification layer used by the interactive
orphans triage workflow.  Tests never touch librpm — ``OrphanInfo``
instances are built by hand so the same suite runs on any host,
including CI machines without rpm bindings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from urpm.core.resolution.orphan_classify import (
    CATEGORY_PREVIOUS_RELEASE,
    CATEGORY_SUBLIB,
    CATEGORY_USERLAND,
    OrphanInfo,
    classify_orphans,
    current_distmajor,
    disttag_major,
    is_previous_release_relic,
    is_soname_sublib,
    parse_disttag,
)


# --- Helpers --------------------------------------------------------------


def _mk(
    name: str,
    evr: str = "1.0-1.mga10",
    arch: str = "x86_64",
    provides: list = None,
) -> OrphanInfo:
    """Build an ``OrphanInfo`` with sensible defaults for tests."""
    return OrphanInfo(
        name=name,
        evr=evr,
        arch=arch,
        nevra=f"{name}-{evr}.{arch}",
        provides=provides or [name],
    )


# --- Disttag parsing ------------------------------------------------------


class TestParseDisttag:
    def test_mga10(self):
        assert parse_disttag("0.2-4.mga10") == "mga10"

    def test_mga9_with_epoch(self):
        assert parse_disttag("1:17.0.19.0.10-1.mga9") == "mga9"

    def test_double_digit(self):
        assert parse_disttag("2.0-3.mga11") == "mga11"

    def test_cauldron(self):
        assert parse_disttag("5-1.mgacauldron") == "mgacauldron"

    def test_no_disttag(self):
        assert parse_disttag("1.0-1") is None

    def test_empty(self):
        assert parse_disttag("") is None

    def test_none(self):
        assert parse_disttag(None) is None


class TestDisttagMajor:
    def test_numeric(self):
        assert disttag_major("mga10") == 10
        assert disttag_major("mga9") == 9

    def test_cauldron_returns_none(self):
        assert disttag_major("mgacauldron") is None

    def test_none_input(self):
        assert disttag_major(None) is None


# --- SONAME sublib detection ---------------------------------------------


class TestIsSonameSublib:
    """The name is never used ; only ``Provides`` decides."""

    def test_gtk_sublib(self):
        pkg = _mk("lib64gtk3_0",
                  provides=["lib64gtk3_0", "libgtk-3.so.0()(64bit)"])
        assert is_soname_sublib(pkg)

    def test_qt6_sublib(self):
        pkg = _mk("lib64qt6core6",
                  provides=["libQt6Core.so.6()(64bit)"])
        assert is_soname_sublib(pkg)

    def test_libreoffice_is_not_sublib(self):
        # libreoffice starts with "lib" but exposes no soname provide
        pkg = _mk("libreoffice",
                  provides=["libreoffice", "config(libreoffice)"])
        assert not is_soname_sublib(pkg)

    def test_librecad_is_not_sublib(self):
        pkg = _mk("librecad", provides=["librecad"])
        assert not is_soname_sublib(pkg)

    def test_libinput_tools_is_not_sublib(self):
        # command-line utility, no soname
        pkg = _mk("libinput-tools", provides=["libinput-tools"])
        assert not is_soname_sublib(pkg)

    def test_mgatools_is_not_sublib(self):
        pkg = _mk("mgatools",
                  provides=["mgatools", "perl(MGATools::iso)"])
        assert not is_soname_sublib(pkg)

    def test_devel_symlink_provide_not_enough(self):
        # ``libfoo.so`` (no version) is the devel symlink, not a
        # runtime soname — must not match.
        pkg = _mk("libfoo-devel", provides=["libfoo.so"])
        assert not is_soname_sublib(pkg)

    def test_multiple_provides_one_soname_enough(self):
        pkg = _mk("lib64foo1",
                  provides=["lib64foo1", "config(lib64foo1)",
                            "libfoo.so.1()(64bit)"])
        assert is_soname_sublib(pkg)

    def test_empty_provides(self):
        pkg = _mk("orphan", provides=[])
        assert not is_soname_sublib(pkg)


# --- Previous-release relic detection ------------------------------------


class TestIsPreviousReleaseRelic:
    def test_mga9_on_mga10(self):
        pkg = _mk("foo", evr="1.0-1.mga9")
        assert is_previous_release_relic(pkg, current_major=10)

    def test_mga10_on_mga10(self):
        pkg = _mk("foo", evr="1.0-1.mga10")
        assert not is_previous_release_relic(pkg, current_major=10)

    def test_mga11_on_mga10_not_relic(self):
        # ahead-of-release package (early adopter / cauldron rebuild)
        pkg = _mk("foo", evr="1.0-1.mga11")
        assert not is_previous_release_relic(pkg, current_major=10)

    def test_no_disttag_returns_false(self):
        pkg = _mk("foo", evr="1.0-1")
        assert not is_previous_release_relic(pkg, current_major=10)

    def test_cauldron_returns_false(self):
        pkg = _mk("foo", evr="1.0-1.mgacauldron")
        assert not is_previous_release_relic(pkg, current_major=10)

    def test_unknown_current_returns_false(self):
        pkg = _mk("foo", evr="1.0-1.mga9")
        assert not is_previous_release_relic(pkg, current_major=None)


# --- classify_orphans -----------------------------------------------------


class TestClassifyOrphans:
    def test_disjoint_buckets(self):
        mga9_pkg = _mk("mga9pkg", evr="1.0-1.mga9")
        sublib = _mk("lib64foo1",
                     provides=["lib64foo1", "libfoo.so.1()(64bit)"])
        userland = _mk("mgatools", provides=["mgatools"])

        buckets = classify_orphans(
            [mga9_pkg, sublib, userland], current_major=10)

        assert buckets[CATEGORY_PREVIOUS_RELEASE] == [mga9_pkg]
        assert buckets[CATEGORY_SUBLIB] == [sublib]
        assert buckets[CATEGORY_USERLAND] == [userland]

    def test_mga9_sublib_bucketed_as_relic_not_sublib(self):
        # relic wins over sublib — the operator will confirm-remove
        # the whole mga9 batch and the sublib will vanish with it.
        pkg = _mk("lib64mga9foo1", evr="1.0-1.mga9",
                  provides=["libmga9foo.so.1()(64bit)"])
        buckets = classify_orphans([pkg], current_major=10)
        assert buckets[CATEGORY_PREVIOUS_RELEASE] == [pkg]
        assert buckets[CATEGORY_SUBLIB] == []

    def test_empty_input(self):
        buckets = classify_orphans([], current_major=10)
        assert buckets == {
            CATEGORY_PREVIOUS_RELEASE: [],
            CATEGORY_SUBLIB: [],
            CATEGORY_USERLAND: [],
        }

    def test_current_major_none_disables_relic_bucket(self):
        pkg = _mk("mga9pkg", evr="1.0-1.mga9")
        buckets = classify_orphans([pkg], current_major=None)
        assert buckets[CATEGORY_PREVIOUS_RELEASE] == []
        # falls through — no soname provide, ends up userland
        assert buckets[CATEGORY_USERLAND] == [pkg]

    def test_preserves_input_order_within_bucket(self):
        a = _mk("a", provides=["a"])
        b = _mk("b", provides=["b"])
        c = _mk("c", provides=["c"])
        buckets = classify_orphans([c, a, b], current_major=10)
        assert buckets[CATEGORY_USERLAND] == [c, a, b]


# --- current_distmajor ----------------------------------------------------


class TestCurrentDistmajor:
    def test_reads_release_number(self, tmp_path: Path):
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "mageia-release").write_text("Mageia release 10 (Official)\n")
        assert current_distmajor(root=str(tmp_path)) == 10

    def test_missing_file(self, tmp_path: Path):
        assert current_distmajor(root=str(tmp_path)) is None

    def test_cauldron(self, tmp_path: Path):
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "mageia-release").write_text("Mageia release cauldron\n")
        # no numeric digit — returns None
        assert current_distmajor(root=str(tmp_path)) is None

    def test_extra_whitespace_and_case(self, tmp_path: Path):
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "mageia-release").write_text("  Mageia   Release   11  \n")
        assert current_distmajor(root=str(tmp_path)) == 11
