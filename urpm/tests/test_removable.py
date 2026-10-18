"""Tests for :mod:`urpm.core.removable` and the layout override.

Two mechanisms are locked down here, and they are the whole of the
removable-media foundation (doc/NOTES_AMOVIBLE_MEDIA.md §7.3).

**Identity by containment.** A disc is recognised by the fact that the
medium we are after has its synthesis readable underneath a mount
point, never by the mount point itself.  That is urpmi's own test, and
it is what lets the same code serve a disc mounted by a desktop
session under ``/run/media/<user>/<label>`` and one mounted by hand on
``/mnt/cdrom``.  A test that hardcoded a mount point would pass while
the mechanism was broken, so every case here builds its own tree in a
temp directory and feeds a synthetic mount table.

**A layout per (server, media) pair.** A Mageia 10 DVD serves core at
``x86_64/media/core``; the mirror serves the same packages, at
identical versions, at ``10/x86_64/media/core/release``.  Before this,
the layout belonged to the media row and was shared by all its
servers, so attaching a disc to ``Core Release`` would have pointed
the disc at the mirror's tree.  The URL assertions below are the
regression: they fail the moment the override stops winning.

The real-disc numbers quoted in the module docstrings were measured on
a Mageia 10 x86_64 pressing; nothing here needs a disc to run.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core import removable
from urpm.core.config import (build_media_url, build_server_url,
                              is_local_server, media_relative_path)
from urpm.core.database import PackageDatabase
from urpm.core.sync import SYNTHESIS_PATH, servers_for_media


# ── Fixtures and helpers ────────────────────────────────────────────────


#: Where a Mageia DVD actually puts its core tree, relative to the
#: mount point.  Read off a real pressing's ``media_info/hdlists``.
DISC_CORE = "x86_64/media/core"

#: Where a mirror puts the same packages.
MIRROR_CORE = "10/x86_64/media/core/release"


@pytest.fixture
def disc(tmp_path):
    """A directory shaped like a mounted Mageia DVD.

    Only what identification looks at is created: the synthesis of
    each medium, at the path the real disc uses.
    """
    for media_path in (DISC_CORE, "x86_64/media/nonfree"):
        synthesis = tmp_path / media_path / SYNTHESIS_PATH
        synthesis.parent.mkdir(parents=True)
        synthesis.write_bytes(b"not really compressed, never read here")
    return tmp_path


def mount_table(tmp_path, entries):
    """Write a ``/proc/mounts`` lookalike and return its path.

    Args:
        entries: ``(device, mount_point, filesystem)`` triples, in the
            order the kernel would list them.
    """
    table = tmp_path / "mounts"
    table.write_text("".join(
        f"{device} {point} {fs} ro,relatime 0 0\n"
        for device, point, fs in entries
    ))
    return table


def cdrom_server(label="Mageia-10-x86_64", server_id=1, priority=90):
    """A ``server`` row standing for a disc, as stored."""
    return {
        "id": server_id,
        "name": f"{label} disc",
        "protocol": removable.PROTOCOL,
        "host": label,
        "base_path": "",
        "priority": priority,
    }


def mirror_server(host="mirror.example.org", server_id=2):
    return {
        "id": server_id,
        "name": host,
        "protocol": "https",
        "host": host,
        "base_path": "/mageia",
        "priority": 50,
    }


CORE_MEDIA = {"id": 10, "name": "Core Release", "relative_path": MIRROR_CORE}


# ── Reading the mount table ─────────────────────────────────────────────


def test_only_pressing_filesystems_are_candidates(tmp_path):
    """Everything else on a running system has to be ignored.

    An ordinary machine has dozens of mounts.  Keeping ``iso9660`` and
    ``udf`` is what urpmi does, and it is enough: the identification
    step would reject a wrong candidate anyway, but probing every
    tmpfs and every btrfs subvolume for a synthesis would turn one
    cheap read into a filesystem walk.
    """
    table = mount_table(tmp_path, [
        ("/dev/nvme0n1p2", "/", "ext4"),
        ("tmpfs", "/run", "tmpfs"),
        ("/dev/sr0", "/run/media/u/Mageia-10-x86_64", "iso9660"),
        ("/dev/sr1", "/mnt/udf-disc", "udf"),
        ("proc", "/proc", "proc"),
    ])
    found = removable.mounted_removable(table)
    assert [str(m.path) for m in found] == [
        "/run/media/u/Mageia-10-x86_64", "/mnt/udf-disc"]
    assert [m.filesystem for m in found] == ["iso9660", "udf"]
    assert found[0].device == "/dev/sr0"


def test_mount_point_with_a_space_is_decoded(tmp_path):
    """``\\040`` in the table is a real space in the path.

    A disc labelled ``Mageia 10`` mounts under a path containing a
    space, and the kernel escapes it because the table is
    whitespace-separated.  Handing the escaped form back would produce
    a path that cannot be opened, and the disc would look absent while
    sitting in the drive.
    """
    table = mount_table(tmp_path, [
        ("/dev/sr0", "/run/media/u/Mageia\\04010", "iso9660"),
    ])
    found = removable.mounted_removable(table)
    assert [str(m.path) for m in found] == ["/run/media/u/Mageia 10"]


def test_a_backslash_that_is_not_an_escape_survives(tmp_path):
    """Only three octal digits make an escape; the rest is literal."""
    table = mount_table(tmp_path, [
        ("/dev/sr0", "/mnt/a\\089b", "iso9660"),
    ])
    assert str(removable.mounted_removable(table)[0].path) == "/mnt/a\\089b"


def test_the_module_constant_decides_which_table_is_read(tmp_path,
                                                         monkeypatch):
    """:data:`MOUNT_TABLE` is resolved per call, not captured.

    Binding it as a default argument is the obvious way to write this
    and it was how the first version read: the default was evaluated
    once at import, so pointing the constant elsewhere changed
    nothing and every test below kept reading the kernel's own table.
    On a developer machine with a Mageia DVD actually mounted that
    produced two tests which passed by looking at the real disc, and
    on a machine without one it produced two tests that passed by
    looking at nothing.  Hence this check, which needs no disc.
    """
    table = mount_table(tmp_path, [("/dev/sr9", "/mnt/elsewhere", "udf")])
    monkeypatch.setattr(removable, "MOUNT_TABLE", table)
    assert [str(m.path) for m in removable.mounted_removable()] == [
        "/mnt/elsewhere"]


def test_no_mount_table_means_no_disc(tmp_path):
    """Absence is an answer, not a crash.

    Reached inside a chroot or a container built without ``/proc``.
    The caller's job is to report that no disc is available, which it
    cannot do if reading the table raised.
    """
    assert removable.mounted_removable(tmp_path / "absent") == []


# ── Identification ──────────────────────────────────────────────────────


def test_a_disc_is_found_by_what_it_carries(disc, tmp_path):
    """The mount point is never the criterion, its contents are."""
    table = mount_table(tmp_path, [("/dev/sr0", str(disc), "iso9660")])
    mounts = removable.mounted_removable(table)
    assert removable.locate(DISC_CORE, mounts).path == disc


def test_the_wrong_disc_is_not_mistaken_for_the_right_one(disc, tmp_path):
    """A mounted pressing that lacks this medium does not qualify.

    This is the multi-disc case reduced to its core: with several discs
    configured, each request has to find *its* disc.  A test that only
    checked « is something mounted » would pass on the wrong one.
    """
    other = tmp_path / "other-disc"
    (other / "i586/media/core" / SYNTHESIS_PATH).parent.mkdir(parents=True)
    (other / "i586/media/core" / SYNTHESIS_PATH).write_bytes(b"x")

    table = mount_table(tmp_path, [
        ("/dev/sr1", str(other), "iso9660"),
        ("/dev/sr0", str(disc), "iso9660"),
    ])
    mounts = removable.mounted_removable(table)
    assert removable.locate(DISC_CORE, mounts).path == disc
    assert removable.locate("i586/media/core", mounts).path == other


def test_a_disc_without_the_synthesis_is_absent(tmp_path):
    """A mounted data disc is not a medium.

    Existence of the directory is not the test — the synthesis is.  A
    pressing whose tree was partially copied somewhere would otherwise
    be accepted and fail later, mid-transaction.
    """
    hollow = tmp_path / "hollow"
    (hollow / DISC_CORE).mkdir(parents=True)
    table = mount_table(tmp_path, [("/dev/sr0", str(hollow), "iso9660")])
    assert removable.locate(DISC_CORE, removable.mounted_removable(table)) is None


# ── Resolution: identity becomes a location ─────────────────────────────


def test_an_unresolved_disc_is_neither_local_nor_fetchable():
    """The stored row is an identity and must look like one.

    ``build_server_url`` stays a pure function of the row, so what it
    produces for a disc is ``cdrom://<label>`` — a scheme libcurl does
    not know, which is the safety net: a missing resolution step fails
    loudly instead of silently reading the wrong place.  And
    ``is_local_server`` must say no, or the local-copy path would run
    against an empty ``base_path``.
    """
    server = cdrom_server()
    assert build_server_url(server) == "cdrom://Mageia-10-x86_64"
    assert not is_local_server(server)
    assert removable.is_removable_server(server)


def test_resolution_turns_the_disc_into_an_ordinary_file_server(disc, tmp_path):
    """Everything downstream then works without knowing about discs."""
    table = mount_table(tmp_path, [("/dev/sr0", str(disc), "iso9660")])
    mounts = removable.mounted_removable(table)

    resolved = removable.resolve(cdrom_server(), DISC_CORE, mounts)

    assert resolved["protocol"] == "file"
    assert resolved["base_path"] == str(disc)
    assert is_local_server(resolved)
    # The row id survives, so reputation and statistics still key on
    # the right server.
    assert resolved["id"] == 1
    # And the pressing stays identifiable for diagnostics even though
    # the protocol now says ``file``.
    assert resolved["removable_identity"] == "Mageia-10-x86_64"
    assert resolved["removable_device"] == "/dev/sr0"


def test_resolution_of_an_absent_disc_yields_nothing(tmp_path):
    """``None`` is how « the disc is not here » travels."""
    table = mount_table(tmp_path, [])
    mounts = removable.mounted_removable(table)
    assert removable.resolve(cdrom_server(), DISC_CORE, mounts) is None


def test_a_mirror_passes_through_resolution_untouched(tmp_path):
    """Callers can funnel every server through without branching."""
    server = mirror_server()
    assert removable.resolve(server, MIRROR_CORE, []) is server


def test_mirrors_only_drops_the_disc():
    """Mirror-as-such operations must not see a pressing.

    Comparing a candidate mirror's synthesis against a disc's could
    never match: the disc carries a subset of the media.
    """
    servers = [cdrom_server(), mirror_server()]
    assert [s["name"] for s in removable.mirrors_only(servers)] == [
        "mirror.example.org"]


# ── The layout override ─────────────────────────────────────────────────


def test_a_mirror_uses_the_shared_media_layout():
    """No override means the media row decides, as it always did."""
    server = mirror_server()
    assert media_relative_path(server, CORE_MEDIA) == MIRROR_CORE
    assert build_media_url(server, CORE_MEDIA) == (
        f"https://mirror.example.org/mageia/{MIRROR_CORE}")


def test_the_disc_layout_wins_over_the_media_layout(disc, tmp_path):
    """The regression this whole column exists for.

    Without the override the disc would be addressed at the mirror's
    relative path, which does not exist on a pressing, and every
    package would 404 off a disc sitting in the drive.
    """
    table = mount_table(tmp_path, [("/dev/sr0", str(disc), "iso9660")])
    mounts = removable.mounted_removable(table)
    resolved = removable.resolve(cdrom_server(), DISC_CORE, mounts)

    assert media_relative_path(resolved, CORE_MEDIA) == DISC_CORE
    assert build_media_url(resolved, CORE_MEDIA) == f"file://{disc}/{DISC_CORE}"
    # And what that URL names is really there, which is the point.
    assert (disc / DISC_CORE / SYNTHESIS_PATH).is_file()


def test_an_empty_override_falls_back_to_the_media_layout():
    """A blank string is not a layout.

    SQLite hands back whatever was stored, and an empty value from a
    bad import must not produce ``file:///mount/`` pointing at the
    disc root.
    """
    server = dict(mirror_server(), media_relative_path="")
    assert media_relative_path(server, CORE_MEDIA) == MIRROR_CORE


# ── End to end, against a real database ─────────────────────────────────


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file."""
    with tempfile.TemporaryDirectory(prefix="removable_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            yield database
        finally:
            database.close()


def _add_core(db):
    return db.add_media(
        name="Core Release", short_name="core_release",
        mageia_version="10", architecture="x86_64",
        relative_path=MIRROR_CORE, is_official=True, enabled=True,
    )


def test_the_link_stores_a_layout_and_the_query_returns_it(db):
    """The per-pair path has to survive the round trip to be of use."""
    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "", priority=90)
    mirror_id = db.add_server("mirror", "https", "mirror.example.org",
                              "/mageia", priority=50)
    db.link_server_media(disc_id, media_id, DISC_CORE)
    db.link_server_media(mirror_id, media_id)

    by_name = {s["name"]: s for s in db.get_servers_for_media(media_id)}
    assert by_name["Mageia 10 DVD"]["media_relative_path"] == DISC_CORE
    # A mirror carries no override, which is what makes the shared
    # layout the default rather than a special case.
    assert by_name["mirror"]["media_relative_path"] is None


