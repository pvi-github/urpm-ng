"""Multi-arch regression tests for ``UrpmDaemon.check_available``.

The daemon exposes ``/api/available`` over HTTP (consumed by LAN peers,
Discover and the rpmdrake-ng GUI). Internally the handler delegates to
:meth:`UrpmDaemon.check_available`, which resolves each requested name
through ``db.get_package`` and returns the version/release/arch/media/
summary tuple.

On a multi-arch host (typically ``x86_64`` with 32-bit media enabled),
the ``packages`` table holds two rows for the same Mageia ``lib*`` name.
Without an arch hint, ``db.get_package`` is free to return either row;
an HTTP client probing for, say, ``lib64fuse2`` could legitimately
receive ``arch='i686'`` and wrongly conclude that the 64-bit package is
unavailable. The fix pins the lookup to ``platform.machine()`` so the
response always reflects what the daemon host can actually install.

These tests use a real ``PackageDatabase`` (no mocks) populated with two
multi-arch rows, and instantiate ``UrpmDaemon`` with stub paths since
``check_available`` only reads ``self.db`` — none of the HTTP/scheduler/
discovery machinery is started.
"""

import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase
from urpm.daemon.daemon import UrpmDaemon


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    """Temporary SQLite-backed PackageDatabase with mageia_version='9'."""
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '9')

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    database = PackageDatabase(db_path)
    yield database

    database.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def daemon(db):
    """Daemon instance wired to the test DB, no start()/HTTP/scheduler."""
    d = UrpmDaemon(
        db_path=str(db.db_path) if hasattr(db, 'db_path') else ':memory:',
        base_dir='/tmp',
        host='127.0.0.1',
        port=0,
        pid_file='/tmp/test-urpmd.pid',
        dev_mode=True,
    )
    d.db = db
    return d


def _import_multiarch_firefox(db):
    """Insert three rows: firefox.x86_64, firefox.i686, firefox-doc.noarch.

    The version/release tuple is identical across the two arch rows, so
    SQLite's ORDER BY epoch/version/release does not pin a winner — the
    arch filter is the only thing that determines which row comes back.
    """
    media_id = db.add_media(
        name="Core Release",
        short_name="core_release",
        mageia_version="9",
        architecture="x86_64",
        relative_path="core/release",
    )

    packages = [
        {
            'name': 'firefox', 'version': '120.0', 'release': '1.mga9',
            'epoch': 0, 'arch': 'x86_64',
            'nevra': 'firefox-120.0-1.mga9.x86_64',
            'summary': 'Firefox web browser',
            'provides': ['firefox', 'firefox(x86-64)'],
            'requires': [], 'filesize': 1000,
        },
        {
            'name': 'firefox', 'version': '120.0', 'release': '1.mga9',
            'epoch': 0, 'arch': 'i686',
            'nevra': 'firefox-120.0-1.mga9.i686',
            'summary': 'Firefox web browser',
            'provides': ['firefox', 'firefox(i686)'],
            'requires': [], 'filesize': 1000,
        },
        {
            'name': 'firefox-doc', 'version': '120.0', 'release': '1.mga9',
            'epoch': 0, 'arch': 'noarch',
            'nevra': 'firefox-doc-120.0-1.mga9.noarch',
            'summary': 'Firefox documentation',
            'provides': ['firefox-doc'],
            'requires': [], 'filesize': 500,
        },
    ]

    db.import_packages(iter(packages), media_id=media_id)
    return media_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckAvailableArch:
    """Multi-arch behaviour of ``UrpmDaemon.check_available``."""

    def test_check_available_returns_native_arch_on_multi_arch_host(
        self, daemon, db, monkeypatch
    ):
        """On an x86_64 host the returned arch must be x86_64, never i686.

        Without the arch hint, ``db.get_package('firefox')`` could return
        either of the two equally-ranked rows; SQLite's tie-breaking is
        an implementation detail. The fix pins the lookup to
        ``platform.machine()``, so the response is deterministic.
        """
        _import_multiarch_firefox(db)
        monkeypatch.setattr('urpm.daemon.daemon.platform.machine',
                            lambda: 'x86_64')

        result = daemon.check_available(['firefox'])

        info = result['packages']['firefox']
        assert info['available'] is True
        assert info['arch'] == 'x86_64'
        assert info['version'] == '120.0'
        assert info['release'] == '1.mga9'

    def test_check_available_returns_i686_arch_on_i686_host(
        self, daemon, db, monkeypatch
    ):
        """Symmetric proof: on a 32-bit host the i686 row must come back.

        Confirms the patch reads ``platform.machine()`` rather than
        hard-coding the lookup arch.
        """
        _import_multiarch_firefox(db)
        monkeypatch.setattr('urpm.daemon.daemon.platform.machine',
                            lambda: 'i686')

        result = daemon.check_available(['firefox'])

        info = result['packages']['firefox']
        assert info['available'] is True
        assert info['arch'] == 'i686'

    def test_check_available_returns_noarch_for_noarch_only_package(
        self, daemon, db, monkeypatch
    ):
        """A ``noarch`` package must surface even when filter is x86_64.

        ``db.get_package(arch='x86_64')`` widens the filter to
        ``arch IN ('x86_64', 'noarch')`` exactly so noarch rows survive.
        Regression-proof that we did not over-filter.
        """
        _import_multiarch_firefox(db)
        monkeypatch.setattr('urpm.daemon.daemon.platform.machine',
                            lambda: 'x86_64')

        result = daemon.check_available(['firefox-doc'])

        info = result['packages']['firefox-doc']
        assert info['available'] is True
        assert info['arch'] == 'noarch'

    def test_check_available_handles_unavailable_package(
        self, daemon, db, monkeypatch
    ):
        """Unknown names must keep the legacy 'not found' shape.

        After ``get_package`` returns ``None``, the handler falls back
        to ``db.search`` for spelling suggestions. The arch filter must
        not break that path — clients of ``/api/available`` rely on
        ``available: False`` being returned for missing names rather
        than the call exploding.
        """
        _import_multiarch_firefox(db)
        monkeypatch.setattr('urpm.daemon.daemon.platform.machine',
                            lambda: 'x86_64')

        result = daemon.check_available(['nonexistent-package-xyz'])

        info = result['packages']['nonexistent-package-xyz']
        assert info['available'] is False
        assert 'suggestions' in info
