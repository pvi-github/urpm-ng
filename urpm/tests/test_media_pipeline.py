"""Tests for :mod:`urpm.core.media_pipeline.upsert_media_tree`.

Covers the four branches of the decision tree (created / updated /
relinked / noop / skipped) plus the invariant refusals.  Uses the
fixtures from :mod:`urpm.tests.fixtures` to drive realistic catalogue
shapes, and patches :func:`urpm.core.media_cfg.fetch_media_cfg` to
serve them locally without HTTP.

The database is exercised end-to-end through a real
:class:`urpm.core.database.PackageDatabase` opened on a temporary
SQLite file — no mock, no monkey-patching of the DB layer.  That way
the tests double as integration tests of the relevant DB methods
(``add_media``, ``get_media_by_version_arch_shortname``,
``link_server_media``, ``server_media_link_exists``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core import media_pipeline
from urpm.core.database import PackageDatabase
from urpm.core.media_pipeline import (
    MediaTreeAttributeError,
    MediaTreeError,
    MediaTreeFetchError,
    UpsertOutcome,
    UpsertResult,
    upsert_media_tree,
)
from urpm.tests.fixtures import (
    assert_well_formed_media,
    load_media_cfg,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file."""
    with tempfile.TemporaryDirectory(prefix="upsert_db_") as tmp:
        db_path = Path(tmp) / "packages.db"
        db = PackageDatabase(db_path=db_path)
        try:
            yield db
        finally:
            db.close()


@pytest.fixture
def serve_catalogue(monkeypatch):
    """Return a function that pins ``fetch_media_cfg`` to a named fixture.

    Usage::

        def test_x(serve_catalogue, db):
            serve_catalogue("official_mageia_9_x86_64")
            upsert_media_tree(db, "https://h/9/x86_64/media/")
    """

    def _serve(fixture_name: str) -> None:
        content = load_media_cfg(fixture_name)

        def fake_fetch(base_url, timeout=10):
            return content

        monkeypatch.setattr(media_pipeline, "fetch_media_cfg", fake_fetch)

    return _serve


@pytest.fixture
def serve_no_catalogue(monkeypatch):
    """Pin ``fetch_media_cfg`` to raise — simulates 404 / network error."""

    def fake_fetch(base_url, timeout=10):
        raise RuntimeError(f"simulated 404 for {base_url}")

    monkeypatch.setattr(media_pipeline, "fetch_media_cfg", fake_fetch)


# ── Branch: created ─────────────────────────────────────────────────────


class TestCreatedBranch:
    """A fresh DB ingesting an official catalogue creates every media."""

    def test_eight_official_media_created(self, db, serve_catalogue):
        serve_catalogue("official_mageia_9_x86_64")

        result = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )

        assert isinstance(result, UpsertResult)
        assert len(result.outcomes) == 8
        assert {o.action for o in result.outcomes} == {"created"}

    def test_every_created_media_carries_proper_attributes(
        self, db, serve_catalogue,
    ):
        serve_catalogue("official_mageia_9_x86_64")
        upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )

        for media in db.list_media():
            servers = db.get_servers_for_media(media["id"])
            assert_well_formed_media(media, servers=servers)

    def test_server_inferred_official_from_catalogue(
        self, db, serve_catalogue,
    ):
        serve_catalogue("official_mageia_9_x86_64")
        result = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )

        servers = list(db.list_servers())
        assert len(servers) == 1
        assert servers[0]["id"] == result.server_id
        assert servers[0]["is_official"] == 1

    def test_display_names_carried_from_catalogue_name_field(
        self, db, serve_catalogue,
    ):
        serve_catalogue("official_mageia_9_x86_64")
        upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )

        names = {m["short_name"]: m["name"] for m in db.list_media()}
        assert names["core_release"] == "Core Release"
        assert names["nonfree_updates"] == "Nonfree Updates"

    def test_custom_catalogue_marks_server_non_official(
        self, db, serve_catalogue,
    ):
        serve_catalogue("custom_signed_mgabiz")
        result = upsert_media_tree(
            db, "https://mageia.biz/9/x86_64/media/",
        )

        servers = list(db.list_servers())
        assert len(servers) == 1
        assert servers[0]["is_official"] == 0
        assert result.outcomes[0].action == "created"