def test_relinking_keeps_a_layout_it_was_not_given(db):
    """Re-adding a server must not silently erase the disc's tree.

    ``link_server_media`` is called from several places that do not
    know whether the pair already exists, and most of them have no
    layout to pass.
    """
    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "")
    db.link_server_media(disc_id, media_id, DISC_CORE)
    db.link_server_media(disc_id, media_id)

    served = db.get_servers_for_media(media_id)[0]
    assert served["media_relative_path"] == DISC_CORE


def test_relinking_with_a_new_layout_corrects_it(db):
    """Re-reading a disc's ``media.cfg`` can fix a wrong path."""
    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "")
    db.link_server_media(disc_id, media_id, "wrong/place")
    db.link_server_media(disc_id, media_id, DISC_CORE)

    served = db.get_servers_for_media(media_id)[0]
    assert served["media_relative_path"] == DISC_CORE


def test_the_official_mesh_leaves_discs_alone(db):
    """Meshing a disc would advertise packages it does not carry.

    The mesh rests on Mageia's mirror convention — any mirror carrying
    the tree carries the whole set at identical paths.  A pressing
    holds a subset of two media and no ``updates`` media at all, so
    that convention is false for it, and every meshed link would cost
    a disc request to discover as missing.
    """
    core_id = _add_core(db)
    updates_id = db.add_media(
        name="Core Updates", short_name="core_updates",
        mageia_version="10", architecture="x86_64",
        relative_path="10/x86_64/media/core/updates",
        is_official=True, enabled=True, update_media=True,
    )
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "", is_official=True)
    mirror_id = db.add_server("mirror", "https", "mirror.example.org",
                              "/mageia", is_official=True)
    db.link_server_media(disc_id, core_id, DISC_CORE)
    db.link_server_media(mirror_id, core_id)

    db.link_official_mesh()

    disc_media = {s["name"] for mid in (core_id, updates_id)
                  for s in db.get_servers_for_media(mid)
                  if s["id"] == disc_id}
    assert disc_media == {"Mageia 10 DVD"}
    assert [s["id"] for s in db.get_servers_for_media(updates_id)] == [mirror_id]


