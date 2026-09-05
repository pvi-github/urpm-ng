"""Relocating the RPM payload, and refusing to start without room.

A distupgrade pulls several GB across a few thousand packages, all of
it under ``/var/lib/urpm/medias/``.  On a machine whose ``/var`` is
sized for ordinary use that filesystem fills up mid-transfer — and
fails late, with the partial payload still occupying the space that
just ran out.

Two things are pinned here.

``payload_dir`` moves **only** the ``.rpm`` files.  The database and
the media metadata (synthesis, ``files.xml``) stay in ``/var/lib/urpm``:
they are small, needed permanently, and relocating them is what
``--urpm-root`` is for.  ``get_cache_path`` is also what ``is_cached``
consults, so both must agree on where a file lives or every download
would be redone.

``assert_space_for`` checks the target filesystem before the first
byte.  Nothing did, previously.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from urpm.core.download import (
    Downloader,
    DownloadItem,
    InsufficientSpaceError,
)


@pytest.fixture(autouse=True)
def neutral_settings(monkeypatch):
    """Keep the host's own ``download.payload_dir`` out of the tests."""
    from urpm.core import settings as settings_mod
    real = settings_mod.get_settings()

    class _Download:
        parallel = 4
        payload_dir = ""

    class _Stub:
        download = _Download()

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Stub())
    monkeypatch.setattr(
        "urpm.core.download.get_settings", lambda: _Stub(), raising=False,
    )
    return _Download


def _item(size: int = 1024) -> DownloadItem:
    """A minimal item on the legacy schema (no ``relative_path``).

    ``filename`` and ``url`` are derived properties, not fields — the
    NEVRA components are what the dataclass takes.
    """
    return DownloadItem(
        name="foo",
        version="1.0",
        release="1.mga10",
        arch="x86_64",
        size=size,
        media_name="Core Release",
        media_url="https://example.invalid/media",
    )


