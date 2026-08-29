"""Tests for the ``media update`` mirror-hostname display.

The ``server_host`` field is populated on every :class:`SyncResult`
so the CLI can show which mirror served each media — parallel syncs
make it hard to tell otherwise, and a stale mirror was invisible in
the previous output.  These tests keep the plumbing honest.
"""

from __future__ import annotations

from urpm.core.sync import SyncResult
from urpm.cli.commands.media import _shorten_host


class TestSyncResultServerHost:
    def test_default_is_none(self):
        assert SyncResult(success=True).server_host is None

    def test_survives_construction(self):
        r = SyncResult(success=True, server_host="mageia.mirror.garr.it")
        assert r.server_host == "mageia.mirror.garr.it"


class TestShortenHost:
    def test_keeps_last_two_labels(self):
        assert _shorten_host("mageia.mirror.garr.it") == "garr.it"

    def test_strips_www(self):
        assert _shorten_host("www.mageia.biz") == "mageia.biz"

    def test_strips_us_country_prefix(self):
        assert _shorten_host("us.mirrors.cicku.me") == "cicku.me"

    def test_strips_mirrors_prefix(self):
        assert _shorten_host("mirrors.kernel.org") == "kernel.org"

    def test_hyphenated_prefix(self):
        # ``ftp-stud.hs-esslingen.de`` — the ``ftp-`` label goes,
        # trailing organisation stays visible.
        assert _shorten_host("ftp-stud.hs-esslingen.de") == "hs-esslingen.de"

    def test_bare_hostname_untouched(self):
        assert _shorten_host("localhost") == "localhost"

    def test_ipv4_preserved(self):
        # An IP has no meaningful org tail to keep — return verbatim
        # rather than mangling to ``1.1``.
        assert _shorten_host("192.168.1.1") == "192.168.1.1"

    def test_empty_string_returns_empty(self):
        assert _shorten_host("") == ""
