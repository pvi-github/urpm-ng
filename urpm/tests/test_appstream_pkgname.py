"""``<pkgname>`` in a generated AppStream component must be the RPM name.

Regression this locks down : ``_generate_appstream_xml`` fed the same
local ``name`` variable to both ``<name>`` (display name) and
``<pkgname>``.  That variable holds the ``.desktop`` entry's ``Name=``
whenever the package ships one, so every application whose desktop
name differs from its package name got a ``<pkgname>`` no package
manager could resolve.

Observed fallout : ``newmoon-browser`` (desktop ``Name=New Moon``)
emitted ``<pkgname>New Moon</pkgname>`` and became invisible in
Discover and GNOME Software — while its ``newmoon-browser-devel`` and
``newmoon-lang-*`` subpackages, which ship no ``.desktop`` and so fell
back to the RPM name, showed up in its place.  ``pkcon`` found the
package throughout, because PackageKit does not consult the catalog.

``<pkgname>`` is the sole key a software centre uses to map a
component back to something installable ; it is never a human-facing
string.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from urpm.core.appstream import AppStreamManager


@dataclass
class _PackageInfo:
    """Stand-in for the RPM metadata object the generator consumes.

    Only the attributes ``_generate_appstream_xml`` actually reads.
    """

    name: str = "newmoon-browser"
    summary: str = "Browse the World Wide Web"
    license: str = "MPL"
    version: str = "34.0.1"
    group: str = "Networking/WWW"
    buildtime: int = 1772000000


@pytest.fixture
def manager(tmp_path):
    """An AppStreamManager pointed at a throwaway base dir.

    ``db`` is unused by ``_generate_appstream_xml``, so ``None`` keeps
    the fixture free of database setup.
    """
    return AppStreamManager(db=None, base_dir=tmp_path)


def _component(xml_path: str) -> ET.Element:
    """Parse a generated file and return its ``<component>`` element."""
    root = ET.parse(xml_path).getroot()
    return root if root.tag == "component" else root.find("component")


def _text(component: ET.Element, tag: str) -> str | None:
    el = component.find(tag)
    return el.text if el is not None else None


class TestPkgnameIsTheRpmName:

    def test_desktop_name_does_not_leak_into_pkgname(self, manager, tmp_path):
        """The exact newmoon case : desktop says « New Moon », the
        package is ``newmoon-browser``.  ``<pkgname>`` must carry the
        latter."""
        out = manager._generate_appstream_xml(
            _PackageInfo(),
            pkg_stem="newmoon-browser-34.0.1-1bdk_mga10.x86_64",
            bin_files=["/usr/bin/newmoon"],
            dest_dir=tmp_path / "out",
            desktop_info={"name": "New Moon",
                          "comment": "Browse the World Wide Web"},
        )
        comp = _component(out)
        assert _text(comp, "pkgname") == "newmoon-browser"

    def test_display_name_still_comes_from_the_desktop_entry(
        self, manager, tmp_path,
    ):
        """Fixing pkgname must not cost us the human-readable name —
        ``<name>`` is exactly where the desktop ``Name=`` belongs."""
        out = manager._generate_appstream_xml(
            _PackageInfo(),
            pkg_stem="newmoon-browser-34.0.1-1bdk_mga10.x86_64",
            bin_files=["/usr/bin/newmoon"],
            dest_dir=tmp_path / "out",
            desktop_info={"name": "New Moon"},
        )
        comp = _component(out)
        assert _text(comp, "name") == "New Moon"

    def test_pkgname_never_contains_whitespace(self, manager, tmp_path):
        """A space in ``<pkgname>`` is the visible signature of this
        bug class — no RPM name has one, every display name might."""
        out = manager._generate_appstream_xml(
            _PackageInfo(),
            pkg_stem="newmoon-browser-34.0.1-1bdk_mga10.x86_64",
            bin_files=[],
            dest_dir=tmp_path / "out",
            desktop_info={"name": "New Moon"},
        )
        pkgname = _text(_component(out), "pkgname")
        assert pkgname and " " not in pkgname

    def test_no_desktop_entry_still_yields_the_rpm_name(
        self, manager, tmp_path,
    ):
        """The path that worked by accident before : no ``.desktop``,
        so the old code fell back to the RPM name.  It must keep
        working now that the two fields are sourced separately."""
        out = manager._generate_appstream_xml(
            _PackageInfo(name="newmoon-lang-fr"),
            pkg_stem="newmoon-lang-fr-34.0.1-1bdk_mga10.x86_64",
            bin_files=[],
            dest_dir=tmp_path / "out",
            desktop_info=None,
        )
        comp = _component(out)
        assert _text(comp, "pkgname") == "newmoon-lang-fr"
        assert _text(comp, "name") == "newmoon-lang-fr"

    def test_pkgname_and_name_may_legitimately_differ(
        self, manager, tmp_path,
    ):
        """The two fields are independent by design.  Asserting they
        differ here is what would have failed loudly before the fix,
        where both were the same variable."""
        out = manager._generate_appstream_xml(
            _PackageInfo(),
            pkg_stem="newmoon-browser-34.0.1-1bdk_mga10.x86_64",
            bin_files=[],
            dest_dir=tmp_path / "out",
            desktop_info={"name": "New Moon"},
        )
        comp = _component(out)
        assert _text(comp, "pkgname") != _text(comp, "name")


# ── State file anchoring ───────────────────────────────────────────


class TestStateFileAnchoring:
    """The extraction state index must live beside the media it
    describes, never in the caller's current directory.

    Regression this exists for : ``_load_state`` / ``_save_state`` /
    ``_purge_missing_rpms`` resolved ``Path(self.CACHE_DIR)`` — a bare
    relative path — so ``.genhdlist/state.json`` was created wherever
    ``urpm genmedia`` happened to be invoked from.

    Two consequences, both silent.  Two media generated from the same
    directory shared one index, so a package could be considered
    already-processed on the strength of the *other* media's run.  And
    running the tool from elsewhere started from scratch with no visible
    reason — which, while validating the ``pkgname`` fix above, made a
    working correction look like it did nothing at all.

    :class:`urpm.core.hdlist.HdlistWriter` already did this correctly
    with an instance ``cache_path``; these tests pin the convergence.
    """

    def test_state_is_written_under_cache_path(self, tmp_path, monkeypatch):
        """Saving lands in the configured directory."""
        cache = tmp_path / "media" / ".genhdlist"
        mgr = AppStreamManager(db=None, base_dir=tmp_path, cache_path=cache)

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        mgr._save_state({"foo-1.0-1.noarch.rpm": {"sha256": "abc"}})

        assert (cache / "state.json").is_file()
        assert not (elsewhere / ".genhdlist").exists(), (
            "state leaked into the current working directory"
        )

    def test_state_round_trips_from_any_directory(self, tmp_path, monkeypatch):
        """Written from one directory, read back from another — the
        index follows the media, not the caller."""
        cache = tmp_path / "media" / ".genhdlist"
        mgr = AppStreamManager(db=None, base_dir=tmp_path, cache_path=cache)

        here = tmp_path / "here"
        there = tmp_path / "there"
        here.mkdir()
        there.mkdir()

        monkeypatch.chdir(here)
        mgr._save_state({"foo-1.0-1.noarch.rpm": {"sha256": "abc"}})

        monkeypatch.chdir(there)
        assert mgr._load_state() == {"foo-1.0-1.noarch.rpm": {"sha256": "abc"}}

    def test_two_media_keep_separate_state(self, tmp_path, monkeypatch):
        """The heart of the bug : distinct media must not share an
        index, or one media's run marks the other's packages done."""
        cache_a = tmp_path / "media-a" / ".genhdlist"
        cache_b = tmp_path / "media-b" / ".genhdlist"
        mgr_a = AppStreamManager(db=None, base_dir=tmp_path, cache_path=cache_a)
        mgr_b = AppStreamManager(db=None, base_dir=tmp_path, cache_path=cache_b)

        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        mgr_a._save_state({"a-1.0-1.noarch.rpm": {"sha256": "aaa"}})
        mgr_b._save_state({"b-1.0-1.noarch.rpm": {"sha256": "bbb"}})

        assert set(mgr_a._load_state()) == {"a-1.0-1.noarch.rpm"}
        assert set(mgr_b._load_state()) == {"b-1.0-1.noarch.rpm"}

    def test_no_cache_path_disables_persistence(self, tmp_path, monkeypatch):
        """``cache_path=None`` is a no-op rather than a crash or a stray
        directory — same contract as HdlistWriter."""
        mgr = AppStreamManager(db=None, base_dir=tmp_path, cache_path=None)
        monkeypatch.chdir(tmp_path)

        mgr._save_state({"foo": {"sha256": "abc"}})

        assert mgr._load_state() == {}
        assert not (tmp_path / ".genhdlist").exists()
