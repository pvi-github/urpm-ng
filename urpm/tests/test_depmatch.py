"""Unit tests for :mod:`urpm.core.resolution.depmatch`.

The parsing and the rpm comparison rules are covered by
``test_orphan_cap_parse.py``, which follows the primitive it was
written for.  What is tested here is the index every reverse-dependency
walk now goes through, and the header reader that feeds it, because
this is where the version used to be dropped.
"""

from __future__ import annotations

import pytest

rpm = pytest.importorskip("rpm")

from urpm.core.resolution.depmatch import (  # noqa: E402
    ProvidesIndex, header_rows, provider_satisfies,
)


SENSE_EQ = rpm.RPMSENSE_EQUAL
SENSE_GE = rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL


class FakeHeader(dict):
    """An rpm header is read by tag; a dict is enough to stand in."""


def _header(**arrays):
    """Build a header carrying the parallel arrays rpm would store."""
    return FakeHeader({
        rpm.RPMTAG_REQUIRENAME: arrays.get('req_names', []),
        rpm.RPMTAG_REQUIREVERSION: arrays.get('req_versions', []),
        rpm.RPMTAG_REQUIREFLAGS: arrays.get('req_flags', []),
        rpm.RPMTAG_PROVIDENAME: arrays.get('prov_names', []),
        rpm.RPMTAG_PROVIDEVERSION: arrays.get('prov_versions', []),
        rpm.RPMTAG_PROVIDEFLAGS: arrays.get('prov_flags', []),
    })


class TestTheIndexHonoursTheVersion:
    """The defect this module exists for.

    Two packages installable side by side provide the same capability
    at two versions.  Matching on the name alone made each of them look
    like a dependency of everything needing the other, which is how a
    dead package stayed on the system for good.
    """

    @staticmethod
    def _typelib():
        index = ProvidesIndex()
        index.add("typelib(GnomeDesktop)", "3.0", "lib64gnome-desktop-gir3.0")
        index.add("typelib(GnomeDesktop)", "4.0", "lib64gnome-desktop-gir4.0")
        return index

    def test_only_the_matching_version_answers(self):
        index = self._typelib()

        assert index.providers("typelib(GnomeDesktop)", SENSE_EQ, "4.0") == {
            "lib64gnome-desktop-gir4.0"}
        assert index.providers("typelib(GnomeDesktop)", SENSE_EQ, "3.0") == {
            "lib64gnome-desktop-gir3.0"}

    def test_an_unversioned_require_takes_every_provider(self):
        # rpm's own rule, and the fast path of the lookup.
        assert self._typelib().providers("typelib(GnomeDesktop)") == {
            "lib64gnome-desktop-gir3.0", "lib64gnome-desktop-gir4.0"}

    def test_a_range_takes_what_falls_inside_it(self):
        index = self._typelib()

        assert index.providers("typelib(GnomeDesktop)", SENSE_GE, "4.0") == {
            "lib64gnome-desktop-gir4.0"}

    def test_an_unknown_capability_answers_nothing(self):
        assert self._typelib().providers("typelib(Nothing)", SENSE_EQ,
                                         "1.0") == set()

    def test_an_unversioned_provider_cannot_satisfy_a_constraint(self):
        # perl-base provides perl(strict) with no version; rpm refuses
        # it for ``Requires: perl(strict) >= 1.30.0`` and so do we.
        index = ProvidesIndex()
        index.add("perl(strict)", "", "perl-base")

        assert index.providers("perl(strict)") == {"perl-base"}
        assert index.providers("perl(strict)", SENSE_GE, "1.30.0") == set()

    def test_a_requirement_row_can_be_passed_whole(self):
        index = self._typelib()
        row = ("typelib(GnomeDesktop)", SENSE_EQ, "4.0")

        assert index.providers_of(row) == {"lib64gnome-desktop-gir4.0"}

    def test_the_owner_is_whatever_the_caller_stored(self):
        index = ProvidesIndex()
        index.add("libfoo.so.1()(64bit)", "1.0", ("lib64foo", "x86_64"))

        assert index.providers("libfoo.so.1()(64bit)") == {
            ("lib64foo", "x86_64")}

    def test_a_synthesis_string_is_parsed_on_the_way_in(self):
        index = ProvidesIndex()
        index.add_capability("typelib(GnomeDesktop)[== 3.0]", "gir3.0")

        assert index.providers("typelib(GnomeDesktop)", SENSE_EQ, "3.0") == {
            "gir3.0"}
        assert index.providers("typelib(GnomeDesktop)", SENSE_EQ,
                               "4.0") == set()


