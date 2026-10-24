"""Which local RPM may be injected into an image tree.

These cases were written against ``_github_pick_asset``, the picker
that chose an asset from the project's GitHub releases. That arm is
gone — the project lives on Mageia's own forge, so it could only ever
404, and reaching for a third party's API to install our own package
was a fragility of its own.

The four properties it checked did not go with it, because
``_detect_local_match`` enforces the same ones on the path that
remains: the target architecture (or ``noarch``), the disttag through
:func:`_accepted_disttags`, the skipping of ``-debuginfo-`` and
``-debugsource-``, and the subpackage, anchored by the glob's
``urpm-ng-core-{version}-{release}`` prefix. So the cases moved here
rather than being deleted, and they now run against real files
instead of a faked HTTP response.

The one worth keeping in mind is the cauldron relaxation: a packager
rebuilds urpm-ng on their current stable host (``mga{N-1}``) to test
it in a cauldron image (``mga{N}``), and urpm-ng-core being noarch
pure Python the disttag is cosmetic there. A numeric target, by
contrast, refuses the crossing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from urpm.core.image_urpm_ng import _accepted_disttags, _detect_local_match


VERSION = "0.9.6"
RELEASE = "1"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A source tree shaped like the one the detector looks for."""
    (tmp_path / "VERSION").write_text(VERSION + "\n")
    (tmp_path / "RELEASE").write_text(RELEASE + "\n")
    (tmp_path / "rpmbuild" / "SPECS").mkdir(parents=True)
    (tmp_path / "rpmbuild" / "SPECS" / "urpm-ng.spec").write_text("")
    (tmp_path / "rpmbuild" / "RPMS" / "noarch").mkdir(parents=True)
    (tmp_path / "rpmbuild" / "RPMS" / "x86_64").mkdir(parents=True)
    monkeypatch.setenv("URPM_NG_SOURCE_DIR", str(tmp_path))
    # The detector also walks up from the current directory looking for
    # a checkout, so without this the suite would see the developer's
    # own tree and whatever they last built in it: the tests asserting
    # that nothing matches passed or failed on the contents of
    # ``rpmbuild/RPMS``.
    monkeypatch.chdir(tmp_path)
    return tmp_path


def publish(checkout, name):
    """Drop a built RPM in the tree, in the directory its arch implies."""
    arch_dir = "noarch" if ".noarch." in name else name.rsplit(".", 2)[-2]
    target = checkout / "rpmbuild" / "RPMS" / arch_dir
    target.mkdir(parents=True, exist_ok=True)
    rpm = target / name
    rpm.write_bytes(b"not a real rpm, never opened here")
    return rpm


# ── The cauldron relaxation ─────────────────────────────────────────────


def test_cauldron_takes_the_previous_stable_when_it_is_all_there_is(checkout):
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga10.noarch.rpm")

    found = _detect_local_match("x86_64", "cauldron", "11")

    assert found is not None
    assert ".mga10." in found.name


def test_cauldron_prefers_nothing_else_when_its_own_is_there(checkout):
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga11.noarch.rpm")

    found = _detect_local_match("x86_64", "cauldron", "11")

    assert found is not None
    assert ".mga11." in found.name


def test_cauldron_with_an_unknown_numeric_accepts_any_mga(checkout):
    """``cmd_init`` could not probe media.cfg, so the numeric is unknown.

    ``_accepted_disttags`` returns None there, which callers read as
    « accept any ``.mga…`` » — and the caller prints a note, so the
    choice stays visible.
    """
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga10.noarch.rpm")

    assert _accepted_disttags("cauldron", None, False) is None
    assert _detect_local_match("x86_64", "cauldron", None) is not None


# ── A numeric target is a hard filter ───────────────────────────────────


def test_a_numeric_target_accepts_its_own_disttag(checkout):
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga10.noarch.rpm")

    assert _detect_local_match("x86_64", "10", "10") is not None


def test_a_numeric_target_refuses_the_crossing(checkout):
    """A stable target must not take a build for another release."""
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga11.noarch.rpm")

    assert _detect_local_match("x86_64", "10", "10") is None


def test_the_packager_can_assume_the_crossing(checkout):
    """``--allow-disttag-mismatch`` is them taking responsibility."""
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga11.noarch.rpm")

    assert _detect_local_match("x86_64", "10", "10",
                               allow_disttag_mismatch=True) is not None


# ── The other three filters ─────────────────────────────────────────────


def test_another_architecture_is_not_accepted(checkout):
    publish(checkout, f"urpm-ng-core-{VERSION}-{RELEASE}.mga10.x86_64.rpm")

    assert _detect_local_match("aarch64", "10", "10") is None


def test_debug_packages_are_skipped(checkout):
    publish(checkout,
            f"urpm-ng-core-debuginfo-{VERSION}-{RELEASE}.mga10.x86_64.rpm")
    publish(checkout,
            f"urpm-ng-core-debugsource-{VERSION}-{RELEASE}.mga10.x86_64.rpm")

    assert _detect_local_match("x86_64", "10", "10") is None


def test_another_subpackage_is_not_mistaken_for_core(checkout):
    publish(checkout, f"urpm-ng-cli-{VERSION}-{RELEASE}.mga10.noarch.rpm")
    core = publish(checkout,
                   f"urpm-ng-core-{VERSION}-{RELEASE}.mga10.noarch.rpm")

    found = _detect_local_match("x86_64", "10", "10")

    assert found is not None
    assert found.name == core.name


# ── And nothing at all ──────────────────────────────────────────────────


def test_an_empty_tree_matches_nothing(checkout):
    assert _detect_local_match("x86_64", "10", "10") is None
