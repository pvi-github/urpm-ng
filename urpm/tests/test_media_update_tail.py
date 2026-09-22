"""A media refresh moves two very different piles of bytes.

The head — synthesis, parse, import — is what a resolution reads, and
weighs 4.8 MB across a typical fifteen media.  The tail is
``files.xml.lzma`` and the AppStream catalogue built from it: 36.3 MB,
read only later by ``urpm f``, ``urpm show --files`` and the distupgrade
file-provides injection.

Measured on the machine this came from, the head imports in six seconds
and the tail is the whole wait.  Worse, the tail is one file: 23 MB for
core/release, on a four-worker pool where the other three finish early
and wait.  At a mirror serving 300 kB/s — ordinary for a busy one — that
is two minutes before the terminal comes back, for bytes the install the
operator is about to run will never open.

So a bare ``urpm media update`` now hands the terminal back after the
head.  The tail cannot simply be "the same sync, later": ``sync_media``
returns early once the freshness check reports nothing changed, and the
tail sits past that return, so a re-run right after a sync would skip
every medium and the tail would never happen.  Hence its own entry
point, keyed on its own MD5.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from urpm.core import sync as sync_mod


class _FakeDB:
    def __init__(self, media):
        self._media = media

    def get_media_by_id(self, media_id):
        return self._media.get(media_id)

    def get_servers_for_media(self, media_id, enabled_only=False):
        return [{"protocol": "https", "host": "mirror.example",
                 "base_path": "/mageia"}]

    def list_media(self):
        return list(self._media.values())


@pytest.fixture
def one_medium():
    return _FakeDB({1: {
        "id": 1, "name": "Core Release", "is_official": 1,
        "relative_path": "10/x86_64/media/core/release",
        "short_name": "core_release",
    }})


@pytest.fixture
def at_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "get_base_dir", lambda **kw: tmp_path)
    return tmp_path


class TestTheTailStandsOnItsOwn:
    """It has to: nothing else will come back for it."""

    def test_it_fetches_the_file_index(self, one_medium, at_tmp):
        with patch.object(sync_mod, "download_from_server") as dl, \
             patch.object(sync_mod, "parse_md5sum_file",
                          return_value={"files.xml.lzma": "abc"}), \
             patch.object(sync_mod, "_fetch_files_xml_if_changed") as fetch:
            dl.return_value = sync_mod.DownloadResult(success=True)
            assert sync_mod.sync_media_tail(
                one_medium, 1, skip_appstream=True) is True
        assert fetch.called

    def test_it_reports_when_no_mirror_answers(self, one_medium, at_tmp):
        """Returning False is what lets a caller say so instead of
        leaving the operator with an index that quietly never came."""
        with patch.object(sync_mod, "download_from_server") as dl:
            dl.return_value = sync_mod.DownloadResult(
                success=False, error="HTTP 404")
            assert sync_mod.sync_media_tail(
                one_medium, 1, skip_appstream=True) is False

    def test_an_unknown_medium_is_not_an_error(self, one_medium, at_tmp):
        assert sync_mod.sync_media_tail(one_medium, 99) is False

    def test_a_failing_fetch_does_not_raise(self, one_medium, at_tmp):
        """It runs after a sync that already succeeded.  A flaky index
        mirror must not turn that into a failure."""
        with patch.object(sync_mod, "download_from_server") as dl, \
             patch.object(sync_mod, "parse_md5sum_file", return_value={}), \
             patch.object(sync_mod, "_fetch_files_xml_if_changed",
                          side_effect=OSError("mirror gone")):
            dl.return_value = sync_mod.DownloadResult(success=True)
            assert sync_mod.sync_media_tail(
                one_medium, 1, skip_appstream=True) is True

    def test_appstream_is_skippable_on_its_own(self, one_medium, at_tmp):
        with patch.object(sync_mod, "download_from_server") as dl, \
             patch.object(sync_mod, "parse_md5sum_file", return_value={}), \
             patch.object(sync_mod, "_fetch_files_xml_if_changed"), \
             patch("urpm.core.appstream.AppStreamManager") as mgr:
            dl.return_value = sync_mod.DownloadResult(success=True)
            sync_mod.sync_media_tail(one_medium, 1, skip_appstream=True)
        assert not mgr.called


class TestTheHandOff:
    """Who finishes the job when the terminal has been handed back."""

    def _results(self, *entries):
        return [(name, MagicMock(success=ok, skipped=skipped))
                for name, ok, skipped in entries]

    def test_the_daemon_is_asked_first(self, one_medium):
        from urpm.cli.commands.media import _finish_media_tail
        with patch("urpm.core.operations.PackageOperations"
                   ".notify_urpmd_media_tail", return_value=True) as notify, \
             patch.object(sync_mod, "sync_media_tail") as direct:
            _finish_media_tail(one_medium,
                               self._results(("Core Release", True, False)),
                               False, MagicMock(urpm_root=None))
        assert notify.called
        assert not direct.called, "the daemon took it; we must not redo it"

    def test_we_do_it_ourselves_when_nothing_answers(self, one_medium):
        """urpmd is optional.  Leaving the index unfetched would not
        fail, it would make `urpm f` answer « nothing found »."""
        from urpm.cli.commands import media as media_mod
        with patch("urpm.core.operations.PackageOperations"
                   ".notify_urpmd_media_tail", return_value=False), \
             patch.object(sync_mod, "sync_media_tail") as direct:
            media_mod._finish_media_tail(
                one_medium, self._results(("Core Release", True, False)),
                False, MagicMock(urpm_root=None))
        assert direct.called

    def test_untouched_media_are_left_out(self, one_medium):
        """A medium whose synthesis did not move has a file index that
        did not move either."""
        from urpm.cli.commands import media as media_mod
        with patch("urpm.core.operations.PackageOperations"
                   ".notify_urpmd_media_tail") as notify:
            media_mod._finish_media_tail(
                one_medium, self._results(("Core Release", True, True)),
                False, MagicMock(urpm_root=None))
        assert not notify.called

    def test_failed_media_are_left_out(self, one_medium):
        from urpm.cli.commands import media as media_mod
        with patch("urpm.core.operations.PackageOperations"
                   ".notify_urpmd_media_tail") as notify:
            media_mod._finish_media_tail(
                one_medium, self._results(("Core Release", False, False)),
                False, MagicMock(urpm_root=None))
        assert not notify.called


class TestTheDoorbell:
    """``_post_to_urpmd`` returns a verdict; the old notifier swallowed."""

    def test_a_refused_connection_is_a_no(self):
        from urpm.core.operations import _post_to_urpmd
        with patch("urllib.request.urlopen",
                   side_effect=ConnectionRefusedError):
            assert _post_to_urpmd("/api/media-tail") is False

    def test_an_empty_list_needs_no_daemon(self):
        from urpm.core.operations import PackageOperations
        with patch("urpm.core.operations._post_to_urpmd") as post:
            assert PackageOperations.notify_urpmd_media_tail([]) is True
        assert not post.called, "nothing to hand over, nothing to ring for"
