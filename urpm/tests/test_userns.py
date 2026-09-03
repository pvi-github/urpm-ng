"""Tests for :mod:`urpm.core.userns`.

The rootless-userns preflight is a small, fast contract : read
``/etc/subuid`` and ``/etc/subgid``, count what's delegated to the
current user, refuse to proceed when the count is 0 or 1 (the
« single-uid mapping » degraded fallback).  Regression this locks
down : cyril's mkimage silent-failed because his ``/etc/subuid``
was empty, ``podman unshare true`` returned 0 anyway (podman's
own fallback), and rpm-cpio then died mid-transaction on chown to
system UIDs it couldn't reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import urpm.core.userns as userns
from urpm.core.userns import (
    BYPASS_ENV,
    UserNamespaceCapability,
    UserNamespaceUnusableError,
    _count_delegated,
    probe_current_user,
    require_userns,
)


class TestCountDelegated:

    def test_missing_file_returns_zero(self, tmp_path):
        """No file on disk → nothing delegated.  Never raises."""
        assert _count_delegated(tmp_path / "does-not-exist", "cyril") == 0

    def test_empty_file_returns_zero(self, tmp_path):
        """The exact state we saw on cyril's box : file exists, empty."""
        p = tmp_path / "subuid"
        p.write_text("")
        assert _count_delegated(p, "cyril") == 0

    def test_single_entry_returns_count(self, tmp_path):
        """The classic populated line — 65536 uids delegated."""
        p = tmp_path / "subuid"
        p.write_text("cyril:100000:65536\n")
        assert _count_delegated(p, "cyril") == 65536

    def test_multiple_entries_are_summed(self, tmp_path):
        """Several delegated ranges for the same user add up.  Not the
        common case but the format allows it — we mirror shadow-utils."""
        p = tmp_path / "subuid"
        p.write_text("cyril:100000:65536\ncyril:200000:1000\n")
        assert _count_delegated(p, "cyril") == 66536

    def test_other_user_ignored(self, tmp_path):
        """Entries for someone else's login don't count toward ours."""
        p = tmp_path / "subuid"
        p.write_text("alice:100000:65536\n")
        assert _count_delegated(p, "cyril") == 0

    def test_malformed_line_skipped(self, tmp_path):
        """Wrong field count / non-integer count are skipped silently
        rather than raising ; we still return whatever valid entries
        we found alongside."""
        p = tmp_path / "subuid"
        p.write_text(
            "cyril:100000:65536\n"
            "malformed\n"
            "cyril:200000:not-a-number\n"
            "cyril:300000:42\n"
        )
        assert _count_delegated(p, "cyril") == 65536 + 42


class TestUsableProperty:
    """The ``usable`` flag drives the whole preflight — pin its
    boundary conditions."""

    def test_root_is_always_usable(self):
        """Root doesn't need delegated ranges — it owns the whole
        UID space already."""
        cap = UserNamespaceCapability(
            username="root", subuid_count=0, subgid_count=0, is_root=True,
        )
        assert cap.usable

    def test_populated_ranges_are_usable(self):
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=65536, subgid_count=65536, is_root=False,
        )
        assert cap.usable

    def test_zero_range_is_not_usable(self):
        """The cyril scenario — /etc/subuid empty."""
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=0, subgid_count=0, is_root=False,
        )
        assert not cap.usable

    def test_count_of_one_is_not_usable(self):
        """The « single-uid mapping » podman fallback — only the
        caller's own UID delegated.  Precisely the broken state we
        guard against ; count=1 must NOT count as usable."""
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=1, subgid_count=1, is_root=False,
        )
        assert not cap.usable

    def test_asymmetric_ranges_are_not_usable(self):
        """Both files must be populated — subuid alone isn't enough,
        chown of gids to system groups (mail=8, etc.) also has to
        succeed."""
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=65536, subgid_count=0, is_root=False,
        )
        assert not cap.usable


class TestProbeCurrentUser:

    def test_probe_reads_both_files(self, tmp_path, monkeypatch):
        """End-to-end : probe pulls the two files and populates the
        capability with what it found."""
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        subuid.write_text("cyril:100000:65536\n")
        subgid.write_text("cyril:100000:32768\n")

        real_path = userns.Path
        def fake_path(arg):
            if arg == "/etc/subuid":
                return subuid
            if arg == "/etc/subgid":
                return subgid
            return real_path(arg)
        monkeypatch.setattr(userns, "Path", fake_path)
        monkeypatch.setattr(userns, "_current_username", lambda: "cyril")
        monkeypatch.setattr(userns.os, "geteuid", lambda: 1000)

        cap = probe_current_user()
        assert cap.username == "cyril"
        assert cap.subuid_count == 65536
        assert cap.subgid_count == 32768
        assert cap.is_root is False
        assert cap.usable is True


class TestRequireUserns:

    def _stub_probe(self, monkeypatch, cap):
        monkeypatch.setattr(userns, "probe_current_user", lambda: cap)

    def test_usable_capability_returns_it(self, monkeypatch):
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=65536, subgid_count=65536, is_root=False,
        )
        self._stub_probe(monkeypatch, cap)
        assert require_userns() is cap

    def test_root_always_passes_even_with_empty_files(self, monkeypatch):
        """Root doesn't need delegated ranges — the check must not
        stand in its way, even when nothing is in /etc/subuid."""
        cap = UserNamespaceCapability(
            username="root", subuid_count=0, subgid_count=0, is_root=True,
        )
        self._stub_probe(monkeypatch, cap)
        assert require_userns() is cap

    def test_bypass_env_passes_even_when_unusable(self, monkeypatch):
        """The escape hatch : ops-side env var bypasses the raise but
        still probes so the state stays observable in logs."""
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=0, subgid_count=0, is_root=False,
        )
        self._stub_probe(monkeypatch, cap)
        monkeypatch.setenv(BYPASS_ENV, "1")
        assert require_userns() is cap

    def test_bypass_only_when_value_is_exactly_one(self, monkeypatch):
        """Set-to-anything-else-than-1 does NOT bypass — avoids the
        classic ``FOO=0`` / ``FOO=false`` confusion where the operator
        thought they had turned it off."""
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=0, subgid_count=0, is_root=False,
        )
        self._stub_probe(monkeypatch, cap)
        monkeypatch.setenv(BYPASS_ENV, "0")
        with pytest.raises(UserNamespaceUnusableError):
            require_userns()

    def test_unusable_raises_with_actionable_message(self, monkeypatch):
        """The exception message must give the operator exactly what
        they need to fix — user name and copy-pasteable commands."""
        cap = UserNamespaceCapability(
            username="cyril", subuid_count=0, subgid_count=0, is_root=False,
        )
        self._stub_probe(monkeypatch, cap)
        monkeypatch.delenv(BYPASS_ENV, raising=False)
        with pytest.raises(UserNamespaceUnusableError) as exc_info:
            require_userns()
        msg = str(exc_info.value)
        # Names the user, so the operator picks the right one on a
        # multi-user box.
        assert "cyril" in msg
        # Shows the counts so the operator can see the raw state.
        assert "subuid_count=0" in msg
        assert "subgid_count=0" in msg
        # Copy-pasteable fix — the exact command a Mageia admin
        # should run.
        assert "usermod" in msg
        assert "--add-subuids" in msg
        assert "--add-subgids" in msg
        assert "podman system migrate" in msg
        # Bypass mechanism is discoverable from the message itself,
        # not hidden in code.
        assert BYPASS_ENV in msg