def test_servers_for_media_hides_an_absent_disc(db, monkeypatch, tmp_path):
    """A disc that is not in the drive is simply one fewer source.

    The mirror behind it has to stay reachable: this is the rule that
    « we never block on a removable medium when the need can be met
    without it » (§6.2) at its lowest layer.
    """
    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "", priority=90)
    mirror_id = db.add_server("mirror", "https", "mirror.example.org",
                              "/mageia", priority=50)
    db.link_server_media(disc_id, media_id, DISC_CORE)
    db.link_server_media(mirror_id, media_id)
    monkeypatch.setattr(removable, "MOUNT_TABLE", mount_table(tmp_path, []))

    media = db.get_media_by_id(media_id)
    usable = servers_for_media(db, media)

    assert [s["id"] for s in usable] == [mirror_id]
    assert build_media_url(usable[0], media).startswith("https://")


def test_metadata_prefers_the_mirror_over_a_present_disc(db, monkeypatch,
                                                         disc, tmp_path):
    """A disc goes last for metadata, whatever its priority.

    This is the regression that cost a real ``media update``.  The disc
    is deliberately given a higher priority than the mirror, because
    for *files* it is the cheaper origin and should win.  For
    *metadata* it must lose: a pressing declares a subset (4488
    packages of a ``Core Release`` the mirror declares 38861 of), so
    taking its synthesis shrinks the medium to what the disc happens
    to carry.  On a configured machine that meant ``media update``
    re-parsing Core Release off the disc and trying to drop 34373
    packages.
    """
    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "", priority=90)
    mirror_id = db.add_server("mirror", "https", "mirror.example.org",
                              "/mageia", priority=50)
    db.link_server_media(disc_id, media_id, DISC_CORE)
    db.link_server_media(mirror_id, media_id)
    monkeypatch.setattr(
        removable, "MOUNT_TABLE",
        mount_table(tmp_path, [("/dev/sr0", str(disc), "iso9660")]))

    media = db.get_media_by_id(media_id)
    usable = servers_for_media(db, media)

    # The disc outranks the mirror in the raw query, and still comes
    # second here.  That gap is the mechanism.
    assert [s["id"] for s in db.get_servers_for_media(media_id)][0] == disc_id
    assert [s["id"] for s in usable] == [mirror_id, disc_id]
    assert build_media_url(usable[0], media).startswith("https://")

    # It is ordered last rather than dropped, so it stays the fallback
    # when no mirror answers: that is the offline case.
    assert build_media_url(usable[1], media) == f"file://{disc}/{DISC_CORE}"


