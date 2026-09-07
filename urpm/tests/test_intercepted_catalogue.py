"""A filtered connection must not read as a corrupt catalogue.

A beta tester's ``urpm image make`` died with::

    Phase 1 échouée : File contains no section headers.
    file: '<string>', line: 1
    '<html>\\n'

e2guardian, a content filter on his network, was interposing and
answering **200** with its own "access denied" page.  The HTTP status
check passed, the HTML reached configparser, and the library exception
escaped ``upsert_media_tree`` before reaching the fallback that turns
an unusable catalogue into ``MediaTreeFetchError``.

The discovery loop is written to warn and move on to the next mirror,
and ``MediaTreeFetchError`` is raised in six places for exactly that.
This one path skipped it, so a single intercepted request killed a run
that had 42 mirrors to try.

Two things follow, and both are needed:

* a 200 whose body is not a media.cfg is a failed fetch, and the
  message should name the interception -- the parser can only say the
  INI is malformed, which sends the operator looking in the wrong
  place;
* a parse failure must land where an unreachable URL lands, not
  escape as a library exception.
"""

from __future__ import annotations

import configparser

import pytest

from urpm.core.media_cfg import _reject_if_not_media_cfg, parse_media_cfg

E2GUARDIAN = (
    "<html>\n<head>\n<title>e2guardian - Acc&egrave;s interdit</title>\n"
    "</head>\n<body bgcolor=#FFFFFF>\n<b>Acc&egrave;s interdit !</b>\n"
    "</body>\n</html>\n"
)

REAL_MEDIA_CFG = (
    "[media_info]\nversion=10\narch=x86_64\nbranch=Official\n\n"
    "[core/release]\nname=Core Release\n"
)


class TestInterceptionIsNamed:

    def test_html_body_with_200_is_refused(self):
        with pytest.raises(RuntimeError):
            _reject_if_not_media_cfg(
                "http://mirror/media/media_info/media.cfg",
                E2GUARDIAN, "text/html")

    def test_the_message_points_at_a_proxy(self):
        """« File contains no section headers » sends the operator
        looking at the mirror.  The cause is on their own network."""
        with pytest.raises(RuntimeError, match="proxy or content filter"):
            _reject_if_not_media_cfg("http://mirror/x", E2GUARDIAN,
                                     "text/html")

    def test_the_message_names_the_url(self):
        """Without it we could not tell which of 42 mirrors was hit --
        which is exactly why the original report took a scan of every
        mirror to chase."""
        with pytest.raises(RuntimeError, match="http://mirror/x"):
            _reject_if_not_media_cfg("http://mirror/x", E2GUARDIAN,
                                     "text/html")

    def test_html_detected_without_a_content_type(self):
        """Interceptors are not obliged to be honest about the type."""
        with pytest.raises(RuntimeError, match="proxy or content filter"):
            _reject_if_not_media_cfg("http://mirror/x", E2GUARDIAN, "")

    def test_content_type_alone_is_enough(self):
        """A body that does not open with '<' but is served as HTML."""
        with pytest.raises(RuntimeError, match="proxy or content filter"):
            _reject_if_not_media_cfg("http://mirror/x", "Access denied\n",
                                     "text/html; charset=utf-8")


class TestOtherRubbishIsAlsoRefused:

    def test_body_without_the_media_info_section(self):
        with pytest.raises(RuntimeError, match="not a media.cfg"):
            _reject_if_not_media_cfg("http://mirror/x",
                                     "just some text\n", "text/plain")

    def test_empty_body(self):
        """A truncated transfer that still reported 200."""
        with pytest.raises(RuntimeError, match="not a media.cfg"):
            _reject_if_not_media_cfg("http://mirror/x", "", "")


class TestARealCatalogueIsAccepted:

    def test_passes(self):
        _reject_if_not_media_cfg(
            "http://mirror/x", REAL_MEDIA_CFG, "application/octet-stream")

    def test_leading_blank_lines_are_tolerated(self):
        _reject_if_not_media_cfg(
            "http://mirror/x", "\n\n" + REAL_MEDIA_CFG, "")

    def test_octet_stream_is_what_mirrors_actually_send(self):
        """Measured on a live mirror: the catalogue comes back as
        application/octet-stream, the directory index as text/html."""
        _reject_if_not_media_cfg(
            "http://mirror/x", REAL_MEDIA_CFG, "application/octet-stream")

    def test_and_it_still_parses(self):
        info, medias = parse_media_cfg(REAL_MEDIA_CFG, "10/x86_64/media")
        assert info.version == "10"
        assert medias


