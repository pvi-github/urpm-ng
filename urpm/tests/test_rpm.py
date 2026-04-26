"""Tests for urpm.core.rpm decoding helpers."""

import rpm

from urpm.core.rpm import decode_rpmdep_sense, decode_rpmsense_flags


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