def test_metadata_falls_back_to_the_disc_with_no_mirror(db, monkeypatch,
                                                        disc, tmp_path):
    """With nothing else, the pressing supplies the metadata.

    The counterpart of the test above, and the reason a disc is put
    last rather than excluded: an install or an upgrade with no network
    has only the disc to read a synthesis from.
    """
    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "", priority=90)
    db.link_server_media(disc_id, media_id, DISC_CORE)
    monkeypatch.setattr(
        removable, "MOUNT_TABLE",
        mount_table(tmp_path, [("/dev/sr0", str(disc), "iso9660")]))

    media = db.get_media_by_id(media_id)
    usable = servers_for_media(db, media)

    assert [s["id"] for s in usable] == [disc_id]
    assert build_media_url(usable[0], media) == f"file://{disc}/{DISC_CORE}"


def test_downloads_still_prefer_the_disc(db, monkeypatch, disc, tmp_path):
    """Files and metadata want opposite things, and both get them.

    The download path takes the servers in the priority order the
    database ranked them, so the disc stays first there: the packages
    are identical, and reading one beats fetching it.  Only the
    metadata path reorders.  A single rule for both would have to
    sacrifice one of the two.
    """
    from urpm.core.download import Downloader

    media_id = _add_core(db)
    disc_id = db.add_server("Mageia 10 DVD", removable.PROTOCOL,
                            "Mageia-10-x86_64", "", priority=90)
    mirror_id = db.add_server("mirror", "https", "mirror.example.org",
                              "/mageia", priority=50)
    db.link_server_media(disc_id, media_id, DISC_CORE)
    db.link_server_media(mirror_id, media_id)
    monkeypatch.setattr(
        removable, "MOUNT_TABLE",
        mount_table(tmp_path, [("/dev/sr0", str(disc), "iso9660")]))

    ranked = [dict(s) for s in db.get_servers_for_media(media_id)]
    resolved = Downloader._resolve_removable_servers(ranked, MIRROR_CORE)

    assert [s["id"] for s in resolved] == [disc_id, mirror_id]
    assert resolved[0]["protocol"] == "file"
    assert resolved[0]["base_path"] == str(disc)
