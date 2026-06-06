"""Tests for ``urpm media add`` display-name handling.

Covers two fixes:

* ``--name`` is honoured when provided (previously silently dropped on
  official Mageia URLs, leaving the user with whichever name the URL
  parser derived).
* The auto-derived path now disambiguates against existing media using
  the shared ``disambiguate_media_name`` helper, so adding the same
  canonical media for a foreign architecture earns the ``(arch)``
  suffix instead of failing on ``UNIQUE(media.name)``.
"""

from __future__ import annotations

import argparse
import gettext
import tempfile
from pathlib import Path

import pytest

from urpm import i18n
from urpm.cli.commands.media import cmd_media_add
from urpm.cli.helpers import package as pkg_helpers
from urpm.core.database import PackageDatabase


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _force_null_translations(monkeypatch):
    """Neutralise gettext so error messages are matched against English."""
    monkeypatch.setattr(i18n, "_translation", gettext.NullTranslations())


@pytest.fixture(autouse=True)
def _pin_system_arch(monkeypatch):
    """Pin the local arch to ``x86_64`` so the foreign/native split is
    predictable in tests regardless of where they run."""
    monkeypatch.setattr(pkg_helpers, 'system_arch', lambda: 'x86_64')
    pkg_helpers.system_arch.cache_clear = lambda: None  # already a closure


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '10')
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)
    yield database
    database.close()
    db_path.unlink(missing_ok=True)


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(
        url=None,
        custom=None,
        name=None,
        version=None,
        disabled=False,
        update=False,
        import_key=False,
        allow_unsigned=False,
        auto=False,
        all=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


URL_X86_64 = "https://example.org/distrib/10/x86_64/media/core/release/"
URL_I586 = "https://example.org/distrib/10/i586/media/core/release/"


def _seed_existing_core_release(db, arch="x86_64", short="core_release"):
    """Pre-populate DB with a Core Release media on the given arch."""
    return db.add_media(
        name="Core Release", short_name=short, mageia_version="10",
        architecture=arch,
        relative_path=f"10/{arch}/media/core/release",
        is_official=True, allow_unsigned=False, enabled=True,
        update_media=False,
    )


# ── --name honoured ───────────────────────────────────────────────────


class TestExplicitNameHonored:
    def test_name_overrides_url_derived(self, db, capsys):
        """``--name "Un test"`` must beat the URL-parsed ``Core Release``."""
        rc = cmd_media_add(
            _make_args(url=URL_X86_64, name="Un test", auto=True), db)
        assert rc == 0
        stored = db.get_media("Un test")
        assert stored is not None, (
            "media should be stored under the user-supplied --name, not the "
            "URL-derived name"
        )
        # And the URL-derived name should NOT exist as a separate row
        assert db.get_media("Core Release") is None

    def test_no_name_keeps_url_derived(self, db, capsys):
        """Without ``--name``, the URL-derived name is used as before."""
        rc = cmd_media_add(
            _make_args(url=URL_X86_64, name=None, auto=True), db)
        assert rc == 0
        assert db.get_media("Core Release") is not None


# ── --name collision → clear error, no silent renaming ────────────────


class TestExplicitNameCollision:
    def test_explicit_name_collision_errors_out(self, db, capsys):
        """``--name X`` when X already exists must error, not silently
        suffix or fall back."""
        _seed_existing_core_release(db, arch="x86_64")
        rc = cmd_media_add(
            _make_args(url=URL_I586, name="Core Release", auto=True), db)
        assert rc != 0, "should refuse to insert when --name collides"
        out = capsys.readouterr().out
        assert "Core Release" in out
        assert "already taken" in out.lower()
        # No phantom "Core Release (i586)" should have been created
        assert db.get_media("Core Release (i586)") is None


# ── Auto-derived collision → arch suffix for foreign archs ────────────


class TestAutoDerivedCollision:
    def test_cross_arch_gets_arch_suffix(self, db, capsys):
        """Adding i586 ``Core Release`` while x86_64 already owns the
        name must end up stored as ``Core Release (i586)``."""
        _seed_existing_core_release(db, arch="x86_64")
        rc = cmd_media_add(
            _make_args(url=URL_I586, name=None, auto=True), db)
        assert rc == 0, "foreign-arch insert should succeed with suffix"
        stored = db.get_media("Core Release (i586)")
        assert stored is not None
        assert stored['architecture'] == 'i586'

    def test_native_arch_collision_errors_out(self, db, capsys):
        """A different short_name colliding by display name on the
        native arch must surface a clear error (no silent renaming)."""
        # Seed with a different short_name so the (version, arch,
        # short_name) pre-check does not catch it before the disambig
        # path runs.
        _seed_existing_core_release(
            db, arch="x86_64", short="core_release_old")
        rc = cmd_media_add(
            _make_args(url=URL_X86_64, name=None, auto=True), db)
        assert rc != 0
        out = capsys.readouterr().out
        assert "already taken" in out.lower()
