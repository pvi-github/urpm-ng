"""Saying where the downloaded files came from.

A mounted installation disc served eight packages out of seventeen and
the report said « 17 téléchargé(s) », so the operator who had just
mounted that disc concluded it was unused.  The provenance was there
all along, in ``cache_files.served_by_server_id``, but nothing surfaced
it.  These tests pin the two halves of the fix : the downloader
counting a disc apart from a mirror, and the report naming what it
counted.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from urpm.cli.display import format_download_sources
from urpm.core.download import (
    Downloader,
    DownloadCoordinator,
    DownloadItem,
    DownloadResult,
)
from urpm.core.removable import (
    PROTOCOL,
    MountPoint,
    is_removable_server,
    resolve,
    was_resolved_from_disc,
)
from urpm.core.sync import SYNTHESIS_PATH


# ── Telling a located disc from a mirror ────────────────────────────────


def disc_row(**overrides):
    row = {
        'id': 7,
        'name': 'Mageia-10-x86_64',
        'protocol': PROTOCOL,
        'host': 'Mageia-10-x86_64',
        'base_path': '',
    }
    row.update(overrides)
    return row


def mirror_row():
    return {
        'id': 3,
        'name': 'rpmfind.net',
        'protocol': 'https',
        'host': 'rpmfind.net',
        'base_path': '/linux/mageia/distrib',
    }


@pytest.fixture
def mounted_disc(tmp_path):
    """A mount carrying ``x86_64/media/core`` with its synthesis."""
    tree = tmp_path / "disc" / "x86_64" / "media" / "core"
    (tree / SYNTHESIS_PATH).parent.mkdir(parents=True)
    (tree / SYNTHESIS_PATH).write_bytes(b"")
    return MountPoint(path=tmp_path / "disc", device="/dev/sr0",
                      filesystem="iso9660")


def test_a_stored_row_is_an_identity_not_yet_a_location():
    """Before resolution there is no disc to report, only an intent."""
    row = disc_row()
    assert is_removable_server(row)
    assert not was_resolved_from_disc(row)


def test_a_located_disc_is_recognised(mounted_disc):
    row = disc_row()
    located = resolve(row, "x86_64/media/core", [mounted_disc])

    assert located is not None
    # ``resolve`` deliberately hands back a ``file`` server, so the
    # protocol test no longer sees a disc ; this is the one that must.
    assert not is_removable_server(located)
    assert was_resolved_from_disc(located)


def test_a_disc_with_no_label_is_still_a_disc(mounted_disc):
    """The trap the predicate is keyed to avoid.

    ``resolve`` stores the row's ``host`` as the disc identity, and
    that is the empty string on a row added without a label.  Keying
    the test on the identity would silently stop counting those.
    """
    located = resolve(disc_row(host=""), "x86_64/media/core",
                      [mounted_disc])

    assert located["removable_identity"] == ""
    assert was_resolved_from_disc(located)


def test_a_mirror_is_never_taken_for_a_disc():
    row = mirror_row()
    assert not is_removable_server(row)
    assert not was_resolved_from_disc(row)
    # ``resolve`` passes a mirror straight through, so a caller can
    # funnel every server through it without branching first.
    assert resolve(row, "10/x86_64/media/core/release", []) is row
    assert not was_resolved_from_disc(
        resolve(row, "10/x86_64/media/core/release", []))


# ── Counting the three origins apart ────────────────────────────────────


class FakeDownloader:
    """Only what :class:`DownloadCoordinator` actually reaches for.

    The transfer itself is not under test here : what is, is that the
    coordinator files each successful result under the right origin.
    """

    only_peers = False
    use_peers = False

    def __init__(self, origins):
        self.origins = origins          # filename -> 'disc' | 'mirror'
        self._session_stats_lock = threading.Lock()
        self._session_server_kbps = {}

    def is_cached(self, item):
        return False

    def get_cache_path(self, item):
        return Path("/nonexistent") / item.filename

    # The coordinator resolves the batch's removable sources before
    # planning; the real implementation returns at once when no source
    # is removable, which is the case here.
    resolve_removable_sources = staticmethod(
        Downloader.resolve_removable_sources)

    def download_one(self, item, progress_callback=None,
                     start_callback=None):
        from_disc = self.origins[item.filename] == 'disc'
        return DownloadResult(
            item=item,
            success=True,
            path=self.get_cache_path(item),
            source_server_id=7 if from_disc else 3,
            from_disc=from_disc,
        )


def item(name, size=1000):
    return DownloadItem(name=name, version="1.0", release="1.mga10",
                        arch="x86_64", media_id=1,
                        relative_path="x86_64/media/core", size=size)


def test_the_counters_are_disjoint_and_name_the_disc():
    items = [item("acpi"), item("grep"), item("bash")]
    origins = {
        items[0].filename: 'disc',
        items[1].filename: 'disc',
        items[2].filename: 'mirror',
    }
    coordinator = DownloadCoordinator(FakeDownloader(origins), max_workers=2)

    results, stats = coordinator.download_all(items, {})

    assert all(r.success for r in results)
    assert stats['from_discs'] == 2
    assert stats['from_upstream'] == 1
    assert stats['from_peers'] == 0
    # Every successful transfer is filed exactly once : a disc that
    # also counted as upstream would double the total and make the
    # report add up to more than it downloaded.
    assert (stats['from_peers'] + stats['from_discs']
            + stats['from_upstream']) == len(results)


# ── What the report says ────────────────────────────────────────────────


def test_the_disc_is_named_beside_the_mirrors():
    line = format_download_sources(
        {'from_peers': 0, 'from_discs': 8, 'from_upstream': 9})
    assert line == "8 from the disc, 9 from mirrors"


def test_a_disc_only_run_does_not_mention_mirrors():
    """Nothing left the machine, and the line has to show that."""
    line = format_download_sources(
        {'from_peers': 0, 'from_discs': 17, 'from_upstream': 0})
    assert line == "17 from the disc"


def test_an_ordinary_run_keeps_naming_the_mirrors():
    line = format_download_sources(
        {'from_peers': 0, 'from_discs': 0, 'from_upstream': 9})
    assert line == "9 from mirrors"


def test_peers_show_their_zero_once_discovery_ran():
    """A zero that carries information : the LAN was asked.

    Kept from the previous report, which always printed the peer
    figure, because ``39 downloaded`` with no origin at all was the
    surprise that comment in ``install.py`` was about.
    """
    line = format_download_sources(
        {'from_peers': 0, 'from_discs': 0, 'from_upstream': 9,
         'peers_used': True})
    assert line == "0 from peers, 9 from mirrors"


def test_peers_switched_off_are_not_mentioned():
    line = format_download_sources(
        {'from_peers': 0, 'from_discs': 2, 'from_upstream': 0,
         'peers_used': False})
    assert line == "2 from the disc"


def test_all_three_origins_read_in_a_fixed_order():
    line = format_download_sources(
        {'from_peers': 1, 'from_discs': 2, 'from_upstream': 3,
         'peers_used': True})
    assert line == "1 from peers, 2 from the disc, 3 from mirrors"


def test_a_fully_cached_run_has_no_origin_to_name():
    line = format_download_sources(
        {'from_peers': 0, 'from_discs': 0, 'from_upstream': 0,
         'peers_used': True})
    assert line == ""


def test_a_caller_with_no_stats_gets_no_line():
    """The summary sites call this unconditionally."""
    assert format_download_sources(None) == ""
    assert format_download_sources({}) == ""
