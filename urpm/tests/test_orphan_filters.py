"""Unit tests for :mod:`urpm.core.resolution.orphan_filters`."""

from __future__ import annotations

import pytest

from urpm.core.resolution.orphan_classify import OrphanInfo
from urpm.core.resolution.orphan_filters import (
    FilterSpec,
    FilterSpecError,
    parse_filters,
)


# --- Helpers --------------------------------------------------------------


def _mk(
    name: str,
    evr: str = "1.0-1.mga10",
    arch: str = "x86_64",
    provides: list = None,
    size: int = 0,
    install_time: int = 0,
    group: str = "",
) -> OrphanInfo:
    return OrphanInfo(
        name=name,
        evr=evr,
        arch=arch,
        nevra=f"{name}-{evr}.{arch}",
        provides=provides or [name],
        size=size,
        install_time=install_time,
        group=group,
    )


# --- Grammar --------------------------------------------------------------


class TestGrammar:
    def test_empty_spec_matches_everything(self):
        spec = parse_filters([])
        assert spec.is_empty()
        assert spec.matches(_mk("foo"))

    def test_none_input(self):
        assert parse_filters(None).is_empty()

    def test_whitespace_only_ignored(self):
        spec = parse_filters(["  ", ""])
        assert spec.is_empty()

    def test_comma_split(self):
        spec = parse_filters(["disttag=mga9,kind=sublib"])
        assert len(spec.predicates) == 2
        assert spec.raw == ["disttag=mga9", "kind=sublib"]

    def test_multiple_flag_occurrences(self):
        spec = parse_filters(["disttag=mga9", "kind=sublib"])
        assert len(spec.predicates) == 2

    def test_malformed_expression(self):
        with pytest.raises(FilterSpecError, match="malformed"):
            parse_filters(["disttag"])

    def test_unknown_key(self):
        with pytest.raises(FilterSpecError, match="unknown filter key"):
            parse_filters(["foo=bar"])


# --- disttag --------------------------------------------------------------


class TestDisttagFilter:
    def test_equality(self):
        spec = parse_filters(["disttag=mga9"])
        assert spec.matches(_mk("a", evr="1-1.mga9"))
        assert not spec.matches(_mk("a", evr="1-1.mga10"))

    def test_negation(self):
        spec = parse_filters(["disttag=!mga10"])
        assert spec.matches(_mk("a", evr="1-1.mga9"))
        assert not spec.matches(_mk("a", evr="1-1.mga10"))

    def test_no_disttag_matches_negated(self):
        spec = parse_filters(["disttag=!mga10"])
        # a package with no disttag is not mga10 → matches negation
        assert spec.matches(_mk("a", evr="1-1"))

    def test_only_supports_equality(self):
        with pytest.raises(FilterSpecError, match="only supports"):
            parse_filters(["disttag>mga9"])

    def test_bang_alone_rejected(self):
        with pytest.raises(FilterSpecError, match="expects a value"):
            parse_filters(["disttag=!"])


# --- kind -----------------------------------------------------------------


class TestKindFilter:
    def test_sublib(self):
        spec = parse_filters(["kind=sublib"])
        sublib = _mk("lib64foo1", provides=["libfoo.so.1()(64bit)"])
        userland = _mk("libreoffice", provides=["libreoffice"])
        assert spec.matches(sublib)
        assert not spec.matches(userland)

    def test_userland_negation(self):
        spec = parse_filters(["kind=!sublib"])
        sublib = _mk("lib64foo1", provides=["libfoo.so.1()(64bit)"])
        userland = _mk("libreoffice", provides=["libreoffice"])
        assert not spec.matches(sublib)
        assert spec.matches(userland)

    def test_userland_positive(self):
        spec = parse_filters(["kind=userland"])
        sublib = _mk("lib64foo1", provides=["libfoo.so.1()(64bit)"])
        userland = _mk("libreoffice", provides=["libreoffice"])
        assert not spec.matches(sublib)
        assert spec.matches(userland)

    def test_unknown_kind(self):
        with pytest.raises(FilterSpecError, match="unknown kind"):
            parse_filters(["kind=weird"])


# --- size -----------------------------------------------------------------


