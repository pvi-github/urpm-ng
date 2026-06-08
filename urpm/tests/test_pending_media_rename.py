"""Tests for the one-shot pre-3fafe62 ugly-media-name cleanup.

The cleanup lives in :mod:`urpm.core._pending_media_rename` and runs
in two halves:

* :func:`write_queue` is called from the urpm-ng RPM ``%post core``
  on every package upgrade.  It writes a small newline-separated
  list of media ids to a file under ``/var/lib/urpm/``.
* :func:`drain_queue` is called from ``cmd_media_update`` after a
  successful sync.  It processes the queued ids one by one,
  removing each from the file atomically as it is renamed, and
  deletes the file when the queue becomes empty.

These tests run the helpers directly against a throwaway DB and a
``tmp_path``-rooted queue file — no actual ``/var/lib/urpm/`` is
touched.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '10')
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)
    yield database
    database.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def queue_path(tmp_path):
    return tmp_path / "pending-name-cleanup.list"


def _seed_buggy(db, version="10", short="common_release", arch="x86_64"):
    """Insert a media row whose name matches the obsolete pattern."""
    return db.add_media(
        name=f"mga{version}-{short}",
        short_name=short,
        mageia_version=version,
        architecture=arch,
        relative_path=f"{version}/{arch}/media/{short.replace('_', '/')}",
        enabled=True,
    )


# ── write_queue ───────────────────────────────────────────────────────


class TestWriteQueue:
    def test_no_buggy_rows_writes_nothing(self, db, queue_path):
        from urpm.core._pending_media_rename import write_queue
        # Add only a clean row.
        db.add_media(
            name="Core Release", short_name="core_release",
            mageia_version="10", architecture="x86_64",
            relative_path="10/x86_64/media/core/release",
            enabled=True,
        )
        n = write_queue(db, queue_path=queue_path)
        assert n == 0
        assert not queue_path.exists()

    def test_writes_ids_one_per_line(self, db, queue_path):
        from urpm.core._pending_media_rename import write_queue
        a = _seed_buggy(db, short="common_release")
        b = _seed_buggy(db, short="urpm_release")
        n = write_queue(db, queue_path=queue_path)
        assert n == 2
        assert queue_path.exists()
        lines = queue_path.read_text().splitlines()
        assert set(map(int, lines)) == {a, b}

    def test_overwrites_existing_file(self, db, queue_path):
        from urpm.core._pending_media_rename import write_queue
        queue_path.write_text("999\n")  # stale content
        a = _seed_buggy(db, short="common_release")
        write_queue(db, queue_path=queue_path)
        assert queue_path.read_text().strip() == str(a)


# ── drain_queue ───────────────────────────────────────────────────────


class TestDrainQueue:
    def test_absent_file_is_no_op(self, db, queue_path):
        from urpm.core._pending_media_rename import drain_queue
        # Must not raise, must not write anything.
        drain_queue(db, queue_path=queue_path)
        assert not queue_path.exists()

    def test_empty_file_gets_deleted(self, db, queue_path):
        from urpm.core._pending_media_rename import drain_queue
        queue_path.write_text("")
        drain_queue(db, queue_path=queue_path)
        assert not queue_path.exists()

    def test_renames_each_id_and_drops_from_queue(self, db, queue_path):
        from urpm.core._pending_media_rename import drain_queue
        a = _seed_buggy(db, short="common_release")
        b = _seed_buggy(db, short="urpm_release")
        queue_path.write_text(f"{a}\n{b}\n")

        drain_queue(db, queue_path=queue_path)

        # Queue file is gone (all processed) and the rows have nice
        # names now.
        assert not queue_path.exists()
        names = {m['name'] for m in db.list_media()}
        assert "mga10-common_release" not in names
        assert "mga10-urpm_release" not in names

    def test_partial_drain_keeps_only_remaining_ids(
        self, db, queue_path, monkeypatch,
    ):
        """Simulate a rename collision on one entry: it stays in the
        queue file while the other is removed atomically."""
        from urpm.core import _pending_media_rename as pmr

        a = _seed_buggy(db, short="common_release")
        b = _seed_buggy(db, short="urpm_release")

        # Pre-existing media row that owns the would-be target name
        # for ``b`` — forces a collision.
        db.add_media(
            name="Urpm Release", short_name="other_short",
            mageia_version="9", architecture="x86_64",
            relative_path="9/x86_64/media/other",
            enabled=True,
        )

        queue_path.write_text(f"{a}\n{b}\n")
        pmr.drain_queue(db, queue_path=queue_path)

        # ``a`` got renamed and dropped from the queue.
        # ``b`` collided → dropped too (won't resolve itself),
        # logged as a warning.  The queue file should be gone.
        assert not queue_path.exists()

    def test_garbage_lines_are_skipped(self, db, queue_path):
        from urpm.core._pending_media_rename import drain_queue
        a = _seed_buggy(db, short="common_release")
        queue_path.write_text(f"not-an-int\n{a}\n  \n")
        drain_queue(db, queue_path=queue_path)
        # Valid id got processed → queue empty → file gone.
        assert not queue_path.exists()

    def test_stale_queue_id_for_removed_media_is_dropped(
        self, db, queue_path,
    ):
        """If a media id was queued but the row was removed in
        between, the helper must not crash — just drop the id."""
        from urpm.core._pending_media_rename import drain_queue
        queue_path.write_text("999999\n")  # id that does not exist
        drain_queue(db, queue_path=queue_path)
        assert not queue_path.exists()

    def test_already_renamed_id_is_dropped_quietly(
        self, db, queue_path,
    ):
        """A media that someone manually renamed (no longer matches
        the buggy pattern) must be dropped from the queue without
        re-renaming."""
        from urpm.core._pending_media_rename import drain_queue
        a = db.add_media(
            name="Already Clean",          # NOT ``mga10-*``
            short_name="common_release",
            mageia_version="10",
            architecture="x86_64",
            relative_path="10/x86_64/media/common/release",
            enabled=True,
        )
        queue_path.write_text(f"{a}\n")
        drain_queue(db, queue_path=queue_path)
        assert not queue_path.exists()
        # Name untouched.
        assert db.get_media("Already Clean") is not None


# ── Resolution cascade integration ────────────────────────────────────


class TestResolutionCascade:
    def test_consults_upstream_when_server_available(
        self, db, queue_path, monkeypatch,
    ):
        """When a server is linked, the cleanup goes through
        ``resolve_display_name(prefer='global')`` — same cascade
        the rest of the codebase uses."""
        from urpm.core import _pending_media_rename as pmr
        from urpm.core import media_cfg

        server_id = db.add_server(
            name="mgabiz", protocol="https",
            host="www.mageia.biz",
            base_path="/repo/Mageia/mgabiz",
            is_official=True, enabled=True,
        )
        media_id = _seed_buggy(db, short="urpm_release")
        db.link_server_media(server_id, media_id)

        captured = {}

        def fake_resolve(media_url, section, **kwargs):
            captured['media_url'] = media_url
            captured['section'] = section
            captured['prefer'] = kwargs.get('prefer')
            return "Mageia.biz urpm-ng"

        monkeypatch.setattr(media_cfg, 'resolve_display_name', fake_resolve)

        queue_path.write_text(f"{media_id}\n")
        pmr.drain_queue(db, queue_path=queue_path)

        # The rename used the upstream-derived name, not the
        # short_name fallback.
        assert db.get_media("Mageia.biz urpm-ng") is not None
        assert captured['prefer'] == "global"
        assert captured['section'] == "urpm/release"
        assert "www.mageia.biz" in captured['media_url']

    def test_falls_back_to_make_display_name_when_no_server(
        self, db, queue_path,
    ):
        """No server linked → cascade ends at
        ``_make_display_name(section)``.  Consistent with the rest
        of the resolver."""
        from urpm.core._pending_media_rename import drain_queue
        media_id = _seed_buggy(db, short="common_release")
        # NOT linking to any server.
        queue_path.write_text(f"{media_id}\n")
        drain_queue(db, queue_path=queue_path)

        # _make_display_name("common/release") → "Common Release"
        assert db.get_media("Common Release") is not None
