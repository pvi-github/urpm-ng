"""Tests for media display-name resolution and DB disambiguation.

Covers two pieces of the cleanup that landed after the ``mga10-…``
naming regression in ``cmd_media_discover``:

* :func:`urpm.core.media_cfg.resolve_display_name` — pure cascade of
  candidate names with network recovery.
* :func:`urpm.cli.helpers.media.disambiguate_media_name` — DB-aware
  collision resolution with the arch-suffix convention.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from urpm.cli.helpers import media as media_helpers
from urpm.cli.helpers.media import (
    MediaNameCollision,
    disambiguate_media_name,
)
from urpm.core import media_cfg
from urpm.core.database import PackageDatabase
from urpm.core.media_cfg import (
    is_ugly_name,
    resolve_display_name,
    _strip_to_last_media_segment,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """Throwaway SQLite-backed PackageDatabase for one test."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)
    yield database
    database.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def system_arch_x86_64(monkeypatch):
    """Pin the local arch to ``x86_64`` for predictable disambiguation."""
    monkeypatch.setattr(
        media_helpers, 'system_arch',
        lambda: 'x86_64', raising=False,
    )
    # disambiguate_media_name imports system_arch lazily, so patch the
    # module it pulls from too.
    from urpm.cli.helpers import package as pkg_helpers
    monkeypatch.setattr(pkg_helpers, 'system_arch', lambda: 'x86_64')


# ── is_ugly_name ──────────────────────────────────────────────────────


class TestIsUglyName:
    @pytest.mark.parametrize("name", [
        "mga10-common_release",
        "urpm_release",
        "core",
        "backports_testing",
        "i686_core_release",
    ])
    def test_snake_kebab_lowercase_is_ugly(self, name):
        assert is_ugly_name(name)

    @pytest.mark.parametrize("name", [
        "Core Release",
        "Common Release",
        "Nonfree Updates",
        "Tainted Backports Testing",
        "Some name",  # one space
        "X",          # one uppercase
    ])
    def test_has_caps_or_space_is_fine(self, name):
        assert not is_ugly_name(name)

    def test_empty_is_ugly(self):
        assert is_ugly_name("")
        assert is_ugly_name(None)


# ── _strip_to_last_media_segment ──────────────────────────────────────


class TestStripToLastMediaSegment:
    def test_strips_back_to_last_media(self):
        url = "https://distrib-coffee.example.org/pub/.../10/i586/media/core/release/"
        out = _strip_to_last_media_segment(url)
        assert out == "https://distrib-coffee.example.org/pub/.../10/i586/media/"

    def test_handles_no_trailing_slash(self):
        url = "https://host/path/media/core/release"
        out = _strip_to_last_media_segment(url)
        assert out == "https://host/path/media/"

    def test_returns_none_when_no_media_segment(self):
        assert _strip_to_last_media_segment(
            "https://host/elsewhere/foo/bar/") is None

    def test_picks_last_media_when_nested(self):
        # Legitimate edge case: a directory called ``media`` appears twice.
        # We want the deepest one (closest to the media itself).
        url = "https://host/media/mirror/10/x86_64/media/core/release/"
        out = _strip_to_last_media_segment(url)
        assert out == "https://host/media/mirror/10/x86_64/media/"


# ── resolve_display_name (no network: explicit + parent path) ─────────


