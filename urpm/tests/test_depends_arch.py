"""Multi-arch regression tests for ``cmd_whatrecommends`` / ``cmd_whatsuggests``.

These two CLI verbs walk:

    1. ``db.get_package(pkg_name)`` to read the target's ``Provides`` list,
    2. ``db.whatrecommends(cap)`` / ``db.whatsuggests(cap)`` for each cap.

On a multi-arch host (typically ``x86_64`` with 32-bit media enabled), the
``packages`` table holds two rows for the same Mageia ``lib64*`` name —
the ``x86_64`` row provides ``libfuse.so.2()(64bit)`` while the ``i686``
row provides the suffix-less ``libfuse.so.2()``. Without an arch hint to
``get_package``, SQLite is free to return either row; if the i686 row
wins, none of the 64-bit recommenders/suggesters of the package can be
found and the user sees a spuriously empty result.

The fix passes ``arch=pick_arch_for_lookup(package, resolve_target_arch(args))``
to ``db.get_package``. These tests exercise the three relevant paths:

* default arch (host) → 64-bit recommenders/suggesters are found,
* ``--arch i686`` → 32-bit recommenders/suggesters are found instead,
* an explicit NEVRA suffix wins over ``--arch``.

The ``rpm`` fallback inside both functions (which scans the live rpmdb
of the host) is neutralised via monkeypatching to keep the test purely
DB-driven.
"""

import argparse
import tempfile
import types
from pathlib import Path

import pytest

from urpm.cli.commands.depends import cmd_whatrecommends, cmd_whatsuggests
from urpm.core.database import PackageDatabase


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    """Temporary SQLite-backed PackageDatabase, with mageia_version='9'."""
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '9')

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    database = PackageDatabase(db_path)
    yield database

    database.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _disable_rpmdb_scan(monkeypatch):
    """Neutralise the rpmdb fallback used inside cmd_what{recommends,suggests}.

    Both verbs iterate over the host rpmdb to catch already-installed
    recommenders/suggesters. Letting that happen during a unit test
    couples the result to whatever happens to be installed on the CI
    box. We swap ``rpm.TransactionSet().dbMatch()`` for an empty
    iterator so the assertions only see DB-side hits.
    """
    try:
        import rpm  # noqa: F401
    except ImportError:
        # If python-rpm is missing, the ``except ImportError`` branch
        # in the code under test handles it natively — nothing to do.
        return

    fake_ts = types.SimpleNamespace(dbMatch=lambda *a, **k: iter([]))
    monkeypatch.setattr('rpm.TransactionSet', lambda *a, **k: fake_ts)


def _import_multiarch_lib64fuse2(db):
    """Insert a realistic Mageia multi-arch shape for ``lib64fuse2``.

    The 32-bit row provides the suffix-less soname ``libfuse.so.2()``,
    the 64-bit row provides ``libfuse.so.2()(64bit)``. Five x86_64
    consumers recommend / suggest the 64-bit cap, one i686 consumer
    recommends / suggests the 32-bit cap. The CLI verb under test
    must surface the right consumer set depending on the target arch.
    """
    media_id = db.add_media(
        name="Core Release",
        short_name="core_release",
        mageia_version="9",
        architecture="x86_64",
        relative_path="core/release",
    )

    packages = [
        # The provider package, two arches.
        {
            'name': 'lib64fuse2', 'version': '2.9.9', 'release': '30.mga9',
            'epoch': 0, 'arch': 'x86_64',
            'nevra': 'lib64fuse2-2.9.9-30.mga9.x86_64',
            'provides': [
                'lib64fuse2',
                'lib64fuse2(x86-64)',
                'libfuse.so.2()(64bit)',
            ],
            'requires': [], 'filesize': 1000,
        },
        {
            'name': 'lib64fuse2', 'version': '2.9.9', 'release': '30.mga9',
            'epoch': 0, 'arch': 'i686',
            'nevra': 'lib64fuse2-2.9.9-30.mga9.i686',
            'provides': [
                'lib64fuse2',
                'lib64fuse2(i686)',
                'libfuse.so.2()',
            ],
            'requires': [], 'filesize': 1000,
        },
    ]

    # Five x86_64 packages that recommend the 64-bit soname.
    for n in range(1, 6):
        packages.append({
            'name': f'app-x{n}', 'version': '1.0', 'release': '1.mga9',
            'epoch': 0, 'arch': 'x86_64',
            'nevra': f'app-x{n}-1.0-1.mga9.x86_64',
            'provides': [f'app-x{n}'],
            'requires': [],
            'recommends': ['libfuse.so.2()(64bit)'],
            'suggests': ['libfuse.so.2()(64bit)'],
            'filesize': 1000,
        })

    # One i686 package that recommends the 32-bit soname.
    packages.append({
        'name': 'app-i', 'version': '1.0', 'release': '1.mga9',
        'epoch': 0, 'arch': 'i686',
        'nevra': 'app-i-1.0-1.mga9.i686',
        'provides': ['app-i'],
        'requires': [],
        'recommends': ['libfuse.so.2()'],
        'suggests': ['libfuse.so.2()'],
        'filesize': 1000,
    })

    db.import_packages(iter(packages), media_id=media_id)
    return media_id


