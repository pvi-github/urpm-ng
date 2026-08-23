"""Tests for TS1 Stage 1 media swap (SPEC_DISTUPGRADE §4.1).

Covers the internal disable helpers and the top-level run_stage1
orchestration (target-media insertion is stubbed to avoid touching
real mirrors)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.stage1 import (
    Stage1Error,
    _disable_source_media,
    _mark_third_party_orphans,
    _transpose_third_party_media,
    _try_transpose_string,
    run_stage1,
)
from urpm.core.distupgrade.version import ReleaseIdentity


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urpm.core.config.get_system_version", lambda: "10")
    d = PackageDatabase(db_path=tmp_path / "packages.db")
    yield d
    d.close()


def _insert_media(db, *, name, version, enabled=1, is_official=1,
                  short_name=None):
    db.conn.execute("""
        INSERT INTO media
          (name, short_name, mageia_version, architecture,
           relative_path, is_official, enabled)
        VALUES (?, ?, ?, 'x86_64', ?, ?, ?)
    """, (name, short_name or name.replace(" ", "_"),
          version, f"{version}/x86_64/media/{name}", is_official, enabled))
    db.conn.commit()


class TestDisableSourceMedia:
    def test_marks_source_only_and_suffixes_name(self, db):
        _insert_media(db, name="core-mga10", version="10")
        _insert_media(db, name="nonfree-mga10", version="10")
        _insert_media(db, name="third-party", version="mgabiz10")

        rows = _disable_source_media(db, "10")
        assert len(rows) == 2
        assert {r["name"] for r in rows} == \
            {"core-mga10", "nonfree-mga10"}   # returned pre-rename

        # Post-rename : source rows carry [mga10] suffix to free the
        # plain display name for the target-release upsert step.
        after = {r["name"]: dict(r) for r in db.list_media()}
        assert after["third-party"]["enabled"] == 1
        assert after["third-party"]["disabled_by"] is None
        assert "core-mga10 [dg:10]" in after
        assert after["core-mga10 [dg:10]"]["enabled"] == 0
        assert after["core-mga10 [dg:10]"]["disabled_by"] == "distupgrade"
        assert after["nonfree-mga10 [dg:10]"]["enabled"] == 0


class TestMarkThirdPartyOrphans:
    def test_marks_non_source_non_target_and_suffixes(self, db):
        _insert_media(db, name="core-mga10", version="10")
        _insert_media(db, name="mgabiz-repo", version="mgabiz10")
        _insert_media(db, name="rpmfusion-11", version="11")

        rows = _mark_third_party_orphans(db, "10", "11")
        assert len(rows) == 1
        assert rows[0]["name"] == "mgabiz-repo"    # pre-rename

        # Post-rename : orphan carries the [mga<version>] suffix.
        after = {r["name"]: dict(r) for r in db.list_media()}
        assert "mgabiz-repo [dg:mgabiz10]" in after
        assert after["mgabiz-repo [dg:mgabiz10]"]["enabled"] == 0
        assert after["mgabiz-repo [dg:mgabiz10]"]["disabled_by"] == \
            "distupgrade_orphan"
        # Source and target-version media untouched by this helper
        assert after["core-mga10"]["enabled"] == 1
        assert after["rpmfusion-11"]["enabled"] == 1


class TestTransposeString:
    def test_mga_prefix_substitution(self):
        assert _try_transpose_string("mga10-mlo", "10", "11") == "mga11-mlo"
        assert (_try_transpose_string("overlay-mga9/", "9", "10")
                == "overlay-mga10/")

    def test_path_segment_substitution(self):
        assert (_try_transpose_string(
            "file:///mnt/mirror/9/x86_64/media/", "9", "10")
            == "file:///mnt/mirror/10/x86_64/media/")

    def test_no_match_returns_none(self):
        assert _try_transpose_string("no-version-here", "10", "11") is None

    def test_empty_returns_none(self):
        assert _try_transpose_string("", "10", "11") is None

    def test_cauldron_not_transposed(self):
        # Non-numeric source → refuse (rolling target, N+1 arithmetic n/a)
        assert _try_transpose_string("mgacauldron-foo",
                                     "cauldron", "12") is None


class TestTransposeThirdPartyMedia:
    def test_transposes_and_disables_source(self, db, tmp_path):
        # Modern layout : server.base_path + media.relative_path.
        # Both mga9 and mga10 overlay dirs exist locally so the
        # file:// reachability probe succeeds for the transposed URL.
        (tmp_path / "9" / "x86_64" / "media" / "urpm").mkdir(parents=True)
        (tmp_path / "10" / "x86_64" / "media" / "urpm").mkdir(parents=True)
        srv_id = db.add_server(
            name="local-overlay", protocol="file", host="",
            base_path=str(tmp_path), is_official=False, enabled=True,
        )
        media_id = db.add_media(
            name="mga9-overlay", short_name="mga9_overlay",
            mageia_version="9", architecture="x86_64",
            relative_path="9/x86_64/media/urpm",
            is_official=False, enabled=True, priority=100,
        )
        db.link_server_media(srv_id, media_id)

        activated, orphaned = _transpose_third_party_media(
            db, "9", "10")

        assert len(activated) == 1
        assert not orphaned

        media = {r["name"]: dict(r) for r in db.list_media()}
        assert media["mga9-overlay [dg:9]"]["enabled"] == 0
        assert media["mga9-overlay [dg:9]"]["disabled_by"] == "distupgrade"
        # Target row was inserted with the plain (freed) name +
        # bumped relative_path.
        assert "mga10-overlay" in media
        assert media["mga10-overlay"]["enabled"] == 1
        assert media["mga10-overlay"]["mageia_version"] == "10"
        assert media["mga10-overlay"]["is_official"] == 0
        assert media["mga10-overlay"]["relative_path"] == \
            "10/x86_64/media/urpm"

    def test_no_transposition_pattern_orphans(self, db, tmp_path):
        db.add_media(
            name="weird-name", short_name="weird",
            mageia_version="9", architecture="x86_64",
            relative_path="", is_official=False, enabled=True,
            priority=100,
            url="https://example.com/repo/",   # no mga9 / /9/
        )

        activated, orphaned = _transpose_third_party_media(
            db, "9", "10", probe=False)

        assert not activated
        assert len(orphaned) == 1
        media = {r["name"]: dict(r) for r in db.list_media()}
        assert media["weird-name [dg:9]"]["enabled"] == 0
        assert media["weird-name [dg:9]"]["disabled_by"] == \
            "distupgrade_orphan"

    def test_probe_failure_orphans(self, db, tmp_path):
        # URL substitution works but target path doesn't exist → orphan
        db.add_media(
            name="mga9-ghost", short_name="mga9_ghost",
            mageia_version="9", architecture="x86_64",
            relative_path="", is_official=False, enabled=True,
            priority=100,
            url=f"file://{tmp_path}/does-not-exist-mga9",
        )
        # Deliberately do NOT create the transposed sibling path.

        activated, orphaned = _transpose_third_party_media(
            db, "9", "10")

        assert not activated
        assert len(orphaned) == 1
        media = {r["name"]: dict(r) for r in db.list_media()}
        assert media["mga9-ghost [dg:9]"]["disabled_by"] == \
            "distupgrade_orphan"


class TestRunStage1:
    def test_writes_state_transitions_and_returns_summary(
            self, db, monkeypatch, tmp_path):

        _insert_media(db, name="core-mga10", version="10")

        # Fake target-media upsert
        monkeypatch.setattr(
            "urpm.core.distupgrade.stage1._insert_target_media",
            lambda db, target, arch, **kw: (
                ["https://mirror/11/x86_64/media/"],
                [],
            ),
        )

        target = ReleaseIdentity(identity="11", numeric="11")
        summary = run_stage1(db, source_identity="10",
                             target=target, arch="x86_64")

        assert len(summary["disabled_source"]) == 1
        assert summary["disabled_source"][0]["name"] == "core-mga10"
        assert summary["created_urls"] == [
            "https://mirror/11/x86_64/media/"]
        # Final state = media_swapped
        from urpm.core.distupgrade.state import read_state
        state = read_state(db)
        assert state["stage"] == "media_swapped"
        assert state["version_to"] == "11"

    def test_no_target_media_raises(self, db, monkeypatch, tmp_path):

        monkeypatch.setattr(
            "urpm.core.distupgrade.stage1._insert_target_media",
            lambda db, target, arch, **kw: ([], ["mirror.example.org"]),
        )

        target = ReleaseIdentity(identity="11", numeric="11")
        with pytest.raises(Stage1Error, match="no target-release media"):
            run_stage1(db, source_identity="10",
                       target=target, arch="x86_64")


class TestChooseTargetUrlSegment:
    """Pure-function tests for the url_version stale-pin rule
    (SPEC_DISTUPGRADE §4.1 — papoteur beta case).

    Regression coverage : a mga9 machine whose servers were
    populated with ``url_version='9'`` at add time would build the
    Stage 1 target catalogue URL under ``/9/`` (the source segment),
    fetch the source catalogue by mistake, and end up with zero
    target-release media in the pool.  ``choose_target_url_segment``
    detects that shape and returns the target segment plus an
    "update needed" flag so the caller refreshes the pin.
    """

    def test_null_falls_back_to_target(self):
        from urpm.core.distupgrade.version import (
            choose_target_url_segment,
        )
        assert choose_target_url_segment(None, "9", "10") == ("10", False)
        assert choose_target_url_segment("", "9", "10") == ("10", False)

    def test_stale_numeric_pin_refreshed(self):
        """papoteur case : url_version='9' == source, target='10'."""
        from urpm.core.distupgrade.version import (
            choose_target_url_segment,
        )
        assert choose_target_url_segment("9", "9", "10") == ("10", True)

    def test_cauldron_alias_preserved(self):
        """Freeze case : mirror serves target under /cauldron/.

        Non-numeric url_version is intentional per-server override,
        preserved verbatim even when it matches source_identity.
        """
        from urpm.core.distupgrade.version import (
            choose_target_url_segment,
        )
        assert choose_target_url_segment(
            "cauldron", "cauldron", "11") == ("cauldron", False)
        assert choose_target_url_segment(
            "cauldron", "10", "11") == ("cauldron", False)

    def test_mismatched_numeric_pin_preserved(self):
        """Non-matching numeric pin is treated as intentional override."""
        from urpm.core.distupgrade.version import (
            choose_target_url_segment,
        )
        assert choose_target_url_segment(
            "10", "9", "11") == ("10", False)


class TestInsertTargetMediaUrlVersionRefresh:
    """Integration : Stage 1's target-media insertion refreshes stale
    url_version pins and journals the pre-image for ``--abort``.
    """

    def test_stale_pin_updates_row_and_journals_snapshot(
            self, db, monkeypatch):
        from urpm.core.distupgrade.stage1 import _insert_target_media

        # Seed one official server with a stale url_version pin.
        conn = db._get_connection()
        conn.execute("""
            INSERT INTO server
              (name, protocol, host, base_path, url_version,
               is_official, enabled)
            VALUES (?, ?, ?, ?, ?, 1, 1)
        """, ("test-mirror", "https", "mirror.example.org",
              "/mageia/distrib", "9"))
        conn.commit()

        captured_urls = []

        def _fake_upsert(db, url, mode, enabled_policy=None):
            captured_urls.append(url)
            r = MagicMock()
            r.outcomes = []
            r.server_was_created = False
            return r

        monkeypatch.setattr(
            "urpm.core.media_pipeline.upsert_media_tree",
            _fake_upsert,
        )

        undo = {
            "modified_media": [],
            "modified_servers": [],
            "created_media_ids": [],
            "created_server_ids": [],
        }
        target = ReleaseIdentity(identity="10", numeric="10")
        created, failed = _insert_target_media(
            db, target, "x86_64",
            source_identity="9",
            undo_journal=undo,
        )

        # URL built under the TARGET segment, not the stale '9'.
        assert captured_urls == [
            "https://mirror.example.org/mageia/distrib/10/x86_64/media/"]
        # Row refreshed on the server.
        row = conn.execute(
            "SELECT url_version FROM server WHERE name=?",
            ("test-mirror",)).fetchone()
        assert row["url_version"] == "10"
        # Pre-image journaled so --abort can restore.
        assert len(undo["modified_servers"]) == 1
        assert undo["modified_servers"][0]["url_version"] == "9"

    def test_null_pin_does_not_journal(self, db, monkeypatch):
        from urpm.core.distupgrade.stage1 import _insert_target_media

        conn = db._get_connection()
        conn.execute("""
            INSERT INTO server
              (name, protocol, host, base_path, url_version,
               is_official, enabled)
            VALUES (?, ?, ?, ?, NULL, 1, 1)
        """, ("test-mirror", "https", "mirror.example.org",
              "/mageia/distrib"))
        conn.commit()

        captured_urls = []

        def _fake_upsert(db, url, mode, enabled_policy=None):
            captured_urls.append(url)
            r = MagicMock()
            r.outcomes = []
            r.server_was_created = False
            return r

        monkeypatch.setattr(
            "urpm.core.media_pipeline.upsert_media_tree",
            _fake_upsert,
        )

        undo = {
            "modified_media": [],
            "modified_servers": [],
            "created_media_ids": [],
            "created_server_ids": [],
        }
        target = ReleaseIdentity(identity="10", numeric="10")
        _insert_target_media(
            db, target, "x86_64",
            source_identity="9",
            undo_journal=undo,
        )

        # URL uses target segment via the fallback.
        assert captured_urls == [
            "https://mirror.example.org/mageia/distrib/10/x86_64/media/"]
        # Row still NULL, no journal entry.
        row = conn.execute(
            "SELECT url_version FROM server WHERE name=?",
            ("test-mirror",)).fetchone()
        assert row["url_version"] is None
        assert undo["modified_servers"] == []

    def test_cauldron_alias_preserved_at_stage1(self, db, monkeypatch):
        from urpm.core.distupgrade.stage1 import _insert_target_media

        conn = db._get_connection()
        conn.execute("""
            INSERT INTO server
              (name, protocol, host, base_path, url_version,
               is_official, enabled)
            VALUES (?, ?, ?, ?, 'cauldron', 1, 1)
        """, ("test-mirror", "https", "mirror.example.org",
              "/mageia/distrib"))
        conn.commit()

        captured_urls = []

        def _fake_upsert(db, url, mode, enabled_policy=None):
            captured_urls.append(url)
            r = MagicMock()
            r.outcomes = []
            r.server_was_created = False
            return r

        monkeypatch.setattr(
            "urpm.core.media_pipeline.upsert_media_tree",
            _fake_upsert,
        )

        undo = {
            "modified_media": [],
            "modified_servers": [],
            "created_media_ids": [],
            "created_server_ids": [],
        }
        target = ReleaseIdentity(identity="11", numeric="11")
        _insert_target_media(
            db, target, "x86_64",
            source_identity="cauldron",
            undo_journal=undo,
        )

        # URL keeps the alias segment.
        assert captured_urls == [
            "https://mirror.example.org/mageia/distrib/cauldron/x86_64/media/"]
        # Row untouched, no journal entry.
        row = conn.execute(
            "SELECT url_version FROM server WHERE name=?",
            ("test-mirror",)).fetchone()
        assert row["url_version"] == "cauldron"
        assert undo["modified_servers"] == []
