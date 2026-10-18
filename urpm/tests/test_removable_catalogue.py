"""Tests for :mod:`urpm.core.removable_catalogue`.

The thing being locked down is the join: which media we already know
does each tree on a disc belong to.

It cannot be the names.  A real Mageia 10 pressing calls its trees
``Core Release (Installer)`` and ``Nonfree Release (Installer)`` while
the mirror calls the same packages ``Core Release`` and
``Nonfree Release``; upstream added that suffix because urpmi has no
way to say « another source for a media I already have » and must
create a separate media instead.  Matching on display names would
join nothing, and a media named after the first source that happened
to be added is a media whose name lies as soon as a mirror attaches.

So the join is the canonical key the database already enforces,
``(mageia_version, architecture, short_name)``, reached by spelling
out the tree a pressing leaves implicit: ``core`` reads as
``core/release``.  Every assertion below exists to catch that
mechanism breaking, and the convergence test at the end is the one
that proves the whole design: a disc added with no network, then a
mirror, and both end up on the same media rows.

The catalogue content used here is the real one, copied off a Mageia
10 x86_64 DVD.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core.config import build_media_url
from urpm.core.database import PackageDatabase
from urpm.core.media_cfg import parse_media_cfg
from urpm.core import removable
from urpm.core.removable import MountPoint
from urpm.core.removable_catalogue import (
    DiscCatalogueError, attach_disc, canonical_section, find_catalogue,
    read_catalogue,
)
from urpm.core.sync import SYNTHESIS_PATH, servers_for_media


#: Verbatim from ``x86_64/media/media_info/media.cfg`` on a Mageia 10
#: x86_64 pressing.  Note what it does *not* declare: no media_type,
#: no updates media, and names that carry an « (Installer) » suffix.
DISC_MEDIA_CFG = """[media_info]
mediacfg_version=2
version=10
branch=Devel
arch=x86_64
askmedia=1
suppl=1

[core]
synthesis=../core/media_info/synthesis.hdlist.cz
hdlist=../core/media_info/hdlist.cz
pubkey=../core/media_info/pubkey
name=Core Release (Installer)
size=3g

[nonfree]
synthesis=../nonfree/media_info/synthesis.hdlist.cz
hdlist=../nonfree/media_info/hdlist.cz
pubkey=../nonfree/media_info/pubkey
name=Nonfree Release (Installer)
size=1g
"""

#: How a mirror publishes the same trees, for the convergence test.
MIRROR_MEDIA_CFG = """[media_info]
version=10
arch=x86_64
branch=Devel

[core/release]
name=Core Release
media_type=official:free:release

[core/updates]
name=Core Updates
media_type=official:free:updates
updates_for=core/release

