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
    _detect_arch,
    _make_short_name,
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


class TestMakeShortName:
    """Regression tests for :func:`_make_short_name`.

    The blogdrake catalogue exposed a silent collapse : the default-arch
    strip rule dropped every arch segment, including ``noarch``, so
    ``free/x86_64`` and ``free/noarch`` both produced short_name
    ``free``.  The second section then noop'd against the first at
    canonical-key lookup and its row was silently dropped.
    """

    def test_default_arch_is_stripped(self):
        assert _make_short_name("../../free/x86_64", "x86_64", "x86_64") == "free"

    def test_noarch_sibling_short_name_when_arch_defaulted(self):
        # Guard: if the caller passed x86_64 (pre-fix behaviour where
        # _detect_arch fell back to info.arch for a path-final noarch),
        # the short_name must at least keep the ``noarch`` qualifier so
        # the canonical key (v, x86_64, …) doesn't collapse against the
        # x86_64 sibling.
        assert _make_short_name("../../free/noarch", "x86_64", "x86_64") == "free_noarch"

    def test_noarch_sibling_short_name_when_arch_detected(self):
        # Production path: _detect_arch now recognises the path-final
        # ``noarch`` and returns 'noarch'.  The strip drops it from
        # the section parts, and the trailing prefix rule (arch !=
        # default_arch) re-prepends it as ``noarch_`` — symmetric with
        # the ``i686_`` prefix on cross-arch media.  Canonical key
        # (v, 'noarch', 'noarch_free') is doubly distinct from the
        # x86_64 sibling — no collision possible.
        assert _make_short_name("../../free/noarch", "noarch", "x86_64") == "noarch_free"

    def test_cross_arch_prefixes(self):
        # Cross-arch section (i686 under an x86_64 tree) keeps its
        # arch as a prefix — historical contract of the function.
        assert _make_short_name(
            "../../i686/media/core/release", "i686", "x86_64"
        ) == "i686_core_release"

    def test_pseudo_arch_srpms_preserved(self):
        # SRPMS isn't in _KNOWN_ARCHES, must survive the filter.
        assert _make_short_name("../../free/SRPMS", "x86_64", "x86_64") == "free_srpms"

    def test_debug_section_untouched(self):
        assert _make_short_name(
            "debug/core/release", "x86_64", "x86_64"
        ) == "debug_core_release"

    def test_mlo_flat_section(self):
        assert _make_short_name("core", "x86_64", "x86_64") == "core"


class TestDetectArch:
    """Regression tests for :func:`_detect_arch`.

    Extension over the historical ``<arch>/media/...`` cross-arch
    pattern : same-tree siblings that carry the arch as the final
    segment (``.../noarch``, ``.../x86_64``) are recognised too, so
    a genuinely noarch media gets ``architecture='noarch'`` in the
    DB and loads into any pool regardless of the caller's arch.
    """

    def test_cross_arch_i686_media(self):
        assert _detect_arch("../../i686/media/core/release", "x86_64") == "i686"

    def test_path_final_noarch(self):
        # Blogdrake pattern: ``[../../free/noarch]``.
        assert _detect_arch("../../free/noarch", "x86_64") == "noarch"

    def test_path_final_x86_64_matches_default(self):
        # Same shape but the arch equals the tree default — that's
        # fine, the arch column ends up = default_arch either way.
        assert _detect_arch("../../free/x86_64", "x86_64") == "x86_64"

    def test_no_arch_in_path_falls_back(self):
        assert _detect_arch("core/release", "x86_64") == "x86_64"

    def test_flat_mlo_section(self):
        assert _detect_arch("core", "x86_64") == "x86_64"

    def test_srpms_is_not_arch(self):
        # ``SRPMS`` isn't in _KNOWN_ARCHES — must not be misread as
        # an arch, we let the DB row inherit info.arch instead.
        assert _detect_arch("../../free/SRPMS", "x86_64") == "x86_64"
