"""Will the disks hold the upgrade — and is the answer derived?

A tester's ``/usr`` went from 1.2 GB to 3.5 GB across mga9 → mga10 and
nothing anywhere had checked whether there was room.  rpm's own disk
check fires at commit time, several gigabytes of download too late to
help anyone decide whether to start.

Two things land in two places, and on a stock Mageia install not on the
same partition: the payload in ``/var/cache/urpm``, the unpacked files
in ``/usr``.  Adding them together and comparing the sum to either one
answers a question nobody asked, so each filesystem is measured on its
own and they collapse into one when ``st_dev`` says they are the same.

What these tests mostly guard is not the arithmetic but its
provenance.  A pre-flight built on invented constants would refuse
upgrades that fit and admit ones that do not, and the operator refused
once for no reason never trusts it again.  So: the margin has to come
from each filesystem's own reserved blocks, the peak from the erases
Tx B actually defers, and the payload has to be charged where it
actually lands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from urpm.core.distupgrade import root_space
from urpm.core.distupgrade.live_system import LiveSystemSnapshot
from urpm.core.distupgrade.root_space import (
    TOLERATED_SHORTFALL_RATIO,
    UNCERTAINTY_RATIO,
    FilesystemNeed,
    batch_transient_bytes,
    RootSpaceError,
    RootSpaceEstimate,
    check_root_space,
    deferred_erase_bytes,
    estimate,
)

MB = 1024 * 1024
GB = 1024 * MB


@dataclass
class Action:
    """``PackageAction``-shaped, with only the fields sizes need."""

    action: str
    name: str
    size: int = 0
    filesize: int = 0
    from_size: int = 0


def _live(critical=(), sacrificial=()):
    return LiveSystemSnapshot(critical=frozenset(critical),
                              sacrificial=frozenset(sacrificial))


@pytest.fixture
def no_live_packages(monkeypatch):
    """Nothing running : erases travel at their libsolv position."""
    from urpm.core.distupgrade import live_system
    monkeypatch.setattr(live_system, "snapshot", lambda *a, **kw: _live())


def _only(est):
    assert len(est.filesystems) == 1, (
        f"expected a single filesystem, got {est.filesystems}")
    return est.filesystems[0]


class TestThePeakIsAboveTheNet:
    """The whole reason a net figure is not enough: Tx B holds erases
    back, so their bytes arrive after everything has been installed."""

    @staticmethod
    def _plan():
        return [
            Action("install", "new", size=1000 * MB),
            Action("remove", "glibc", size=300 * MB),
        ]

    def _peak(self, monkeypatch, live):
        from urpm.core.distupgrade import live_system
        monkeypatch.setattr(live_system, "snapshot", lambda *a, **kw: live)
        return _only(estimate(self._plan()))

    def test_the_net_is_the_same_either_way(self, monkeypatch):
        """The removal is deducted from the footprint in both cases;
        only *when* its bytes come back changes."""
        for live in (_live(critical=["glibc"]), _live()):
            assert self._peak(monkeypatch, live).unpacked == 700 * MB

    def test_deferring_an_erase_costs_exactly_its_size(self, monkeypatch):
        """An erase held to the last batch frees nothing while the
        installs are landing, so the peak carries it in full."""
        held = self._peak(monkeypatch, _live(critical=["glibc"]))
        travelling = self._peak(monkeypatch, _live())
        assert held.deferred == 300 * MB
        assert travelling.deferred == 0
        assert held.required - travelling.required == 300 * MB, (
            "space freed as the batches go by is space the peak never "
            "needs -- counting it would refuse upgrades that fit"
        )

    def test_a_sacrificial_erase_is_deferred_too(self, monkeypatch):
        """Penultimate batch is still after every install."""
        from urpm.core.distupgrade import live_system
        monkeypatch.setattr(live_system, "snapshot",
                            lambda *a, **kw: _live(sacrificial=["firefox"]))
        actions = [Action("remove", "firefox", size=250 * MB)]
        assert deferred_erase_bytes(actions) == 250 * MB

    def test_no_removals_means_no_snapshot(self, monkeypatch):
        """``snapshot()`` walks every process on the machine; skipping
        it when there is nothing to rank keeps an install-only plan
        from paying half a second for a zero."""
        from urpm.core.distupgrade import live_system

        def _boom(*_a, **_kw):
            raise AssertionError("snapshot must not be taken here")

        monkeypatch.setattr(live_system, "snapshot", _boom)
        assert deferred_erase_bytes([Action("install", "new", size=MB)]) == 0

    def test_an_empty_snapshot_defers_nothing(self, no_live_packages):
        """An unreadable /proc is not a degraded estimate: Tx B will
        defer nothing either, so zero is the correct answer."""
        actions = [Action("remove", "glibc", size=300 * MB)]
        assert deferred_erase_bytes(actions) == 0


class TestThePayloadAndTheFootprintNeverCoexist:
    """Tx B unlinks each batch's ``.rpm`` as soon as it commits
    (``stage3._purge_installed_batch_rpms``), so the download cache
    drains at the rate the footprint arrives.  The disk holds the
    payload at the start and the footprint at the end.

    Adding the two is what made this check demand 14.44 GB on a
    migration that ran three times in 5.98 GB.
    """

    def test_the_peak_is_the_larger_of_the_two_not_the_sum(self):
        need = FilesystemNeed(mountpoint=Path("/"), payload=3 * GB,
                              unpacked=4 * GB, available=8 * GB)
        assert need.required == 4 * GB, (
            "payload + footprint would be 7 GB, and the disk never "
            "holds both at once"
        )

    def test_a_payload_bigger_than_the_footprint_sets_the_peak(self):
        """Everything downloaded before anything is unpacked: the
        cache alone is the high-water mark."""
        need = FilesystemNeed(mountpoint=Path("/"), payload=5 * GB,
                              unpacked=GB, available=8 * GB)
        assert need.required == 5 * GB

    def test_the_terms_that_really_are_simultaneous_still_add(self):
        """One batch mid-commit, the deferred erases and the declared
        allowance sit on top of the peak, not beside it."""
        need = FilesystemNeed(mountpoint=Path("/"), payload=3 * GB,
                              unpacked=4 * GB, deferred=100 * MB,
                              transient=600 * MB, uncertainty=GB,
                              available=8 * GB)
        assert need.required == 4 * GB + 100 * MB + 600 * MB + GB


class TestTheTransientIsOneBatchUnpacked:
    """A batch is capped in compressed bytes; what it costs on disk
    before its commit is its uncompressed size."""

    def test_it_converts_the_batch_cap_by_the_plans_own_ratio(self):
        from urpm.core.distupgrade.stage3 import TX_B_BATCH_BYTES
        from urpm.core.transaction_sizes import TransactionSizes
        sizes = TransactionSizes(download=GB, installed=3 * GB, freed=0)
        assert batch_transient_bytes(sizes) == TX_B_BATCH_BYTES * 3

    def test_a_plan_with_nothing_to_download_has_no_transient(self):
        """No batch to be caught between its writes and its commit."""
        from urpm.core.transaction_sizes import TransactionSizes
        assert batch_transient_bytes(
            TransactionSizes(download=0, installed=GB, freed=0)) == 0

    def test_it_never_exceeds_the_whole_plan(self):
        """A plan smaller than one batch is installed in one go; the
        transient cannot be larger than everything there is."""
        from urpm.core.transaction_sizes import TransactionSizes
        sizes = TransactionSizes(download=MB, installed=4 * MB, freed=0)
        assert batch_transient_bytes(sizes) == 4 * MB

    def test_it_reads_the_constant_that_governs_the_batches(self):
        """Not a copy of 200 MB : the same constant stage3 slices
        with, so the two cannot drift apart."""
        import inspect
        source = inspect.getsource(batch_transient_bytes)
        assert "TX_B_BATCH_BYTES" in source
        assert "200" not in source


class TestTheAllowanceIsDeclaredNotHidden:
    """The one figure here that is chosen rather than read.  It is
    proportional, named, and printed on its own line — a visible
    allowance stays arguable, a hidden one becomes dogma."""

    def test_it_scales_with_the_plan(self, no_live_packages):
        actions = [Action("install", "new", size=10 * GB, filesize=3 * GB)]
        need = _only(estimate(actions))
        assert need.uncertainty == int(10 * GB * UNCERTAINTY_RATIO)

    def test_a_small_plan_gets_a_small_allowance(self, no_live_packages):
        """A point release must not be charged a cross-release margin."""
        actions = [Action("install", "new", size=100 * MB, filesize=30 * MB)]
        need = _only(estimate(actions))
        assert need.uncertainty == int(100 * MB * UNCERTAINTY_RATIO)

    def test_it_has_its_own_line_in_the_report(self):
        need = FilesystemNeed(mountpoint=Path("/"), unpacked=GB,
                              uncertainty=100 * MB, available=8 * GB)
        est = RootSpaceEstimate(filesystems=(need,))
        assert "allowance" in root_space.describe(est)

    def test_a_payload_only_filesystem_carries_none(self, no_live_packages,
                                                    tmp_path):
        """Nothing is unpacked on the download partition, so no
        scriptlet runs there and no allowance applies."""
        payload = tmp_path / "payload"
        payload.mkdir()
        from urpm.core.distupgrade import live_system
        need = FilesystemNeed(mountpoint=payload, payload=GB,
                              available=3 * GB)
        assert need.holds_payload_only
        assert need.uncertainty == 0


class TestTheMeasuredMigration:
    """The mga9 → mga10 run that exposed both over-counts.

    Reported figures: 5.98 GB free, 3.43 GB of payload, 11 GB of gross
    installed footprint, 7.5 GB of it replacing installed versions.
    The machine completed this migration three times, bottoming out
    around 700 MB free.  The check refused it at 14.44 GB needed.
    """

    @pytest.fixture
    def plan(self):
        return [Action("upgrade", "everything", size=11 * GB,
                       filesize=int(3.43 * GB), from_size=int(7.5 * GB))]

    @pytest.fixture
    def measured(self, monkeypatch, no_live_packages):
        monkeypatch.setattr(
            root_space, "_free_and_reserved",
            lambda p: (int(5.98 * GB), int(0.884 * GB)))

    def test_it_no_longer_refuses(self, plan, measured):
        est = estimate(plan, payload_dir=Path("/"))
        assert est.fits, root_space.describe(est)

    def test_the_prediction_lands_near_the_observed_low_water_mark(
            self, plan, measured):
        """Observed: about 700 MB free at the worst moment."""
        need = _only(estimate(plan, payload_dir=Path("/")))
        left = need.available - need.required
        assert 0.5 * GB < left < GB, (
            f"predicted {left / GB:.2f} GB left, observed about 0.7"
        )

    def test_the_net_footprint_is_no_longer_the_gross_one(self, plan,
                                                          measured):
        """11 GB arrives, 7.5 GB of it replaces something."""
        need = _only(estimate(plan, payload_dir=Path("/")))
        assert 3 * GB < need.unpacked < 4 * GB


class TestEachFilesystemGetsItsOwnVerdict:
    """`/var/cache/urpm` and `/usr` are not on the same partition on a
    stock install, and one sum over both answers neither question."""

    @pytest.fixture
    def split(self, monkeypatch, tmp_path):
        """A payload directory on a filesystem of its own."""
        payload = tmp_path / "payload"
        payload.mkdir()
        monkeypatch.setattr(
            root_space, "_device",
            lambda p: 42 if str(p).startswith(str(payload)) else 1)
        monkeypatch.setattr(
            root_space, "_free_and_reserved",
            lambda p: ((3 * GB, 0) if str(p).startswith(str(payload))
                       else (4 * GB, 0)))
        return payload

    def test_the_payload_is_charged_where_it_lands(self, no_live_packages,
                                                   split):
        actions = [Action("install", "new", size=3 * GB, filesize=2 * GB)]
        est = estimate(actions, payload_dir=split)
        assert len(est.filesystems) == 2
        footprint, payload = est.filesystems
        assert footprint.payload == 0, (
            "the download does not land on the filesystem being unpacked to"
        )
        assert payload.payload == 2 * GB
        assert payload.unpacked == 0

    def test_a_payload_elsewhere_does_not_condemn_the_root(
            self, no_live_packages, split):
        """3 GB unpacked into 4 GB free, 2 GB downloaded into 3 GB free.
        Summed against either filesystem it would be refused; measured
        separately it fits, and it does fit."""
        actions = [Action("install", "new", size=3 * GB, filesize=2 * GB)]
        est = estimate(actions, payload_dir=split)
        assert est.fits
        assert est.short == ()

    def test_a_full_payload_partition_is_caught_on_its_own(
            self, no_live_packages, split):
        """The root has all the room in the world and it does not help:
        the download still has nowhere to go."""
        actions = [Action("install", "new", size=100 * MB, filesize=9 * GB)]
        est = estimate(actions, payload_dir=split)
        assert not est.fits
        assert [need.mountpoint for need in est.short] == [split]

    def test_the_same_filesystem_collapses_to_one_verdict(
            self, no_live_packages):
        actions = [Action("install", "new", size=500 * MB,
                          filesize=200 * MB)]
        need = _only(estimate(actions, payload_dir=Path("/")))
        assert need.payload == 200 * MB
        assert need.required == (500 * MB + need.transient
                                 + need.uncertainty), (
            "one filesystem, one verdict : the footprint sets the base "
            "and the payload is subsumed by it, not added to it"
        )

    def test_a_payload_dir_not_created_yet_is_still_measured(
            self, no_live_packages, tmp_path):
        """``payload_dir`` pointing at a directory nobody has created
        is the normal state on a first run -- the downloader makes it,
        after this check.  Measuring the parent names the same
        filesystem; failing to measure would drop the check silently."""
        missing = tmp_path / "not" / "there" / "yet"
        actions = [Action("install", "new", size=MB, filesize=MB)]
        est = estimate(actions, payload_dir=missing)
        assert est.filesystems, "the check must still produce a verdict"
        assert sum(need.payload for need in est.filesystems) == MB

    def test_nothing_left_to_fetch_lists_no_payload_filesystem(
            self, no_live_packages, split):
        """On ``--resume`` the payload is already on disk and nothing
        more will land there; a verdict on it would be noise."""
        actions = [Action("install", "new", size=GB, filesize=GB)]
        est = estimate(actions, payload_dir=split, payload_bytes=0)
        assert len(est.filesystems) == 1


class TestTheMarginIsTheFilesystemsOwn:
    """No invented number.  ``statvfs`` publishes the reserve that
    ``mkfs`` set aside, and the check simply declines to spend it."""

    def test_available_is_the_non_root_figure(self, no_live_packages):
        need = _only(estimate([]))
        st = os.statvfs(root_space._footprint_target(Path("/")))
        assert need.available == st.f_bavail * st.f_frsize

    def test_reserved_is_the_gap_between_free_and_available(
            self, no_live_packages):
        need = _only(estimate([]))
        st = os.statvfs(root_space._footprint_target(Path("/")))
        assert need.reserved == (st.f_bfree - st.f_bavail) * st.f_frsize

    def test_the_reserve_is_never_spent(self):
        """Root could dip into it.  The verdict must not."""
        need = FilesystemNeed(mountpoint=Path("/"), unpacked=900 * MB,
                              available=800 * MB, reserved=500 * MB)
        assert not need.fits, (
            "900 MB needed, 800 available and 500 reserved -- fitting "
            "only by eating the reserve is not fitting"
        )

    def test_no_reserve_is_reported_as_none(self):
        """A filesystem made with ``-m 0`` has no reserve, which is
        what its administrator asked for; nothing is invented to
        replace it."""
        est = RootSpaceEstimate(filesystems=(
            FilesystemNeed(mountpoint=Path("/"), available=GB, reserved=0),))
        assert "reserved blocks" not in root_space.describe(est)


class TestTheFilesystemMeasuredIsTheRightOne:

    def test_usr_is_what_gets_measured(self, tmp_path):
        """Better than 90% of a cross-release upgrade unpacks there."""
        (tmp_path / "usr").mkdir()
        assert root_space._footprint_target(tmp_path) == tmp_path / "usr"

    def test_a_tree_without_usr_falls_back_to_the_root(self, tmp_path):
        """Keeps this usable against a chroot or an image being built."""
        assert root_space._footprint_target(tmp_path) == tmp_path

    def test_mountpoint_walks_up_to_a_real_mount_point(self):
        found = root_space._mountpoint(Path("/usr/lib"))
        assert os.path.ismount(found), (
            f"{found} is not a mount point; the estimate would name a "
            "filesystem the operator cannot act on"
        )

    def test_mountpoint_stops_at_the_boundary_it_finds(self, tmp_path,
                                                       monkeypatch):
        """Two levels with the same device, the parent with another:
        the walk must stop at the child of the changed one."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        monkeypatch.setattr(
            root_space, "_device",
            lambda p: 9 if Path(p) in (tmp_path, tmp_path.parent) else 1)
        assert root_space._mountpoint(deep) == tmp_path / "a"

    def test_a_separate_var_is_reported_not_silently_ignored(
            self, no_live_packages, tmp_path, monkeypatch):
        """Package metadata carries no file list, so the footprint
        cannot be split across filesystems.  Saying which mount is
        charged elsewhere beats a figure that quietly means something
        else."""
        (tmp_path / "usr").mkdir()
        (tmp_path / "var").mkdir()
        monkeypatch.setattr(
            root_space, "_device",
            lambda p: 7 if Path(p).name == "var" else 1)
        est = estimate([], root=tmp_path)
        assert tmp_path / "var" in est.unmodelled

    def test_a_measured_filesystem_is_not_also_called_unmodelled(
            self, no_live_packages, tmp_path, monkeypatch):
        """When the payload lives on the separate /var, /var *is*
        measured -- listing it as unmodelled would contradict the block
        printed just above."""
        (tmp_path / "usr").mkdir()
        var = tmp_path / "var"
        var.mkdir()
        monkeypatch.setattr(
            root_space, "_device",
            lambda p: 7 if Path(p).name == "var" else 1)
        monkeypatch.setattr(root_space, "_free_and_reserved",
                            lambda p: (10 * GB, 0))
        monkeypatch.setattr(root_space, "_mountpoint", lambda p: Path(p))
        est = estimate([Action("install", "new", size=MB, filesize=MB)],
                       root=tmp_path, payload_dir=var)
        assert var not in est.unmodelled


