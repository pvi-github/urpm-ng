"""Erases must not pull the floor out from under the running system.

Tx B is sliced into batches to bound the disk peak.  rpm keeps files in
place until a transaction commits, so an erase interleaved at its
libsolv position is safe *inside* one batch — and not across batches:
the package is gone from disk while batches 4..N run, and the X server,
the display manager and the terminal running the upgrade still need it.
A tester's desktop froze exactly there.

Deferring every erase to the last batch is not the alternative: that is
what doubled the on-disk footprint of a cross-release upgrade and
filled a tester's root partition.  Both failures happened on real
machines, so erases are ranked instead:

* not running       → stays at its libsolv position, frees space early
* running, expendable → penultimate batch (Firefox, LibreOffice)
* load-bearing      → final batch only

The critical rank is derived, not listed, wherever /proc allows it.
The one thing it cannot derive is named: under Wayland the compositor
runs as the session user, child of the session, indistinguishable from
Firefox by every process attribute there is.
"""

from __future__ import annotations

import os

import pytest

from urpm.core.distupgrade.live_system import (
    TIER_CRITICAL,
    TIER_INTERLEAVED,
    TIER_SACRIFICIAL,
    LiveSystemSnapshot,
    _ancestors,
    snapshot,
)
from urpm.core.distupgrade.stage3 import (
    _defer_erases_the_system_is_using,
    erase_entry,
)


class TestTheTerminalIsProtectedByAncestry:
    """A Konsole has the same uid and the same session as Firefox.  The
    only thing that tells them apart is that one of them is why we are
    running at all."""

    def test_our_ancestors_reach_init(self):
        chain = _ancestors(os.getpid())
        assert os.getpid() in chain
        assert 1 in chain, "the walk must terminate at init"

    def test_the_walk_terminates_on_a_cycle(self):
        """A malformed /proc must not spin at the head of a migration."""
        assert _ancestors(1) == {1}

    def test_every_ancestor_is_a_live_process(self):
        for pid in _ancestors(os.getpid()):
            assert os.path.isdir(f"/proc/{pid}")


class TestTheSnapshotRanksTheRealMachine:

    @pytest.fixture(scope="class")
    def live(self):
        return snapshot()

    def test_the_shell_is_critical(self, live):
        """Our own ancestor chain runs through it."""
        assert live.tier("bash") == TIER_CRITICAL

    def test_libc_is_critical(self, live):
        assert live.tier("glibc") == TIER_CRITICAL

    def test_a_package_nobody_runs_is_interleaved(self, live):
        assert live.tier("a-package-that-does-not-exist") == TIER_INTERLEAVED

    def test_the_two_sets_do_not_overlap(self, live):
        """A package cannot be both expendable and load-bearing; the
        ranking would depend on lookup order."""
        assert not (live.critical & live.sacrificial)


class TestBatchPlacement:
    """Driven with a hand-built snapshot: the placement rules must hold
    whatever happens to be running on the machine running the tests."""

    @staticmethod
    def _live(critical=(), sacrificial=()):
        return LiveSystemSnapshot(
            critical=frozenset(critical), sacrificial=frozenset(sacrificial))

    @pytest.fixture
    def batches(self):
        return [
            ["a-1-1.x86_64", erase_entry("firefox")],
            ["b-1-1.x86_64", erase_entry("glibc")],
            ["c-1-1.x86_64", erase_entry("lib64old1")],
            ["d-1-1.x86_64"],
        ]

    def _apply(self, monkeypatch, batches, live):
        from urpm.core.distupgrade import live_system
        monkeypatch.setattr(live_system, "snapshot", lambda *a, **kw: live)
        return _defer_erases_the_system_is_using(batches)

    def test_unused_erase_keeps_its_position(self, monkeypatch, batches):
        result = self._apply(monkeypatch, batches,
                             self._live(critical=["glibc"],
                                        sacrificial=["firefox"]))
        assert erase_entry("lib64old1") in result[2], (
            "an erase nothing is using must stay where libsolv put it -- "
            "that placement is the whole disk saving"
        )

    def test_critical_erase_moves_to_the_last_batch(self, monkeypatch,
                                                    batches):
        result = self._apply(monkeypatch, batches,
                             self._live(critical=["glibc"],
                                        sacrificial=["firefox"]))
        assert erase_entry("glibc") in result[-1]
        assert erase_entry("glibc") not in result[1]

    def test_expendable_erase_moves_to_the_penultimate_batch(
            self, monkeypatch, batches):
        """Before the core, so its volume comes back while there is
        still something to gain -- and after everything safe."""
        result = self._apply(monkeypatch, batches,
                             self._live(critical=["glibc"],
                                        sacrificial=["firefox"]))
        assert erase_entry("firefox") in result[-2]
        assert erase_entry("firefox") not in result[-1]

    def test_installs_are_never_moved(self, monkeypatch, batches):
        """Only erases are ranked; moving an install would break the
        topological order every batch depends on."""
        result = self._apply(monkeypatch, batches,
                             self._live(critical=["glibc"],
                                        sacrificial=["firefox"]))
        for index, name in enumerate(("a", "b", "c", "d")):
            assert f"{name}-1-1.x86_64" in result[index]

    def test_nothing_is_lost(self, monkeypatch, batches):
        before = [e for batch in batches for e in batch]
        result = self._apply(monkeypatch, batches,
                             self._live(critical=["glibc"],
                                        sacrificial=["firefox"]))
        after = [e for batch in result for e in batch]
        assert sorted(map(str, before)) == sorted(map(str, after))

    def test_an_empty_snapshot_changes_nothing(self, monkeypatch, batches):
        """/proc unreadable : degraded to the previous behaviour, never
        a wrong ordering."""
        result = self._apply(monkeypatch, batches, self._live())
        assert result == batches

    def test_a_single_batch_is_left_alone(self, monkeypatch):
        """There is no penultimate batch to move anything to, and rpm
        orders within a transaction by itself."""
        single = [["a-1-1.x86_64", erase_entry("glibc")]]
        result = self._apply(monkeypatch, single,
                             self._live(critical=["glibc"]))
        assert result == single
