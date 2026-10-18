"""Who serves what, when a disc is mounted.

Measured on a real Mageia 9 → 10 distupgrade: of 2747 packages, the
mounted disc served **359** and the mirrors 2388, although the disc
carried 69% of what was being installed (2093 of the 3015 packages, at
identical NEVRA).

The planner was to blame.  It splits the volume between servers in
proportion to their measured bandwidth, and a ``cdrom`` row has none,
so the disc entered the arithmetic at the 1 KB/s fallback against
mirrors measured between 74 and 1942 KB/s and came out with a residual
share.  Nothing could ever correct it either: a bandwidth is only
recorded for transfers that took at least half a second, which a local
copy never does.

Giving the disc a speed would have been the wrong repair.  For a
removable source the throughput is beside the point — nobody who
inserts a disc expects a rate to be negotiated — so a local source
leaves the bandwidth competition entirely and takes what it can serve.
The mirrors keep the rest, and the two run at the same time.

Every test here feeds a synthetic mount table: the developer's machine
has a real Mageia disc mounted, and a test that read ``/proc/mounts``
would pass or fail on what is in the drive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from urpm.core import removable
from urpm.core.download import (
    DownloadItem,
    Downloader,
    _assign_local_sources,
    _plan_server_assignments,
)
from urpm.core.sync import SYNTHESIS_PATH


#: Where a Mageia DVD puts its core tree, relative to the mount point.
DISC_CORE = "x86_64/media/core"

#: Where a mirror puts the same packages.
MIRROR_CORE = "10/x86_64/media/core/release"

#: A medium no pressing carries: an updated package is a different
#: NEVRA, in this medium, which no disc is ever attached to.
MIRROR_UPDATES = "10/x86_64/media/core/updates"


@pytest.fixture(autouse=True)
def no_real_disc(tmp_path, monkeypatch):
    """Point the mount table at an empty file by default."""
    empty = tmp_path / "mounts-empty"
    empty.write_text("")
    monkeypatch.setattr(removable, "MOUNT_TABLE", empty)


@pytest.fixture
def mounted_disc(tmp_path, monkeypatch):
    """A tree shaped like a mounted DVD, declared in the mount table."""
    root = tmp_path / "disc"
    synthesis = root / DISC_CORE / SYNTHESIS_PATH
    synthesis.parent.mkdir(parents=True)
    synthesis.write_bytes(b"never read by the planner")

    table = tmp_path / "mounts"
    table.write_text(f"/dev/sr0 {root} iso9660 ro,relatime 0 0\n")
    monkeypatch.setattr(removable, "MOUNT_TABLE", table)
    return root


def disc_server(server_id=10):
    """A stored ``cdrom`` row, with its own layout for this medium."""
    return {
        "id": server_id,
        "name": "Mageia-10-x86_64",
        "protocol": removable.PROTOCOL,
        "host": "Mageia-10-x86_64",
        "base_path": "",
        "priority": 90,
        "media_relative_path": DISC_CORE,
        "bandwidth_kbps": None,
    }


def mirror(server_id, host, kbps):
    return {
        "id": server_id,
        "name": host,
        "protocol": "https",
        "host": host,
        "base_path": "/mageia",
        "priority": 50,
        "bandwidth_kbps": kbps,
    }


def items(count, *, servers, relative_path=MIRROR_CORE, size=1_000_000,
          first=0):
    """*count* items, each with its own copy of *servers*.

    Copied per item the way ``operations.py`` builds them, because the
    planner reorders each item's list in place.
    """
    return [
        DownloadItem(
            name=f"pkg{i:04d}", version="1.0", release="1.mga10",
            arch="x86_64", media_id=1 if relative_path == MIRROR_CORE else 2,
            relative_path=relative_path,
            servers=[dict(s) for s in servers],
            size=size,
        )
        for i in range(first, first + count)
    ]


def first_server(item):
    return item.servers[0]


# ── The regression ──────────────────────────────────────────────────────


def test_the_disc_serves_every_item_it_can(mounted_disc):
    batch = items(20, servers=[mirror(1, "fast.example.org", 1942),
                               disc_server()])

    Downloader.resolve_removable_sources(batch)
    _plan_server_assignments(batch, {})

    assert all(first_server(item)["id"] == 10 for item in batch)


def test_a_fast_mirror_does_not_outbid_the_disc(mounted_disc):
    """The exact shape of the field failure.

    The mirrors' speeds are the ones measured on the reporting
    machine; the disc has none, which is why it used to lose.
    """
    batch = items(20, servers=[
        mirror(1, "fr2.rpmfind.net", 1942),
        mirror(2, "ftp-stud.hs-esslingen.de", 297),
        mirror(3, "ftp.klid.dk", 74),
        disc_server(),
    ])

    Downloader.resolve_removable_sources(batch)
    _plan_server_assignments(batch, {"1": 1942})

    served_by_disc = sum(1 for item in batch if first_server(item)["id"] == 10)
    assert served_by_disc == 20


def test_the_mirrors_keep_what_the_disc_cannot_serve(mounted_disc):
    """Updates come from a medium no disc is attached to.

    Those items must still go through the proportional allocation, so
    the two streams work at the same time rather than one after the
    other.
    """
    mirrors = [mirror(1, "fast.example.org", 1942),
               mirror(2, "slow.example.org", 74)]
    release = items(10, servers=mirrors + [disc_server()])
    updates = items(10, servers=mirrors, relative_path=MIRROR_UPDATES,
                    first=100)

    batch = release + updates
    Downloader.resolve_removable_sources(batch)
    _plan_server_assignments(batch, {})

    assert all(first_server(item)["id"] == 10 for item in release)
    assert all(first_server(item)["id"] in (1, 2) for item in updates)


def test_the_disc_remains_a_first_choice_not_the_only_one(mounted_disc):
    """A disc pulled out mid-run has to fall through to the mirrors.

    ``download_one`` walks the list in order, so what matters is that
    the mirrors are still behind the disc rather than dropped.
    """
    batch = items(1, servers=[mirror(1, "a.example.org", 500),
                              mirror(2, "b.example.org", 500),
                              disc_server()])

    Downloader.resolve_removable_sources(batch)
    _plan_server_assignments(batch, {})

    ids = [server["id"] for server in batch[0].servers]
    assert ids[0] == 10
    assert sorted(ids[1:]) == [1, 2]


# ── Without a disc, nothing changes ─────────────────────────────────────


def test_an_absent_disc_leaves_the_bandwidth_plan_alone():
    """No mount table entry: the disc is not a source at all."""
    batch = items(4, servers=[mirror(1, "fast.example.org", 2000),
                              mirror(2, "slow.example.org", 100),
                              disc_server()])

    Downloader.resolve_removable_sources(batch)
    _plan_server_assignments(batch, {})

    assert all(server["protocol"] == "https"
               for item in batch for server in item.servers)
    # And the proportional allocation still happened: the fast mirror
    # has the bulk of a volume it can swallow inside the target time.
    assert all(first_server(item)["id"] == 1 for item in batch)


def test_a_mirror_only_batch_never_reads_the_mount_table(monkeypatch):
    """The ordinary path must not pay for the removable support."""
    reads = []
    monkeypatch.setattr(removable, "mounted_removable",
                        lambda table=None: reads.append(1) or [])

    batch = items(5, servers=[mirror(1, "a.example.org", 500)])
    Downloader.resolve_removable_sources(batch)

    assert reads == []


def test_the_mount_table_is_read_once_for_the_whole_batch(mounted_disc,
                                                          monkeypatch):
    """It used to be read per package: 2747 times on the distupgrade."""
    real = removable.mounted_removable
    reads = []

    def counting(table=None):
        reads.append(1)
        return real(table)

    monkeypatch.setattr(removable, "mounted_removable", counting)

    batch = items(50, servers=[mirror(1, "a.example.org", 500),
                               disc_server()])
    Downloader.resolve_removable_sources(batch)

    assert len(reads) == 1
    # Resolution locates, it does not reorder: the promotion is the
    # planner's job, so what every item holds at this point is a
    # located ``file`` source somewhere in its list.
    assert all(any(server["protocol"] == "file" for server in item.servers)
               for item in batch)


# ── Independence of the items ───────────────────────────────────────────


def test_each_item_keeps_its_own_server_order(mounted_disc):
    """One item's preference must not reorder another's list.

    ``_assign_local_sources`` sorts in place, which is only safe
    because every item owns its copy; this fails loudly if a caller
    ever shares one list between items.
    """
    shared = [mirror(1, "a.example.org", 500), disc_server()]
    with_disc = items(1, servers=shared)
    without = items(1, servers=[mirror(1, "a.example.org", 500)], first=50)

    batch = with_disc + without
    Downloader.resolve_removable_sources(batch)
    _assign_local_sources(batch)

    assert first_server(with_disc[0])["id"] == 10
    assert first_server(without[0])["id"] == 1


def test_items_with_a_local_source_leave_the_arithmetic(mounted_disc):
    """What the function returns is what the bandwidth planner gets."""
    local = items(3, servers=[mirror(1, "a.example.org", 500),
                              disc_server()])
    network = items(2, servers=[mirror(1, "a.example.org", 500)], first=50)

    Downloader.resolve_removable_sources(local + network)
    remaining = _assign_local_sources(local + network)

    assert [item.name for item in remaining] == [item.name for item in network]


def test_a_plain_local_mirror_is_local_too(tmp_path):
    """A ``file://`` tree costs no network either.

    Same reasoning as the disc, and the same answer: it serves what it
    has and the mirrors share the rest.
    """
    local = {
        "id": 7, "name": "local-mirror", "protocol": "file",
        "host": "", "base_path": str(tmp_path), "priority": 10,
    }
    batch = items(5, servers=[mirror(1, "fast.example.org", 2000), local])

    _plan_server_assignments(batch, {})

    assert all(first_server(item)["id"] == 7 for item in batch)


# ── What a miss on the disc must cost ───────────────────────────────────
#
# Preferring the disc only works if a package it does not carry falls
# through cheaply.  A pressing serves a subset by construction (4489 of
# Core Release's 38861 packages, measured), so this is the common case,
# not the exception.


#: A real package, built by ``urpm/tests/gen_test_rpms.py``.  Its name
#: matches what ``DownloadItem.filename`` builds for name=t,
#: version=2, release=1, arch=x86_64.
FIXTURE_RPM = (Path(__file__).parent / "media" / "orphans-2"
               / "t-2-1.x86_64.rpm")


class RecordingDb:
    """Just enough database to watch the reputation updates."""

    def __init__(self):
        self.stats_calls = []

    def update_server_stats(self, server_id, **kwargs):
        self.stats_calls.append((server_id, kwargs))

    # Touched by the cache registration path, which defers to the main
    # thread and is not under test here.
    def register_cache_file(self, *args, **kwargs):
        return 1

    def is_peer_blacklisted(self, *args, **kwargs):
        return False


@pytest.fixture
def downloader(tmp_path):
    from urpm.core.download import Downloader as RealDownloader
    db = RecordingDb()
    dl = RealDownloader(cache_dir=tmp_path / "cache", use_peers=False, db=db)
    return dl, db


def local_server(server_id, base_path):
    return {
        "id": server_id, "name": f"local{server_id}", "protocol": "file",
        "host": "", "base_path": str(base_path), "priority": 10,
    }


def test_a_missing_file_on_a_local_source_is_a_hard_error(downloader,
                                                          tmp_path):
    """Not transient: there is no network between us and the bytes.

    Classified transient, each miss would be retried three times with
    one then two seconds of backoff before reaching a mirror.
    """
    dl, _ = downloader
    missing = tmp_path / "nothing-here.rpm"
    cache = tmp_path / "out.rpm"

    ok, error = dl._download_from_url(f"file://{missing}", cache)

    assert not ok
    assert error.is_hard


def test_a_miss_is_not_charged_to_the_local_source(downloader, tmp_path):
    """``urpm server list`` would otherwise show a disc with thousands.

    Two local sources: the first does not carry the package, the second
    does.  The download has to succeed from the second, and the first
    must not be marked as failing.
    """
    if not FIXTURE_RPM.is_file():
        pytest.skip("test media not generated (make test-media)")
    dl, db = downloader

    empty = tmp_path / "pressing"
    (empty / DISC_CORE).mkdir(parents=True)
    full = tmp_path / "mirror"
    (full / DISC_CORE).mkdir(parents=True)
    (full / DISC_CORE / FIXTURE_RPM.name).write_bytes(
        FIXTURE_RPM.read_bytes())

    item = DownloadItem(
        name="t", version="2", release="1", arch="x86_64",
        media_id=1, relative_path=DISC_CORE, size=FIXTURE_RPM.stat().st_size,
        servers=[local_server(10, empty), local_server(11, full)],
    )

    result = dl.download_one(item, max_retries=3)

    assert result.success, result.error
    assert result.source_server_id == 11
    failures = [sid for sid, kw in db.stats_calls if kw.get("success") is False]
    assert failures == []


def test_a_mirror_is_still_charged_for_a_hard_failure(downloader, tmp_path):
    """The distinction is locality, not the error.

    A mirror that cannot be reached is telling us something about
    itself, and that signal has to keep reaching the statistics.  The
    host is in ``.invalid``, which RFC 2606 reserves as never
    resolvable, so this costs one failed lookup and no network.
    """
    dl, db = downloader
    item = DownloadItem(
        name="t", version="2", release="1", arch="x86_64",
        media_id=1, relative_path=DISC_CORE, size=10,
        servers=[{
            "id": 12, "name": "mirror.invalid", "protocol": "http",
            "host": "mirror.invalid", "base_path": "/mageia",
            "priority": 50, "ip_mode": "ipv4",
        }],
    )

    result = dl.download_one(item, timeout=1, max_retries=1)

    assert not result.success
    assert (12, {"success": False}) in db.stats_calls