class TestSizeFilter:
    def test_gt_bytes(self):
        spec = parse_filters(["size>100"])
        assert spec.matches(_mk("a", size=200))
        assert not spec.matches(_mk("a", size=50))
        assert not spec.matches(_mk("a", size=100))  # strict

    def test_lt_kilobytes(self):
        spec = parse_filters(["size<10K"])
        assert spec.matches(_mk("a", size=5000))
        assert not spec.matches(_mk("a", size=20000))

    def test_megabytes(self):
        spec = parse_filters(["size>1M"])
        assert spec.matches(_mk("a", size=2 * 1024 ** 2))
        assert not spec.matches(_mk("a", size=100 * 1024))

    def test_gigabyte_fractional(self):
        spec = parse_filters(["size>1.5G"])
        assert spec.matches(_mk("a", size=2 * 1024 ** 3))
        assert not spec.matches(_mk("a", size=1024 ** 3))

    def test_equality_rejected(self):
        with pytest.raises(FilterSpecError, match="'<' or '>'"):
            parse_filters(["size=100"])

    def test_malformed_value(self):
        with pytest.raises(FilterSpecError, match="malformed size"):
            parse_filters(["size>lots"])


# --- installed ------------------------------------------------------------


class TestInstalledFilter:
    NOW = 1_700_000_000  # frozen test clock

    def test_lt_30d_recent_matches(self):
        # installed 10 days ago → matches "installed<30d"
        spec = parse_filters(["installed<30d"], now=self.NOW)
        recent = _mk("a", install_time=self.NOW - 10 * 86400)
        old = _mk("a", install_time=self.NOW - 60 * 86400)
        assert spec.matches(recent)
        assert not spec.matches(old)

    def test_gt_1y_old_matches(self):
        spec = parse_filters(["installed>1y"], now=self.NOW)
        recent = _mk("a", install_time=self.NOW - 100 * 86400)
        ancient = _mk("a", install_time=self.NOW - 400 * 86400)
        assert not spec.matches(recent)
        assert spec.matches(ancient)

    def test_units(self):
        spec = parse_filters(["installed<2w"], now=self.NOW)
        just_now = _mk("a", install_time=self.NOW - 3600)
        assert spec.matches(just_now)

    def test_malformed_duration(self):
        with pytest.raises(FilterSpecError, match="malformed duration"):
            parse_filters(["installed<forever"])


# --- group ----------------------------------------------------------------


class TestGroupFilter:
    def test_equality(self):
        spec = parse_filters(["group=Documentation"])
        assert spec.matches(_mk("a", group="Documentation"))
        assert not spec.matches(_mk("a", group="System/Libraries"))

    def test_negation(self):
        spec = parse_filters(["group=!System/Libraries"])
        assert spec.matches(_mk("a", group="Documentation"))
        assert not spec.matches(_mk("a", group="System/Libraries"))


# --- regex on name / provides --------------------------------------------


class TestRegexFilters:
    def test_name_regex(self):
        spec = parse_filters(["name~=^lib64"])
        assert spec.matches(_mk("lib64foo1"))
        assert not spec.matches(_mk("libreoffice"))

    def test_provides_regex(self):
        spec = parse_filters(["provides~=libgtk.*"])
        assert spec.matches(_mk("a", provides=["libgtk-3.so.0()(64bit)"]))
        assert not spec.matches(_mk("a", provides=["libc.so.6"]))

    def test_invalid_regex(self):
        with pytest.raises(FilterSpecError, match="invalid regex"):
            parse_filters(["name~=[unclosed"])

    def test_equality_op_rejected_on_regex_key(self):
        with pytest.raises(FilterSpecError, match="only supports '~='"):
            parse_filters(["name=foo"])


# --- Composition ---------------------------------------------------------


class TestComposition:
    def test_and_composition(self):
        spec = parse_filters(["disttag=mga10,kind=!sublib"])
        userland_mga10 = _mk(
            "mgatools", evr="0.2-4.mga10", provides=["mgatools"])
        sublib_mga10 = _mk(
            "lib64foo1", evr="1-1.mga10",
            provides=["libfoo.so.1()(64bit)"])
        userland_mga9 = _mk(
            "mgatools", evr="0.2-4.mga9", provides=["mgatools"])
        assert spec.matches(userland_mga10)
        assert not spec.matches(sublib_mga10)
        assert not spec.matches(userland_mga9)

    def test_apply_returns_subset(self):
        spec = parse_filters(["disttag=mga9"])
        pkgs = [
            _mk("a", evr="1-1.mga9"),
            _mk("b", evr="1-1.mga10"),
            _mk("c", evr="1-1.mga9"),
        ]
        matched = spec.apply(pkgs)
        assert [p.name for p in matched] == ["a", "c"]

    def test_empty_spec_apply_returns_all(self):
        spec = parse_filters([])
        pkgs = [_mk("a"), _mk("b")]
        assert spec.apply(pkgs) == pkgs
