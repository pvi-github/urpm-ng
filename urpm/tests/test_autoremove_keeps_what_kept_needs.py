"""``urpm autoremove`` sorts its orphans into three piles: blocked by
the blacklist, warned by the redlist, and safe.  Only the safe pile is
removed.

Taking a package out of the removal list does not take out what it
depends on, though.  Declining to remove ``dhcp-client`` left
``dhcp-common`` in the list, and rpm refused the whole transaction
after the operator had already confirmed it:

    Dependency: dhcp-client-3:4.4.3P1-7.mga10
                requires dhcp-common = 3:4.4.3P1-7.mga10

The blacklist pile has the same exposure and is worse, because there
is no prompt at all: the operator never saw a question.

``needed_by_kept`` answers "what do the survivors still need that we
were about to remove", transitively, since a rescued package can itself
depend on another candidate.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import rpm

from urpm.core.resolution.orphans import OrphansMixin


def _hdr(name, *, requires=(), provides=()):
    tags = {
        rpm.RPMTAG_NAME: name,
        rpm.RPMTAG_REQUIRENAME: list(requires),
        rpm.RPMTAG_PROVIDENAME: list(provides) or [name],
    }
    h = MagicMock()
    h.__getitem__ = lambda _self, tag: tags.get(tag)
    return h


class _Detector(OrphansMixin):
    def __init__(self):
        self.root = None


#: The run that failed, reduced.  ``dhcp-client`` is redlisted and kept;
#: ``dhcp-common`` was queued for removal.  ``lib64dhcp`` sits one level
#: further down to exercise the transitive case.
def _dhcp_rpmdb():
    return [
        _hdr('dhcp-client', requires=['dhcp-common']),
        _hdr('dhcp-common', requires=['libdhcp.so.1()(64bit)']),
        _hdr('lib64dhcp', provides=['libdhcp.so.1()(64bit)', 'lib64dhcp']),
        _hdr('unrelated-orphan'),
    ]


@pytest.fixture
def rpmdb():
    ts = MagicMock()
    ts.dbMatch.return_value = _dhcp_rpmdb()
    ctx = MagicMock()
    ctx.__enter__ = lambda _self: ts
    ctx.__exit__ = lambda *a: False
    with patch('urpm.core.rpmdb.open_ts', return_value=ctx):
        yield ts


CANDIDATES = {'dhcp-common', 'lib64dhcp', 'unrelated-orphan'}


class TestWhatTheSurvivorsStillNeed:

    def test_a_direct_dependency_is_rescued(self, rpmdb):
        """The reported failure: keep dhcp-client, keep dhcp-common."""
        rescued = _Detector().needed_by_kept(CANDIDATES, {'dhcp-client'})
        assert 'dhcp-common' in rescued

    def test_the_rescue_follows_the_chain(self, rpmdb):
        """Rescuing dhcp-common is pointless if the library it needs
        goes out in the same transaction."""
        rescued = _Detector().needed_by_kept(CANDIDATES, {'dhcp-client'})
        assert 'lib64dhcp' in rescued

    def test_an_unrelated_orphan_is_still_removed(self, rpmdb):
        """The guard against over-rescuing: autoremove must keep doing
        its job for everything the survivors do not need."""
        rescued = _Detector().needed_by_kept(CANDIDATES, {'dhcp-client'})
        assert 'unrelated-orphan' not in rescued

    def test_a_blacklisted_package_rescues_just_the_same(self, rpmdb):
        """Blacklist and redlist differ only in whether the operator
        was asked; the dependency consequence is identical."""
        rescued = _Detector().needed_by_kept(CANDIDATES, {'dhcp-client'})
        assert rescued == {'dhcp-common', 'lib64dhcp'}


class TestWhenThereIsNothingToDo:

    def test_nothing_kept_means_no_rpmdb_walk(self, rpmdb):
        """The common case is an autoremove with an empty redlist
        intersection; it must not pay for a full rpmdb read."""
        assert _Detector().needed_by_kept(CANDIDATES, set()) == set()
        assert not rpmdb.dbMatch.called

    def test_nothing_queued_means_no_rpmdb_walk(self, rpmdb):
        assert _Detector().needed_by_kept(set(), {'dhcp-client'}) == set()
        assert not rpmdb.dbMatch.called
