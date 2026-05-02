"""UX fix regression test for ``urpm install --arch X foo``.

Bug 4 (caught during the in-vivo arch-series test): when ``foo`` is
not available for arch ``X`` but exists in another arch, the install
path used to report ``Nothing to do`` — misleading, since the package
is *not* installed and the request just cannot be satisfied for that
arch. The fix emits an explicit error listing the archs actually
available.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pytest

from urpm.cli.commands.install import cmd_install
from urpm.core.database import PackageDatabase


@pytest.fixture
def db(monkeypatch):
    """Throwaway SQLite-backed PackageDatabase for one test."""
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '9')
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)
    yield database
    database.close()
    db_path.unlink(missing_ok=True)


def _make_args(**overrides) -> argparse.Namespace:
    """Build an argparse-shaped object with sensible install defaults."""
    base = dict(
        arch=None,
        packages=[],
        nodeps=False,
        download_only=False,
        nosignature=True,
        debug=None,
        watched=None,
        buildrequires=None,
        install_src=False,
        without_recommends=True,
        with_suggests=False,
        prefer=None,
        no_atomic=False,
        reinstall=False,
        show_all=False,
        allow_no_root=True,
        auto=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed_x86_64_package(db: PackageDatabase, name: str = 'lib64fuse2') -> int:
    """Insert a single x86_64-only package row, return its media id."""
    media_id = db.add_media(
        name="Core 64bit Release",
        short_name="core_release_x86_64",
        mageia_version="9",
        architecture="x86_64",
        relative_path="9/x86_64/media/core/release",
    )
    db.import_packages(iter([{
        'name': name,
        'version': '2.9.9',
        'release': '1.mga9',
        'epoch': 0,
        'arch': 'x86_64',
        'nevra': f'{name}-2.9.9-1.mga9.x86_64',
        'summary': 'libfuse2 64-bit shared library',
        'provides': [name, 'libfuse.so.2()(64bit)'],
        'requires': [],
        'filesize': 100000,
    }]), media_id=media_id)
    return media_id


class TestInstallArchNotAvailable:
    """``urpm install --arch i686 lib64fuse2`` must NOT say ``Nothing to do``."""

    def test_unavailable_arch_emits_explicit_error(self, db, capsys, monkeypatch):
        _seed_x86_64_package(db, name='lib64fuse2')

        # Bypass the resolver: the unavailable-arch detection runs at the
        # ``Nothing to do`` branch, which is reached when result.actions
        # is empty.  We stub _resolve_with_alternatives to return that
        # exact state — this isolates the assertion to the message path.
        from urpm.cli.commands import install as install_mod
        from urpm.core.resolver import Resolution

        monkeypatch.setattr(
            install_mod,
            "_resolve_with_alternatives",
            lambda *a, **kw: (
                Resolution(success=True, actions=[], problems=[]),
                False,
            ),
        )

        args = _make_args(arch='i686', packages=['lib64fuse2'], auto=True)
        rc = cmd_install(args, db)
        out = capsys.readouterr().out
        # Must not print the legacy "no-op" message (English or French).
        assert "Nothing to do" not in out and "Rien à faire" not in out, (
            "install must NOT report a no-op when the package is missing for the arch"
        )
        assert rc == 1
        assert "lib64fuse2" in out
        assert "i686" in out
        assert "x86_64" in out  # message lists archs actually available

    def test_genuine_no_op_still_reports_nothing_to_do(self, db, capsys, monkeypatch):
        """Sanity: a real already-installed case keeps the legacy message."""
        _seed_x86_64_package(db, name='lib64fuse2')

        from urpm.cli.commands import install as install_mod
        from urpm.core.resolver import Resolution

        monkeypatch.setattr(
            install_mod,
            "_resolve_with_alternatives",
            lambda *a, **kw: (
                Resolution(success=True, actions=[], problems=[]),
                False,
            ),
        )

        args = _make_args(arch='x86_64', packages=['lib64fuse2'], auto=True)
        rc = cmd_install(args, db)
        out = capsys.readouterr().out
        # Locale-agnostic: any of the standard "no-op" messages.
        assert ("Nothing to do" in out) or ("Rien à faire" in out)
        assert rc == 0
