"""A declared disc across the release switch.

Two failures observed on a real mga9 → mga10 upgrade, in that order.

First, Stage 1 built a target-catalogue URL for *every* official
server, the disc included, and reported
``URL 'cdrom://Mageia-10-x86_64/10/x86_64/media/' is not reachable as a
media catalogue``.  Twice wrong: the scheme is an identity rather than
something libcurl fetches, by design, and a pressing has no release
level at all — its trees sit at ``x86_64/media/``.  Worse, the loop
also refreshes each server's ``url_version`` pin, which would have
stamped the target release onto a row whose release is pressed into
the plastic.

Second, excluding the disc from that loop is not enough.  The media
the switch creates are new rows, so a disc attached to the source
release ends up attached to nothing, and the download stage ignores a
disc the operator deliberately declared — which is the whole point of
an offline upgrade.

The disc in the drive during a mga9 upgrade is a **mga10** pressing.
It must therefore never drive media creation: the mirrors say what the
target release contains and which sections the operator kept.  The
re-attachment links and never creates, and these tests pin that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from urpm.core import removable
from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.stage1 import (
    _insert_target_media,
    _relink_mounted_discs,
)
from urpm.core.distupgrade.version import ReleaseIdentity
from urpm.core.sync import SYNTHESIS_PATH


#: What a Mageia 10 x86_64 pressing declares, trimmed to the two trees
#: that matter here.  Verbatim shape of a real disc's ``media.cfg``.
DISC_MEDIA_CFG = """\
[media_info]
version=10
arch=x86_64
branch=Official

[core]
name=Core Release (Installer)
hdlist=hdlist_core.cz
size=4489

[nonfree]
name=Nonfree Release (Installer)
hdlist=hdlist_nonfree.cz
size=33
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urpm.core.config.get_system_version", lambda root=None: "9")
    d = PackageDatabase(db_path=tmp_path / "packages.db")
    yield d
    d.close()


@pytest.fixture
def mounted_disc(tmp_path, monkeypatch):
    """A mga10 pressing, mounted, with its catalogue and synthesis."""
    root = tmp_path / "Mageia-10-x86_64"
    for tree in ("core", "nonfree"):
        info = root / "x86_64" / "media" / tree / "media_info"
        info.mkdir(parents=True)
        (info / "synthesis.hdlist.cz").write_bytes(b"opaque here")
    catalogue = root / "x86_64" / "media" / "media_info"
    catalogue.mkdir(parents=True, exist_ok=True)
    (catalogue / "media.cfg").write_text(DISC_MEDIA_CFG)

    table = tmp_path / "mounts"
    table.write_text(f"/dev/sr0 {root} iso9660 ro,relatime 0 0\n")
    monkeypatch.setattr(removable, "MOUNT_TABLE", table)
    return root


@pytest.fixture
def no_disc(tmp_path, monkeypatch):
    empty = tmp_path / "mounts-empty"
    empty.write_text("")
    monkeypatch.setattr(removable, "MOUNT_TABLE", empty)


def declare_disc(db, label="Mageia-10-x86_64"):
    """What ``urpm media add-cdrom`` leaves in the database."""
    return db.add_server(
        name=label, protocol=removable.PROTOCOL, host=label,
        base_path="", is_official=True, priority=90,
    )


def insert_target_medium(db, *, name, short_name, version="10"):
    db.conn.execute("""
        INSERT INTO media
          (name, short_name, mageia_version, architecture,
           relative_path, is_official, enabled)
        VALUES (?, ?, ?, 'x86_64', ?, 1, 1)
    """, (name, short_name, version,
          f"{version}/x86_64/media/{short_name}"))
    db.conn.commit()
    return db.get_media(name)


# ── The disc is not a catalogue source ──────────────────────────────────


def test_the_release_switch_does_not_fetch_a_disc(db, no_disc):
    """It used to try ``cdrom://<label>/10/x86_64/media/``.

    The upsert is stubbed: what is under test is which servers the
    loop even considers.
    """
    declare_disc(db)
    db.add_server(name="mirror.example.org", protocol="https",
                  host="mirror.example.org", base_path="/mageia",
                  is_official=True, priority=50)

    seen = []

    def fake_upsert(_db, url, **kwargs):
        seen.append(url)
        result = type("R", (), {})()
        result.outcomes = []
        result.server_id = 1
        result.server_was_created = False
        return result

    import urpm.core.media_pipeline as pipeline
    original = pipeline.upsert_media_tree
    pipeline.upsert_media_tree = fake_upsert
    try:
        _insert_target_media(db, ReleaseIdentity(identity="10", numeric="10"), "x86_64",
                             source_identity="9")
    finally:
        pipeline.upsert_media_tree = original

    assert seen, "no server was considered at all"
    assert not any(url.startswith("cdrom://") for url in seen), seen