# ── Branch: noop ────────────────────────────────────────────────────────


class TestNoopBranch:
    """Running the primitive twice on the same URL is idempotent."""

    def test_second_call_yields_noop_outcomes(self, db, serve_catalogue):
        serve_catalogue("official_mageia_9_x86_64")
        first = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )
        second = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )

        assert {o.action for o in first.outcomes} == {"created"}
        assert {o.action for o in second.outcomes} == {"noop"}
        # Same server reused
        assert first.server_id == second.server_id


# ── Branch: relinked ────────────────────────────────────────────────────


class TestRelinkedBranch:
    """A media linked to server A gets re-linked when discovered via server B."""

    def test_second_server_relinks_existing_media(
        self, db, serve_catalogue,
    ):
        serve_catalogue("official_mageia_9_x86_64")
        upsert_media_tree(
            db, "https://mirror-a.example/mageia/9/x86_64/media/",
        )
        # Second mirror, same catalogue
        result_b = upsert_media_tree(
            db, "https://mirror-b.example/mageia/9/x86_64/media/",
        )

        # Server count went from 1 to 2
        servers = list(db.list_servers())
        assert len(servers) == 2
        # Every outcome on the second call is a relink
        assert {o.action for o in result_b.outcomes} == {"relinked"}
        # Media count unchanged
        assert len(db.list_media()) == 8

    def test_relinked_media_has_two_servers(self, db, serve_catalogue):
        serve_catalogue("official_mageia_9_x86_64")
        upsert_media_tree(db, "https://mirror-a.example/mageia/9/x86_64/media/")
        upsert_media_tree(db, "https://mirror-b.example/mageia/9/x86_64/media/")

        core_release = db.get_media_by_version_arch_shortname(
            "9", "x86_64", "core_release",
        )
        servers = db.get_servers_for_media(core_release["id"])
        assert len(servers) == 2


# ── Branch: skipped (collision without reconcile) ───────────────────────


class TestSkippedBranch:
    """Display-name collision on native arch is reported, not silently merged."""

    def test_native_arch_collision_skips_without_reconcile(
        self, db, serve_catalogue,
    ):
        # Pre-existing media on x86_64 with name "Core Release" but a
        # different canonical key (different short_name).
        db.add_media(
            name="Core Release",
            short_name="custom_core",
            mageia_version="9",
            architecture="x86_64",
            relative_path="9/x86_64/media/core/release",
            is_official=False,
        )

        serve_catalogue("official_mageia_9_x86_64")
        result = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
            mode="discover",
        )

        core_release_outcomes = [
            o for o in result.outcomes if o.short_name == "core_release"
        ]
        assert len(core_release_outcomes) == 1
        assert core_release_outcomes[0].action == "skipped"
        assert "already taken" in core_release_outcomes[0].reason


# ── Branch: updated (reconcile mode adopts placeholder rows) ────────────


