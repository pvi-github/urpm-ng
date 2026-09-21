"""The catalogue files urpm-ng writes must stay world-readable.

Every download path already chmods what it writes, but that only fixes
a file it actually fetches, and a file is fetched when its MD5 changes.
One left at 0600 by an older build — ``shutil.move`` from a
``NamedTemporaryFile`` hands over exactly that — therefore keeps the
wrong mode until the mirror happens to republish it.  On
``core/release`` that was months on the machine where this surfaced.

The damage is a silent wrong answer, not an error: ``cmd_find`` tests
``exists()``, which succeeds for a root-owned 0600 file, hands it to
the decompressor, which fails, and the match list comes back empty.
``urpm f bash`` answers « nothing found » rather than « I cannot read
this index ».

The pass closing that window is deliberately keyed on a closed table
rather than on the directory listing.  The last test here is the one
that matters for the future: a file urpm-ng did not put there is none
of its business, so an artefact meant to stay root-only can share the
directory without being flattened open.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from urpm.core.sync import MEDIA_INFO_MODES, enforce_media_info_modes


class _FakeDB:
    """The two calls :func:`enforce_media_info_modes` makes."""

    def __init__(self, media, servers=None):
        self._media = media
        self._servers = servers or {}

    def list_media(self):
        return self._media

    def get_servers_for_media(self, media_id):
        return self._servers.get(media_id, [{"protocol": "https"}])


def _official(tmp_path: Path, media_id: int = 1, rel: str = "10/x86_64/core"):
    """A medium with its ``media_info`` directory laid out on disk."""
    media = {"id": media_id, "is_official": 1, "relative_path": rel,
             "short_name": "core"}
    d = tmp_path / "medias" / "official" / rel / "media_info"
    d.mkdir(parents=True)
    return media, d


def _write(path: Path, mode: int, body: bytes = b"x") -> Path:
    path.write_bytes(body)
    os.chmod(path, mode)
    return path


@pytest.fixture
def at_tmp_root(tmp_path, monkeypatch):
    """Point the module's base directory at a throwaway tree."""
    monkeypatch.setattr("urpm.core.sync.get_base_dir", lambda **kw: tmp_path)
    return tmp_path


class TestTheClosedTable:

    def test_it_names_the_catalogue_files(self):
        assert "synthesis.hdlist.cz" in MEDIA_INFO_MODES
        assert "files.xml.lzma" in MEDIA_INFO_MODES
        assert "MD5SUM" in MEDIA_INFO_MODES

    def test_every_entry_is_world_readable(self):
        for name, mode in MEDIA_INFO_MODES.items():
            assert mode & 0o044, f"{name} would not be readable by a user"

    def test_no_entry_is_writable_by_anyone_else(self):
        for name, mode in MEDIA_INFO_MODES.items():
            assert not mode & 0o022, f"{name} would be group/world writable"


class TestRestoringTheMode:

    def test_a_locked_down_file_is_reopened(self, at_tmp_root):
        media, d = _official(at_tmp_root)
        f = _write(d / "files.xml.lzma", 0o600)
        assert enforce_media_info_modes(_FakeDB([media])) == 1
        assert f.stat().st_mode & 0o777 == 0o644

    def test_a_correct_file_is_left_alone(self, at_tmp_root):
        media, d = _official(at_tmp_root)
        _write(d / "files.xml.lzma", 0o644)
        assert enforce_media_info_modes(_FakeDB([media])) == 0

    def test_every_named_file_is_covered(self, at_tmp_root):
        media, d = _official(at_tmp_root)
        for name in MEDIA_INFO_MODES:
            _write(d / name, 0o600)
        assert enforce_media_info_modes(_FakeDB([media])) == len(
            MEDIA_INFO_MODES)

    def test_a_missing_file_is_not_an_error(self, at_tmp_root):
        """A medium synced before a given artefact existed, or one the
        mirror simply does not publish."""
        media, _d = _official(at_tmp_root)
        assert enforce_media_info_modes(_FakeDB([media])) == 0

    def test_it_is_idempotent(self, at_tmp_root):
        media, d = _official(at_tmp_root)
        _write(d / "MD5SUM", 0o600)
        db = _FakeDB([media])
        assert enforce_media_info_modes(db) == 1
        assert enforce_media_info_modes(db) == 0

    def test_disabled_media_are_visited_too(self, at_tmp_root):
        """The case the conditional fetch can never repair: a medium
        switched off today is a medium switched on tomorrow, and its
        files are never re-downloaded in the meantime."""
        media, d = _official(at_tmp_root)
        media["enabled"] = 0
        _write(d / "synthesis.hdlist.cz", 0o600)
        assert enforce_media_info_modes(_FakeDB([media])) == 1


