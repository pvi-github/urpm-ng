"""Unit tests for :func:`urpm.core.resolution.orphans._parse_synthesis_cap`.

The parser turns a raw synthesis capability string into
``(name, sense, evr)``.  Its contract is delicate because brackets play
two unrelated roles in that format: they delimit the trailing qualifier
and version groups (``[*]``, ``[>= 1.2]``), *and* they occur inside
capability names produced by Python extras
(``python3.13dist(coverage[toml])``).

Reading a capability from the left therefore truncates every extras
name, which silently deletes the dependency edge that goes through it —
the failure that made an upgrade drop ``python3-coverage+toml`` as a
false orphan while ``python3-pytest-cov`` still required it.
"""

from __future__ import annotations

import pytest

rpm = pytest.importorskip("rpm")

from urpm.core.resolution.orphans import (  # noqa: E402
    _parse_synthesis_cap,
    _provider_satisfies,
)


# --- Names without a trailing group ---------------------------------------


@pytest.mark.parametrize("cap", [
    "bash",
    "perl(Foo::Bar)",
    "libfoo.so.1()(64bit)",
    "python3.13dist(coverage[toml])",
    "python3dist(tomcli[all])",
    "python3.13dist(pbs-installer[download])",
])
def test_unversioned_names_are_returned_verbatim(cap):
    """No trailing bracket group: the whole string is the name."""
    assert _parse_synthesis_cap(cap) == (cap, 0, "")


# --- Version constraint ---------------------------------------------------


def test_simple_version_constraint():
    name, sense, evr = _parse_synthesis_cap("libpng[>= 1.6.0]")
    assert name == "libpng"
    assert evr == "1.6.0"
    assert sense & rpm.RPMSENSE_GREATER
    assert sense & rpm.RPMSENSE_EQUAL


@pytest.mark.parametrize("op,expected", [
    ("<", rpm.RPMSENSE_LESS),
    ("<=", rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL),
    ("=", rpm.RPMSENSE_EQUAL),
    ("==", rpm.RPMSENSE_EQUAL),
    (">=", rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL),
    (">", rpm.RPMSENSE_GREATER),
])
def test_every_operator_maps_to_its_sense(op, expected):
    assert _parse_synthesis_cap(f"foo[{op} 1.2]") == ("foo", expected, "1.2")


# --- The ``[*]`` qualifier ------------------------------------------------


def test_star_qualifier_is_stripped_and_carries_no_version():
    assert _parse_synthesis_cap("/bin/sh[*]") == ("/bin/sh", 0, "")


def test_star_qualifier_before_a_constraint_keeps_the_constraint():
    """Regression: ``NAME[*][op evr]`` used to lose its version.

    Reading from the left matched the ``[*]`` bracket, so the operator
    was parsed as ``"*][>="`` — absent from the sense map, hence sense
    ``0``, i.e. "unversioned", i.e. satisfied by any provider.
    """
    name, sense, evr = _parse_synthesis_cap("apache[*][>= 2.0.54]")
    assert name == "apache"
    assert evr == "2.0.54"
    assert sense & rpm.RPMSENSE_GREATER

    # The constraint is now actually enforced downstream.
    assert _provider_satisfies("2.4.68", sense, evr) is True
    assert _provider_satisfies("1.3.0", sense, evr) is False


# --- Inner brackets combined with a trailing group -------------------------


def test_extras_name_with_version_constraint():
    """Both roles of the bracket in one string."""
    name, sense, evr = _parse_synthesis_cap(
        "python3.13dist(dulwich[pgp])[== 0.24.8]")
    assert name == "python3.13dist(dulwich[pgp])"
    assert evr == "0.24.8"
    assert sense & rpm.RPMSENSE_EQUAL


def test_extras_edge_survives_the_round_trip():
    """The case that produced the false orphan.

    A require read from the rpmdb keeps its name intact; the matching
    provide read from synthesis must parse to that same name, otherwise
    no edge is established and the provider looks unrequired.
    """
    rpmdb_side = "python3.13dist(coverage[toml])"
    synthesis_side = _parse_synthesis_cap(
        "python3.13dist(coverage[toml])[== 7.16.0]")
    assert synthesis_side[0] == rpmdb_side
    assert _provider_satisfies(
        "7.16.0", *_parse_synthesis_cap(
            "python3.13dist(coverage[toml])[>= 7.5]")[1:]) is True


# --- Unrecognised trailing groups stay part of the name --------------------


@pytest.mark.parametrize("cap", [
    "foo[]",
    "foo[bar]",
    "foo[>=]",
    "foo[?? 1.2]",
])
def test_unrecognised_trailing_group_is_not_a_constraint(cap):
    """Never truncate on a group we cannot make sense of.

    Dropping the tail would invent a capability name that exists
    nowhere, which is exactly how edges disappear.
    """
    assert _parse_synthesis_cap(cap) == (cap, 0, "")


def test_lone_bracket_does_not_loop_or_raise():
    assert _parse_synthesis_cap("]") == ("]", 0, "")
    assert _parse_synthesis_cap("[") == ("[", 0, "")
