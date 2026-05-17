"""Unit tests for the conditional ``files.xml.lzma`` refresh.

Exercises :func:`urpm.core.sync._fetch_files_xml_if_changed` — the
helper that decides, after every ``urpm media update``, whether to
re-download a medium's file list.  The function is non-fatal by
design: every failure mode (missing MD5SUM entry, download error,
checksum mismatch) must leave the on-disk state untouched and the
database tracker unchanged so the next sync retries.

These tests stub out the network with ``unittest.mock`` and a tiny
fake database so they can run anywhere — no Mageia mirror needed.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from urpm.core import sync as sync_mod
from urpm.core.sync import DownloadResult, _fetch_files_xml_if_changed


class _FakeDB:
    """Minimal DB stub: only the two methods _fetch_files_xml_if_changed touches."""

    def __init__(self, stored_md5=None):
        self._stored_md5 = stored_md5
        self.updates = []  # records every call to update_media_files_xml_md5

    def get_media_by_id(self, media_id):
        return {'id': media_id, 'files_xml_md5': self._stored_md5}

    def update_media_files_xml_md5(self, media_id, md5):
        self.updates.append((media_id, md5))


@pytest.fixture
def cache_dir(tmp_path):
    """Stand-in for the ``<media>/media_info`` directory."""
    p = tmp_path / "media_info"
    p.mkdir()
    return p


def _media():
    """Stand-in v8 media dict — only fields read by build_media_url matter."""
    return {'id': 42, 'name': 'TestMedia',
            'relative_path': '10/x86_64/media/core/release'}


def _server():
    return {'id': 1, 'host': 'mirror.example',
            'protocol': 'https', 'base_path': '/mageia'}


# -------------------------------------------------------------------------
# 1. MD5SUM does not list files.xml.lzma → no-op.
# -------------------------------------------------------------------------

def test_noop_when_md5sum_has_no_entry(cache_dir):
    db = _FakeDB(stored_md5='aaa')

    # Empty md5sums dict — nothing for files.xml.lzma.
    with patch.object(sync_mod, 'download_from_server') as dl_srv, \
         patch.object(sync_mod, 'download_file') as dl:
        _fetch_files_xml_if_changed(
            db, 42, _media(), _server(), 'https://x/', cache_dir,
            md5sums={},
        )

    # Neither download path is called, DB is not updated, and we did
    # not create a phantom files.xml.lzma.
    dl_srv.assert_not_called()
    dl.assert_not_called()
    assert db.updates == []
    assert not (cache_dir / "files.xml.lzma").exists()


# -------------------------------------------------------------------------
# 2. MD5 unchanged AND file already on disk → skip the download.
# -------------------------------------------------------------------------

def test_skip_when_md5_unchanged_and_file_present(cache_dir):
    md5 = 'd41d8cd98f00b204e9800998ecf8427e'
    db = _FakeDB(stored_md5=md5)
    (cache_dir / "files.xml.lzma").write_bytes(b'stale-but-present')

    with patch.object(sync_mod, 'download_from_server') as dl_srv, \
         patch.object(sync_mod, 'download_file') as dl:
        _fetch_files_xml_if_changed(
            db, 42, _media(), _server(), 'https://x/', cache_dir,
            md5sums={'files.xml.lzma': md5},
        )

    dl_srv.assert_not_called()
    dl.assert_not_called()
    assert db.updates == []
    assert (cache_dir / "files.xml.lzma").read_bytes() == b'stale-but-present'


# -------------------------------------------------------------------------
# 2bis. MD5 matches but the file is missing from disk → must re-fetch.
# -------------------------------------------------------------------------

def test_download_when_db_in_sync_but_file_missing(cache_dir):
    md5 = 'aabbccddeeff00112233445566778899'
    db = _FakeDB(stored_md5=md5)

    def fake_dl(url, dest, server, *args, **kw):
        Path(dest).write_bytes(b'fresh-payload')
        return DownloadResult(success=True, path=dest, size=13, md5=md5)

    with patch.object(sync_mod, 'download_from_server', side_effect=fake_dl), \
         patch.object(sync_mod, 'download_file') as dl:
        _fetch_files_xml_if_changed(
            db, 42, _media(), _server(), 'https://x/', cache_dir,
            md5sums={'files.xml.lzma': md5},
        )

    dl.assert_not_called()  # server path used
    assert db.updates == [(42, md5)]
    assert (cache_dir / "files.xml.lzma").read_bytes() == b'fresh-payload'


# -------------------------------------------------------------------------
# 3. Remote MD5 differs → download, verify, persist, update DB.
# -------------------------------------------------------------------------

def test_download_when_md5_changed(cache_dir):
    new_md5 = '0123456789abcdef0123456789abcdef'
    db = _FakeDB(stored_md5='oldhash')

    def fake_dl(url, dest, server, *args, **kw):
        Path(dest).write_bytes(b'new-payload')
        return DownloadResult(success=True, path=dest, size=11, md5=new_md5)

    with patch.object(sync_mod, 'download_from_server', side_effect=fake_dl):
        _fetch_files_xml_if_changed(
            db, 42, _media(), _server(), 'https://x/', cache_dir,
            md5sums={'files.xml.lzma': new_md5},
        )

    assert db.updates == [(42, new_md5)]
    assert (cache_dir / "files.xml.lzma").read_bytes() == b'new-payload'


# -------------------------------------------------------------------------
# 4. Downloaded body hashes to a different value → keep old copy,
#    do NOT update the DB.
# -------------------------------------------------------------------------

def test_skip_persist_on_checksum_mismatch(cache_dir):
    advertised = 'expected-md5'
    actual = 'different-md5'
    db = _FakeDB(stored_md5='oldhash')
    (cache_dir / "files.xml.lzma").write_bytes(b'previous-good-copy')

    def fake_dl(url, dest, server, *args, **kw):
        Path(dest).write_bytes(b'corrupted-bytes')
        return DownloadResult(success=True, path=dest, size=16, md5=actual)

    with patch.object(sync_mod, 'download_from_server', side_effect=fake_dl):
        _fetch_files_xml_if_changed(
            db, 42, _media(), _server(), 'https://x/', cache_dir,
            md5sums={'files.xml.lzma': advertised},
        )

    # Mismatch → previous on-disk copy is preserved, DB unchanged,
    # next sync will retry.
    assert db.updates == []
    assert (cache_dir / "files.xml.lzma").read_bytes() == b'previous-good-copy'


# -------------------------------------------------------------------------
# 5. Download failure → log debug, no DB update.
# -------------------------------------------------------------------------

def test_skip_persist_on_download_failure(cache_dir):
    db = _FakeDB(stored_md5='oldhash')

    def fake_dl(url, dest, server, *args, **kw):
        return DownloadResult(success=False, error='HTTP 503')

    with patch.object(sync_mod, 'download_from_server', side_effect=fake_dl):
        _fetch_files_xml_if_changed(
            db, 42, _media(), _server(), 'https://x/', cache_dir,
            md5sums={'files.xml.lzma': 'whatever'},
        )

    assert db.updates == []
    # No files.xml.lzma left behind, no exception raised.
    assert not (cache_dir / "files.xml.lzma").exists()


# -------------------------------------------------------------------------
# 6. Legacy single-URL path (server=None) uses download_file instead.
# -------------------------------------------------------------------------

def test_legacy_url_path_uses_download_file(cache_dir):
    md5 = 'legacy-md5'
    db = _FakeDB(stored_md5=None)

    def fake_dl(url, dest, *args, **kw):
        Path(dest).write_bytes(b'legacy-payload')
        return DownloadResult(success=True, path=dest, size=14, md5=md5)

    with patch.object(sync_mod, 'download_file', side_effect=fake_dl) as dl, \
         patch.object(sync_mod, 'download_from_server') as dl_srv:
        _fetch_files_xml_if_changed(
            db, 42, _media(), None, 'https://legacy.example/media',
            cache_dir, md5sums={'files.xml.lzma': md5},
        )

    dl_srv.assert_not_called()
    assert dl.call_count == 1
    assert db.updates == [(42, md5)]
    assert (cache_dir / "files.xml.lzma").read_bytes() == b'legacy-payload'