[nonfree/release]
name=Nonfree Release
media_type=official:non-free:release
"""


# ── Fixtures ────────────────────────────────────────────────────────────


def make_disc(root: Path, *, arch: str = "x86_64",
              cfg: str = DISC_MEDIA_CFG,
              trees=("core", "nonfree")) -> MountPoint:
    """Build a directory shaped like a mounted pressing.

    Everything identification and catalogue reading touch is created,
    and nothing else: the catalogue at
    ``<arch>/media/media_info/media.cfg`` and one synthesis per tree.
    """
    catalogue = root / arch / "media" / "media_info" / "media.cfg"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(cfg)
    for tree in trees:
        synthesis = root / arch / "media" / tree / SYNTHESIS_PATH
        synthesis.parent.mkdir(parents=True)
        synthesis.write_bytes(b"synthesis, never decompressed here")
    return MountPoint(path=root, device="/dev/sr0", filesystem="iso9660")


@pytest.fixture(autouse=True)
def no_real_discs(tmp_path, monkeypatch):
    """Hide whatever is in the developer's drive.

    Not a convenience: without this the tests below read the real
    mount table, and a Mageia DVD actually mounted carries its trees
    at the same relative paths as the fixtures.  Three tests then
    resolved against the real disc, one failing on the mount path and
    **two passing for the wrong reason** — including the one asserting
    the device name, since the real drive is also ``/dev/sr0``.

    Returns the table's path so :func:`disc` can declare itself in it.
    """
    table = tmp_path / "mounts"
    table.write_text("")
    monkeypatch.setattr(removable, "MOUNT_TABLE", table)
    return table


@pytest.fixture
def disc(tmp_path, no_real_discs):
    """A pressing, mounted as far as the mount table is concerned."""
    mount = make_disc(tmp_path / "Mageia-10-x86_64")
    no_real_discs.write_text(
        f"{mount.device} {mount.path} {mount.filesystem} ro,relatime 0 0\n")
    return mount


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file."""
    with tempfile.TemporaryDirectory(prefix="removable_cat_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            yield database
        finally:
            database.close()


def add_mirror(db, cfg: str = MIRROR_MEDIA_CFG):
    """Add the media a mirror declares, the ordinary way."""
    from urpm.core.media_pipeline import upsert_media_tree

    return upsert_media_tree(
        db,
        "https://mirror.example.org/mageia/10/x86_64/media/",
        catalogue=parse_media_cfg(cfg, "10/x86_64/media"),
        raw_catalogue=cfg,
    )


# ── Canonicalising a section ────────────────────────────────────────────


@pytest.mark.parametrize("section,expected", [
    ("core", "core/release"),
    ("nonfree", "nonfree/release"),
    ("tainted", "tainted/release"),
    # Already explicit: a disc written like a mirror needs no help, and
    # appending the implied tree would invent ``core/updates/release``.
    ("core/release", "core/release"),
    ("core/updates", "core/updates"),
    ("nonfree/backports_testing", "nonfree/backports_testing"),
    # Cross-arch references keep their prefix.
    ("../../i686/media/core", "../../i686/media/core/release"),
])
def test_canonical_section(section, expected):
    assert canonical_section(section) == expected


def test_canonical_section_tolerates_slashes_and_blanks():
    assert canonical_section("/core/") == "core/release"
    assert canonical_section("") == ""


# ── Finding and reading the catalogue ───────────────────────────────────


def test_the_catalogue_is_found_without_knowing_the_architecture(tmp_path):
    """The top level of a pressing is an arch directory.

    Hardcoding ``x86_64`` would make this work on exactly one kind of
    disc, and Mageia ships one per architecture.
    """
    disc = make_disc(tmp_path / "Mageia-10-aarch64", arch="aarch64")
    found = find_catalogue(disc)
    assert found == disc.path / "aarch64/media/media_info/media.cfg"


def test_a_disc_without_a_catalogue_is_not_one(tmp_path):
    """A music CD or a data backup is not an error, just not a source."""
    plain = tmp_path / "Holiday Photos"
    (plain / "DCIM").mkdir(parents=True)
    mount = MountPoint(path=plain, device="/dev/sr0", filesystem="iso9660")
    assert find_catalogue(mount) is None
    with pytest.raises(DiscCatalogueError):
        read_catalogue(mount)


def test_reading_a_real_catalogue_yields_mirror_identities(disc):
    """The whole join, on the real file.

    What must come out is what a mirror would have produced for the
    same trees — short names, display names — because that is what
    the canonical key is formed from.
    """
    catalogue = read_catalogue(disc)

    assert catalogue.root == "x86_64/media"
    assert catalogue.info.version == "10"
    assert catalogue.info.arch == "x86_64"

    by_section = {m.section: m for m in catalogue.media}
    assert set(by_section) == {"core/release", "nonfree/release"}
    assert by_section["core/release"].short_name == "core_release"
    assert by_section["core/release"].name == "Core Release"
    assert by_section["nonfree/release"].short_name == "nonfree_release"
    assert by_section["nonfree/release"].name == "Nonfree Release"

    # The disc's own « (Installer) » labels must not survive: a media
    # named after one source misleads as soon as another attaches.
    assert all("Installer" not in m.name for m in catalogue.media)

    # A pressing of the distribution is official, even though its
    # catalogue carries no media_type to infer it from.  Inferring
    # « not official » from a missing field would let unsigned
    # packages through on the one source whose provenance is least in
    # doubt.
    assert all(m.is_official for m in catalogue.media)


def test_the_disc_layout_is_kept_apart_from_the_mirror_layout(disc):
    """Two paths, and keeping them apart is the point.

    The media row must carry the mirror layout, because every other
    server of that media serves it there; the disc's own layout goes
    on the link.  The entries leave ``relative_path`` empty so the
    media pipeline derives the mirror one from the canonical section.
    """
    catalogue = read_catalogue(disc)

    assert catalogue.disc_paths == {
        "core/release": "x86_64/media/core",
        "nonfree/release": "x86_64/media/nonfree",
    }
    assert all(m.relative_path == "" for m in catalogue.media)


def test_a_catalogue_without_version_and_arch_is_refused(tmp_path):
    """Those two form half the canonical key.

    Guessing them would attach the disc to the wrong release, which is
    worse than refusing: a 9 disc feeding the 10 media would resolve
    and install.
    """
    headless = DISC_MEDIA_CFG.replace("version=10\n", "").replace(
        "arch=x86_64\n", "")
    disc = make_disc(tmp_path / "Mystery", cfg=headless)
    with pytest.raises(DiscCatalogueError) as raised:
        read_catalogue(disc)
    assert "version" in str(raised.value)


def test_a_catalogue_declaring_no_media_is_refused(tmp_path):
    bare = "[media_info]\nversion=10\narch=x86_64\n"
    disc = make_disc(tmp_path / "Empty", cfg=bare, trees=())
    with pytest.raises(DiscCatalogueError):
        read_catalogue(disc)


# ── Attaching ───────────────────────────────────────────────────────────


def test_attaching_to_media_that_already_exist_creates_nothing(db, disc):
    """The online case: the disc is a cheaper origin, not a catalogue.

    Measured on the real pressing: its core tree holds 4488 package
    names, every one at a version identical to the mirror's
    ``Core Release``.  Creating a second media for them would put
    4488 duplicate candidates in the pool for no gain.
    """
    add_mirror(db)
    before = {m["name"] for m in db.list_media()}

    result = attach_disc(db, read_catalogue(disc))

    assert {m["name"] for m in db.list_media()} == before
    assert {o.action for o in result.outcomes} <= {"relinked", "noop"}
    assert {o.media_name for o in result.outcomes} == {
        "Core Release", "Nonfree Release"}


def test_attaching_with_nothing_configured_creates_the_media(db, disc):
    """The offline case, which is why the feature exists.

    Upgrading 9 to 10 from a DVD with no network means the release 10
    media do not exist yet.  They get created with the mirror's own
    attributes, so the rows are already shaped to accept mirrors.
    """
    result = attach_disc(db, read_catalogue(disc))

    assert {o.action for o in result.outcomes} == {"created"}
    core = db.get_media("Core Release")
    assert core["mageia_version"] == "10"
    assert core["architecture"] == "x86_64"
    assert core["relative_path"] == "10/x86_64/media/core/release"
    assert core["is_official"] == 1
    assert core["enabled"] == 1


def test_the_created_media_is_readable_from_the_disc(db, disc):
    """A created media is useless if nothing can read it.

    This is the assertion that would have caught the two layouts being
    confused: with the disc pointed at the mirror's relative path,
    the URL below would name a directory that exists on no pressing.
    """
    catalogue = read_catalogue(disc)
    attach_disc(db, catalogue)

    core = db.get_media("Core Release")
    usable = servers_for_media(db, core)

    assert len(usable) == 1
    url = build_media_url(usable[0], core)
    assert url == f"file://{disc.path}/x86_64/media/core"
    assert Path(url[len("file://"):], SYNTHESIS_PATH).is_file()


def test_a_mirror_added_afterwards_joins_the_same_media(db, disc):
    """Convergence, which is the whole argument for this design.

    A disc added offline, then the network comes back and a mirror is
    added the ordinary way.  Nothing may be rewritten and nothing may
    be duplicated: the mirror lands on the same media rows, with no
    layout override because it serves the shared one, and the disc
    keeps its own.
    """
    attach_disc(db, read_catalogue(disc))
    disc_media = {m["name"]: m["id"] for m in db.list_media()}

    add_mirror(db)

    after = {m["name"]: m["id"] for m in db.list_media()}
    # Same rows, same ids.
    for name, media_id in disc_media.items():
        assert after[name] == media_id
    # And the mirror brought what the disc could not carry.
    assert "Core Updates" in after

    core = db.get_media("Core Release")
    layouts = {s["protocol"]: s["media_relative_path"]
               for s in db.get_servers_for_media(core["id"])}
    assert layouts["cdrom"] == "x86_64/media/core"
    assert layouts["https"] is None


def test_the_mirror_keeps_the_metadata_once_both_are_attached(db, disc):
    """Attaching a disc must not change where the metadata comes from.

    The disc is given the higher priority on purpose, because for
    files it is the cheaper origin.  For metadata the mirror has to
    stay in front, or ``media update`` re-reads Core Release off a
    pressing that declares a subset of it.  The disc stays in the
    list, last, as the offline fallback.
    """
    add_mirror(db)
    attach_disc(db, read_catalogue(disc))

    core = db.get_media("Core Release")
    usable = servers_for_media(db, core)

    # ``file`` and not ``cdrom`` in second place: resolution already
    # turned the disc into a plain local server, which is what lets
    # the fallback work without any caller knowing about discs.
    assert [s["protocol"] for s in usable] == ["https", "file"]
    assert build_media_url(usable[0], core).startswith("https://")
    assert usable[1]["removable_device"] == "/dev/sr0"

    # And the media the disc does not carry is untouched by it.
    updates = db.get_media("Core Updates")
    assert [s["protocol"] for s in servers_for_media(db, updates)] == ["https"]


def test_attaching_twice_is_a_noop(db, disc):
    """Running the command again must not pile up rows or lose a path."""
    catalogue = read_catalogue(disc)
    attach_disc(db, catalogue)
    media_before = {m["name"]: m["id"] for m in db.list_media()}
    servers_before = [s["name"] for s in db.list_servers()]

    attach_disc(db, catalogue)

    assert {m["name"]: m["id"] for m in db.list_media()} == media_before
    assert [s["name"] for s in db.list_servers()] == servers_before
    core = db.get_media("Core Release")
    assert db.get_servers_for_media(core["id"])[0][
        "media_relative_path"] == "x86_64/media/core"
