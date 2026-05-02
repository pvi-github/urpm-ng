"""Tests for ``urpm.cli.helpers.package`` arch-aware lookups.

These tests cover the multi-arch correctness of two helpers:

* :func:`urpm.cli.helpers.package.resolve_virtual_package` — must
  forward an ``arch`` hint to :meth:`PackageDatabase.get_package` so
  that a host with 32-bit media enabled does not silently pick up an
  ``i686`` row when the user asked for ``x86_64``.

* :func:`urpm.cli.helpers.package.pick_arch_for_lookup` — encodes the
  rule used by ``cmd_install`` to pick an arch hint:

    * an explicit NEVRA's arch wins;
    * otherwise the caller-supplied ``target_arch`` is used.

The tests use a real on-disk SQLite database populated through
:meth:`PackageDatabase.import_packages`, with two rows for the same
name (``lib64fuse2`` ``i686`` + ``x86_64``) so the bug surface
(``get_package('lib64fuse2')`` returning either row, depending on SQLite
ordering) is genuinely reproducible.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.cli.helpers.package import (
    pick_arch_for_lookup,
    resolve_virtual_package,
)
from urpm.core.database import PackageDatabase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def multiarch_db(monkeypatch):
    """A real SQLite DB with two arches (i686, x86_64) for ``lib64fuse2``.

    Also includes a noarch sibling and an unrelated package so the
    interactive code paths in ``resolve_virtual_package`` (family
    grouping) don't accidentally fire on a single-row pool.
    """
    # Match mageia_version="9" set on the test media so the version
    # filter inside get_package() lets the rows through.
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '9')

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    db = PackageDatabase(db_path)
    media_id = db.add_media(
        name="Core Release",
        short_name="core_release",
        mageia_version="9",
        architecture="x86_64",  # host arch metadata, not a per-row filter
        relative_path="core/release",
    )

    # Two NEVRAs, same name, different arches — the exact shape of the
    # multi-arch bug we are guarding against.
    packages = [
        {
            'name': 'lib64fuse2',
            'version': '2.9.9',
            'release': '30.mga9',
            'epoch': 0,
            'arch': 'i686',
            'nevra': 'lib64fuse2-2.9.9-30.mga9.i686',
            'summary': 'FUSE library (i686)',
            'provides': [],
            'requires': [],
            'filesize': 60000,
        },
        {
            'name': 'lib64fuse2',
            'version': '2.9.9',
            'release': '30.mga9',
            'epoch': 0,
            'arch': 'x86_64',
            'nevra': 'lib64fuse2-2.9.9-30.mga9.x86_64',
            'summary': 'FUSE library (x86_64)',
            'provides': [],
            'requires': [],
            'filesize': 70000,
        },
        {
            'name': 'libfuse-doc',
            'version': '2.9.9',
            'release': '30.mga9',
            'epoch': 0,
            'arch': 'noarch',
            'nevra': 'libfuse-doc-2.9.9-30.mga9.noarch',
            'summary': 'FUSE documentation',
            'provides': [],
            'requires': [],
            'filesize': 30000,
        },
    ]
    db.import_packages(iter(packages), media_id=media_id)

    yield db

    db.close()
    db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Database-level: arch hint actually filters rows
# ---------------------------------------------------------------------------


class TestGetPackageArchFilter:
    """The DB lookup itself must honour the ``arch=`` hint.

    These tests pin the contract that ``resolve_virtual_package``
    relies on — without them, the helper-level fix is moot.
    """

    def test_arch_x86_64_returns_x86_64_row(self, multiarch_db):
        pkg = multiarch_db.get_package('lib64fuse2', arch='x86_64')
        assert pkg is not None
        assert pkg['arch'] == 'x86_64'

    def test_arch_i686_returns_i686_row(self, multiarch_db):
        pkg = multiarch_db.get_package('lib64fuse2', arch='i686')
        assert pkg is not None
        assert pkg['arch'] == 'i686'

    def test_arch_none_legacy_returns_some_row(self, multiarch_db):
        """Without arch hint, get_package() returns *some* row.

        This is the legacy behaviour we preserve for backward compat.
        We do not pin which arch — that is exactly the source of the
        bug fixed at the caller layer.
        """
        pkg = multiarch_db.get_package('lib64fuse2')
        assert pkg is not None
        assert pkg['arch'] in {'i686', 'x86_64'}

    def test_noarch_is_visible_under_x86_64_filter(self, multiarch_db):
        """``noarch`` must always pass the arch filter."""
        pkg = multiarch_db.get_package('libfuse-doc', arch='x86_64')
        assert pkg is not None
        assert pkg['arch'] == 'noarch'


# ---------------------------------------------------------------------------
# Helper-level: resolve_virtual_package forwards arch and behaves correctly
# ---------------------------------------------------------------------------


class TestResolveVirtualPackageArch:
    """``resolve_virtual_package`` must forward ``arch`` to ``get_package``.

    The function returns concrete *names* — the visible bug surface is
    indirect. We assert the contract two ways:

    1. Spy on ``db.get_package`` and check ``arch=`` is forwarded.
    2. Run on a real DB and check the function works (no crash, sane
       return) under each arch hint and under ``arch=None`` (legacy).
    """

    def _make_spy(self, db):
        """Wrap ``db.get_package`` so we can observe the kwargs."""
        seen = []
        original = db.get_package

        def spy(name, arch=None):
            seen.append({'name': name, 'arch': arch})
            return original(name, arch=arch)

        db.get_package = spy  # type: ignore[assignment]
        return seen

    def test_forwards_arch_x86_64(self, multiarch_db):
        seen = self._make_spy(multiarch_db)
        result = resolve_virtual_package(
            multiarch_db, 'lib64fuse2', auto=True, install_all=False, arch='x86_64'
        )
        # Spy captured the forwarded arch.
        assert any(c['name'] == 'lib64fuse2' and c['arch'] == 'x86_64' for c in seen)
        # Function returned the canonical name (no provider explosion
        # because the rows have no extra @provides, so families == 1).
        assert result == ['lib64fuse2']

    def test_forwards_arch_i686(self, multiarch_db):
        seen = self._make_spy(multiarch_db)
        resolve_virtual_package(
            multiarch_db, 'lib64fuse2', auto=True, install_all=False, arch='i686'
        )
        assert any(c['name'] == 'lib64fuse2' and c['arch'] == 'i686' for c in seen)

    def test_arch_none_legacy_forwards_none(self, multiarch_db):
        """Backward-compat: omitting ``arch`` must keep the legacy call."""
        seen = self._make_spy(multiarch_db)
        resolve_virtual_package(
            multiarch_db, 'lib64fuse2', auto=True, install_all=False
        )
        assert any(c['name'] == 'lib64fuse2' and c['arch'] is None for c in seen)

    def test_arch_does_not_break_unknown_name(self, multiarch_db):
        """Unknown name + arch hint must still return ``[name]`` gracefully."""
        result = resolve_virtual_package(
            multiarch_db, 'does-not-exist', auto=True, install_all=False, arch='x86_64'
        )
        assert result == ['does-not-exist']


# ---------------------------------------------------------------------------
# pick_arch_for_lookup — covers the cmd_install dispatcher rule
# ---------------------------------------------------------------------------


class TestPickArchForLookup:
    """The helper that ``cmd_install`` uses to pick an arch per package."""

    def test_plain_name_uses_target_arch(self):
        # User typed ``lib64fuse2`` with --arch x86_64 (or no flag, default x86_64).
        assert pick_arch_for_lookup('lib64fuse2', 'x86_64') == 'x86_64'

    def test_plain_name_under_i686_target(self):
        # Steam/Wine scenario: ``--arch i686 lib64fuse2``.
        assert pick_arch_for_lookup('lib64fuse2', 'i686') == 'i686'

    def test_explicit_nevra_arch_wins_over_target(self):
        """An explicit ``.i686`` NEVRA must beat a ``x86_64`` default.

        Regression guard: ``urpm install lib64fuse2-2.9.9-30.mga10.i686``
        on a host where ``args.arch`` is ``x86_64`` (implicit default)
        must still look up the i686 row.
        """
        nevra = 'lib64fuse2-2.9.9-30.mga10.i686'
        assert pick_arch_for_lookup(nevra, 'x86_64') == 'i686'

    def test_explicit_nevra_x86_64(self):
        nevra = 'firefox-120.0-1.mga10.x86_64'
        assert pick_arch_for_lookup(nevra, 'i686') == 'x86_64'

    def test_nevra_noarch_kept(self):
        nevra = 'libfuse-doc-2.9.9-30.mga10.noarch'
        assert pick_arch_for_lookup(nevra, 'x86_64') == 'noarch'

    def test_virtual_capability_uses_target(self):
        """``pkgconfig(foo)`` is not a NEVRA — must fall back to target."""
        assert pick_arch_for_lookup('pkgconfig(foo)', 'x86_64') == 'x86_64'

    def test_glob_uses_target(self):
        """A glob like ``firefox*`` is not a NEVRA — must fall back."""
        assert pick_arch_for_lookup('firefox*', 'x86_64') == 'x86_64'

    def test_versioned_name_without_arch_suffix_uses_target(self):
        """``lib64polkit1-devel-127`` is a literal Mageia name.

        It has no ``.arch`` suffix so ``extract_pkg_name`` returns it
        as-is — we must therefore use ``target_arch``, not invent one
        from the trailing ``-127``.
        """
        assert pick_arch_for_lookup('lib64polkit1-devel-127', 'x86_64') == 'x86_64'


# ---------------------------------------------------------------------------
# Integration: cmd_install caller pipeline picks the correct arch per pkg
# ---------------------------------------------------------------------------


class TestCmdInstallArchPropagation:
    """Pin the contract between ``cmd_install`` and ``resolve_virtual_package``.

    Rather than driving the full ``cmd_install`` (which would require
    root, a real transaction, etc.), we replay its dispatcher loop —
    the exact 5 lines that compute ``pkg_arch`` and call
    ``_resolve_virtual_package`` — and assert the arches it ends up
    forwarding for several command lines.
    """

    @staticmethod
    def _run_dispatcher(package_names, target_arch):
        """Replay the cmd_install dispatcher loop using the public helpers."""
        from urpm.cli.helpers.package import (
            extract_pkg_name as _extract_pkg_name,
        )

        seen_calls = []

        def fake_resolve(db, pkg_name, auto, install_all, arch=None):
            seen_calls.append({'pkg_name': pkg_name, 'arch': arch})
            return [pkg_name]

        for pkg in package_names:
            pkg_name = _extract_pkg_name(pkg)
            pkg_arch = pick_arch_for_lookup(pkg, target_arch)
            fake_resolve(None, pkg_name, False, False, arch=pkg_arch)

        return seen_calls

    def test_plain_name_with_arch_x86_64(self):
        calls = self._run_dispatcher(['lib64fuse2'], 'x86_64')
        assert calls == [{'pkg_name': 'lib64fuse2', 'arch': 'x86_64'}]

    def test_explicit_nevra_overrides_default(self):
        """Regression-proof: ``foo-1-1.mga10.i686`` keeps its own arch."""
        calls = self._run_dispatcher(
            ['lib64fuse2-2.9.9-30.mga10.i686'], 'x86_64'
        )
        assert calls == [{'pkg_name': 'lib64fuse2', 'arch': 'i686'}]

    def test_steam_wine_scenario(self):
        """``urpm install --arch i686 wine-i686`` (plain name) → arch=i686."""
        calls = self._run_dispatcher(['wine-i686'], 'i686')
        # ``wine-i686`` has no ``.arch`` suffix so it is a plain name;
        # the i686 *target* must drive the lookup.
        assert calls == [{'pkg_name': 'wine-i686', 'arch': 'i686'}]

    def test_mixed_command_line(self):
        """A NEVRA and a plain name in the same call get distinct arches."""
        calls = self._run_dispatcher(
            ['firefox', 'lib64fuse2-2.9.9-30.mga10.i686'], 'x86_64'
        )
        assert calls == [
            {'pkg_name': 'firefox', 'arch': 'x86_64'},
            {'pkg_name': 'lib64fuse2', 'arch': 'i686'},
        ]
