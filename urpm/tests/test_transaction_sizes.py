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

from types import SimpleNamespace

from urpm.cli.helpers.transaction_sizes import (
    TransactionSizes,
    compute_sizes,
    format_totals,
)

MB = 1024 * 1024


def _action(kind: str, size: int, filesize: int | None = None):
    return SimpleNamespace(
        action=SimpleNamespace(value=kind), size=size, filesize=filesize)


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

    def test_sign_is_shown(self):
        """A bare « 890 MB » next to « freed » reads as more space used,
        which is the opposite of the truth."""
        sizes = compute_sizes([
            _action("install", 10 * MB, 3 * MB),
            _action("remove", 900 * MB),
        ])
        assert "-" in format_totals(sizes, count=2)

    def test_positive_net_is_signed_too(self):
        sizes = compute_sizes([
            _action("install", 300 * MB, 90 * MB),
            _action("remove", 100 * MB),
        ])
        assert "+" in format_totals(sizes, count=2)


class TestTheLine:

    def test_names_both_volumes(self):
        line = format_totals(compute_sizes(
            [_action("install", 100 * MB, 30 * MB)]), count=1)
        low = line.lower()
        assert "30" in line and "100" in line
        assert "download" in low or "téléchargement" in low

    def test_stays_quiet_about_freed_when_nothing_is_removed(self):
        """« freed 0 B » on an ordinary install is noise, and noise is
        what trains an operator to stop reading the line that matters."""
        line = format_totals(compute_sizes(
            [_action("install", 100 * MB, 30 * MB)]), count=1)
        assert "freed" not in line.lower()
        assert "net" not in line.lower()

    def test_mentions_freed_when_something_is_removed(self):
        line = format_totals(compute_sizes([
            _action("install", 100 * MB, 30 * MB),
            _action("remove", 50 * MB),
        ]), count=2)
        low = line.lower()
        assert "freed" in low or "libér" in low