class TestTheParserStillRaisesOnHtml:
    """The pipeline relies on catching this; if configparser ever
    stopped raising, the guard below it would be silently dead."""

    def test_configparser_rejects_html(self):
        with pytest.raises(configparser.Error):
            parse_media_cfg(E2GUARDIAN, "10/x86_64/media")


class TestThePipelineTurnsItIntoAMirrorFailure:
    """End of the chain: the discovery loop catches MediaTreeError and
    moves to the next mirror.  A configparser exception escaping
    ``upsert_media_tree`` bypassed that entirely and killed the run.

    ``fetch_media_cfg`` now refuses HTML before the parser ever sees
    it, so this guard is defence for any other source of a bad body --
    a cached file, a peer, a future fetch path.  It is kept because the
    cost of it being absent was a whole ``urpm image make``.
    """

    @pytest.fixture
    def db(self):
        import tempfile
        from pathlib import Path
        from urpm.core.database import PackageDatabase
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            path = Path(handle.name)
        database = PackageDatabase(path)
        yield database
        database.close()
        path.unlink(missing_ok=True)

    def test_unparsable_catalogue_raises_the_catchable_error(
            self, db, monkeypatch):
        from urpm.core import media_pipeline
        from urpm.core.media_pipeline import (
            MediaTreeError,
            MediaTreeFetchError,
            upsert_media_tree,
        )

        monkeypatch.setattr(media_pipeline, "fetch_media_cfg",
                            lambda *a, **kw: E2GUARDIAN)

        with pytest.raises(MediaTreeFetchError) as caught:
            upsert_media_tree(
                db, "http://mirror.example/unrecognised/", mode="discover")

        # The loop catches the base class; a subclass that escaped it
        # would be as fatal as the configparser exception was.
        assert isinstance(caught.value, MediaTreeError)

    def test_configparser_error_does_not_escape(self, db, monkeypatch):
        from urpm.core import media_pipeline
        from urpm.core.media_pipeline import upsert_media_tree

        monkeypatch.setattr(media_pipeline, "fetch_media_cfg",
                            lambda *a, **kw: E2GUARDIAN)
        try:
            upsert_media_tree(
                db, "http://mirror.example/unrecognised/", mode="discover")
        except configparser.Error:  # pragma: no cover - the defect
            pytest.fail("configparser error escaped upsert_media_tree")
        except Exception:
            pass


class TestTheCheckIsActuallyWired:
    """Testing the predicate is not testing its use.

    An earlier version of this file only called
    ``_reject_if_not_media_cfg`` directly: removing the call from
    ``fetch_media_cfg`` left every test green.  These drive the fetch
    itself, with pycurl stubbed at the transport.
    """

    @pytest.fixture
    def curl(self, monkeypatch):
        """Stub pycurl so ``fetch_media_cfg`` runs without a network."""
        import pycurl

        state = {"body": b"", "code": 200, "type": "text/html"}

        class FakeCurl:
            def __init__(self):
                self._write = None

            def setopt(self, opt, value):
                if opt == pycurl.WRITEFUNCTION:
                    self._write = value

            def perform(self):
                self._write(state["body"])

            def getinfo(self, info):
                if info == pycurl.HTTP_CODE:
                    return state["code"]
                if info == pycurl.CONTENT_TYPE:
                    return state["type"]
                return None

            def close(self):
                pass

        monkeypatch.setattr(pycurl, "Curl", FakeCurl)
        return state

    def test_intercepted_200_is_refused_by_the_fetch(self, curl):
        from urpm.core.media_cfg import fetch_media_cfg

        curl["body"] = E2GUARDIAN.encode()
        curl["code"] = 200
        curl["type"] = "text/html"

        with pytest.raises(RuntimeError, match="proxy or content filter"):
            fetch_media_cfg("http://mirror.example/media")

    def test_a_real_catalogue_comes_back_intact(self, curl):
        from urpm.core.media_cfg import fetch_media_cfg

        curl["body"] = REAL_MEDIA_CFG.encode()
        curl["code"] = 200
        curl["type"] = "application/octet-stream"

        assert fetch_media_cfg("http://mirror.example/media") == REAL_MEDIA_CFG

    def test_http_error_still_wins(self, curl):
        """The status check must keep priority: a 403 from the filter
        should read as a 403, not as « looks like HTML »."""
        from urpm.core.media_cfg import fetch_media_cfg

        curl["body"] = E2GUARDIAN.encode()
        curl["code"] = 403
        curl["type"] = "text/html"

        with pytest.raises(RuntimeError, match="HTTP 403"):
            fetch_media_cfg("http://mirror.example/media")
