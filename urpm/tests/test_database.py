"""Tests for SQLite database"""

import pytest
import tempfile
from pathlib import Path
from urpm.core.database import PackageDatabase


@pytest.fixture
def db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    database = PackageDatabase(db_path)
    yield database

    database.close()
    db_path.unlink(missing_ok=True)


class TestMedia:
    """Tests for media management."""

    def test_add_media(self, db):
        media_id = db.add_media(
            name="Core Release",
            url="http://mirrors.mageia.org/distrib/9/x86_64/media/core/release",
            enabled=True
        )
        assert media_id > 0

    def test_list_media(self, db):
        db.add_media(name="Core Release", url="http://example.com/core")
        db.add_media(name="Core Updates", url="http://example.com/updates", update=True)

        media_list = db.list_media()
        assert len(media_list) == 2
        assert media_list[0]['name'] == "Core Release"
        assert media_list[1]['update_media'] == 1

    def test_remove_media(self, db):
        db.add_media(name="Test", url="http://test.com")
        assert len(db.list_media()) == 1

        db.remove_media("Test")
        assert len(db.list_media()) == 0

    def test_enable_disable_media(self, db):
        db.add_media(name="Test", url="http://test.com")

        db.enable_media("Test", enabled=False)
        media = db.get_media("Test")
        assert media['enabled'] == 0

        db.enable_media("Test", enabled=True)
        media = db.get_media("Test")
        assert media['enabled'] == 1


class TestPackages:
    """Tests for package operations."""

    def test_import_and_search(self, db):
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

        count = db.import_packages(iter(packages))
        assert count == 2

        results = db.search('fire')
        assert len(results) == 1
        assert results[0]['name'] == 'firefox'

    def test_get_package(self, db):
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
        db.import_packages(iter(packages))

        pkg = db.get_package('vim')
        assert pkg is not None
        assert pkg['name'] == 'vim'
        assert pkg['version'] == '9.0'
        assert 'editor' in pkg['provides']
        assert 'ncurses' in pkg['requires']

    def test_whatprovides(self, db):
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
        db.import_packages(iter(packages))

        providers = db.whatprovides('libc.so.6')
        assert len(providers) == 1
        assert providers[0]['name'] == 'glibc'


class TestConfig:
    """Tests for configuration storage."""

    def test_set_get_config(self, db):
        db.set_config('last_update', '2024-01-15')
        assert db.get_config('last_update') == '2024-01-15'

    def test_get_missing_config(self, db):
        assert db.get_config('nonexistent') is None
        assert db.get_config('nonexistent', 'default') == 'default'


class TestStats:
    """Tests for statistics."""

    def test_empty_stats(self, db):
        stats = db.get_stats()
        assert stats['packages'] == 0
        assert stats['media'] == 0

    def test_stats_with_data(self, db):
        db.add_media(name="Test", url="http://test.com")
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
        db.import_packages(iter(packages), media_id=1)

        stats = db.get_stats()
        assert stats['packages'] == 1
        assert stats['media'] == 1
        assert stats['provides'] == 1
        assert stats['requires'] == 2