class TestResolveDisplayNameNoNetwork:
    def test_explicit_name_short_circuits(self):
        name = resolve_display_name(
            media_url="https://anything/",
            section="core/release",
            explicit_name="Mon Custom Name",
        )
        assert name == "Mon Custom Name"

    def test_explicit_name_is_trimmed(self):
        name = resolve_display_name(
            media_url="https://anything/",
            section="core/release",
            explicit_name="  Padded  ",
        )
        assert name == "Padded"

    def test_parent_cfg_supplies_good_name(self):
        parent = {"core/release": {"name": "Core Release"}}
        name = resolve_display_name(
            media_url="https://anything/",
            section="core/release",
            parent_cfg_sections=parent,
        )
        assert name == "Core Release"

    def test_parent_cfg_rejects_ugly_falls_through(self):
        # When parent has an ugly name and there is no network reachable,
        # the computed fallback (from section) is used.
        parent = {"core/release": {"name": "core_release"}}
        # Force both network probes to fail by pointing at a host that
        # will not resolve (the helper swallows errors).
        with patch.object(media_cfg, '_try_fetch_name', return_value=None):
            name = resolve_display_name(
                media_url="https://nope.invalid/media/core/release/",
                section="core/release",
                parent_cfg_sections=parent,
            )
        # _make_display_name turns "core/release" into "Core Release"
        assert name == "Core Release"

    def test_no_parent_no_network_uses_computed(self):
        with patch.object(media_cfg, '_try_fetch_name', return_value=None):
            name = resolve_display_name(
                media_url="https://nope.invalid/media/common/release/",
                section="common/release",
            )
        assert name == "Common Release"


# ── resolve_display_name (network mocked) ─────────────────────────────


class TestResolveDisplayNameNetwork:
    def test_local_first_local_wins(self):
        """``prefer='local'`` should query local before global."""
        calls = []

        def fake_fetch(media_url, section, scope, timeout=5):
            calls.append(scope)
            return "Local Hit" if scope == "local" else None

        with patch.object(media_cfg, '_try_fetch_name', side_effect=fake_fetch):
            name = resolve_display_name(
                media_url="https://host/media/foo/release/",
                section="foo/release",
                prefer="local",
            )
        assert name == "Local Hit"
        assert calls == ["local"]  # global not tried, local won

    def test_global_first_falls_back_to_local(self):
        """``prefer='global'`` tries global, falls back to local on miss."""
        calls = []

        def fake_fetch(media_url, section, scope, timeout=5):
            calls.append(scope)
            return "Local Recovery" if scope == "local" else None

        with patch.object(media_cfg, '_try_fetch_name', side_effect=fake_fetch):
            name = resolve_display_name(
                media_url="https://host/media/foo/release/",
                section="foo/release",
                prefer="global",
            )
        assert name == "Local Recovery"
        assert calls == ["global", "local"]


# ── disambiguate_media_name ───────────────────────────────────────────


class TestDisambiguate:
    def _seed_media(self, db, name, arch, short, version="10"):
        return db.add_media(
            name=name, short_name=short, mageia_version=version,
            architecture=arch,
            relative_path=f"{version}/{arch}/media/{short}",
            is_official=True, allow_unsigned=False, enabled=True,
            update_media=False,
        )

    def test_no_collision_returns_base(self, db, system_arch_x86_64):
        out = disambiguate_media_name(db, "Core Release", "x86_64")
        assert out == "Core Release"

    def test_foreign_arch_collision_gets_suffix(self, db, system_arch_x86_64):
        self._seed_media(db, "Core Release", "x86_64", "core_release")
        out = disambiguate_media_name(db, "Core Release", "i586")
        assert out == "Core Release (i586)"

    def test_native_arch_collision_raises(self, db, system_arch_x86_64):
        existing_id = self._seed_media(
            db, "Core Release", "x86_64", "core_release")
        with pytest.raises(MediaNameCollision) as exc_info:
            disambiguate_media_name(db, "Core Release", "x86_64")
        assert exc_info.value.base_name == "Core Release"
        assert exc_info.value.existing['id'] == existing_id

    def test_cascading_collision_raises(self, db, system_arch_x86_64):
        # Both ``Core Release`` AND ``Core Release (i586)`` already taken.
        self._seed_media(db, "Core Release", "x86_64", "core_release")
        self._seed_media(
            db, "Core Release (i586)", "i586", "core_release_i586",
            version="9")
        with pytest.raises(MediaNameCollision):
            disambiguate_media_name(db, "Core Release", "i586")
