"""``urpm install https://host/path/pkg.rpm``.

A URL used to satisfy :func:`is_local_rpm` (it ends in ``.rpm`` and
holds a slash), so it was handed to ``Path()`` and the command died on
« file not found » having never tried to fetch anything.

The fetched file lands beside ``medias/``, never inside it.  That tree
mirrors a remote one medium by medium, and every file in it is expected
to correspond to something its mirror serves; a package named by a URL
belongs to no medium, so filing it under one would leave that medium's
cache disagreeing with its source for no benefit.

Being cache all the same, it has to be swept by ``urpm cache flush``
and counted as an orphan by ``urpm cache clean``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from urpm.core.cache import CacheManager, flush_payloads
from urpm.core.download import (
    STANDALONE_DIR_NAME,
    fetch_standalone_rpm,
    standalone_rpm_dir,
)
from urpm.core.rpm import is_local_rpm, is_remote_rpm, local_rpm_path


class TestSpecClassification:

    @pytest.mark.parametrize("url", [
        "https://host/path/foo-1.0-1.mga10.x86_64.rpm",
        "http://host/foo.rpm",
        "ftp://host/pub/foo.rpm",
    ])
    def test_a_url_is_remote_and_not_local(self, url):
        """Both halves matter: claiming it is local is the old bug."""
        assert is_remote_rpm(url)
        assert not is_local_rpm(url)

    @pytest.mark.parametrize("spec", [
        "/tmp/foo.rpm", "./foo.rpm", "../build/foo.rpm",
    ])
    def test_a_path_stays_local(self, spec):
        assert is_local_rpm(spec)
        assert not is_remote_rpm(spec)

    @pytest.mark.parametrize("spec", ["foo", "foo-1.0", "https://host/page"])
    def test_a_name_is_neither(self, spec):
        assert not is_local_rpm(spec)
        assert not is_remote_rpm(spec)

    def test_file_scheme_is_a_path(self):
        """``file://`` names a local file.  It satisfied is_local_rpm
        before too, but the scheme was never stripped, so it died on
        « file not found » just like an http URL."""
        spec = "file:///tmp/foo-1.0.rpm"
        assert is_local_rpm(spec)
        assert not is_remote_rpm(spec)
        assert local_rpm_path(spec) == Path("/tmp/foo-1.0.rpm")

    def test_a_percent_encoded_path_is_decoded(self):
        assert local_rpm_path("file:///tmp/a%20b/foo.rpm") == Path(
            "/tmp/a b/foo.rpm")

    def test_a_plain_path_passes_through(self):
        assert local_rpm_path("/tmp/foo.rpm") == Path("/tmp/foo.rpm")


class TestStandaloneLocation:

    def test_it_sits_beside_medias_not_inside(self, tmp_path):
        """The constraint this whole layout exists for."""
        directory = standalone_rpm_dir(str(tmp_path))
        assert directory.parent == tmp_path
        assert "medias" not in directory.parts
        assert directory.name == STANDALONE_DIR_NAME

    def test_it_is_created_on_demand(self, tmp_path):
        assert standalone_rpm_dir(str(tmp_path)).is_dir()


class TestFetch:

    def test_a_url_that_names_no_rpm_is_refused_before_the_network(
            self, tmp_path):
        """No request goes out: there is nothing to fetch."""
        path, reason = fetch_standalone_rpm(
            "https://host/index.html", payload_dir=str(tmp_path))
        assert path is None
        assert ".rpm" in reason

    def test_a_body_that_is_not_an_rpm_leaves_nothing_behind(
            self, tmp_path, monkeypatch):
        """A mirror answering 404 with an HTML page is the common case.
        Keeping the file would poison the next run, and rpm would
        report something far less obvious about it."""
        def fake_download(url, dest, **kwargs):
            Path(dest).write_bytes(b"<html>404</html>")
            result = type("R", (), {"success": True, "md5": "", "size": 16})
            return result()

        monkeypatch.setattr("urpm.core.sync.download_file", fake_download)
        path, reason = fetch_standalone_rpm(
            "https://host/foo.rpm", payload_dir=str(tmp_path))
        assert path is None
        assert reason
        assert list(standalone_rpm_dir(str(tmp_path)).iterdir()) == []

    def test_a_failed_transfer_leaves_nothing_behind(self, tmp_path,
                                                     monkeypatch):
        """No partial file, so a retry never resumes onto a truncated
        body."""
        def fake_download(url, dest, **kwargs):
            Path(dest).write_bytes(b"half")
            result = type("R", (), {"success": False, "error": "HTTP 500"})
            return result()

        monkeypatch.setattr("urpm.core.sync.download_file", fake_download)
        path, reason = fetch_standalone_rpm(
            "https://host/foo.rpm", payload_dir=str(tmp_path))
        assert path is None
        assert "500" in reason
        assert list(standalone_rpm_dir(str(tmp_path)).iterdir()) == []

    def test_the_name_comes_from_the_url_path(self, tmp_path, monkeypatch):
        """Not from a redirect, not from a header: the packager typed a
        URL and expects that file name in the cache."""
        seen = {}

        def fake_download(url, dest, **kwargs):
            seen['dest'] = Path(dest)
            # Minimal well-formed RPM lead so is_valid_rpm passes.
            Path(dest).write_bytes(b"\xed\xab\xee\xdb" + b"\0" * 92)
            result = type("R", (), {"success": True, "md5": "", "size": 96})
            return result()

        monkeypatch.setattr("urpm.core.sync.download_file", fake_download)
        path, reason = fetch_standalone_rpm(
            "https://host/p/foo-1.0-1.mga10.x86_64.rpm?token=x",
            payload_dir=str(tmp_path))
        assert reason == ""
        assert path is not None
        assert path.name == "foo-1.0-1.mga10.x86_64.rpm"
        assert seen['dest'].parent.name == STANDALONE_DIR_NAME


class TestSourceRpms:
    """``--install-src`` takes the same specs as plain install.

    Its own resolver handled a ``.src.rpm`` path, a non-source local
    file (refused) and a package name.  A URL fell into the first case,
    was handed to ``Path()`` and vanished as « not found », so
    ``--install-src`` refused what ``install`` accepted, for no reason
    a user could guess.
    """

    def test_a_remote_source_rpm_is_remote(self):
        assert is_remote_rpm("https://host/p/foo-1.0-1.mga10.src.rpm")

    def test_a_file_url_source_rpm_resolves_to_its_path(self, tmp_path):
        srpm = tmp_path / "foo-1.0-1.mga10.src.rpm"
        srpm.write_bytes(b"\xed\xab\xee\xdb")
        from urpm.cli.commands.install import _resolve_srpm_path

        assert _resolve_srpm_path(f"file://{srpm}", None) == srpm

    def test_a_url_is_fetched_rather_than_looked_up_on_disk(self,
                                                            monkeypatch):
        """The regression: Case 1 used to swallow it silently."""
        from urpm.cli.commands import install as install_cmd

        fetched = {}

        def fake_fetch(url):
            fetched['url'] = url
            return Path("/var/lib/urpm/downloads/foo-1.0-1.mga10.src.rpm")

        monkeypatch.setattr(install_cmd, "_fetch_remote_rpm", fake_fetch)
        resolved = install_cmd._resolve_srpm_path(
            "https://host/p/foo-1.0-1.mga10.src.rpm", None)
        assert fetched['url'].startswith("https://")
        assert resolved is not None and resolved.name.endswith(".src.rpm")


class _FakeDb:
    """Just enough database for CacheManager and the orphan sweep."""

    class _Conn:
        def execute(self, *a, **kw):
            return iter(())

    def __init__(self):
        self.conn = self._Conn()


class TestCacheKnowsAboutIt:
    """A cache you cannot clean is a leak."""

    @pytest.fixture
    def cache_tree(self, tmp_path):
        (tmp_path / "medias" / "official").mkdir(parents=True)
        (tmp_path / "medias" / "official" / "from-media.rpm").write_bytes(
            b"x" * 10)
        standalone = standalone_rpm_dir(str(tmp_path))
        (standalone / "from-url.rpm").write_bytes(b"y" * 20)
        return tmp_path

    def test_both_directories_are_payload_directories(self, cache_tree):
        manager = CacheManager(_FakeDb(), cache_tree)
        assert set(manager.payload_dirs) == {
            manager.medias_dir, manager.standalone_dir}

    def test_flush_removes_the_url_fetched_file_too(self, cache_tree):
        files, total = flush_payloads(_FakeDb(), cache_tree)
        assert files == 2
        assert total == 30
        assert not (cache_tree / STANDALONE_DIR_NAME
                    / "from-url.rpm").exists()

    def test_flush_dry_run_keeps_it(self, cache_tree):
        files, _total = flush_payloads(_FakeDb(), cache_tree, dry_run=True)
        assert files == 2
        assert (cache_tree / STANDALONE_DIR_NAME / "from-url.rpm").exists()

    def test_a_missing_standalone_dir_is_not_an_error(self, tmp_path):
        """Nothing was ever fetched from a URL on this machine."""
        (tmp_path / "medias").mkdir()
        manager = CacheManager(_FakeDb(), tmp_path)
        assert manager.payload_dirs == (manager.medias_dir,)
        assert flush_payloads(_FakeDb(), tmp_path) == (0, 0)
