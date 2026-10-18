"""The daemon must not spin when it runs out of file descriptors.

``socketserver`` swallows an ``OSError`` raised by ``accept()`` and
returns immediately, while the connection it failed to accept keeps the
listening socket readable.  ``serve_forever`` then selects, fails to
accept, and selects again with nothing in between: on descriptor
exhaustion that is a busy loop.  A tester's daemon held a core at 100%
for days, and since the error was swallowed nothing in the logs said
why.

The exhaustion itself was a leak, fixed in
``PackageDatabase._acquire_connection``.  What is tested here is the
behaviour when it happens anyway: stand down briefly, and say so.
"""

from __future__ import annotations

import errno
import socketserver

import pytest

from urpm.daemon import server as server_module
from urpm.daemon.server import (
    EMFILE_BACKOFF_SECONDS,
    ResilientThreadingHTTPServer,
)


@pytest.fixture
def unbound_server():
    """An instance without a listening socket.

    ``get_request`` is the whole subject, and binding a port to test it
    would make the test depend on a free one.
    """
    return object.__new__(ResilientThreadingHTTPServer)


@pytest.fixture
def accept_raising(monkeypatch):
    """Make the inherited ``get_request`` fail with a chosen errno."""
    def arrange(code):
        def boom(self):
            raise OSError(code, "arranged for the test")
        monkeypatch.setattr(socketserver.TCPServer, "get_request", boom)
    return arrange


@pytest.fixture
def slept(monkeypatch):
    """Record the back-off instead of serving it."""
    recorded = []
    monkeypatch.setattr(server_module.time, "sleep", recorded.append)
    return recorded


def test_running_out_of_descriptors_stands_the_loop_down(
        unbound_server, accept_raising, slept):
    accept_raising(errno.EMFILE)

    with pytest.raises(OSError) as raised:
        unbound_server.get_request()

    # Re-raised, because socketserver's own handler is what skips this
    # round; swallowing it here would accept a connection we do not have.
    assert raised.value.errno == errno.EMFILE
    assert slept == [EMFILE_BACKOFF_SECONDS]


def test_the_system_wide_limit_counts_too(
        unbound_server, accept_raising, slept):
    """``ENFILE`` is the same situation with a different culprit."""
    accept_raising(errno.ENFILE)

    with pytest.raises(OSError):
        unbound_server.get_request()
    assert slept == [EMFILE_BACKOFF_SECONDS]


def test_an_ordinary_accept_failure_is_not_delayed(
        unbound_server, accept_raising, slept):
    """A client that goes away is routine and must cost nothing.

    Sleeping on every failed accept would throttle a daemon serving
    peers that hang up, which is the common case this must not touch.
    """
    accept_raising(errno.ECONNABORTED)

    with pytest.raises(OSError):
        unbound_server.get_request()
    assert slept == []


def test_the_back_off_is_short_enough_to_stay_responsive():
    """Between a spinning loop and a daemon that stops answering."""
    assert 0 < EMFILE_BACKOFF_SECONDS <= 1.0


def test_a_successful_accept_is_handed_through(unbound_server, monkeypatch,
                                               slept):
    sentinel = ("socket", ("127.0.0.1", 40404))
    monkeypatch.setattr(socketserver.TCPServer, "get_request",
                        lambda self: sentinel)

    assert unbound_server.get_request() is sentinel
    assert slept == []
