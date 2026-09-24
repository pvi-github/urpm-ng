"""A distupgrade that fails to drop the old same-name package leaves
two versions installed at once.  A tester's machine came out of a
9 → 10 run with 133 mga9 leftovers among 2362 packages and twelve
duplicated names, ``gvfs`` among them.

``urpm autoremove`` then offered to remove 177 packages, and rpm
refused the transaction: ``gvfs-1.50.4-1.1.mga9`` still needed
``libbluray.so.2``, ``libnfs.so.14`` and ``libgcr-base-3.so.1``, all
three provided by packages in the list.

The cause was not the leftovers themselves.  The detector indexed
every dependency map on the package *name* with a plain assignment, so
the second header rpm handed it overwrote the first.  The mga10 gvfs
needs none of those three libraries; the mga9 one needs all three, and
its requires were the ones discarded.  Their providers then looked
unreferenced and were classified as orphans.

The tell was an asymmetry in the same loop: ``provides_map``
accumulated with ``setdefault``, the requires did not.  What follows
pins the union behaviour on all three detection paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import rpm

from urpm.core.resolution.orphans import OrphansMixin


def _hdr(name, version, release, *, requires=(), provides=(),
         recommends=(), suggests=(), supplements=(), arch='x86_64'):
    """A stand-in for an rpm header, indexable by RPMTAG_*."""
    tags = {
        rpm.RPMTAG_NAME: name,
        rpm.RPMTAG_VERSION: version,
        rpm.RPMTAG_RELEASE: release,
        rpm.RPMTAG_ARCH: arch,
        rpm.RPMTAG_EPOCH: None,
        rpm.RPMTAG_SIZE: 1024,
        rpm.RPMTAG_REQUIRENAME: list(requires),
        rpm.RPMTAG_PROVIDENAME: list(provides),
        rpm.RPMTAG_RECOMMENDNAME: list(recommends),
        rpm.RPMTAG_SUGGESTNAME: list(suggests),
        rpm.RPMTAG_SUPPLEMENTNAME: list(supplements),
        rpm.RPMTAG_SUMMARY: '',
        rpm.RPMTAG_GROUP: '',
    }
    h = MagicMock()
    h.__getitem__ = lambda _self, tag: tags.get(tag)
    return h


#: The tester's situation, reduced to what matters.  Two ``gvfs`` are
#: installed; only the old one still needs ``libbluray.so.2``.
#: ``with_player`` adds a second, visible requirer of the same library.
#: The erase-driven paths need one to have something to erase; the
#: whole-system scan must NOT have one, or that requirer would protect
#: the library on its own and hide the defect under test.
def _rpmdb_with_duplicate_gvfs(with_player=False):
    pkgs = [
        _hdr('gvfs', '1.50.4', '1.1.mga9',
             requires=['libbluray.so.2()(64bit)'],
             provides=['gvfs']),
        _hdr('gvfs', '1.58.0', '4.mga10',
             requires=['libbluray.so.6()(64bit)'],
             provides=['gvfs']),
        _hdr('lib64bluray2', '1.3.4', '1.mga9',
             provides=['libbluray.so.2()(64bit)', 'lib64bluray2']),
        _hdr('lib64bluray6', '1.4.0', '1.mga10',
             provides=['libbluray.so.6()(64bit)', 'lib64bluray6']),
    ]
    if with_player:
        pkgs.append(_hdr('player', '1.0', '1.mga10',
                         requires=['libbluray.so.2()(64bit)'],
                         provides=['player']))
    return pkgs


class _Detector(OrphansMixin):
    """Minimal host for the mixin, with the on-disk state stubbed out."""

    def __init__(self, tmp_path, unrequested):
        self.root = None
        self._tmp = tmp_path
        self._unrequested = {n.lower() for n in unrequested}

    def _get_unrequested_packages(self):
        return set(self._unrequested)

    def _get_builddep_packages(self):
        return {}

    def _get_unrequested_file(self):
        return self._tmp / 'unrequested'

    def _get_builddeps_file(self):
        return self._tmp / 'builddeps'


@pytest.fixture
def detector(tmp_path):
    # Both libraries came in as dependencies, so both are eligible to
    # be reported as orphans; only the protection differs.
    return _Detector(tmp_path, {'lib64bluray2', 'lib64bluray6'})


@pytest.fixture
def duplicate_rpmdb():
    ts = MagicMock()
    ts.dbMatch.return_value = _rpmdb_with_duplicate_gvfs()
    ctx = MagicMock()
    ctx.__enter__ = lambda _self: ts
    ctx.__exit__ = lambda *a: False
    with patch('urpm.core.rpmdb.open_ts', return_value=ctx):
        yield ts


class TestADuplicateNameKeepsBothSetsOfDependencies:

    def test_the_old_version_still_protects_its_library(
            self, detector, duplicate_rpmdb):
        """The heart of it: ``lib64bluray2`` is needed by the mga9 gvfs
        and must not be offered for removal, whichever header rpm
        enumerated last."""
        names = {o.name for o in detector.find_all_orphans()}
        assert 'lib64bluray2' not in names

    def test_a_library_nothing_needs_is_still_found(
            self, detector, duplicate_rpmdb):
        """Guards the other direction: the fix must not simply protect
        everything, or autoremove would stop doing its job."""
        detector._unrequested.add('lib64unused')
        duplicate_rpmdb.dbMatch.return_value = (
            _rpmdb_with_duplicate_gvfs()
            + [_hdr('lib64unused', '1.0', '1.mga10',
                    provides=['libunused.so.1()(64bit)', 'lib64unused'])]
        )
        names = {o.name for o in detector.find_all_orphans()}
        assert 'lib64unused' in names

    def test_enumeration_order_does_not_change_the_verdict(
            self, detector, duplicate_rpmdb):
        """rpm gives no ordering guarantee between two headers sharing
        a name.  Before the fix the answer depended on it."""
        forward = {o.name for o in detector.find_all_orphans()}
        duplicate_rpmdb.dbMatch.return_value = list(
            reversed(_rpmdb_with_duplicate_gvfs()))
        backward = {o.name for o in detector.find_all_orphans()}
        assert forward == backward


class TestTheOtherTwoDetectionPaths:
    """``find_all_orphans`` is what ``urpm autoremove`` calls, but the
    same indexing sat in two more places, reachable by other verbs.

    Both are driven by an erasure: removing ``player`` makes
    ``lib64bluray2`` a candidate, since ``player`` was the visible
    requirer.  The old ``gvfs`` needs it too, and that is the edge the
    name-keyed overwrite used to destroy.
    """

    def test_iterative_detection_keeps_both_requires(
            self, detector, duplicate_rpmdb):
        duplicate_rpmdb.dbMatch.return_value = _rpmdb_with_duplicate_gvfs(
            with_player=True)
        orphans = detector._find_orphans_iterative({'player'})
        assert 'lib64bluray2' not in {o.name for o in orphans}

    def test_erase_driven_detection_keeps_both_requires(
            self, detector, duplicate_rpmdb):
        duplicate_rpmdb.dbMatch.return_value = _rpmdb_with_duplicate_gvfs(
            with_player=True)
        orphans = detector.find_erase_orphans(['player'])
        assert 'lib64bluray2' not in {o.name for o in orphans}