class TestWhatItRefusesToTouch:

    def test_a_file_outside_the_table_is_left_alone(self, at_tmp_root):
        """The whole reason the table is closed.  Something deliberately
        kept root-only must survive the pass untouched."""
        media, d = _official(at_tmp_root)
        secret = _write(d / "operator-private.key", 0o600)
        assert enforce_media_info_modes(_FakeDB([media])) == 0
        assert secret.stat().st_mode & 0o777 == 0o600

    def test_a_symlink_is_not_followed(self, at_tmp_root):
        """Chasing a link would let a crafted media_info aim the chmod
        at a file outside the cache."""
        media, d = _official(at_tmp_root)
        target = _write(at_tmp_root / "elsewhere", 0o600)
        (d / "files.xml.lzma").symlink_to(target)
        assert enforce_media_info_modes(_FakeDB([media])) == 0
        assert target.stat().st_mode & 0o777 == 0o600

    def test_a_directory_bearing_a_listed_name_is_skipped(self, at_tmp_root):
        media, d = _official(at_tmp_root)
        (d / "MD5SUM").mkdir()
        assert enforce_media_info_modes(_FakeDB([media])) == 0

    def test_local_media_are_skipped(self, at_tmp_root):
        """A ``file://`` medium's media_info is not a copy we own but
        the medium itself, usually mounted or shared."""
        media, d = _official(at_tmp_root)
        f = _write(d / "files.xml.lzma", 0o600)
        db = _FakeDB([media], servers={1: [{"protocol": "file"}]})
        assert enforce_media_info_modes(db) == 0
        assert f.stat().st_mode & 0o777 == 0o600


class TestItNeverBreaksTheSync:
    """It runs after a sync that has already succeeded."""

    def test_a_refused_chmod_is_counted_not_raised(self, at_tmp_root,
                                                   monkeypatch, caplog):
        media, d = _official(at_tmp_root)
        _write(d / "files.xml.lzma", 0o600)

        def _denied(*a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "chmod", _denied)
        assert enforce_media_info_modes(_FakeDB([media])) == 0
        assert any("Could not restore the mode" in r.message
                   for r in caplog.records)

    def test_a_medium_without_servers_is_still_checked(self, at_tmp_root):
        """``get_servers_for_media`` raising, or returning nothing, must
        not skip the medium — an orphan row still has files on disk."""
        media, d = _official(at_tmp_root)
        _write(d / "MD5SUM", 0o600)

        class _Raising(_FakeDB):
            def get_servers_for_media(self, media_id):
                raise RuntimeError("no such media")

        assert enforce_media_info_modes(_Raising([media])) == 1

    def test_several_media_are_all_visited(self, at_tmp_root):
        m1, d1 = _official(at_tmp_root, 1, "10/x86_64/core")
        m2, d2 = _official(at_tmp_root, 2, "10/x86_64/tainted")
        _write(d1 / "MD5SUM", 0o600)
        _write(d2 / "synthesis.hdlist.cz", 0o600)
        assert enforce_media_info_modes(_FakeDB([m1, m2])) == 2
