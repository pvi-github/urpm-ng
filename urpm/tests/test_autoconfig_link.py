"""Tests for :func:`urpm.cli.commands.server._link_new_servers_to_media`.

Verifies the invariant this helper enforces: newly-added servers get
linked to **every media they host** — including media that are
currently disabled.  The regression this locks down is a testeur bug
where 39 disabled ``urpmi.cfg`` mirrorlist entries (Debug / Testing /
Backports / 32bit) stayed orphan after ``urpm server autoconfig``,
because the earlier implementation scanned enabled media only.  A
later ``urpm media enable`` would then have nothing to sync from.

The helper is exercised end-to-end against a real
:class:`urpm.core.database.PackageDatabase` on a temp SQLite file;
only :func:`urllib.request.urlopen` is patched so the HEAD probes
resolve without network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.database import PackageDatabase


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file."""
    with tempfile.TemporaryDirectory(prefix="autoconfig_link_") as tmp:
        db_path = Path(tmp) / "packages.db"
        db = PackageDatabase(db_path=db_path)
        try:
            yield db
        finally:
            db.close()


def _add_media(db, name, short_name, relative_path, *, enabled=True):
    return db.add_media(
        name=name,
        short_name=short_name,
        mageia_version="9",
        architecture="x86_64",
        relative_path=relative_path,
        is_official=True,
        allow_unsigned=False,
        enabled=enabled,
        update_media=False,
        priority=50,
        url=None,
    )


def _add_server(db, name, host, base_path="/mageia"):
    return db.add_server(
        name=name,
        protocol="https",
        host=host,
        base_path=base_path,
        is_official=True,
        enabled=True,
        priority=50,
    )


# ── Tests ───────────────────────────────────────────────────────────────


class TestLinkNewServersToMedia:
    """One class, one invariant: every media the server hosts gets linked."""

    def test_disabled_media_are_scanned_and_linked(self, db):
        """The regression: previously disabled media were skipped.

        A ``urpm media import`` from a Mageia ``urpmi.cfg`` inserts
        Backports/Testing/Debug/32bit repos as disabled by default.
        Even so, the autoconfig scan MUST link them — else a later
        ``urpm media enable Core Backports`` finds itself without a
        server to sync from.
        """
        enabled_id = _add_media(
            db, "Core Release", "core_release",
            "9/x86_64/media/core/release", enabled=True,
        )
        disabled_id = _add_media(
            db, "Core Backports", "core_backports",
            "9/x86_64/media/core/backports", enabled=False,
        )
        server_id = _add_server(db, "mirror-a", "a.mirror.example")

        with patch("urpm.cli.commands.server.urlopen") as urlopen_mock:
            urlopen_mock.return_value = MagicMock()  # all HEADs succeed
            from urpm.cli.commands.server import _link_new_servers_to_media
            count = _link_new_servers_to_media(
                db, [{"id": server_id, "name": "mirror-a"}],
            )

        assert count == 2, (
            f"expected both enabled and disabled media linked, got {count}"
        )
        assert db.server_media_link_exists(server_id, enabled_id)
        assert db.server_media_link_exists(server_id, disabled_id), (
            "disabled media MUST be linked — the whole point of the fix"
        )

    def test_media_without_relative_path_excluded_from_scan(self, db):
        """A media row with an empty ``relative_path`` cannot be probed.

        Guards against issuing a nonsensical HEAD like
        ``https://host//media_info/MD5SUM`` (double slash, no media),
        which some servers 200 on, producing spurious links.
        """
        _add_media(db, "Empty", "empty", "", enabled=True)
        real_id = _add_media(
            db, "Core Release", "core_release",
            "9/x86_64/media/core/release", enabled=True,
        )
        server_id = _add_server(db, "srv", "srv.example")

        head_urls = []

        def fake_urlopen(req, *args, **kwargs):
            head_urls.append(req.full_url)
            return MagicMock()

        with patch("urpm.cli.commands.server.urlopen", side_effect=fake_urlopen):
            from urpm.cli.commands.server import _link_new_servers_to_media
            count = _link_new_servers_to_media(
                db, [{"id": server_id, "name": "srv"}],
            )

        assert count == 1
        assert len(head_urls) == 1
        assert head_urls[0].endswith(
            "/9/x86_64/media/core/release/media_info/MD5SUM"
        ), head_urls
        assert db.server_media_link_exists(server_id, real_id)

    def test_head_failure_produces_no_link(self, db):
        """HEAD raises → no link.  The server does not host that media."""
        media_id = _add_media(
            db, "Nonfree Release", "nonfree_release",
            "9/x86_64/media/nonfree/release",
        )
        server_id = _add_server(db, "core-only-mirror", "core-only.example")

        def failing_urlopen(req, *args, **kwargs):
            raise OSError("HTTP 404")

        with patch("urpm.cli.commands.server.urlopen", side_effect=failing_urlopen):
            from urpm.cli.commands.server import _link_new_servers_to_media
            count = _link_new_servers_to_media(
                db, [{"id": server_id, "name": "core-only-mirror"}],
            )

        assert count == 0
        assert not db.server_media_link_exists(server_id, media_id)

    def test_multiple_servers_each_probed_independently(self, db):
        """Each server in *added* is scanned on its own base_url.

        A media hosted by one mirror but not another ends up linked
        to only the mirror that responds 2xx.
        """
        media_id = _add_media(
            db, "Core Release", "core_release",
            "9/x86_64/media/core/release",
        )
        srv_a_id = _add_server(db, "srv-a", "a.example")
        srv_b_id = _add_server(db, "srv-b", "b.example")

        def selective_urlopen(req, *args, **kwargs):
            if "a.example" in req.full_url:
                return MagicMock()
            raise OSError("HTTP 404")

        with patch("urpm.cli.commands.server.urlopen", side_effect=selective_urlopen):
            from urpm.cli.commands.server import _link_new_servers_to_media
            count = _link_new_servers_to_media(db, [
                {"id": srv_a_id, "name": "srv-a"},
                {"id": srv_b_id, "name": "srv-b"},
            ])

        assert count == 1
        assert db.server_media_link_exists(srv_a_id, media_id)
        assert not db.server_media_link_exists(srv_b_id, media_id)

    def test_no_double_count_on_existing_link(self, db):
        """If a (server, media) link already exists, the scan MUST NOT
        count it again.  Otherwise the ``N links created`` message
        reported to the user overstates the outcome.
        """
        media_id = _add_media(
            db, "Core Release", "core_release",
            "9/x86_64/media/core/release",
        )
        server_id = _add_server(db, "srv", "srv.example")
        db.link_server_media(server_id, media_id)  # pre-existing link

        with patch("urpm.cli.commands.server.urlopen") as urlopen_mock:
            urlopen_mock.return_value = MagicMock()
            from urpm.cli.commands.server import _link_new_servers_to_media
            count = _link_new_servers_to_media(
                db, [{"id": server_id, "name": "srv"}],
            )

        assert count == 0, (
            f"expected 0 new links (pre-existing link should not be counted), "
            f"got {count}"
        )

    def test_empty_media_table_returns_zero(self, db):
        """No media in the DB → nothing to scan, no error, zero links."""
        server_id = _add_server(db, "srv", "srv.example")

        with patch("urpm.cli.commands.server.urlopen") as urlopen_mock:
            from urpm.cli.commands.server import _link_new_servers_to_media
            count = _link_new_servers_to_media(
                db, [{"id": server_id, "name": "srv"}],
            )

        assert count == 0
        assert urlopen_mock.call_count == 0, (
            "no HEAD should be issued when there is no media to scan"
        )
