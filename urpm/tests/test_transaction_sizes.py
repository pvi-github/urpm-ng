"""Download volume and installed footprint are two different numbers.

``PackageAction`` carries both — ``filesize`` is the compressed RPM,
``size`` the unpacked footprint — and the resolver filled its
``install_size`` field from one on the distupgrade path and from the
other everywhere else.  ``upgrade`` then printed the result under
« Download size ».

On a cross-release upgrade that announced a 13.4 GB download which was
in fact the installed footprint.  That figure decides whether an
operator starts at all, and whether they clear space on the right
partition: the payload can live somewhere other than ``/``.

A third number was computed and never shown — what removals give back.
Without it the summary answers « how much arrives », never « will my
disk hold this ».
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from urpm.cli.helpers.transaction_sizes import (
    TransactionSizes,
    compute_sizes,
    format_totals,
)

MB = 1024 * 1024


def _action(kind: str, size: int, filesize: int | None = None,
            from_size: int = 0, media_name: str = "Core Release"):
    return SimpleNamespace(
        action=SimpleNamespace(value=kind), size=size, filesize=filesize,
        from_size=from_size, media_name=media_name)


class TestTheTwoAreKeptApart:

    def test_download_comes_from_filesize(self):
        sizes = compute_sizes([_action("install", 100 * MB, 30 * MB)])
        assert sizes.download == 30 * MB

    def test_installed_comes_from_size(self):
        sizes = compute_sizes([_action("install", 100 * MB, 30 * MB)])
        assert sizes.installed == 100 * MB

    def test_they_do_not_collapse_into_one(self):
        """The defect in one line: reading the same number twice."""
        sizes = compute_sizes([_action("upgrade", 500 * MB, 120 * MB)])
        assert sizes.download != sizes.installed

    def test_filesize_falls_back_to_size(self):
        """Synthesis metadata is not always complete, and a download
        total that silently reads zero is worse than one that
        over-estimates."""
        sizes = compute_sizes([_action("install", 80 * MB, None)])
        assert sizes.download == 80 * MB


class TestWhatCountsAsIncoming:

    def test_install_upgrade_and_reinstall_all_count(self):
        """A reinstall fetches and unpacks its payload like any other,
        even though the net footprint barely moves."""
        sizes = compute_sizes([
            _action("install", 10, 1),
            _action("upgrade", 20, 2),
            _action("reinstall", 40, 4),
        ])
        assert sizes.installed == 70
        assert sizes.download == 7

    def test_removals_do_not_count_as_incoming(self):
        sizes = compute_sizes([_action("remove", 900 * MB)])
        assert sizes.download == 0
        assert sizes.installed == 0

    def test_removals_are_counted_as_freed(self):
        sizes = compute_sizes([_action("remove", 900 * MB)])
        assert sizes.freed == 900 * MB

    def test_missing_sizes_are_treated_as_zero(self):
        """A ``None`` from an incomplete synthesis must not raise in the
        middle of a summary the operator is waiting on."""
        sizes = compute_sizes([_action("install", None, None)])
        assert sizes == TransactionSizes(download=0, installed=0, freed=0)

    def test_empty_plan(self):
        assert compute_sizes([]) == TransactionSizes(0, 0, 0)


class TestTheNetFigure:
    """The one that answers « will my disk hold this »."""

    def test_positive_when_more_arrives_than_leaves(self):
        sizes = compute_sizes([
            _action("install", 300 * MB, 90 * MB),
            _action("remove", 100 * MB),
        ])
        assert sizes.net == 200 * MB

    def test_negative_when_removals_win(self):
        sizes = compute_sizes([
            _action("install", 10 * MB, 3 * MB),
            _action("remove", 900 * MB),
        ])
        assert sizes.net == -890 * MB

    def test_a_negative_net_reads_as_freed(self):
        """890 MB, not 900: the install spends 10 of what the removal
        gave back, and the operator is told what the disk actually
        gains."""
        line = format_totals(compute_sizes([
            _action("install", 10 * MB, 3 * MB),
            _action("remove", 900 * MB),
        ]), count=2)
        low = line.lower()
        assert "freed" in low
        assert "890" in line

    def test_a_positive_net_reads_as_used(self):
        line = format_totals(compute_sizes([
            _action("install", 300 * MB, 90 * MB),
            _action("remove", 100 * MB),
        ]), count=2)
        low = line.lower()
        assert "used" in low
        assert "200" in line

    def test_a_zero_net_says_so(self):
        """Swapping a package for one of the same weight neither frees
        nor spends, and « 0 B freed » invites a second reading."""
        line = format_totals(compute_sizes([
            _action("upgrade", 40 * MB, 12 * MB, from_size=40 * MB),
        ]), count=1)
        low = line.lower()
        assert "no net change" in low


class TestTheLine:

    def test_names_both_volumes(self):
        line = format_totals(compute_sizes(
            [_action("install", 100 * MB, 30 * MB)]), count=1)
        low = line.lower()
        assert "30" in line and "100" in line
        assert "download" in low

    def test_stays_quiet_about_freed_when_nothing_is_removed(self):
        """« freed 0 B » on an ordinary install is noise, and noise is
        what trains an operator to stop reading the line that matters."""
        line = format_totals(compute_sizes(
            [_action("install", 100 * MB, 30 * MB)]), count=1)
        assert "freed" not in line.lower()
        assert "net" not in line.lower()

    def test_reports_the_net_not_the_gross(self):
        """Installing 100 MB while removing 50 uses 50 more; saying
        « 50 MB freed » would be true of one half of the transaction and
        false of the whole."""
        line = format_totals(compute_sizes([
            _action("install", 100 * MB, 30 * MB),
            _action("remove", 50 * MB),
        ]), count=2)
        low = line.lower()
        assert "used" in low
        assert "freed" not in low

    def test_an_upgrade_does_not_claim_the_old_version_as_a_gain(self):
        """The reported case : firefox handed over as a local file,
        390 MB replacing 388.  The line announced « freed 388 MB », as
        if the upgrade were a cleanup, when the disk ends up 2 MB
        heavier."""
        line = format_totals(compute_sizes([
            _action("upgrade", 390 * MB, 120 * MB, from_size=388 * MB),
        ]), count=1)
        low = line.lower()
        assert "388" not in line
        assert "used" in low


class TestTheReplacedVersionsAreFreedToo:
    """An upgrade is one libsolv step carrying only the *new* solvable.

    The version it replaces never becomes an action, so counting only
    explicit removals made a cross-release plan look like pure growth:
    11 GB announced where the net was under 4, and a distupgrade
    refused for want of 8 GB that were never going to be needed.
    """

    def test_an_upgrade_gives_back_what_it_replaces(self):
        sizes = compute_sizes(
            [_action("upgrade", 9 * MB, 3 * MB, from_size=8 * MB)])
        assert sizes.installed == 9 * MB
        assert sizes.freed == 8 * MB
        assert sizes.net == MB, "a 9 MB package over an 8 MB one grows by 1"

    def test_explicit_removals_still_count(self):
        """Both kinds of freeing land in the same figure."""
        sizes = compute_sizes([
            _action("upgrade", 9 * MB, 3 * MB, from_size=8 * MB),
            _action("remove", 4 * MB),
        ])
        assert sizes.freed == 12 * MB

    def test_a_plain_install_replaces_nothing(self):
        sizes = compute_sizes([_action("install", 5 * MB, 2 * MB)])
        assert sizes.freed == 0
        assert sizes.net == 5 * MB

    def test_a_shrinking_upgrade_is_net_negative(self):
        """mga N+1 is usually bigger, but not package by package."""
        sizes = compute_sizes(
            [_action("upgrade", 2 * MB, MB, from_size=6 * MB)])
        assert sizes.net == -4 * MB

    def test_the_download_is_unaffected(self):
        """What crosses the network does not depend on what it
        replaces."""
        sizes = compute_sizes(
            [_action("upgrade", 9 * MB, 3 * MB, from_size=8 * MB)])
        assert sizes.download == 3 * MB

    def test_an_action_without_the_field_is_tolerated(self):
        """Actions synthesised outside the solver -- rpmdb-only orphan
        detection, the --nodeps fast path -- carry no ``from_size``."""
        bare = SimpleNamespace(action=SimpleNamespace(value="upgrade"),
                               size=5 * MB, filesize=2 * MB)
        assert compute_sizes([bare]).freed == 0


class TestWhatIsAlreadyInTheCache:
    """The figure that decides whether the wait is worth it.

    The download total is summed over the plan, before anything has
    looked at the payload directory — the cache check used to live
    inside the download stage, well past the confirmation prompt.  So
    someone whose cache held most of the transaction was told they
    would fetch all of it.
    """

    def _sizes(self, cached: int) -> TransactionSizes:
        return dataclasses.replace(
            compute_sizes([_action("install", 300 * MB, 100 * MB)]),
            cached=cached)

    def test_to_fetch_is_the_remainder(self):
        assert self._sizes(70 * MB).to_fetch == 30 * MB

    def test_nothing_cached_leaves_the_total_alone(self):
        assert self._sizes(0).to_fetch == 100 * MB

    def test_everything_cached_means_nothing_to_fetch(self):
        assert self._sizes(100 * MB).to_fetch == 0

    def test_it_never_goes_negative(self):
        """``cached`` is summed over the download items and ``download``
        over the actions; a medium with neither URL nor server yields an
        action without an item.  A broken configuration must not print a
        negative byte count."""
        assert self._sizes(250 * MB).to_fetch == 0

    def test_the_line_names_both(self):
        line = format_totals(self._sizes(70 * MB), count=1)
        assert "30" in line and "70" in line
        assert "cach" in line.lower()

    def test_the_line_stays_quiet_when_the_cache_is_empty(self):
        """A parenthesis reading « 0 B already cached » on every fresh
        install is the kind of noise that stops being read."""
        line = format_totals(self._sizes(0), count=1)
        assert "cach" not in line.lower()


class TestAPackageAlreadyOnTheDisk:
    """``urpm install ./foo.rpm`` fetches nothing."""

    def test_a_local_rpm_is_not_a_download(self):
        sizes = compute_sizes(
            [_action("install", 390 * MB, 120 * MB,
                     media_name="@LocalRPMs")])
        assert sizes.download == 0

    def test_it_still_occupies_its_footprint(self):
        sizes = compute_sizes(
            [_action("install", 390 * MB, 120 * MB,
                     media_name="@LocalRPMs")])
        assert sizes.installed == 390 * MB

    def test_the_fallback_does_not_resurrect_it(self):
        """A header carries no download size.  With the fallback still
        applying, the missing figure would be replaced by the installed
        footprint and announce 390 MB crossing a network nothing is
        crossing."""
        sizes = compute_sizes(
            [_action("install", 390 * MB, None, media_name="@LocalRPMs")])
        assert sizes.download == 0

    def test_media_packages_in_the_same_plan_still_count(self):
        """A local RPM usually drags dependencies in behind it, and
        those do get fetched."""
        sizes = compute_sizes([
            _action("install", 390 * MB, 120 * MB,
                    media_name="@LocalRPMs"),
            _action("install", 10 * MB, 4 * MB),
        ])
        assert sizes.download == 4 * MB
        assert sizes.installed == 400 * MB