def _capture_listed_pkgs(capsys):
    """Parse stdout of ``cmd_what{recommends,suggests}`` into a name set.

    Both verbs print:

        Packages that recommend X: N

            app-x1
            app-x2
            ...

    A blank line separates the header. We just keep two-space-indented
    entries.
    """
    out = capsys.readouterr().out
    return {
        line.strip()
        for line in out.splitlines()
        if line.startswith('  ') and line.strip()
    }


# ---------------------------------------------------------------------------
# cmd_whatrecommends
# ---------------------------------------------------------------------------


class TestWhatRecommendsArch:
    """Multi-arch behaviour of ``urpm whatrecommends``."""

    def test_finds_64bit_recommenders_when_arch_x86_64(self, db, capsys):
        """``--arch x86_64`` must surface the five x86_64 recommenders.

        Without the arch hint passed to ``db.get_package``, SQLite may
        return the i686 row of ``lib64fuse2`` whose ``Provides`` only
        carry suffix-less sonames — the SELECT on
        ``recommends.capability = 'libfuse.so.2()'`` then misses every
        x86_64 consumer that uses ``libfuse.so.2()(64bit)`` and the
        result set is empty (or limited to ``app-i``).
        """
        _import_multiarch_lib64fuse2(db)
        args = argparse.Namespace(package='lib64fuse2', arch='x86_64')

        rc = cmd_whatrecommends(args, db)

        listed = _capture_listed_pkgs(capsys)
        assert rc == 0
        assert listed == {'app-x1', 'app-x2', 'app-x3', 'app-x4', 'app-x5'}
        assert 'app-i' not in listed

    def test_arch_i686_returns_32bit_recommender(self, db, capsys):
        """``--arch i686`` flips the result to the 32-bit consumer.

        Symmetric proof that the arch hint is honoured rather than
        hard-coded to the host arch — important on 32-bit boxes and
        for inspection paths that deliberately query a foreign arch.
        """
        _import_multiarch_lib64fuse2(db)
        args = argparse.Namespace(package='lib64fuse2', arch='i686')

        rc = cmd_whatrecommends(args, db)

        listed = _capture_listed_pkgs(capsys)
        assert rc == 0
        assert listed == {'app-i'}

    def test_nevra_arch_overrides_args_arch(self, db, capsys):
        """A NEVRA's ``.arch`` suffix must win over ``--arch``.

        ``urpm whatrecommends lib64fuse2-2.9.9-30.mga9.i686 --arch x86_64``
        must hit the i686 row (the user typed it explicitly), so the
        result is ``app-i``, not ``app-x*``.
        """
        _import_multiarch_lib64fuse2(db)
        args = argparse.Namespace(
            package='lib64fuse2-2.9.9-30.mga9.i686',
            arch='x86_64',
        )

        rc = cmd_whatrecommends(args, db)

        listed = _capture_listed_pkgs(capsys)
        assert rc == 0
        assert listed == {'app-i'}


# ---------------------------------------------------------------------------
# cmd_whatsuggests
# ---------------------------------------------------------------------------


class TestWhatSuggestsArch:
    """Multi-arch behaviour of ``urpm whatsuggests`` — strictly isomorphic."""

    def test_finds_64bit_suggesters_when_arch_x86_64(self, db, capsys):
        """``--arch x86_64`` must surface the five x86_64 suggesters."""
        _import_multiarch_lib64fuse2(db)
        args = argparse.Namespace(package='lib64fuse2', arch='x86_64')

        rc = cmd_whatsuggests(args, db)

        listed = _capture_listed_pkgs(capsys)
        assert rc == 0
        assert listed == {'app-x1', 'app-x2', 'app-x3', 'app-x4', 'app-x5'}
        assert 'app-i' not in listed

    def test_arch_i686_returns_32bit_suggester(self, db, capsys):
        """``--arch i686`` flips the result to the 32-bit consumer."""
        _import_multiarch_lib64fuse2(db)
        args = argparse.Namespace(package='lib64fuse2', arch='i686')

        rc = cmd_whatsuggests(args, db)

        listed = _capture_listed_pkgs(capsys)
        assert rc == 0
        assert listed == {'app-i'}

    def test_nevra_arch_overrides_args_arch(self, db, capsys):
        """A NEVRA's ``.arch`` suffix must win over ``--arch``."""
        _import_multiarch_lib64fuse2(db)
        args = argparse.Namespace(
            package='lib64fuse2-2.9.9-30.mga9.i686',
            arch='x86_64',
        )

        rc = cmd_whatsuggests(args, db)

        listed = _capture_listed_pkgs(capsys)
        assert rc == 0
        assert listed == {'app-i'}
