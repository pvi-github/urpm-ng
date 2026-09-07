"""The release-N cache must go before release N+1 is downloaded.

By the time Phase A is done the machine is up to date, so the cached
payloads have done their job: they were needed to reach the state
Stage 2 computes its plan against, and nothing after this reads them.
Stage 2 then pulls the whole target release onto the same partition.

Purging only what Phase A downloaded would miss the case that matters
most.  A careful operator runs ``urpm upgrade`` themselves the day
before; Phase A then has nothing to fetch, several hundred megabytes
are already sitting in the cache, and the machine most likely to be
short of room reclaims nothing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core.distupgrade.phase_a import purge_source_release_payloads


@pytest.fixture
def db():
    from urpm.core.database import PackageDatabase
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        path = Path(handle.name)
    database = PackageDatabase(path)
    yield database
    database.close()
    path.unlink(missing_ok=True)


@pytest.fixture
def cache(tmp_path):
    """A cache laid out like the real one: db and metadata beside the
    payloads, payloads under ``medias/``."""
    medias = tmp_path / "medias" / "Core Release"
    medias.mkdir(parents=True)
    (medias / "foo-1.0-1.mga10.x86_64.rpm").write_bytes(b"x" * 4096)
    (medias / "bar-2.0-1.mga10.noarch.rpm").write_bytes(b"y" * 2048)
    (medias / "synthesis.hdlist.cz").write_bytes(b"metadata")
    (tmp_path / "packages.db").write_bytes(b"database")
    (tmp_path / "appstream").mkdir()
    (tmp_path / "appstream" / "core.xml").write_bytes(b"components")
    return tmp_path


class TestPayloadsGo:

    def test_rpms_are_removed(self, db, cache):
        freed_files, freed_bytes = purge_source_release_payloads(db, cache)
        assert freed_files == 2
        assert freed_bytes == 4096 + 2048
        assert not list((cache / "medias").rglob("*.rpm"))

    def test_it_reports_what_it_freed(self, db, cache):
        """The figure is what makes the difference visible in the log
        when a migration later runs out of room anyway."""
        _files, freed_bytes = purge_source_release_payloads(db, cache)
        assert freed_bytes > 0


class TestEverythingElseStays:
    """The database and the media metadata live beside the payloads,
    are small, and are needed throughout."""

    def test_the_database_survives(self, db, cache):
        purge_source_release_payloads(db, cache)
        assert (cache / "packages.db").exists()

    def test_media_metadata_survives(self, db, cache):
        purge_source_release_payloads(db, cache)
        assert (cache / "medias" / "Core Release" / "synthesis.hdlist.cz").exists()

    def test_appstream_survives(self, db, cache):
        purge_source_release_payloads(db, cache)
        assert (cache / "appstream" / "core.xml").exists()


class TestItNeverBreaksTheMigration:
    """Reclaiming space is an optimisation; failing at it must not stop
    a migration that would otherwise have run."""

    def test_a_missing_cache_is_not_an_error(self, db, tmp_path):
        assert purge_source_release_payloads(db, tmp_path) == (0, 0)

    def test_an_empty_cache_is_not_an_error(self, db, tmp_path):
        (tmp_path / "medias").mkdir()
        assert purge_source_release_payloads(db, tmp_path) == (0, 0)

    def test_an_unreadable_file_is_skipped_not_fatal(self, db, cache,
                                                     monkeypatch):
        import pathlib
        real_unlink = pathlib.Path.unlink

        def refuse(self, *a, **kw):
            if self.name.startswith("foo"):
                raise PermissionError("read-only")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "unlink", refuse)
        freed_files, _bytes = purge_source_release_payloads(db, cache)
        assert freed_files == 1, "the other payload must still go"


class TestTheFlushVerb:
    """``urpm cache flush`` is the same sweep, offered to an operator
    who is short of disk rather than mid-migration.

    Distinct from ``clean``, which removes only what the database no
    longer references — on a machine whose cache is entirely current,
    ``clean`` frees nothing at all.
    """

    def _args(self, **kw):
        import argparse
        base = {"dry_run": False, "auto": True}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_dry_run_removes_nothing(self, db, cache, monkeypatch, capsys):
        from urpm.cli.commands import cache as cache_cmd
        from urpm.core import cache as core_cache

        real = core_cache.flush_payloads
        monkeypatch.setattr(
            core_cache, "flush_payloads",
            lambda d, base_dir=None, dry_run=False: real(d, cache, dry_run))

        rc = cache_cmd.cmd_cache_flush(self._args(dry_run=True), db)
        assert rc == 0
        assert len(list((cache / "medias").rglob("*.rpm"))) == 2, (
            "--dry-run must leave the cache untouched"
        )

    def test_it_reports_the_volume_before_acting(self, db, cache,
                                                 monkeypatch, capsys):
        """An operator deciding whether to spend their cache needs the
        figure first."""
        from urpm.cli.commands import cache as cache_cmd
        from urpm.core import cache as core_cache

        real = core_cache.flush_payloads
        monkeypatch.setattr(
            core_cache, "flush_payloads",
            lambda d, base_dir=None, dry_run=False: real(d, cache, dry_run))

        cache_cmd.cmd_cache_flush(self._args(dry_run=True), db)
        assert "2" in capsys.readouterr().out

    def test_it_removes_when_confirmed(self, db, cache, monkeypatch):
        from urpm.cli.commands import cache as cache_cmd
        from urpm.core import cache as core_cache

        real = core_cache.flush_payloads
        monkeypatch.setattr(
            core_cache, "flush_payloads",
            lambda d, base_dir=None, dry_run=False: real(d, cache, dry_run))

        assert cache_cmd.cmd_cache_flush(self._args(), db) == 0
        assert not list((cache / "medias").rglob("*.rpm"))

    def test_an_empty_cache_says_so(self, db, tmp_path, monkeypatch, capsys):
        from urpm.cli.commands import cache as cache_cmd
        from urpm.core import cache as core_cache

        monkeypatch.setattr(
            core_cache, "flush_payloads",
            lambda *a, **kw: (0, 0))
        assert cache_cmd.cmd_cache_flush(self._args(), db) == 0
        assert "No cached RPM" in capsys.readouterr().out


class TestCleanLooksWherePayloadsAre:
    """``urpm cache clean`` removes the ``.rpm`` no synthesis references
    any more — superseded versions, packages dropped from a medium.

    It was reading ``~/.cache/urpm/medias``, which does not exist: the
    only thing under ``~/.cache/urpm`` is the mkimage chroots.  So it
    answered « no RPM cache found » and did nothing, on a machine
    holding gigabytes of orphans.  Verified on the live system before
    the fix.
    """

    def _args(self, **kw):
        import argparse
        base = {"dry_run": True, "auto": True, "verbose": False}
        base.update(kw)
        return argparse.Namespace(**base)

    @pytest.fixture
    def cache_with_orphan(self, db, tmp_path):
        """One payload the database knows, one it does not."""
        medias = tmp_path / "medias" / "Core Release"
        medias.mkdir(parents=True)
        (medias / "known-1.0-1.mga10.x86_64.rpm").write_bytes(b"k" * 512)
        (medias / "orphan-9.9-9.mga10.x86_64.rpm").write_bytes(b"o" * 1024)

        media_id = db.add_media(
            name="Core Release", short_name="core_release",
            mageia_version="10", architecture="x86_64",
            relative_path="10/x86_64/media/core/release")
        db.conn.execute(
            "INSERT INTO packages (name, name_lower, version, release, arch, "
            "nevra, media_id) VALUES (?,?,?,?,?,?,?)",
            ("known", "known", "1.0", "1.mga10", "x86_64",
             "known-1.0-1.mga10.x86_64", media_id))
        db.conn.commit()
        return tmp_path

    def _run(self, db, base, monkeypatch, capsys, **kw):
        from urpm.cli.commands import cache as cache_cmd
        from urpm.core import cache as core_cache

        real = core_cache.CacheManager
        monkeypatch.setattr(
            core_cache, "CacheManager",
            lambda d, base_dir=None: real(d, base))
        cache_cmd.cmd_cache_clean(self._args(**kw), db)
        return capsys.readouterr().out

    def test_it_finds_the_orphan(self, db, cache_with_orphan, monkeypatch,
                                 capsys):
        out = self._run(db, cache_with_orphan, monkeypatch, capsys)
        assert "orphan-9.9-9.mga10.x86_64.rpm" in out

    def test_it_spares_what_the_synthesis_still_lists(
            self, db, cache_with_orphan, monkeypatch, capsys):
        out = self._run(db, cache_with_orphan, monkeypatch, capsys)
        assert "known-1.0-1.mga10.x86_64.rpm" not in out

    def test_dry_run_removes_nothing(self, db, cache_with_orphan,
                                     monkeypatch, capsys):
        self._run(db, cache_with_orphan, monkeypatch, capsys)
        assert len(list((cache_with_orphan / "medias").rglob("*.rpm"))) == 2
