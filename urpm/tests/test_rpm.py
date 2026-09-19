"""Tests for urpm.core.rpm decoding helpers."""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import rpm

from urpm.core.rpm import (
    decode_rpmdep_sense,
    decode_rpmsense_flags,
    read_rpm_header,
)


class TestDecodeRpmsenseFlags:
    def test_less(self):
        assert decode_rpmsense_flags(rpm.RPMSENSE_LESS) == "<"

    def test_greater(self):
        assert decode_rpmsense_flags(rpm.RPMSENSE_GREATER) == ">"

    def test_equal(self):
        assert decode_rpmsense_flags(rpm.RPMSENSE_EQUAL) == "="

    def test_less_equal(self):
        assert decode_rpmsense_flags(rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL) == "<="

    def test_greater_equal(self):
        assert (
            decode_rpmsense_flags(rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL) == ">="
        )

    def test_no_compare(self):
        assert decode_rpmsense_flags(0) == ""

    def test_ignores_unrelated_bits(self):
        assert decode_rpmsense_flags(rpm.RPMSENSE_PREREQ | rpm.RPMSENSE_EQUAL) == "="


class TestDecodeRpmdepSense:
    def test_requires(self):
        assert decode_rpmdep_sense(rpm.RPMDEP_SENSE_REQUIRES) == "requires"

    def test_conflicts(self):
        assert decode_rpmdep_sense(rpm.RPMDEP_SENSE_CONFLICTS) == "conflicts"

    def test_unknown_value_falls_back(self):
        assert decode_rpmdep_sense(9999) == "unknown"

    def test_obsoletes_when_available(self):
        const = getattr(rpm, "RPMDEP_SENSE_OBSOLETES", None)
        if const is None:
            assert decode_rpmdep_sense(99) == "unknown"
        else:
            assert decode_rpmdep_sense(const) == "obsoletes"


@pytest.fixture(scope="module")
def built_rpm(tmp_path_factory) -> Path:
    """A real package, built here, carrying compressible payload.

    A header exposes only the unpacked size; the compressed one is the
    file itself.  Making the payload compressible is the point of the
    fixture: with random bytes the two figures would land close enough
    for a swap between them to pass unnoticed.
    """
    if shutil.which("rpmbuild") is None:
        pytest.skip("rpmbuild is not installed")

    top = tmp_path_factory.mktemp("rpmbuild")
    spec = top / "urpm-sizes.spec"
    spec.write_text(textwrap.dedent("""\
        Summary:   Fixture for the two size figures
        Name:      urpm-sizes-fixture
        Version:   1.0
        Release:   1
        License:   GPLv2
        Group:     Development/Other
        BuildArch: noarch

        %description
        Built by the test suite.

        %install
        mkdir -p %{buildroot}/usr/share/urpm-sizes-fixture
        for i in $(seq 1 40); do
            yes "the same line over and over" | head -n 2000 \\
                > %{buildroot}/usr/share/urpm-sizes-fixture/$i.txt
        done

        %files
        /usr/share/urpm-sizes-fixture
    """))

    proc = subprocess.run(
        ["rpmbuild", "--define", f"_topdir {top}",
         "--define", "_sourcedir " + str(top), "-bb", str(spec)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"rpmbuild failed: {proc.stderr[-400:]}")

    built = list((top / "RPMS").rglob("*.rpm"))
    assert built, "rpmbuild reported success but produced no package"
    return built[0]


class TestTheHeaderCarriesBothSizes:
    """``size`` is what the disk holds, ``filesize`` what is fetched.

    Only the first comes from the header — a package file has no tag
    for its own length — and leaving the second out made every local
    RPM read as zero bytes in the transaction summary.
    """

    def test_size_is_the_unpacked_footprint(self, built_rpm):
        info = read_rpm_header(str(built_rpm))
        assert info["size"] == 40 * 2000 * len("the same line over and over\n")

    def test_filesize_is_the_file_on_disk(self, built_rpm):
        info = read_rpm_header(str(built_rpm))
        assert info["filesize"] == built_rpm.stat().st_size

    def test_the_two_are_not_the_same_number(self, built_rpm):
        """The payload compresses, so a swap between the fields shows."""
        info = read_rpm_header(str(built_rpm))
        assert info["filesize"] < info["size"]

    def test_a_missing_file_is_still_none(self, tmp_path):
        assert read_rpm_header(str(tmp_path / "absent.rpm")) is None