class TestPayloadDirDefaults:

    def test_defaults_to_cache_dir(self, tmp_path):
        """Unchanged layout unless someone asks otherwise."""
        d = Downloader(cache_dir=tmp_path)
        assert d.payload_dir == tmp_path

    def test_config_key_is_honoured(self, tmp_path, neutral_settings):
        neutral_settings.payload_dir = str(tmp_path / "elsewhere")
        d = Downloader(cache_dir=tmp_path)
        assert d.payload_dir == tmp_path / "elsewhere"

    def test_argument_beats_the_config_key(self, tmp_path, neutral_settings):
        """``--download-dir`` is the one-off override; the config key is
        the durable answer.  The explicit request wins."""
        neutral_settings.payload_dir = str(tmp_path / "from-config")
        d = Downloader(cache_dir=tmp_path,
                       payload_dir=str(tmp_path / "from-flag"))
        assert d.payload_dir == tmp_path / "from-flag"

    def test_directory_is_created(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist"
        Downloader(cache_dir=tmp_path, payload_dir=str(target))
        assert target.is_dir()

    def test_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = Downloader(cache_dir=tmp_path, payload_dir="~/rpms")
        assert d.payload_dir == tmp_path / "rpms"


class TestPayloadDirRouting:

    def test_rpms_land_under_payload_dir(self, tmp_path):
        payload = tmp_path / "big"
        d = Downloader(cache_dir=tmp_path, payload_dir=str(payload))
        path = d.get_cache_path(_item())
        assert payload in path.parents

    def test_rpms_stay_out_of_cache_dir(self, tmp_path):
        """The point of the exercise : nothing heavy under the default
        cache once the payload is relocated."""
        payload = tmp_path / "big"
        cache = tmp_path / "state"
        d = Downloader(cache_dir=cache, payload_dir=str(payload))
        assert cache not in d.get_cache_path(_item()).parents

    def test_layout_is_preserved(self, tmp_path):
        """Relocating must not reshuffle the tree — the ``medias/``
        layout is what makes a relocated cache still readable."""
        payload = tmp_path / "big"
        d = Downloader(cache_dir=tmp_path, payload_dir=str(payload))
        path = d.get_cache_path(_item())
        assert "medias" in path.parts
        assert path.name == "foo-1.0-1.mga10.x86_64.rpm"

    def test_is_cached_looks_where_downloads_go(self, tmp_path):
        """``is_cached`` and ``get_cache_path`` must never disagree,
        or every package would be re-downloaded on each run."""
        payload = tmp_path / "big"
        d = Downloader(cache_dir=tmp_path, payload_dir=str(payload))
        item = _item()
        target = d.get_cache_path(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Minimal valid RPM magic so the cache check accepts the file.
        target.write_bytes(b"\xed\xab\xee\xdb" + b"\x00" * 1024)
        assert d.is_cached(item)


class TestSpacePreflight:

    def _downloader_with_free(self, tmp_path, monkeypatch, free_bytes):
        d = Downloader(cache_dir=tmp_path)

        class _Stat:
            f_bavail = free_bytes
            f_frsize = 1

        monkeypatch.setattr(os, "statvfs", lambda p: _Stat())
        return d

    def test_passes_when_there_is_room(self, tmp_path, monkeypatch):
        d = self._downloader_with_free(tmp_path, monkeypatch, 10 * 1024 ** 3)
        assert d.assert_space_for([_item(1024)]) is None

    def test_raises_when_short(self, tmp_path, monkeypatch):
        d = self._downloader_with_free(tmp_path, monkeypatch, 1024)
        with pytest.raises(InsufficientSpaceError):
            d.assert_space_for([_item(5 * 1024 ** 3)])

    def test_margin_is_enforced(self, tmp_path, monkeypatch):
        """Room for exactly the payload is not enough : RPMs are
        unpacked from the same filesystem, and a device at zero free
        bytes fails in uglier ways than one refusing early."""
        payload = 1024 ** 3
        d = self._downloader_with_free(tmp_path, monkeypatch, payload)
        with pytest.raises(InsufficientSpaceError):
            d.assert_space_for([_item(payload)])

    def test_empty_list_is_a_noop(self, tmp_path, monkeypatch):
        d = self._downloader_with_free(tmp_path, monkeypatch, 0)
        assert d.assert_space_for([]) is None

    def test_unreadable_target_does_not_raise(self, tmp_path, monkeypatch):
        """A statvfs failure is not evidence of a space problem — let
        the download surface the real error instead of guessing."""
        d = Downloader(cache_dir=tmp_path)

        def _boom(_p):
            raise OSError("nope")

        monkeypatch.setattr(os, "statvfs", _boom)
        assert d.assert_space_for([_item(5 * 1024 ** 3)]) is None

    def test_message_carries_figures_and_the_way_out(
        self, tmp_path, monkeypatch,
    ):
        """« no space left on device » names no filesystem and no
        remedy.  This one has to do both."""
        d = self._downloader_with_free(tmp_path, monkeypatch, 1024)
        with pytest.raises(InsufficientSpaceError) as exc:
            d.assert_space_for([_item(5 * 1024 ** 3)])
        msg = str(exc.value)
        assert str(tmp_path) in msg
        assert "GB" in msg
        assert "--download-dir" in msg
        assert "payload_dir" in msg

    def test_checked_against_the_payload_filesystem(
        self, tmp_path, monkeypatch,
    ):
        """The check must look at where the RPMs actually go, which is
        not necessarily the filesystem holding /var."""
        payload = tmp_path / "big"
        d = Downloader(cache_dir=tmp_path, payload_dir=str(payload))
        seen = []

        class _Stat:
            f_bavail = 10 * 1024 ** 3
            f_frsize = 1

        monkeypatch.setattr(
            os, "statvfs", lambda p: (seen.append(Path(p)), _Stat())[1],
        )
        d.assert_space_for([_item()])
        assert seen == [payload]