class TestTheRefusal:

    @pytest.fixture
    def cramped(self, monkeypatch):
        monkeypatch.setattr(
            root_space, "estimate",
            lambda *a, **kw: RootSpaceEstimate(filesystems=(
                FilesystemNeed(mountpoint=Path("/"), unpacked=3 * GB,
                               deferred=500 * MB, payload=GB,
                               available=2 * GB, reserved=300 * MB),)))

    @pytest.fixture
    def payload_partition_full(self, monkeypatch):
        monkeypatch.setattr(
            root_space, "estimate",
            lambda *a, **kw: RootSpaceEstimate(filesystems=(
                FilesystemNeed(mountpoint=Path("/"), unpacked=GB,
                               available=50 * GB),
                FilesystemNeed(mountpoint=Path("/var"), payload=9 * GB,
                               available=2 * GB),)))

    def test_it_raises_with_the_shortfall(self, cramped):
        with pytest.raises(RootSpaceError) as excinfo:
            check_root_space([])
        assert "short" in str(excinfo.value)

    def test_it_names_the_commands_that_recover_space(self, cramped):
        with pytest.raises(RootSpaceError) as excinfo:
            check_root_space([])
        message = str(excinfo.value)
        for command in ("urpm cache flush", "urpm autoremove --oldkernels",
                        "urpm autoremove"):
            assert command in message

    def test_a_full_payload_partition_does_not_send_you_to_autoremove(
            self, payload_partition_full):
        """Removing packages from a root with 50 GB free frees nothing
        on the partition that is actually full."""
        with pytest.raises(RootSpaceError) as excinfo:
            check_root_space([])
        message = str(excinfo.value)
        assert "urpm cache flush" in message
        assert "urpm autoremove" not in message
        assert "payload_dir" in message, (
            "the durable fix for a small cache partition is to move the "
            "payload, and distupgrade has no --download-dir flag"
        )

    def test_only_the_short_filesystem_is_named(self, payload_partition_full):
        with pytest.raises(RootSpaceError) as excinfo:
            check_root_space([])
        first_line = str(excinfo.value).splitlines()[0]
        assert "/var" in first_line
        assert not first_line.startswith("/ ")

    def test_it_says_what_it_does_not_count(self, cramped):
        """Scriptlet output -- initramfs, font caches -- is owned by no
        package and appears in no plan.  Stating it beats padding the
        figures with a coefficient that pretends to know."""
        with pytest.raises(RootSpaceError) as excinfo:
            check_root_space([])
        assert "initramfs" in str(excinfo.value)

    def test_a_plan_that_fits_returns_the_estimate(self, no_live_packages):
        est = check_root_space([])
        assert est.fits
        assert _only(est).required == 0


