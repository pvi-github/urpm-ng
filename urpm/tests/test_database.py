"""Tests for SQLite database"""

import pytest
import sqlite3
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


# ---------------------------------------------------------------------------
# RPM-semantic version collation
# ---------------------------------------------------------------------------

class TestRpmVersionCollation:
    """Regression tests for the ``rpm_version_compare`` SQLite collation.

    The default SQLite TEXT collation orders ``"7.6.7.2"`` AFTER
    ``"24.2.7.2"`` because ``'7' > '2'`` in ASCII. RPM semantics say the
    opposite (``24.2.7.2`` > ``7.6.7.2``). Without the custom collation
    installed by :class:`PackageDatabase`, ``get_package(..., LIMIT 1)``
    silently returns the older NEVRA, which then breaks the orphan
    detector and torpedoes the libsolv transaction (rpmlib refuses the
    upgrade because the older version lacks the modern ``Requires:`` it
    needs to keep ``lib64zxcvbn0`` in the post-state).

    These tests exercise the collation both directly (raw SQL) and
    through the public API (``get_package``).
    """

    # ------------------------------------------------------------------
    # Direct collation tests on a real SQLite connection
    # ------------------------------------------------------------------

    def _seed_two_versions(self, conn):
        """Create a minimal ``packages``-like table and insert the
        ``libreoffice-core`` row pair that triggers the bug.
        """
        conn.execute("""
            CREATE TABLE packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                epoch INTEGER DEFAULT 0,
                version TEXT NOT NULL,
                release TEXT NOT NULL
            )
        """)
        conn.executemany(
            "INSERT INTO packages (name, epoch, version, release) VALUES (?, ?, ?, ?)",
            [
                ('libreoffice-core', 0, '7.6.7.2',  '1.mga9'),
                ('libreoffice-core', 0, '24.2.7.2', '1.4.mga9'),
            ],
        )
        conn.commit()

    def test_collation_picks_semantically_latest(self, tmp_path):
        """Direct SQL: ``ORDER BY version COLLATE rpm_version_compare
        DESC LIMIT 1`` returns the row with the semantically-latest
        version (``24.2.7.2``), not the lex-latest (``7.6.7.2``).
        """
        from urpm.core.database import _register_rpm_collation

        db_path = tmp_path / 'collation.db'
        conn = sqlite3.connect(str(db_path))
        try:
            _register_rpm_collation(conn)
            self._seed_two_versions(conn)

            cur = conn.execute("""
                SELECT version FROM packages
                WHERE name = 'libreoffice-core'
                ORDER BY version COLLATE rpm_version_compare DESC
                LIMIT 1
            """)
            assert cur.fetchone()[0] == '24.2.7.2'

            # Sanity check: without the collation, lex sort returns
            # ``7.6.7.2`` first (the very bug we are fixing).
            cur = conn.execute("""
                SELECT version FROM packages
                WHERE name = 'libreoffice-core'
                ORDER BY version DESC
                LIMIT 1
            """)
            assert cur.fetchone()[0] == '7.6.7.2'
        finally:
            conn.close()

    def test_collation_handles_multi_digit_segments(self, tmp_path):
        """RPM numeric segment compare: ``1.10`` > ``1.2``.

        Lex sort would put ``1.2`` first (because ``'2' > '1'`` at the
        second component); RPM sort puts ``1.10`` first.
        """
        from urpm.core.database import _register_rpm_collation

        conn = sqlite3.connect(':memory:')
        try:
            _register_rpm_collation(conn)
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.executemany("INSERT INTO t VALUES (?)", [('1.10',), ('1.2',)])

            cur = conn.execute(
                "SELECT v FROM t ORDER BY v COLLATE rpm_version_compare DESC"
            )
            assert [row[0] for row in cur] == ['1.10', '1.2']
        finally:
            conn.close()

    def test_collation_handles_empty_and_none(self, tmp_path):
        """Collation must not raise on empty/NULL strings: empty < non-empty.

        ``rpm.labelCompare`` raises ``ValueError`` on an empty version,
        so the collation guards explicitly. The contract we want: an
        empty/missing component sorts strictly below a populated one
        (matches RPM semantics where missing release is "less than"
        present).
        """
        from urpm.core.database import _rpm_version_collation

        assert _rpm_version_collation('', '') == 0
        assert _rpm_version_collation('', '1.0') < 0
        assert _rpm_version_collation('1.0', '') > 0
        # ``None`` tolerance (SQLite may pass NULL through as Python None
        # on some platforms; we coerce to '').
        assert _rpm_version_collation(None, None) == 0
        assert _rpm_version_collation(None, '1.0') < 0

    # ------------------------------------------------------------------
    # End-to-end tests via PackageDatabase.get_package()
    # ------------------------------------------------------------------

    def _import_libreoffice_mga9_pair(self, db):
        """Insert the two-row libreoffice-core scenario via the real
        import code path (not raw DML), so we exercise exactly what
        ``urpm update_media`` builds in production.
        """
        media_id = db.add_media(
            name="Updates",
            short_name="updates",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/updates",
        )

        packages = [
            {
                'name': 'libreoffice-core',
                'version': '7.6.7.2',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'libreoffice-core-7.6.7.2-1.mga9.x86_64',
                'summary': 'LibreOffice core (old branch)',
                'provides': ['libreoffice-core'],
                'requires': [],
                'filesize': 30000000,
            },
            {
                'name': 'libreoffice-core',
                'version': '24.2.7.2',
                'release': '1.4.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'libreoffice-core-24.2.7.2-1.4.mga9.x86_64',
                'summary': 'LibreOffice core (new branch)',
                'provides': ['libreoffice-core', 'libzxcvbn.so.0()(64bit)'],
                'requires': ['libzxcvbn.so.0()(64bit)'],
                'filesize': 30000000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)
        return media_id

    def test_get_package_returns_semantically_latest_not_lex_latest_libreoffice_core_mga9(self, db):
        """Production regression: ``get_package('libreoffice-core',
        arch='x86_64')`` must return the ``24.2.7.2`` row, not the
        ``7.6.7.2`` row.

        Scenario reproduced from the live mga9 install where the
        ``Release`` media held ``7.6.7.2`` and ``Updates`` held
        ``24.2.7.2``. Lex sort returned the old row, the orphan
        detector read its (smaller) Requires set, built a bogus
        post_reverse, flagged ``lib64zxcvbn0`` as orphan, and
        ``ts.check()`` aborted the whole upgrade. We assert the new
        EVR wins.
        """
        self._import_libreoffice_mga9_pair(db)

        pkg = db.get_package('libreoffice-core', arch='x86_64')
        assert pkg is not None
        assert pkg['version'] == '24.2.7.2', (
            f"get_package returned the lex-latest row instead of the "
            f"semantically-latest one: got version={pkg['version']!r}, "
            f"release={pkg['release']!r}. Lexicographic sort over RPM "
            f"versions is the bug."
        )
        assert pkg['release'] == '1.4.mga9'
        assert pkg['nevra'] == 'libreoffice-core-24.2.7.2-1.4.mga9.x86_64'

    def test_get_package_without_arch_hint_also_uses_semantic_sort(self, db):
        """The collation must apply on BOTH branches of ``get_package``
        (with and without ``arch``). Caller without an arch hint must
        also see the semantically-latest row.
        """
        self._import_libreoffice_mga9_pair(db)

        pkg = db.get_package('libreoffice-core')
        assert pkg is not None
        assert pkg['version'] == '24.2.7.2'

    def test_get_package_picks_higher_epoch(self, db):
        """Sanity: epoch still dominates version. ``1:1.0`` > ``0:99.0``.

        Epoch is stored as INTEGER so SQLite will sort it numerically
        and skip the collation; this test simply guards against any
        regression that might convert epoch to TEXT in the future.
        """
        media_id = db.add_media(
            name="Test Epoch",
            short_name="test_epoch",
            mageia_version="9",
            architecture="x86_64",
            relative_path="test/epoch",
        )

        packages = [
            {
                'name': 'epochtest',
                'version': '99.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'epochtest-99.0-1.mga9.x86_64',
                'summary': 'High version, low epoch',
                'provides': ['epochtest'],
                'requires': [],
                'filesize': 1000,
            },
            {
                'name': 'epochtest',
                'version': '1.0',
                'release': '1.mga9',
                'epoch': 1,
                'arch': 'x86_64',
                'nevra': 'epochtest-1:1.0-1.mga9.x86_64',
                'summary': 'Low version, high epoch',
                'provides': ['epochtest'],
                'requires': [],
                'filesize': 1000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        pkg = db.get_package('epochtest', arch='x86_64')
        assert pkg is not None
        assert pkg['epoch'] == 1
        assert pkg['version'] == '1.0'


# ---------------------------------------------------------------------------
# Component-based exact NEVRA lookup
# ---------------------------------------------------------------------------

class TestGetPackageExact:
    """Tests for :meth:`PackageDatabase.get_package_exact`.

    The orphan detector uses this method to recover the **exact** row
    libsolv chose to install (e.g. a Held older version, or a pinned
    EVR), instead of the semantically-latest row that
    :meth:`get_package` would return. Because the ``packages.nevra``
    column is stored without the ``epoch:`` prefix while
    :attr:`PackageAction.nevra` carries it for ``epoch > 0``, the
    lookup must go through separate columns — that is the contract
    these tests pin.
    """

    def _import_libreoffice_mga9_pair(self, db):
        """Insert two libreoffice-core versions at the same arch."""
        media_id = db.add_media(
            name="Updates",
            short_name="updates",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/updates",
        )
        packages = [
            {
                'name': 'libreoffice-core',
                'version': '7.6.7.2',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'libreoffice-core-7.6.7.2-1.mga9.x86_64',
                'provides': ['libreoffice-core'],
                'requires': [],
                'filesize': 30000000,
            },
            {
                'name': 'libreoffice-core',
                'version': '24.2.7.2',
                'release': '1.4.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'libreoffice-core-24.2.7.2-1.4.mga9.x86_64',
                'provides': ['libreoffice-core', 'libzxcvbn.so.0()(64bit)'],
                'requires': ['libzxcvbn.so.0()(64bit)'],
                'filesize': 30000000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)
        return media_id

    def test_get_package_exact_matches_all_components(self, db):
        """Two versions of the same N+arch — exact lookup picks the one
        whose ``(version, release)`` matches, ignoring which is newer.

        Pins the contract that ``get_package_exact`` is **not**
        version-sorting: it is a deterministic key→row lookup, the
        opposite of ``get_package``. The orphan detector relies on
        this when libsolv chooses a non-latest version.
        """
        self._import_libreoffice_mga9_pair(db)

        # New row — exact lookup returns the new branch.
        pkg = db.get_package_exact(
            'libreoffice-core', '24.2.7.2', '1.4.mga9', 'x86_64',
        )
        assert pkg is not None
        assert pkg['version'] == '24.2.7.2'
        assert pkg['release'] == '1.4.mga9'
        assert pkg['nevra'] == 'libreoffice-core-24.2.7.2-1.4.mga9.x86_64'

        # Old row — exact lookup returns the legacy branch, even though
        # it is semantically older than the other row.
        pkg = db.get_package_exact(
            'libreoffice-core', '7.6.7.2', '1.mga9', 'x86_64',
        )
        assert pkg is not None
        assert pkg['version'] == '7.6.7.2'
        assert pkg['release'] == '1.mga9'

    def test_get_package_exact_returns_None_on_no_match(self, db):
        """No row with the requested ``(N,V,R,A)`` ⇒ ``None``."""
        self._import_libreoffice_mga9_pair(db)

        # Version exists but release does not.
        assert db.get_package_exact(
            'libreoffice-core', '24.2.7.2', '999.mga9', 'x86_64',
        ) is None

        # Package name does not exist at all.
        assert db.get_package_exact(
            'no-such-pkg', '1.0', '1.mga9', 'x86_64',
        ) is None

        # Right NVR but wrong arch.
        assert db.get_package_exact(
            'libreoffice-core', '24.2.7.2', '1.4.mga9', 'i686',
        ) is None

    def test_get_package_exact_with_epoch_matches_exactly(self, db):
        """Epoch filter: when provided, must match exactly; when None,
        any epoch passes.

        This is the contract that fixes the rolled-back B-attempt: a
        textual NEVRA comparison breaks for ``epoch > 0`` packages
        because the DB stores the NEVRA without the ``E:`` prefix.
        Component-based lookup with an explicit epoch sidesteps the
        string-format question entirely.
        """
        media_id = db.add_media(
            name="Updates",
            short_name="updates",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/updates",
        )
        packages = [
            {
                'name': 'epochpkg',
                'version': '1.0',
                'release': '1.mga9',
                'epoch': 1,
                'arch': 'x86_64',
                # Mageia synthesis stores NEVRA without epoch prefix —
                # the very inconsistency we are guarding against.
                'nevra': 'epochpkg-1.0-1.mga9.x86_64',
                'provides': ['epochpkg'],
                'requires': [],
                'filesize': 1000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        # Exact epoch match.
        pkg = db.get_package_exact(
            'epochpkg', '1.0', '1.mga9', 'x86_64', epoch=1,
        )
        assert pkg is not None
        assert pkg['epoch'] == 1

        # Wrong epoch ⇒ no match (the row has epoch=1, not 0).
        assert db.get_package_exact(
            'epochpkg', '1.0', '1.mga9', 'x86_64', epoch=0,
        ) is None

        # Unspecified epoch ⇒ matches regardless of the stored value.
        pkg = db.get_package_exact(
            'epochpkg', '1.0', '1.mga9', 'x86_64', epoch=None,
        )
        assert pkg is not None
        assert pkg['epoch'] == 1

    def test_get_package_exact_arch_constraint(self, db):
        """``arch`` is matched exactly (no ``noarch`` fallback).

        Unlike :meth:`get_package` (which accepts ``arch IN (?,
        'noarch')`` as a compatibility filter for multi-arch systems),
        :meth:`get_package_exact` is a strict key lookup: the action
        already knows which arch libsolv selected, and a ``noarch`` row
        with the same NVR is a **different** package, not a fallback.
        """
        media_id = db.add_media(
            name="Updates",
            short_name="updates",
            mageia_version="9",
            architecture="x86_64",
            relative_path="core/updates",
        )
        packages = [
            {
                'name': 'multiarch',
                'version': '1.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'x86_64',
                'nevra': 'multiarch-1.0-1.mga9.x86_64',
                'provides': ['multiarch'],
                'requires': [],
                'filesize': 1000,
            },
            {
                'name': 'multiarch',
                'version': '1.0',
                'release': '1.mga9',
                'epoch': 0,
                'arch': 'noarch',
                'nevra': 'multiarch-1.0-1.mga9.noarch',
                'provides': ['multiarch'],
                'requires': [],
                'filesize': 1000,
            },
        ]
        db.import_packages(iter(packages), media_id=media_id)

        pkg = db.get_package_exact('multiarch', '1.0', '1.mga9', 'x86_64')
        assert pkg is not None
        assert pkg['arch'] == 'x86_64'

        pkg = db.get_package_exact('multiarch', '1.0', '1.mga9', 'noarch')
        assert pkg is not None
        assert pkg['arch'] == 'noarch'

        # i686 was not imported, so the strict lookup misses — even
        # though a noarch row exists.
        assert db.get_package_exact(
            'multiarch', '1.0', '1.mga9', 'i686',
        ) is None


class TestUnregisterCacheFile:
    """Tests for the path-based cache record removal helper used by
    the resilient install pipeline when a corrupt RPM is purged.
    """

    def _seed_media(self, db, name="Core Release"):
        return db.add_media(
            name=name, short_name=name.lower().replace(' ', '_'),
            mageia_version="9", architecture="x86_64",
            relative_path="core/release", enabled=True,
        )

    def test_removes_existing_row(self, db):
        media_id = self._seed_media(db)
        db.register_cache_file(
            filename="foo-1.0-1.mga9.x86_64.rpm",
            media_id=media_id,
            file_path="/var/cache/urpm/medias/Core Release/foo-1.0-1.mga9.x86_64.rpm",
            file_size=1024,
        )
        ok = db.unregister_cache_file(
            "/var/cache/urpm/medias/Core Release/foo-1.0-1.mga9.x86_64.rpm",
        )
        assert ok is True
        # The row is gone — lookup by filename returns None.
        assert db.get_cache_file(
            "foo-1.0-1.mga9.x86_64.rpm", media_id=media_id,
        ) is None

    def test_returns_false_when_missing(self, db):
        """Cache table is advisory: a missing path is not an error."""
        ok = db.unregister_cache_file("/nowhere/never-registered.rpm")
        assert ok is False

    def test_does_not_touch_other_rows(self, db):
        media_id = self._seed_media(db)
        db.register_cache_file(
            filename="keep.rpm", media_id=media_id,
            file_path="/cache/keep.rpm", file_size=10,
        )
        db.register_cache_file(
            filename="drop.rpm", media_id=media_id,
            file_path="/cache/drop.rpm", file_size=20,
        )
        db.unregister_cache_file("/cache/drop.rpm")
        # ``keep.rpm`` survives.
        assert db.get_cache_file("keep.rpm", media_id=media_id) is not None
        assert db.get_cache_file("drop.rpm", media_id=media_id) is None


class TestSchemaV30Migration:
    """Tests for the v29 → v30 schema bump (bug #3 iteration B).

    A fresh database boots straight to v30; an existing v29 database
    is upgraded by the ``MIGRATIONS`` dict.  Both paths must end up
    with the same final schema.
    """

    def _expected_v30_shape(self, db):
        conn = db._get_connection()

        # server gained two security columns
        server_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(server)").fetchall()}
        assert {"blacklisted_at", "blacklist_reason"} <= server_cols

        # cache_files gained the provenance column
        cache_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(cache_files)").fetchall()}
        assert "served_by_server_id" in cache_cols

        # server_failure_events table exists with the documented shape
        sfe_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(server_failure_events)").fetchall()]
        assert sfe_cols == [
            "id", "server_id", "ts", "category", "weight", "detail",
        ]

        # The query path of mirror selection relies on this index
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_sfe_server_ts'"
        ).fetchone()
        assert idx is not None

    def test_fresh_db_bootstraps_to_v30(self, db):
        from urpm.core.database import SCHEMA_VERSION
        assert SCHEMA_VERSION == 30
        # Bootstrap path through CREATE TABLE IF NOT EXISTS:
        self._expected_v30_shape(db)

    def test_v29_db_is_upgraded_to_v30(self, monkeypatch):
        """A pre-existing v29 database must accept the migration and
        end up structurally identical to a fresh v30."""
        import tempfile
        import sqlite3
        from pathlib import Path
        from urpm.core.database import PackageDatabase

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        # Build a minimal v29 image by hand: just the tables that the
        # migration touches, with the v29-shape schema (no blacklist
        # columns, no event table, no served_by_server_id).
        raw = sqlite3.connect(str(db_path))
        raw.executescript("""
            CREATE TABLE server (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                protocol TEXT,
                host TEXT NOT NULL,
                base_path TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE media (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE cache_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                media_id INTEGER,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                added_time INTEGER NOT NULL,
                UNIQUE(filename, media_id)
            );
            CREATE TABLE schema_info (
                version INTEGER PRIMARY KEY
            );
            INSERT INTO schema_info (version) VALUES (29);
        """)
        raw.commit()
        raw.close()

        monkeypatch.setattr(
            'urpm.core.config.get_system_version', lambda: '10',
        )
        db = PackageDatabase(db_path)
        try:
            self._expected_v30_shape(db)
        finally:
            db.close()
            db_path.unlink(missing_ok=True)


class TestSecurityBlacklist:
    """Tests for the iteration-B security blacklist (bug #3)."""

    def _seed_server(self, db, name="alpha"):
        return db.add_server(
            name=name, protocol="https", host=f"{name}.example.org",
            base_path="/", is_official=True,
        )

    def test_fresh_server_is_not_blacklisted(self, db):
        sid = self._seed_server(db)
        assert db.is_blacklisted(sid) is False
        assert db.list_blacklisted_servers() == []

    def test_blacklist_records_reason_and_timestamp(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="tampered RPM in core/release")

        assert db.is_blacklisted(sid) is True
        listed = db.list_blacklisted_servers()
        assert len(listed) == 1
        assert listed[0]["name"] == "alpha"
        assert listed[0]["blacklist_reason"] == "tampered RPM in core/release"
        assert listed[0]["blacklisted_at"] is not None

    def test_unblacklist_clears_the_state(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="bad sig")

        cleared = db.unblacklist_server(sid)
        assert cleared is True
        assert db.is_blacklisted(sid) is False
        assert db.list_blacklisted_servers() == []

    def test_unblacklist_idempotent_on_clean_server(self, db):
        """No-op on a server that was not blacklisted to begin with."""
        sid = self._seed_server(db)
        cleared = db.unblacklist_server(sid)
        assert cleared is False

    def test_blacklisted_server_excluded_from_mirror_pool(self, db):
        a = self._seed_server(db, "alpha")
        b = self._seed_server(db, "beta")
        mid = db.add_media(
            name="Core Release", short_name="core_release",
            mageia_version="10", architecture="x86_64",
            relative_path="10/x86_64/media/core/release",
            enabled=True,
        )
        db.link_server_media(a, mid)
        db.link_server_media(b, mid)

        # Both visible initially
        names_before = {s["name"] for s in db.get_servers_for_media(mid)}
        assert names_before == {"alpha", "beta"}

        db.blacklist_server(a, reason="signature failure")
        names_after = {s["name"] for s in db.get_servers_for_media(mid)}
        assert names_after == {"beta"}

        # include_blacklisted=True bypasses the filter (admin path)
        names_admin = {
            s["name"]
            for s in db.get_servers_for_media(mid, include_blacklisted=True)
        }
        assert names_admin == {"alpha", "beta"}


class TestReputationScoring:
    """Tests for the sliding-window reputation event log (bug #3 iter B)."""

    def _seed_server(self, db, name="alpha"):
        return db.add_server(
            name=name, protocol="https", host=f"{name}.example.org",
            base_path="/", is_official=True,
        )

    def test_fresh_server_scores_100(self, db):
        sid = self._seed_server(db)
        assert db.get_server_reputation_score(sid) == 100

    def test_corrupt_event_costs_10(self, db):
        sid = self._seed_server(db)
        db.record_server_failure(
            sid, category="corrupt",
            detail="foo-1.0-1.mga10.x86_64.rpm",
        )
        assert db.get_server_reputation_score(sid) == 90

    def test_multiple_events_accumulate(self, db):
        sid = self._seed_server(db)
        db.record_server_failure(sid, "corrupt")     # -10
        db.record_server_failure(sid, "http_4xx")    # -3
        db.record_server_failure(sid, "network")     # -5
        assert db.get_server_reputation_score(sid) == 100 - 10 - 3 - 5

    def test_score_clamps_to_zero(self, db):
        sid = self._seed_server(db)
        # 11 corrupt events × 10 = 110 penalty; floor at 0, not negative
        for _ in range(11):
            db.record_server_failure(sid, "corrupt")
        assert db.get_server_reputation_score(sid) == 0

    def test_unknown_category_does_not_move_score(self, db):
        sid = self._seed_server(db)
        db.record_server_failure(sid, "unknown_category")
        assert db.get_server_reputation_score(sid) == 100
        # ...but the row is still in the log for forensics
        recent = db.get_server_recent_failures(sid)
        assert len(recent) == 1
        assert recent[0]["category"] == "unknown_category"

    def test_explicit_weight_overrides_default(self, db):
        sid = self._seed_server(db)
        db.record_server_failure(sid, "corrupt", weight=20)
        assert db.get_server_reputation_score(sid) == 80

    def test_events_outside_window_do_not_count(self, db, monkeypatch):
        sid = self._seed_server(db)
        import time

        # Force an old timestamp directly into the table.
        conn = db._get_connection()
        old_ts = int(time.time()) - 48 * 3600  # 2 days ago
        conn.execute(
            "INSERT INTO server_failure_events "
            "(server_id, ts, category, weight) VALUES (?, ?, ?, ?)",
            (sid, old_ts, "corrupt", 10),
        )
        conn.commit()

        # 24-hour window: the event is outside, score stays at 100.
        assert db.get_server_reputation_score(sid, window_hours=24) == 100
        # 72-hour window: the event is inside, score drops.
        assert db.get_server_reputation_score(sid, window_hours=72) == 90

    def test_reputation_breaks_mirror_pool_tie(self, db):
        a = self._seed_server(db, "alpha")
        b = self._seed_server(db, "beta")
        mid = db.add_media(
            name="Core Release", short_name="core_release",
            mageia_version="10", architecture="x86_64",
            relative_path="10/x86_64/media/core/release",
            enabled=True,
        )
        db.link_server_media(a, mid)
        db.link_server_media(b, mid)

        # alpha picks up two corrupt events; beta stays clean.
        db.record_server_failure(a, "corrupt")
        db.record_server_failure(a, "corrupt")

        servers = db.get_servers_for_media(mid)
        # beta first (score 100), alpha second (score 80)
        assert servers[0]["name"] == "beta"
        assert servers[0]["reputation_score"] == 100
        assert servers[1]["name"] == "alpha"
        assert servers[1]["reputation_score"] == 80


class TestBlacklistAcknowledge:
    """Tests for the user-side acknowledgement of a security
    blacklist (bug #3 iteration B).

    The ack stops the persistent banner from re-displaying the alert
    at every CLI invocation without reactivating the server.
    """

    def _seed_server(self, db, name="alpha"):
        return db.add_server(
            name=name, protocol="https", host=f"{name}.example.org",
            base_path="/", is_official=True,
        )

    def test_blacklist_logs_event_to_failure_log(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(
            sid, reason="served bad sig",
            detail="foo-1.0-1.mga10.x86_64.rpm — BAD signature: NOKEY",
        )
        failures = db.get_server_recent_failures(sid)
        # The blacklist event lands as a 'signature' row with weight 0
        # so the reputation score is unaffected (compromise drives the
        # binary blacklist, not the gradient score).
        assert len(failures) == 1
        assert failures[0]["category"] == "signature"
        assert failures[0]["weight"] == 0
        assert "BAD signature" in failures[0]["detail"]

    def test_blacklist_does_not_move_reputation_score(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="r")
        assert db.get_server_reputation_score(sid) == 100

    def test_acknowledge_sets_timestamp(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="r")
        assert db.acknowledge_blacklist(sid) is True
        row = db.get_server_by_id(sid)
        assert row["blacklist_acknowledged_at"] is not None

    def test_acknowledge_on_clean_server_is_noop(self, db):
        sid = self._seed_server(db)
        assert db.acknowledge_blacklist(sid) is False

    def test_double_acknowledge_is_noop(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="r")
        assert db.acknowledge_blacklist(sid) is True
        assert db.acknowledge_blacklist(sid) is False  # second call

    def test_unacknowledged_only_filter(self, db):
        a = self._seed_server(db, "alpha")
        b = self._seed_server(db, "beta")
        db.blacklist_server(a, reason="r1")
        db.blacklist_server(b, reason="r2")
        db.acknowledge_blacklist(a)

        all_listed = db.list_blacklisted_servers()
        assert {s["name"] for s in all_listed} == {"alpha", "beta"}

        nag_listed = db.list_blacklisted_servers(unacknowledged_only=True)
        # alpha was acknowledged, only beta remains in the nag queue.
        assert {s["name"] for s in nag_listed} == {"beta"}

    def test_new_blacklist_event_re_arms_the_alert(self, db):
        """A fresh blacklisting after an ack must start nagging
        again — a new compromise event means new mistrust."""
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="first failure")
        db.acknowledge_blacklist(sid)
        # First incident is now silent.
        assert db.list_blacklisted_servers(unacknowledged_only=True) == []

        # A second failure is detected: blacklist_server re-arms.
        db.blacklist_server(sid, reason="second failure")
        nagged = db.list_blacklisted_servers(unacknowledged_only=True)
        assert len(nagged) == 1
        assert nagged[0]["blacklist_reason"] == "second failure"

    def test_unblacklist_clears_acknowledgement(self, db):
        sid = self._seed_server(db)
        db.blacklist_server(sid, reason="r")
        db.acknowledge_blacklist(sid)
        db.unblacklist_server(sid)
        row = db.get_server_by_id(sid)
        assert row["blacklisted_at"] is None
        assert row["blacklist_acknowledged_at"] is None
