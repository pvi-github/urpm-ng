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
        # Simulate a row inserted by ``add_media_legacy`` — the
        # placeholder fingerprint.
        db.add_media_legacy(
            name="Core Release",
            url="https://mirror.example/9/x86_64/media/core/release",
        )
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