class TestARefusalCannotBeSurerThanTheEstimate:
    """The check carries a declared allowance for what the plan cannot
    describe.  Stopping a migration for less than half of that claims a
    precision it does not have — which is what happened to a tester
    stopped 169 MB short by a figure hedged by 1.1 GB, on a machine
    that had completed the same migration three times.
    """

    @staticmethod
    def _need(shortfall, uncertainty=GB):
        """A filesystem short by exactly *shortfall*."""
        return FilesystemNeed(
            mountpoint=Path("/"), unpacked=4 * GB, uncertainty=uncertainty,
            available=4 * GB + uncertainty - shortfall)

    def test_the_band_is_half_the_allowance(self):
        need = self._need(0, uncertainty=GB)
        assert need.tolerance == int(GB * TOLERATED_SHORTFALL_RATIO)

    def test_a_shortfall_inside_the_band_does_not_block(self):
        need = self._need(100 * MB)
        assert not need.fits
        assert not need.blocks

    def test_a_shortfall_past_the_band_blocks(self):
        need = self._need(600 * MB)
        assert need.blocks

    def test_the_boundary_belongs_to_the_tolerant_side(self):
        """Exactly at the band is still inside it : a strict > keeps
        the refusal for gaps the model can actually resolve."""
        need = self._need(int(GB * TOLERATED_SHORTFALL_RATIO))
        assert not need.blocks

    def test_a_payload_partition_gets_no_band(self):
        """Its figure is a sum of file sizes, known to the byte.  There
        is no uncertainty to spend, so any shortfall is real."""
        need = FilesystemNeed(mountpoint=Path("/var"), payload=2 * GB,
                              available=2 * GB - MB)
        assert need.tolerance == 0
        assert need.blocks

    def test_check_returns_instead_of_raising_inside_the_band(self,
                                                              monkeypatch):
        monkeypatch.setattr(
            root_space, "estimate",
            lambda *a, **kw: RootSpaceEstimate(
                filesystems=(self._need(100 * MB),)))
        est = check_root_space([])
        assert est.tight, "the operator must still be told"
        assert not est.blocking

    def test_check_still_raises_past_the_band(self, monkeypatch):
        monkeypatch.setattr(
            root_space, "estimate",
            lambda *a, **kw: RootSpaceEstimate(
                filesystems=(self._need(600 * MB),)))
        with pytest.raises(RootSpaceError):
            check_root_space([])

    def test_one_blocking_filesystem_stops_the_lot(self, monkeypatch):
        """A tolerable gap elsewhere does not soften a real one."""
        monkeypatch.setattr(
            root_space, "estimate",
            lambda *a, **kw: RootSpaceEstimate(filesystems=(
                self._need(100 * MB),
                FilesystemNeed(mountpoint=Path("/var"), payload=2 * GB,
                               available=GB))))
        with pytest.raises(RootSpaceError) as excinfo:
            check_root_space([])
        assert "/var" in str(excinfo.value)


