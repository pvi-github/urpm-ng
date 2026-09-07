"""A missing AppStream blob is expected, not a problem to report.

Mageia's official media publish no AppStream: the three candidate
names all 404.  AppStream only exists where the media were built with
``urpm genmedia``.  So on an ordinary machine every medium lacks it, at
every sync.

The 404 path was already quiet, and so was the network-error path.
What was not: a body that came back but would not decompress raised a
warning per medium per sync — ``Failed to decompress AppStream for Core
Release: Input format not supported by decoder``.  Same outcome as the
404, same fallback right below it (generate from synthesis), and
warnings printed on every normal run are what teach an operator to stop
reading warnings.
"""

from __future__ import annotations

import inspect
import lzma

from urpm.core import appstream


def _sync_source() -> str:
    return inspect.getsource(appstream.AppStreamManager.sync_media_appstream)


class TestAbsenceIsQuiet:

    def test_undecompressable_body_is_not_a_warning(self):
        source = _sync_source()
        handler = source[source.index("except lzma.LZMAError"):]
        handler = handler[:handler.index("except Exception")]
        assert "logger.warning" not in handler, (
            "an expected absence must not warn on every sync"
        )
        assert "logger.debug" in handler

    def test_404_stays_quiet_too(self):
        source = _sync_source()
        assert "No upstream AppStream" in source

    def test_network_error_stays_quiet(self):
        """A file:// medium with no appstream blob is the same
        non-event."""
        source = _sync_source()
        handler = source[source.index("except URLError"):]
        handler = handler[:handler.index("except lzma.LZMAError")]
        assert "logger.debug" in handler
        assert "logger.warning" not in handler


class TestRealProblemsStillSpeak:
    """Quiet about the expected, not about the unexpected."""

    def test_a_non_404_http_error_still_warns(self):
        source = _sync_source()
        handler = source[source.index("except HTTPError"):]
        handler = handler[:handler.index("except URLError")]
        assert "logger.warning" in handler, (
            "a 500 or a 403 is not an expected absence"
        )

    def test_an_unexpected_exception_still_warns(self):
        source = _sync_source()
        handler = source[source.index("except Exception"):]
        assert "logger.warning" in handler


class TestTheFallbackStillRuns:
    """Tolerating the absence is only acceptable because something is
    generated instead — otherwise it would be silence, not tolerance."""

    def test_generation_follows_every_handler(self):
        source = _sync_source()
        assert source.index("except lzma.LZMAError") < source.index(
            "generate_for_media")

    def test_the_decoder_error_is_the_one_seen_in_the_field(self):
        """`Input format not supported by decoder` is an LZMAError, so
        it lands in the handler this file is about."""
        with __import__("pytest").raises(lzma.LZMAError):
            lzma.decompress(b"<html>\n<body>404</body>\n</html>\n")