def test_the_disc_keeps_the_release_it_was_pressed_with(db, no_disc):
    """The ``url_version`` pin must not be rewritten on a disc.

    A mirror serves every release from one host, so moving its pin
    from 9 to 10 is what migration is.  A pressing carries one
    release, physically.
    """
    server_id = declare_disc(db)
    before = db.get_server_by_id(server_id)

    import urpm.core.media_pipeline as pipeline
    original = pipeline.upsert_media_tree
    pipeline.upsert_media_tree = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the disc must not reach the upsert"))
    try:
        _insert_target_media(db, ReleaseIdentity(identity="10", numeric="10"), "x86_64",
                             source_identity="9")
    finally:
        pipeline.upsert_media_tree = original

    after = db.get_server_by_id(server_id)
    assert after["url_version"] == before["url_version"]


# ── The disc follows the switch, by linking only ────────────────────────


def test_a_declared_disc_is_attached_to_the_target_media(db, mounted_disc):
    declare_disc(db)
    core = insert_target_medium(db, name="Core Release",
                                short_name="core_release")

    linked = _relink_mounted_discs(db)

    assert ("Core Release", "x86_64/media/core") in linked
    servers = db.get_servers_for_media(core["id"])
    assert [s["protocol"] for s in servers] == [removable.PROTOCOL]
    assert servers[0]["media_relative_path"] == "x86_64/media/core"


def test_the_disc_layout_wins_over_the_mirror_one(db, mounted_disc):
    """The medium's own path is the mirror's; the pair carries the disc's."""
    declare_disc(db)
    core = insert_target_medium(db, name="Core Release",
                                short_name="core_release")

    _relink_mounted_discs(db)

    row = db.get_media_by_id(core["id"])
    assert row["relative_path"] == "10/x86_64/media/core_release"
    served = db.get_servers_for_media(core["id"])[0]
    assert served["media_relative_path"] == "x86_64/media/core"


def test_a_tree_with_no_target_medium_is_not_created(db, mounted_disc):
    """The one thing a mga10 disc must never do on a mga9 system.

    Only ``Core Release`` exists among the target media here; the
    disc also carries nonfree, which the operator did not keep.  It
    must be left out, not conjured.
    """
    declare_disc(db)
    insert_target_medium(db, name="Core Release", short_name="core_release")

    before = {row["name"] for row in db.list_media()}
    linked = _relink_mounted_discs(db)
    after = {row["name"] for row in db.list_media()}

    assert after == before
    assert [name for name, _layout in linked] == ["Core Release"]


def test_an_undeclared_disc_is_left_alone(db, mounted_disc):
    """A disc that merely happens to be mounted is not enrolled."""
    insert_target_medium(db, name="Core Release", short_name="core_release")

    assert _relink_mounted_discs(db) == []
    assert not [s for s in db.list_servers()
                if s["protocol"] == removable.PROTOCOL]


def test_no_disc_mounted_is_not_an_error(db, no_disc):
    declare_disc(db)
    insert_target_medium(db, name="Core Release", short_name="core_release")

    assert _relink_mounted_discs(db) == []


def test_relinking_twice_changes_nothing(db, mounted_disc):
    """Stage 1 can be re-run after an abort."""
    declare_disc(db)
    core = insert_target_medium(db, name="Core Release",
                                short_name="core_release")

    first = _relink_mounted_discs(db)
    second = _relink_mounted_discs(db)

    assert first == second
    assert len(db.get_servers_for_media(core["id"])) == 1


def test_an_unreadable_disc_does_not_fail_the_switch(db, tmp_path,
                                                     monkeypatch):
    """The mirrors alone are enough to proceed."""
    declare_disc(db)
    insert_target_medium(db, name="Core Release", short_name="core_release")

    def boom():
        raise OSError("drive went away mid-read")

    monkeypatch.setattr(
        "urpm.core.removable_catalogue.read_mounted_catalogues", boom)

    assert _relink_mounted_discs(db) == []
