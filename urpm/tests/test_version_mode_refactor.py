"""Regression tests for the ``mageia-version`` → ``version-mode`` unification.

Verify the migration wipes the retired config key, the fallback stub
os-release seed writes the right file, ``_infer_urpm_root`` finds the
chroot for a standard-layout db_path, and ``config version-mode`` runs
its media-availability preflight before persisting the choice.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase, SCHEMA_VERSION
from urpm.cli.commands.media import _seed_stub_os_release


@pytest.fixture
def fresh_db():
    """Create a throwaway DB at v37 (the current schema)."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    db = PackageDatabase(db_path)
    yield db
    db.close()
    db_path.unlink(missing_ok=True)


class TestMigrationV36ToV37:
    """The v36→v37 migration retires the ``mageia-version`` config
    key so ``version-mode`` becomes the single source of truth."""

    def test_schema_bumped(self):
        assert SCHEMA_VERSION == 37

    def test_migration_deletes_mageia_version(self):
        # Seed a DB at v36 by hand : create the config table + write
        # the key + set schema_version=36, then let PackageDatabase's
        # opener drive the migration.
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO config (key, value) VALUES "
                         "('mageia-version', 'cauldron')")
            conn.execute("CREATE TABLE schema_info (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO schema_info (version) VALUES (36)")
            conn.commit()
            conn.close()
            db = PackageDatabase(db_path)
            assert db.get_config('mageia-version') is None
            db.close()
        finally:
            db_path.unlink(missing_ok=True)


class TestInferUrpmRoot:
    """Path heuristic used when the caller omits urpm_root."""

    def test_host_layout_returns_none(self):
        assert (
            PackageDatabase._infer_urpm_root(
                Path("/var/lib/urpm/packages.db"))
            is None
        )

    def test_chroot_layout_returns_root(self):
        assert (
            PackageDatabase._infer_urpm_root(
                Path("/some/chroot/var/lib/urpm/packages.db"))
            == "/some/chroot"
        )

    def test_non_standard_layout_returns_none(self):
        # Ad-hoc test DB, not under a chroot ; the caller must set
        # urpm_root explicitly if it matters.
        assert (
            PackageDatabase._infer_urpm_root(
                Path("/tmp/whatever.db"))
            is None
        )


class TestSeedStubOsRelease:
    """The chroot bootstrap stub is what makes ``version-mode='system'``
    give the right answer inside a fresh chroot before
    ``mageia-release-common`` is installed there."""

    def test_writes_expected_fields(self, tmp_path):
        _seed_stub_os_release(str(tmp_path), "cauldron")
        content = (tmp_path / "etc" / "os-release").read_text()
        assert 'ID=mageia' in content
        assert 'VERSION_ID="cauldron"' in content

    def test_numeric_identity_survives_round_trip(self, tmp_path):
        from urpm.core.config import get_system_version
        _seed_stub_os_release(str(tmp_path), "11")
        assert get_system_version(root=str(tmp_path)) == "11"

    def test_missing_etc_is_created(self, tmp_path):
        # ``tmp_path`` starts without /etc — the seeder must build it.
        assert not (tmp_path / "etc").exists()
        _seed_stub_os_release(str(tmp_path), "10")
        assert (tmp_path / "etc" / "os-release").exists()


class TestVersionModePreflight:
    """`config version-mode` runs the media-availability check that
    was previously the job of ``urpm distro-switch``."""

    def _add_media(self, db, name, mageia_version, enabled=True):
        return db.add_media(
            name=name,
            short_name=name.lower().replace(' ', '_'),
            mageia_version=mageia_version,
            architecture='x86_64',
            relative_path=f'{mageia_version}/x86_64/media/core/release',
            enabled=enabled,
        )

    def _run_config(self, db, mode):
        # Emulate the cmd_config version-mode setter path with a fresh
        # DB so we hit exactly the preflight logic.  We rely on the
        # actual ``get_db_path`` inside cmd_config so we point it at
        # our test DB via env override.
        import os
        os.environ['URPM_DB_PATH_OVERRIDE'] = str(db.db_path)
        from urpm.cli.commands import config as config_module
        args = argparse.Namespace(config_cmd='version-mode', mode=mode)
        try:
            return config_module.cmd_config(args)
        finally:
            os.environ.pop('URPM_DB_PATH_OVERRIDE', None)

    def test_cauldron_without_media_refuses(self, fresh_db, monkeypatch):
        # No media at all — preflight refuses the switch.
        monkeypatch.setattr(
            'urpm.core.config.get_db_path',
            lambda urpm_root=None: fresh_db.db_path)
        args = argparse.Namespace(config_cmd='version-mode', mode='cauldron')
        from urpm.cli.commands.config import cmd_config
        rc = cmd_config(args)
        assert rc == 2
        assert fresh_db.get_config('version-mode') is None

    def test_cauldron_with_matching_media_succeeds(self, fresh_db, monkeypatch):
        self._add_media(fresh_db, 'Core Release', 'cauldron')
        monkeypatch.setattr(
            'urpm.core.config.get_db_path',
            lambda urpm_root=None: fresh_db.db_path)
        args = argparse.Namespace(config_cmd='version-mode', mode='cauldron')
        from urpm.cli.commands.config import cmd_config
        rc = cmd_config(args)
        assert rc == 0
        assert fresh_db.get_config('version-mode') == 'cauldron'

    def test_auto_bypasses_preflight(self, fresh_db, monkeypatch):
        # ``auto`` clears any pin without checking media coverage —
        # the resolver's built-in heuristic takes over.
        fresh_db.set_config('version-mode', 'cauldron')
        monkeypatch.setattr(
            'urpm.core.config.get_db_path',
            lambda urpm_root=None: fresh_db.db_path)
        args = argparse.Namespace(config_cmd='version-mode', mode='auto')
        from urpm.cli.commands.config import cmd_config
        rc = cmd_config(args)
        assert rc == 0
        assert fresh_db.get_config('version-mode') is None