class TestReadingADependencyOffAHeader:
    """The three parallel arrays, zipped back together in one place."""

    def test_the_version_travels_with_the_name(self):
        hdr = _header(req_names=["typelib(GnomeDesktop)"],
                      req_versions=["4.0"], req_flags=[SENSE_EQ])

        assert header_rows(hdr, 'requires') == [
            ("typelib(GnomeDesktop)", SENSE_EQ, "4.0")]

    def test_the_sense_keeps_only_the_comparison_bits(self):
        # PREREQ and the scriptlet bits say when a dependency matters,
        # not what satisfies it, and would corrupt the comparison.
        hdr = _header(req_names=["bash"], req_versions=["5.0"],
                      req_flags=[SENSE_GE | rpm.RPMSENSE_SCRIPT_PRE])

        assert header_rows(hdr, 'requires') == [("bash", SENSE_GE, "5.0")]

    def test_rpmlib_capabilities_are_dropped(self):
        hdr = _header(req_names=["rpmlib(FileDigests)", "bash"],
                      req_versions=["4.6.0-1", ""], req_flags=[SENSE_GE, 0])

        assert header_rows(hdr, 'requires') == [("bash", 0, "")]

    def test_file_dependencies_are_kept_unless_asked_otherwise(self):
        hdr = _header(req_names=["/bin/sh"], req_versions=[""], req_flags=[0])

        assert header_rows(hdr, 'requires') == [("/bin/sh", 0, "")]
        assert header_rows(hdr, 'requires', skip_files=True) == []

    def test_a_short_version_array_does_not_raise(self):
        # rpm always writes three arrays of equal length; a header read
        # from a damaged package may not.
        hdr = _header(req_names=["bash", "coreutils"], req_versions=["5.0"],
                      req_flags=[SENSE_GE])

        assert header_rows(hdr, 'requires') == [
            ("bash", SENSE_GE, "5.0"), ("coreutils", 0, "")]

    def test_a_header_feeds_the_index_directly(self):
        hdr = _header(prov_names=["typelib(GnomeDesktop)", "lib64gnome"],
                      prov_versions=["3.0", "44.4-1"],
                      prov_flags=[SENSE_EQ, SENSE_EQ])
        index = ProvidesIndex()
        index.add_header(hdr, "lib64gnome-desktop-gir3.0")

        assert index.providers("typelib(GnomeDesktop)", SENSE_EQ, "3.0") == {
            "lib64gnome-desktop-gir3.0"}
        assert index.providers("typelib(GnomeDesktop)", SENSE_EQ,
                               "4.0") == set()


class TestTheRulesMatchRpm:
    """Spot checks against rpm's own answers, on real Mageia shapes."""

    @pytest.mark.parametrize("prov_evr,sense,req_evr,expected", [
        ("2.86.5-1", SENSE_GE, "2.86", True),
        ("2.86.5-1", SENSE_GE, "2.87", False),
        ("3.0", SENSE_EQ, "3.0", True),
        ("3.0", SENSE_EQ, "4.0", False),
        # A require that omits the release compares on the version
        # alone, which is how rpm accepts foo-1-5 for `= 1`.
        ("1-5", SENSE_EQ, "1", True),
        ("1-5", SENSE_EQ, "1-4", False),
    ])
    def test_a_handful_of_verdicts(self, prov_evr, sense, req_evr, expected):
        assert provider_satisfies(prov_evr, sense, req_evr) is expected