class TestTheWarningSaysWhatItDoesNotKnow:

    @staticmethod
    def _tight():
        return RootSpaceEstimate(filesystems=(FilesystemNeed(
            mountpoint=Path("/"), unpacked=4 * GB, uncertainty=GB,
            available=4 * GB + GB - 100 * MB),))

    def test_nothing_tight_says_nothing(self, no_live_packages):
        est = RootSpaceEstimate(filesystems=(FilesystemNeed(
            mountpoint=Path("/"), unpacked=GB, available=8 * GB),))
        assert root_space.shortfall_warning(est) == ""

    def test_it_gives_both_figures(self):
        """The gap alone reads as a verdict; the gap next to the band
        it sits inside reads as what it is."""
        text = root_space.shortfall_warning(self._tight())
        assert "100.0 MB" in text
        assert "512.0 MB" in text

    def test_it_offers_the_way_out(self):
        assert "urpm cache flush" in root_space.shortfall_warning(
            self._tight()) or "autoremove" in root_space.shortfall_warning(
                self._tight())

    def test_it_does_not_pretend_to_a_verdict(self):
        """« may still fit » is the honest claim ; « cannot hold » is
        the one reserved for a gap past the band."""
        text = root_space.shortfall_warning(self._tight())
        assert "may still fit" in text
        assert "cannot hold" not in text


