"""Regression tests for :func:`urpm.core.image_urpm_ng._github_pick_asset`.

Before the fix, the picker built a raw ``f".mga{mageia_release}."``
substring probe and skipped assets that didn't contain it verbatim.
For ``mageia_release='cauldron'`` that meant looking for
``.mgacauldron.`` — a token no Mageia RPM has ever carried — so the
GitHub fallback path (Rule 4) rejected every asset on cauldron
targets.  Delegating to :func:`_accepted_disttags` restores parity
with Rule 1 (local match) and unblocks the cauldron mkimage /
distupgrade code paths.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from urpm.core.image_urpm_ng import _github_pick_asset


def _make_release_body(assets):
    body = json.dumps({"assets": [
        {"name": name, "browser_download_url": f"https://x/{name}"}
        for name in assets
    ]}).encode()
    return io.BytesIO(body)


@contextmanager
def fake_release(assets):
    """Patch urlopen to serve a fake GitHub release with these assets."""
    with patch("urpm.core.image_urpm_ng.urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value = _make_release_body(assets)
        yield


class TestCauldronRegression:
    """Cauldron path — the case that motivated the fix."""

    def test_cauldron_picks_mga_n_minus_one_when_only_available(self):
        # Only asset published is the mga10 build (packager rebuilt on
        # their stable host); cauldron target is numeric 11.
        with fake_release(["urpm-ng-core-0.9.6-1.mga10.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "cauldron", "11", False, print)
        assert url == "https://x/urpm-ng-core-0.9.6-1.mga10.noarch.rpm"

    def test_cauldron_picks_mga_n_when_available(self):
        with fake_release(["urpm-ng-core-0.9.6-1.mga11.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "cauldron", "11", False, print)
        assert url == "https://x/urpm-ng-core-0.9.6-1.mga11.noarch.rpm"

    def test_cauldron_unknown_numeric_accepts_any_mga(self):
        # When cmd_init couldn't probe media.cfg, target_numeric is
        # None and _accepted_disttags returns None (accept any .mga).
        with fake_release(["urpm-ng-core-0.9.6-1.mga10.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "cauldron", None, False, print)
        assert url is not None


class TestNumericTargets:
    """Numeric targets — the disttag is a hard filter."""

    def test_mga10_accepts_matching_asset(self):
        with fake_release(["urpm-ng-core-0.9.6-1.mga10.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "10", "10", False, print)
        assert url is not None

    def test_mga10_rejects_mga11_only_asset(self):
        # A stable-release target refuses cross-N crossings — the
        # historical behaviour we must not break.
        with fake_release(["urpm-ng-core-0.9.6-1.mga11.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "10", "10", False, print)
        assert url is None

    def test_mga11_accepts_matching_asset(self):
        with fake_release(["urpm-ng-core-0.9.6-1.mga11.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "11", "11", False, print)
        assert url is not None


class TestFilterInvariants:
    """Filters that were correct before the fix and stay correct."""

    def test_arch_still_matched(self):
        # aarch64 target must not accept an x86_64 asset even if
        # disttag lines up.
        with fake_release(["urpm-ng-core-0.9.6-1.mga10.x86_64.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "aarch64", "10", "10", False, print)
        assert url is None

    def test_debug_assets_still_skipped(self):
        with fake_release([
            "urpm-ng-core-debuginfo-0.9.6-1.mga10.x86_64.rpm",
            "urpm-ng-core-debugsource-0.9.6-1.mga10.x86_64.rpm",
        ]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "10", "10", False, print)
        assert url is None

    def test_only_matching_subpackage_wins(self):
        # Assets for other subpackages must be ignored.
        with fake_release([
            "urpm-ng-cli-0.9.6-1.mga10.noarch.rpm",
            "urpm-ng-core-0.9.6-1.mga10.noarch.rpm",
        ]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "10", "10", False, print)
        assert url == "https://x/urpm-ng-core-0.9.6-1.mga10.noarch.rpm"


class TestAllowDisttagMismatch:
    """--allow-disttag-mismatch bypasses the accepted set."""

    def test_forces_any_mga_through(self):
        # allow_disttag_mismatch=True → accepted=None → accept any
        # .mga… — even a wildly wrong N.
        with fake_release(["urpm-ng-core-0.9.6-1.mga7.noarch.rpm"]):
            url = _github_pick_asset(
                "0.9.6", "x86_64", "10", "10", True, print)
        assert url is not None
