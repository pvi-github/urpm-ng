"""Tests for :func:`urpm.core.transaction_queue._install_prob_filter`.

The policy driving the ``RPMPROB_FILTER_*`` bitmask on an install
transaction : baseline flags on every install ; recovery flags added when
the caller opted into ``--force`` or ``--reinstall``.

Regression captured : ``--reinstall`` on a chroot with leftover files from
an earlier aborted transaction used to die with rpm-cpio
« chown failed - Directory not empty » because REPLACE{NEW,OLD}FILES was
gated on ``op.force`` alone.  This suite pins the shared branch so a future
refactor can't silently reintroduce the divergence.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import rpm

from urpm.core.transaction_queue import _install_prob_filter


BASELINE = rpm.RPMPROB_FILTER_DISKSPACE | rpm.RPMPROB_FILTER_REPLACEPKG
RECOVERY = (
    rpm.RPMPROB_FILTER_OLDPACKAGE
    | rpm.RPMPROB_FILTER_REPLACENEWFILES
    | rpm.RPMPROB_FILTER_REPLACEOLDFILES
)


def _op(**overrides) -> SimpleNamespace:
    """Cheap stand-in for :class:`QueuedOperation` — only fields the
    filter policy looks at are populated."""
    base = {"force": False, "reinstall": False}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBaselineFilters:

    def test_plain_install_gets_baseline_only(self):
        """No ``--force`` and no ``--reinstall`` : DISKSPACE + REPLACEPKG."""
        assert _install_prob_filter(_op()) == BASELINE

    def test_baseline_never_carries_recovery_flags(self):
        """The recovery bits stay off unless explicitly opted into."""
        mask = _install_prob_filter(_op())
        assert mask & rpm.RPMPROB_FILTER_OLDPACKAGE == 0
        assert mask & rpm.RPMPROB_FILTER_REPLACENEWFILES == 0
        assert mask & rpm.RPMPROB_FILTER_REPLACEOLDFILES == 0


class TestForceGate:

    def test_force_pulls_in_recovery_flags(self):
        """``--force`` = classic ``rpm -Uvh --force`` semantics : baseline
        plus OLDPACKAGE plus REPLACE{NEW,OLD}FILES."""
        assert _install_prob_filter(_op(force=True)) == BASELINE | RECOVERY

    def test_force_keeps_baseline(self):
        """Force is additive over the baseline, it never clears it."""
        mask = _install_prob_filter(_op(force=True))
        assert mask & BASELINE == BASELINE


class TestReinstallGate:
    """Regression : ``--reinstall`` must trigger the same recovery flags as
    ``--force``.  The contract is « make it install, whatever state the
    disk is in » — same policy at the RPM layer."""

    def test_reinstall_pulls_in_recovery_flags(self):
        assert _install_prob_filter(_op(reinstall=True)) == BASELINE | RECOVERY

    def test_reinstall_matches_force_exactly(self):
        """Force and reinstall produce the identical mask — they share the
        same rationale, no divergence in either direction."""
        assert (
            _install_prob_filter(_op(reinstall=True))
            == _install_prob_filter(_op(force=True))
        )

    def test_force_and_reinstall_together_is_idempotent(self):
        """Setting both flags is not additive — the recovery bits are
        already fully on with either one alone."""
        assert (
            _install_prob_filter(_op(force=True, reinstall=True))
            == _install_prob_filter(_op(force=True))
        )