class TestDescribeIsShownEvenWhenItFits:
    """« It fits with 3 GB to spare » and « it fits with 40 MB to
    spare » are the same verdict and not the same decision."""

    def test_the_figures_are_all_there(self):
        est = RootSpaceEstimate(filesystems=(
            FilesystemNeed(mountpoint=Path("/"), unpacked=2 * GB,
                           deferred=500 * MB, payload=GB,
                           available=8 * GB, reserved=400 * MB),))
        text = root_space.describe(est)
        for expected in ("free space", "net footprint", "downloaded RPMs",
                         "held to the end", "peak needed"):
            assert expected in text

    def test_a_net_negative_upgrade_shows_its_sign(self):
        """Removals can outweigh arrivals; the sign is the point."""
        est = RootSpaceEstimate(filesystems=(
            FilesystemNeed(mountpoint=Path("/"), unpacked=-2 * GB,
                           available=8 * GB),))
        assert "-2.00 GB" in root_space.describe(est)

    def test_a_net_negative_plan_needs_nothing(self):
        """An upgrade that removes more than it brings needs no extra
        room.  Reporting a negative peak would print
        « -2147483648 B » at an operator and mean nothing."""
        need = FilesystemNeed(mountpoint=Path("/"), unpacked=-2 * GB,
                              available=8 * GB)
        assert need.required == 0
        assert need.fits

    def test_the_columns_line_up_across_filesystems(self):
        """Padding is computed, not written into the msgid: a column
        laid out with spaces in English goes ragged everywhere else --
        and the width has to span both blocks, not each on its own."""
        est = RootSpaceEstimate(filesystems=(
            FilesystemNeed(mountpoint=Path("/"), unpacked=2 * GB,
                           deferred=500 * MB, available=8 * GB),
            FilesystemNeed(mountpoint=Path("/var"), payload=GB,
                           available=3 * GB)))
        import re
        columns = set()
        for line in root_space.describe(est).splitlines():
            match = re.match(r"  (\S.*?)\s{2,}(\S)", line)
            if match:
                columns.add(match.start(2))
        assert len(columns) == 1, (
            f"values start at different columns: {columns}")

    def test_a_payload_only_filesystem_has_no_footprint_line(self):
        """« net footprint +0 B » on a partition that only holds the
        download says nothing true."""
        est = RootSpaceEstimate(filesystems=(
            FilesystemNeed(mountpoint=Path("/var"), payload=GB,
                           available=3 * GB),))
        assert "net footprint" not in root_space.describe(est)

    def test_zero_terms_stay_out_of_the_way(self):
        """« held to the end 0 B » on a plan that defers nothing is
        noise, and noise is what trains an operator to stop reading."""
        est = RootSpaceEstimate(filesystems=(
            FilesystemNeed(mountpoint=Path("/"), unpacked=GB,
                           available=8 * GB),))
        text = root_space.describe(est)
        assert "held to the end" not in text
        assert "downloaded RPMs" not in text
