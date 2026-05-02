"""Multi-arch regression test for ``cmd_erase`` post-resolve filtering.

After resolving the erase set, ``cmd_erase`` walks each
:class:`PackageAction` to reclassify it as "explicit" when one of its
``Provides`` matches what the user typed on the command line. The
reclassification reads ``pkg_info['provides']`` from
``db.get_package(action.name)``.

On a multi-arch host, ``packages`` may hold two rows for the same
Mageia name — typically an ``x86_64`` row and an ``i686`` row of a
Mageia ``lib64*`` package. Without an arch hint, SQLite is free to
return either row, and the ``Provides`` list of the wrong arch may
*not* contain the capability the user actually asked for. The action
then stays misclassified as a "reverse dependency" instead of an
explicit removal — which is purely cosmetic (the package is still
erased) but defeats the headline section users see right before
confirming.

The fix passes ``arch=action.arch`` so the lookup pins to the same row
the resolver picked. This test exercises that contract end-to-end with
a real SQLite-backed :class:`PackageDatabase` and a stubbed resolver.
"""

import argparse
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.resolver import (
    InstallReason,
    PackageAction,
    Resolution,
    TransactionType,
)


# ---------------------------------------------------------------------------
# Fixtures
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


def _import_multiarch_lib64fuse2(db):
    """Insert ``lib64fuse2`` two arches with arch-distinct capabilities.

    The x86_64 row provides ``fuse2cap-64``, the i686 row provides
    ``fuse2cap-32``. A user erasing ``fuse2cap-64`` should see the
    x86_64 ``lib64fuse2`` action reclassified as explicit. Without the
    arch hint, ``db.get_package('lib64fuse2')`` may return the i686
    row, whose ``Provides`` do not contain ``fuse2cap-64``, and the
    action stays under "Reverse dependencies".
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
            'name': 'lib64fuse2', 'version': '2.9.9', 'release': '30.mga9',
            'epoch': 0, 'arch': 'x86_64',
            'nevra': 'lib64fuse2-2.9.9-30.mga9.x86_64',
            'provides': ['lib64fuse2', 'lib64fuse2(x86-64)', 'fuse2cap-64'],
            'requires': [], 'filesize': 1000,
        },
        {
            'name': 'lib64fuse2', 'version': '2.9.9', 'release': '30.mga9',
            'epoch': 0, 'arch': 'i686',
            'nevra': 'lib64fuse2-2.9.9-30.mga9.i686',
            'provides': ['lib64fuse2', 'lib64fuse2(i686)', 'fuse2cap-32'],
            'requires': [], 'filesize': 1000,
        },
    ]

    db.import_packages(iter(packages), media_id=media_id)
    return media_id


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubResolver:
    """Minimal resolver stand-in.

    ``cmd_erase`` calls :meth:`resolve_remove` to get the action set
    and :meth:`find_erase_orphans` to extend it. Both are stubbed to
    return exactly what the test prepares; the interesting logic
    under test is the post-resolve reclassification loop, not the
    libsolv-driven planning.
    """

    def __init__(self, actions):
        self._actions = actions

    def resolve_remove(self, package_names, clean_deps=False):
        return Resolution(
            success=True,
            actions=list(self._actions),
            problems=[],
            remove_size=sum(a.size for a in self._actions),
        )

    def find_erase_orphans(self, erase_names, erase_recommends=False,
                           keep_suggests=False):
        return []


def _action(name, arch, *, action=TransactionType.REMOVE):
    """Build a minimal :class:`PackageAction` for the test."""
    return PackageAction(
        action=action,
        name=name,
        evr='2.9.9-30.mga9',
        arch=arch,
        nevra=f'{name}-2.9.9-30.mga9.{arch}',
        size=1000,
        reason=InstallReason.DEPENDENCY,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestCmdEraseExplicitClassificationArch:
    """``cmd_erase`` must read ``pkg_info['provides']`` from the row
    matching ``action.arch`` to correctly reclassify multi-arch
    packages as explicit when the user erases one of their arch-only
    capabilities.
    """

    def _make_args(self, packages):
        """Build the ``argparse.Namespace`` ``cmd_erase`` expects."""
        return argparse.Namespace(
            packages=list(packages),
            auto=True,                # skip interactive confirmation
            test=True,                # dry run — no real removal
            allow_no_root=True,       # bypass root check
            auto_orphans=False,
            keep_orphans=True,        # skip find_erase_orphans logic
            erase_recommends=False,
            keep_suggests=False,
            debug=None,
            urpm_root='/',
            rpm_root='/',
            root='/',
            nosignature=True,
            noscripts=False,
            nodeps=False,
            force=False,
        )

    def test_x86_64_row_provides_drive_explicit_reclassification(
        self, db, capsys
    ):
        """User erases the cap ``fuse2cap-64`` only present on the
        x86_64 row of ``lib64fuse2``. The x86_64 action must surface
        under the "Requested" section, not "Reverse dependencies",
        because its arch-pinned provides include ``fuse2cap-64``.
        """
        _import_multiarch_lib64fuse2(db)
        actions = [_action('lib64fuse2', 'x86_64')]
        stub = _StubResolver(actions)

        with patch('urpm.cli.commands.remove._create_resolver',
                   return_value=stub):
            from urpm.cli.commands.remove import cmd_erase

            args = self._make_args(['fuse2cap-64'])
            rc = cmd_erase(args, db)

        assert rc == 0  # dry run exits 0
        out = capsys.readouterr().out

        # Headline classification under test: the package must appear
        # in the "Requested"/"Demandés" block, NOT the
        # "Reverse dependencies"/"Dépendances inverses" one. We accept
        # either translation so the test is locale-agnostic.
        assert 'lib64fuse2-2.9.9-30.mga9.x86_64' in out
        assert ('Requested' in out) or ('Demand' in out)
        # The reverse-deps section is only printed when there ARE
        # reverse-dep actions; ensure none here.
        assert 'Reverse dependencies' not in out
        assert 'inverses' not in out

    def test_i686_row_provides_drive_explicit_reclassification(
        self, db, capsys
    ):
        """Symmetric: erasing ``fuse2cap-32`` must reclassify the
        i686 action as explicit. Pre-fix this test would also pass
        *by accident* on systems where SQLite happens to return the
        i686 row first; with the fix it is deterministic.
        """
        _import_multiarch_lib64fuse2(db)
        actions = [_action('lib64fuse2', 'i686')]
        stub = _StubResolver(actions)

        with patch('urpm.cli.commands.remove._create_resolver',
                   return_value=stub):
            from urpm.cli.commands.remove import cmd_erase

            args = self._make_args(['fuse2cap-32'])
            rc = cmd_erase(args, db)

        assert rc == 0
        out = capsys.readouterr().out

        assert 'lib64fuse2-2.9.9-30.mga9.i686' in out
        assert ('Requested' in out) or ('Demand' in out)
        assert 'Reverse dependencies' not in out
        assert 'inverses' not in out
