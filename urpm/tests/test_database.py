"""Tests for SQLite database"""

import pytest
import tempfile
from pathlib import Path
from urpm.core.database import PackageDatabase


@pytest.fixture
def db(monkeypatch):
    """Create a temporary database for testing.

    Patches get_system_version() to return "9" so that test packages
    with mageia_version="9" pass the version filter.
    """
    # Patch get_system_version to return "9" for consistent testing
    # This ensures version filtering matches our test media (mageia_version="9")
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '9')

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    database = PackageDatabase(db_path)
    yield database

    database.close()
    db_path.unlink(missing_ok=True)


class TestMedia:
    """Tests for media management."""

    def test_add_media(self, db):
        """Test adding media with the current API signature."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release",
            enabled=True
        )
        assert media_id > 0

        # Verify media was added
        media = db.get_media("Core Release")
        assert media is not None
        assert media['name'] == "Core Release"
        assert media['short_name'] == "core_release"
        assert media['mageia_version'] == "9"

    def test_add_media_legacy(self, db):
        """Test legacy add_media API for backwards compatibility."""
        media_id = db.add_media_legacy(
            name="Legacy Media",
            url="http://example.com/media",
            enabled=True,
            update=False
        )
        assert media_id > 0

        media = db.get_media("Legacy Media")
        assert media is not None
        assert media['url'] == "http://example.com/media"

    def test_list_media(self, db):
        """Test listing multiple media sources."""
        db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )
        db.add_media(
            name="Core Updates",
            short_name="core_updates",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/updates",
            update_media=True
        )

        media_list = db.list_media()
        assert len(media_list) == 2
        assert media_list[0]['name'] == "Core Release"
        assert media_list[1]['update_media'] == 1

    def test_remove_media(self, db):
        """Test removing a media source."""
        db.add_media(
            name="Test",
            short_name="test",
            mageia_version="9",
            architecture="x86_64",
            relative_path="test"
        )
        assert len(db.list_media()) == 1

        db.remove_media("Test")
        assert len(db.list_media()) == 0

    def test_enable_disable_media(self, db):
        """Test enabling and disabling media."""
        db.add_media(
            name="Test",
            short_name="test",
            mageia_version="9",
            architecture="x86_64",
            relative_path="test"
        )

        db.enable_media("Test", enabled=False)
        media = db.get_media("Test")
        assert media['enabled'] == 0

        db.enable_media("Test", enabled=True)
        media = db.get_media("Test")
        assert media['enabled'] == 1


class TestPackages:
    """Tests for package operations."""

    def _create_media_and_packages(self, db):
        """Helper: create a media and import test packages."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )

        packages = [
            {
                'name': 'firefox',
                'version': '120.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'firefox-120.0-1.mga9.x86_64',
                'summary': 'Web browser',
                'provides': ['firefox', 'webrunner'],
                'requires': ['libgtk3', 'libnspr4'],
                'filesize': 200000000,
            },
            {
                'name': 'thunderbird',
                'version': '115.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'thunderbird-115.0-1.mga9.x86_64',
                'summary': 'Email client',
                'provides': ['thunderbird'],
                'requires': ['libgtk3'],
                'filesize': 220000000,
            },
        ]

        count = db.import_packages(iter(packages), media_id=media_id)
        return media_id, count

    def test_import_packages(self, db):
        """Test importing packages into a media."""
        media_id, count = self._create_media_and_packages(db)
        assert count == 2

    def test_search_packages(self, db):
        """Test searching for packages by name pattern."""
        self._create_media_and_packages(db)

        # Search should find firefox
        results = db.search('fire')
        assert len(results) == 1
        assert results[0]['name'] == 'firefox'

    def test_search_no_results(self, db):
        """Test search returns empty for non-matching pattern."""
        self._create_media_and_packages(db)

        results = db.search('nonexistent')
        assert len(results) == 0

    def test_get_package(self, db):
        """Test getting a package by exact name."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )

        packages = [
            {
                'name': 'vim',
                'version': '9.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'vim-9.0-1.mga9.x86_64',
                'summary': 'Text editor',
                'provides': ['vim', 'editor'],
                'requires': ['ncurses'],
                'filesize': 220000000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        pkg = db.get_package('vim')
        assert pkg is not None
        assert pkg['name'] == 'vim'
        assert pkg['version'] == '9.0'

    def test_get_package_not_found(self, db):
        """Test get_package returns None for non-existent package."""
        pkg = db.get_package('nonexistent')
        assert pkg is None

    def test_whatprovides(self, db):
        """Test finding packages that provide a capability."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )

        packages = [
            {
                'name': 'glibc',
                'version': '2.38',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'glibc-2.38-1.mga9.x86_64',
                'provides': ['libc.so.6', 'glibc'],
                'requires': [],
                'filesize': 220000000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        providers = db.whatprovides('libc.so.6')
        assert len(providers) == 1
        assert providers[0]['name'] == 'glibc'


class TestConfig:
    """Tests for configuration storage."""

    def test_set_get_config(self, db):
        """Test storing and retrieving config values."""
        db.set_config('last_update', '2024-01-15')
        assert db.get_config('last_update') == '2024-01-15'

    def test_get_missing_config(self, db):
        """Test getting missing config returns None or default."""
        assert db.get_config('nonexistent') is None
        assert db.get_config('nonexistent', 'default') == 'default'

    def test_update_config(self, db):
        """Test updating an existing config value."""
        db.set_config('key', 'value1')
        db.set_config('key', 'value2')
        assert db.get_config('key') == 'value2'


class TestStats:
    """Tests for statistics."""

    def test_empty_stats(self, db):
        """Test stats on empty database."""
        stats = db.get_stats()
        assert stats['packages'] == 0
        assert stats['media'] == 0

    def test_stats_with_data(self, db):
        """Test stats after adding media and packages."""
        media_id = db.add_media(
            name="Test",
            short_name="test",
            mageia_version="9",
            architecture="x86_64",
            relative_path="test"
        )

        packages = [
            {
                'name': 'test',
                'version': '1.0',
                'release': '1',
                'epoch': 0,
                'arch': 'noarch',
                'nevra': 'test-1.0-1.noarch',
                'provides': ['test'],
                'requires': ['dep1', 'dep2'],
                'filesize': 220000000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        stats = db.get_stats()
        assert stats['packages'] == 1
        assert stats['media'] == 1
        assert stats['provides'] == 1
        assert stats['requires'] == 2


class TestPackageVersioning:
    """Tests for package version handling."""

    def test_multiple_versions(self, db):
        """Test handling multiple versions of the same package."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )

        packages = [
            {
                'name': 'vim',
                'version': '9.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'vim-9.0-1.mga9.x86_64',
                'provides': ['vim'],
                'requires': [],
                'filesize': 100000,
            },
            {
                'name': 'vim',
                'version': '9.1',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'vim-9.1-1.mga9.x86_64',
                'provides': ['vim'],
                'requires': [],
                'filesize': 100000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        # get_package should return latest version
        pkg = db.get_package('vim')
        assert pkg is not None
        assert pkg['version'] == '9.1'

    def test_get_package_by_nevra(self, db):
        """Test getting a specific package version by NEVRA."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )

        packages = [
            {
                'name': 'vim',
                'version': '9.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'vim-9.0-1.mga9.x86_64',
                'provides': ['vim'],
                'requires': [],
                'filesize': 100000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        pkg = db.get_package_by_nevra('vim-9.0-1.mga9.x86_64')
        assert pkg is not None
        assert pkg['name'] == 'vim'
        assert pkg['version'] == '9.0'


class TestGetPackageArchFilter:
    """Regression tests for the optional ``arch`` filter on ``get_package``.

    On a multi-arch system (typically x86_64 with 32-bit media enabled),
    the package table can hold three rows for the same N-V-R: ``i686``,
    ``x86_64`` and ``noarch``. Without an arch hint, SQLite is free to
    return any of them, and a foreign-arch row carries the wrong sonames
    in ``Requires`` (``libfoo.so.2()`` without the ``(64bit)`` qualifier),
    which then fails to match the capabilities provided by 64-bit
    packages — the orphan detector mistakenly flags surviving providers.
    These tests pin the fix so a future refactor cannot silently drop
    the arch filter again.
    """

    def _import_multiarch_foo(self, db):
        """Insert three rows of ``foo`` (i686 + x86_64 + noarch) at the
        same NVR, plus a control row of an unrelated package."""
        media_id = db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/release"
        )
        packages = [
            {
                'name': 'foo', 'version': '1.0', 'release': '1.mga9',
                'epoch': 0, 'arch': 'i686',
                'nevra': 'foo-1.0-1.mga9.i686',
                'provides': ['foo'], 'requires': ['libfoo.so.2()'],
                'filesize': 1000,
            },
            {
                'name': 'foo', 'version': '1.0', 'release': '1.mga9',
                'epoch': 0, 'arch': 'x86_64',
                'nevra': 'foo-1.0-1.mga9.x86_64',
                'provides': ['foo'], 'requires': ['libfoo.so.2()(64bit)'],
                'filesize': 1000,
            },
            {
                'name': 'foo', 'version': '1.0', 'release': '1.mga9',
                'epoch': 0, 'arch': 'noarch',
                'nevra': 'foo-1.0-1.mga9.noarch',
                'provides': ['foo'], 'requires': [],
                'filesize': 1000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)
        return media_id

    def test_no_arch_hint_returns_one_row(self, db):
        """Backward compat: ``arch=None`` keeps the historical behaviour
        (no arch filter, one arbitrary row returned)."""
        self._import_multiarch_foo(db)

        pkg = db.get_package('foo')
        assert pkg is not None
        assert pkg['name'] == 'foo'
        assert pkg['arch'] in {'i686', 'x86_64', 'noarch'}

    def test_arch_hint_x86_64_returns_x86_64_row(self, db):
        """``arch='x86_64'`` selects the x86_64 row even when i686 and
        noarch rows exist for the same NVR."""
        self._import_multiarch_foo(db)

        pkg = db.get_package('foo', arch='x86_64')
        assert pkg is not None
        assert pkg['arch'] == 'x86_64'
        assert pkg['nevra'] == 'foo-1.0-1.mga9.x86_64'

    def test_arch_hint_i686_returns_i686_row(self, db):
        """``arch='i686'`` selects the i686 row.

        Confirms the filter is symmetric and not hard-coded to the host
        arch — useful for inspection paths that may want a foreign-arch
        row deliberately.
        """
        self._import_multiarch_foo(db)

        pkg = db.get_package('foo', arch='i686')
        assert pkg is not None
        assert pkg['arch'] == 'i686'

    def test_arch_hint_falls_back_to_noarch(self, db):
        """``arch='aarch64'`` matches no native row but falls back to the
        ``noarch`` row, which is universally compatible.

        The filter is expressed as ``arch IN (?, 'noarch')`` precisely so
        a noarch package is reachable from any requested arch hint.
        """
        self._import_multiarch_foo(db)

        pkg = db.get_package('foo', arch='aarch64')
        assert pkg is not None
        assert pkg['arch'] == 'noarch'

    def test_arch_hint_unknown_package_returns_none(self, db):
        """``arch`` on a missing name still returns ``None`` (no crash,
        no spurious match)."""
        self._import_multiarch_foo(db)

        pkg = db.get_package('bar', arch='x86_64')
        assert pkg is None