class TestReconcileBranch:
    """Reconcile mode adopts and updates legacy placeholder rows."""

    def test_reconcile_updates_unknown_unknown_row(
        self, db, serve_catalogue,
    ):
        # Simulate a pre-refactor legacy row: mageia_version='unknown',
        # architecture='unknown', relative_path=''. This is the
        # placeholder fingerprint reconcile mode is designed to adopt.
        import time
        db.conn.execute(
            """
            INSERT INTO media (name, url, mirrorlist, enabled, update_media,
                              short_name, mageia_version, architecture,
                              relative_path, is_official, added_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Core Release",
             "https://mirror.example/9/x86_64/media/core/release",
             None, 1, 0, "core_release", "unknown", "unknown", "", 1,
             int(time.time())),
        )
        db.conn.commit()
        # Verify the placeholder shape we're about to repair.
        leg = db.get_media("Core Release")
        assert leg["mageia_version"] == "unknown"
        assert leg["relative_path"] == ""

        serve_catalogue("official_mageia_9_x86_64")
        result = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
            mode="reconcile",
        )

        # The placeholder row got adopted, its core fields updated.
        updated_outcomes = [o for o in result.outcomes if o.action == "updated"]
        assert len(updated_outcomes) == 1
        fixed = db.get_media("Core Release")
        assert fixed["mageia_version"] == "9"
        assert fixed["architecture"] == "x86_64"
        assert fixed["relative_path"] == "9/x86_64/media/core/release"
        # And it's now linked to the server (invariant a).
        servers = db.get_servers_for_media(fixed["id"])
        assert len(servers) == 1


# ── Invariant violations: refusals ──────────────────────────────────────


class TestRefusals:
    """Hard refusals when no source provides a required attribute."""

    def test_unreachable_url_raises_fetch_error(self, db, serve_no_catalogue):
        # Catalogue absent + URL not a recognised Mageia pattern.
        with pytest.raises(MediaTreeFetchError):
            upsert_media_tree(db, "https://random.example/not-a-mageia-tree/")

    def test_mlo_empty_arch_falls_back_to_url(self, db, serve_catalogue):
        # ``mlo_arch_empty`` has ``arch=`` empty in [media_info].
        # The URL ``.../9/x86_64/media/`` provides the missing arch.
        serve_catalogue("mlo_arch_empty")
        result = upsert_media_tree(
            db,
            "https://repository.mageialinux-online.org/9/x86_64/media/",
        )

        for media in db.list_media():
            servers = db.get_servers_for_media(media["id"])
            assert_well_formed_media(media, servers=servers)
        assert all(m["architecture"] == "x86_64" for m in db.list_media())

    def test_invalid_mode_raises_value_error(self, db):
        with pytest.raises(ValueError):
            upsert_media_tree(
                db, "https://mirror.example/", mode="bogus",
            )

    def test_no_orphan_media_after_failed_collision(
        self, db, serve_catalogue,
    ):
        """Even when one outcome is 'skipped', the others must respect
        the at-least-one-server invariant.  Crucially, no orphan media
        is left behind by a partial run."""
        db.add_media(
            name="Core Release",
            short_name="alien",
            mageia_version="9",
            architecture="x86_64",
            relative_path="elsewhere",
            is_official=False,
        )

        serve_catalogue("official_mageia_9_x86_64")
        upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
        )

        # Every media that was actually created in this run must have
        # a server linked (invariant a).
        for media in db.list_media():
            servers = db.get_servers_for_media(media["id"])
            if media["short_name"] == "alien":
                # Pre-existing custom row — out of scope, may or may not
                # have a server.
                continue
            assert servers, (
                f"media {media['name']!r} (short_name={media['short_name']!r}) "
                f"created without a server link — invariant (a) violated"
            )


# ── Hint-driven overrides ───────────────────────────────────────────────


class TestHintOverrides:
    """The hint dict carries caller-provided values when the catalogue
    is silent.  Used by ``urpm media import`` to inject the urpmi.cfg
    user-chosen name, by ``cmd_media_add --name`` etc."""

    def test_explicit_name_via_hint_wins(self, db, serve_catalogue):
        # When the URL points to a custom catalogue with a single
        # media, and the caller passes hint={'name': 'mgabiz-stable'},
        # the resulting row uses that name.
        serve_catalogue("custom_signed_mgabiz")
        result = upsert_media_tree(
            db, "https://mageia.biz/9/x86_64/media/",
            hint={"name": "mgabiz-stable"},
        )

        assert result.outcomes[0].action == "created"
        assert db.get_media("mgabiz-stable") is not None


# ── Pre-parsed catalogue, filter and enabled_policy hooks ────────────


class TestCatalogueParam:
    """``catalogue=...`` lets the caller pre-load the parsed catalogue
    (preview + dry-run flow) and skip the second fetch."""

    def test_pre_parsed_catalogue_avoids_fetch(self, db, monkeypatch):
        # Make any fetch attempt blow up — proves it never runs.
        from urpm.core import media_cfg

        def boom(*_args, **_kw):
            raise AssertionError("fetch_media_cfg must not be called")

        monkeypatch.setattr(media_pipeline, "fetch_media_cfg", boom)
        monkeypatch.setattr(media_cfg, "fetch_media_cfg", boom)

        raw = load_media_cfg("official_mageia_9_x86_64")
        info_medias = parse_pre_loaded(raw)

        result = upsert_media_tree(
            db,
            "https://mirror.example/mageia/9/x86_64/media/",
            catalogue=info_medias,
            raw_catalogue=raw,
        )

        assert len(result.outcomes) == 8
        assert {o.action for o in result.outcomes} == {"created"}


class TestMediaFilter:
    """``media_filter`` lets the caller drop entries before any DB write."""

    def test_filter_keeps_only_release_media(self, db, serve_catalogue):
        serve_catalogue("official_mageia_9_x86_64")

        # Keep only release media (drop updates, backports, etc.).
        def only_release(m):
            return m.section.endswith("/release")

        result = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
            media_filter=only_release,
        )

        # core/release, nonfree/release, tainted/release = 3 entries
        assert len(result.outcomes) == 3
        assert all(o.short_name.endswith("_release") for o in result.outcomes)

    def test_filter_skip_all_yields_zero_outcomes(
        self, db, serve_catalogue,
    ):
        serve_catalogue("official_mageia_9_x86_64")
        result = upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
            media_filter=lambda m: False,
        )
        assert result.outcomes == []
        # Server still upserted even when no media is processed —
        # consistent with the "discover what's there" semantics.
        assert result.server_id is not None


class TestEnabledPolicy:
    """``enabled_policy`` overrides the default ``not noauto`` rule."""

    def test_policy_forces_everything_off(self, db, serve_catalogue):
        serve_catalogue("official_mageia_9_x86_64")
        upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
            enabled_policy=lambda m: False,
        )

        for media in db.list_media():
            assert media["enabled"] == 0

    def test_policy_forces_backports_on(self, db, serve_catalogue):
        serve_catalogue("official_mageia_9_x86_64")

        # noauto=1 backports are normally disabled; force them on for
        # this run while leaving the rest at catalogue default.
        def smart(m):
            if m.section.startswith("core/backports"):
                return True
            return not m.noauto

        upsert_media_tree(
            db, "https://mirror.example/mageia/9/x86_64/media/",
            enabled_policy=smart,
        )

        backports = next(
            m for m in db.list_media() if m["short_name"] == "core_backports"
        )
        assert backports["enabled"] == 1


# Helper used by TestCatalogueParam.
def parse_pre_loaded(raw):
    """Re-parse a media.cfg locally to mimic what a CLI command would do
    before passing the result to ``upsert_media_tree`` via ``catalogue=``.
    """
    from urpm.core.media_cfg import parse_media_cfg
    return parse_media_cfg(raw, "9/x86_64/media")


class TestSplitReleaseArchTail:
    """The helper that detects ``.../<version>/<arch>`` at the tail
    of a mirror URL path and returns ``(server_root, url_version)``.

    Regression coverage for the ``urpm mkimage --release 11`` bug:
    during a freeze the mirrorlist API returns URLs like
    ``.../distrib/cauldron/x86_64/`` even for release ``11``, and
    the old naive suffix-strip left the tail in place so downstream
    URL reconstruction doubled the arch.
    """

    def test_numeric_release_tail_is_stripped(self):
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail("/distrib/10/x86_64", "x86_64")
        assert root == "/distrib"
        assert ver == "10"

    def test_cauldron_release_tail_is_stripped(self):
        """The freeze case: mirror URL exposes the release under
        ``cauldron`` even when we asked for a numeric release."""
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail(
            "/distrib/cauldron/x86_64", "x86_64",
        )
        assert root == "/distrib"
        assert ver == "cauldron"

    def test_trailing_slash_tolerated(self):
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail("/distrib/10/x86_64/", "x86_64")
        assert root == "/distrib"
        assert ver == "10"

    def test_arch_mismatch_leaves_path_intact(self):
        """URL tail arch doesn't match target arch → no strip,
        no url_version.  Caller falls back to release identity."""
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail(
            "/distrib/10/i586", "x86_64",
        )
        assert root == "/distrib/10/i586"
        assert ver is None

    def test_custom_layout_no_version_returns_intact(self):
        """A third-party repo that doesn't follow the Mageia layout
        yields ``(path, None)`` — reconstruction stays untouched."""
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail(
            "/repo/myrepo/x86_64", "x86_64",
        )
        # 'myrepo' doesn't match ``\\d+`` or ``cauldron`` → not a
        # Mageia version segment; leave the path as-is.
        assert root == "/repo/myrepo/x86_64"
        assert ver is None

    def test_deep_nested_base_path(self):
        """Real-world case: mirrors expose Mageia under nested paths
        like ``/pub/linux/Mageia/distrib/10/x86_64``."""
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail(
            "/pub/linux/Mageia/distrib/10/x86_64", "x86_64",
        )
        assert root == "/pub/linux/Mageia/distrib"
        assert ver == "10"

    def test_bare_arch_at_root_returns_intact(self):
        """A path that's only the arch without any prefix → no
        recognisable Mageia pair, leave as-is."""
        from urpm.core.media_pipeline import split_release_arch_tail
        root, ver = split_release_arch_tail("/x86_64", "x86_64")
        assert root == "/x86_64"
        assert ver is None


class TestBuildMediaUrlIgnoresUrlVersion:
    """``build_media_url`` no longer substitutes the first segment of
    ``media.relative_path`` with ``server.url_version``.

    That runtime rewrite made the mutable server cache authoritative
    over the URL every download used, and a dormant VM whose
    ``url_version`` predated a mirror layout change kept building
    wrong URLs indefinitely.  ``media.relative_path`` — written at
    add/discover time under the "URL wins for release identity"
    rule — is now the single source of truth.  ``url_version`` is
    still consulted at the *add* layer (``cmd_init``,
    ``image_urpm_ng``) where the data was just refreshed by the
    probe.
    """

    def _srv(self, **overrides):
        base = {
            "protocol": "https", "host": "mirror.example.org",
            "base_path": "/distrib", "url_version": None,
        }
        base.update(overrides)
        return base

    def test_relative_path_used_as_is_when_url_version_null(self):
        from urpm.core.config import build_media_url
        srv = self._srv()
        media = {"relative_path": "10/x86_64/media/core/release"}
        assert build_media_url(srv, media) == (
            "https://mirror.example.org/distrib/10/x86_64/media/core/release"
        )

    def test_url_version_matching_first_segment_still_noop(self):
        from urpm.core.config import build_media_url
        srv = self._srv(url_version="10")
        media = {"relative_path": "10/x86_64/media/core/release"}
        assert build_media_url(srv, media) == (
            "https://mirror.example.org/distrib/10/x86_64/media/core/release"
        )

    def test_stale_url_version_does_not_rewrite(self):
        """The dormant-cauldron regression : a stale ``url_version``
        must not silently rewrite the URL.  ``relative_path`` wins."""
        from urpm.core.config import build_media_url
        srv = self._srv(url_version="cauldron")
        media = {"relative_path": "11/x86_64/media/core/release"}
        assert build_media_url(srv, media) == (
            "https://mirror.example.org/distrib/11/x86_64/media/core/release"
        )
