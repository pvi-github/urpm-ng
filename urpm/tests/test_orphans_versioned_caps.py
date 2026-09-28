"""Two versions of the same capability, side by side on one system.

A GNOME desktop coming out of a 9 to 10 distupgrade carries
``lib64gnome-desktop-gir3.0`` and ``lib64gnome-desktop-gir4.0``
together: both provide ``typelib(GnomeDesktop)``, at ``3.0`` and
``4.0``, and each consumer asks for one precise version.

The detectors used to index the capability *name* alone.  Every
consumer of the 4.0 typelib therefore looked like a consumer of the
3.0 one, the dead package was reported as still needed, and nothing
ever proposed its removal.  On a tester's machine ``urpme
--auto-orphans`` offered the two leftovers while ``urpm autoremove``
found nothing at all, which is what sent us looking.

The shape is not specific to typelibs: ``lua(abi) = 5.1`` against
``lua(abi) = 5.4`` produces it too, and so does any library whose
soname is carried by parallel-installable packages.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import rpm

from urpm.core.resolution.orphans import OrphansMixin


EQ = rpm.RPMSENSE_EQUAL


def _hdr(name, version, release, *, requires=(), provides=(),
         recommends=(), suggests=(), supplements=(), arch='x86_64'):
    """A stand-in for an rpm header whose dependencies carry versions.

    Each dependency is given as ``(name, sense, evr)`` and split back
    into the three parallel arrays rpm stores.
    """
    def arrays(rows):
        names, senses, evrs = [], [], []
        for row in rows:
            if isinstance(row, str):
                row = (row, 0, '')
            names.append(row[0])
            senses.append(row[1])
            evrs.append(row[2])
        return names, senses, evrs

    req_names, req_senses, req_evrs = arrays(requires)
    prov_names, prov_senses, prov_evrs = arrays(provides)
    rec_names, rec_senses, rec_evrs = arrays(recommends)
    sug_names, sug_senses, sug_evrs = arrays(suggests)
    sup_names, sup_senses, sup_evrs = arrays(supplements)

    tags = {
        rpm.RPMTAG_NAME: name,
        rpm.RPMTAG_VERSION: version,
        rpm.RPMTAG_RELEASE: release,
        rpm.RPMTAG_ARCH: arch,
        rpm.RPMTAG_EPOCH: None,
        rpm.RPMTAG_SIZE: 1024,
        rpm.RPMTAG_REQUIRENAME: req_names,
        rpm.RPMTAG_REQUIREFLAGS: req_senses,
        rpm.RPMTAG_REQUIREVERSION: req_evrs,
        rpm.RPMTAG_PROVIDENAME: prov_names,
        rpm.RPMTAG_PROVIDEFLAGS: prov_senses,
        rpm.RPMTAG_PROVIDEVERSION: prov_evrs,
        rpm.RPMTAG_RECOMMENDNAME: rec_names,
        rpm.RPMTAG_RECOMMENDFLAGS: rec_senses,
        rpm.RPMTAG_RECOMMENDVERSION: rec_evrs,
        rpm.RPMTAG_SUGGESTNAME: sug_names,
        rpm.RPMTAG_SUGGESTFLAGS: sug_senses,
        rpm.RPMTAG_SUGGESTVERSION: sug_evrs,
        rpm.RPMTAG_SUPPLEMENTNAME: sup_names,
        rpm.RPMTAG_SUPPLEMENTFLAGS: sup_senses,
        rpm.RPMTAG_SUPPLEMENTVERSION: sup_evrs,
        rpm.RPMTAG_SUMMARY: '',
        rpm.RPMTAG_GROUP: '',
    }
    header = MagicMock()
    header.__getitem__ = lambda _self, tag: tags.get(tag)
    return header


def _gnome_rpmdb():
    """The two typelibs, and one consumer of each.

    ``gnome-shell`` is the explicitly installed desktop; it needs the
    4.0 typelib.  ``gnome-characters`` needs the 3.0 one, and is only
    added where a legitimate consumer is required.
    """
    return [
        _hdr('gnome-shell', '48.0', '1.mga10',
             requires=[('typelib(GnomeDesktop)', EQ, '4.0')],
             provides=[('gnome-shell', EQ, '48.0-1.mga10')]),
        _hdr('lib64gnome-desktop-gir3.0', '44.4', '1.mga10',
             provides=[('typelib(GnomeDesktop)', EQ, '3.0'),
                       ('lib64gnome-desktop-gir3.0', EQ, '44.4-1.mga10')]),
        _hdr('lib64gnome-desktop-gir4.0', '48.0', '1.mga10',
             provides=[('typelib(GnomeDesktop)', EQ, '4.0'),
                       ('lib64gnome-desktop-gir4.0', EQ, '48.0-1.mga10')]),
    ]


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
    # Both typelibs came in as dependencies of the desktop.
    return _Detector(tmp_path, {'lib64gnome-desktop-gir3.0',
                                'lib64gnome-desktop-gir4.0'})


def _rpmdb(headers):
    ts = MagicMock()
    ts.dbMatch.return_value = headers
    ctx = MagicMock()
    ctx.__enter__ = lambda _self: ts
    ctx.__exit__ = lambda *a: False
    return patch('urpm.core.rpmdb.open_ts', return_value=ctx)


class TestTheWholeSystemScan:
    """``urpm autoremove``, and every verb that reads an orphan status."""

    def test_the_stale_typelib_is_an_orphan(self, detector):
        with _rpmdb(_gnome_rpmdb()):
            orphans = {o.name for o in detector.find_all_orphans()}

        assert 'lib64gnome-desktop-gir3.0' in orphans

    def test_the_one_actually_needed_is_kept(self, detector):
        with _rpmdb(_gnome_rpmdb()):
            orphans = {o.name for o in detector.find_all_orphans()}

        assert 'lib64gnome-desktop-gir4.0' not in orphans

    def test_a_consumer_of_the_old_version_protects_it(self, detector):
        # The same system with gnome-characters, which needs 3.0: the
        # package is legitimately alive and must not be offered.
        headers = _gnome_rpmdb() + [
            _hdr('gnome-characters', '46.0', '1.mga10',
                 requires=[('typelib(GnomeDesktop)', EQ, '3.0')],
                 provides=[('gnome-characters', EQ, '46.0-1.mga10')]),
        ]
        with _rpmdb(headers):
            orphans = {o.name for o in detector.find_all_orphans()}

        assert orphans == set()

    def test_an_unversioned_require_still_takes_any_provider(self, detector):
        # rpm's own rule, and the behaviour of every dependency that
        # carries no constraint at all: nothing here becomes an orphan.
        headers = _gnome_rpmdb()[1:] + [
            _hdr('gnome-shell', '48.0', '1.mga10',
                 requires=['typelib(GnomeDesktop)'],
                 provides=[('gnome-shell', EQ, '48.0-1.mga10')]),
        ]
        with _rpmdb(headers):
            orphans = {o.name for o in detector.find_all_orphans()}

        assert orphans == set()


class TestTheErasePath:
    """``urpm e``, which proposes the orphans of what it removes."""

    def test_only_the_freed_version_is_proposed(self, detector):
        # A desktop that uses both typelibs, and one other package still
        # using the 3.0 one.  Erasing the desktop frees the 4.0 typelib
        # and only that one: the 3.0 one keeps a live consumer.
        #
        # This is where the old rule cost a package: both typelibs were
        # candidates, so « is there another provider left? » answered no
        # for the 4.0 one as well, and it was kept for a consumer that
        # never wanted it.
        headers = [
            _hdr('gnome-shell', '48.0', '1.mga10',
                 requires=[('typelib(GnomeDesktop)', EQ, '4.0'),
                           ('typelib(GnomeDesktop)', EQ, '3.0')],
                 provides=[('gnome-shell', EQ, '48.0-1.mga10')]),
        ] + _gnome_rpmdb()[1:] + [
            _hdr('gnome-characters', '46.0', '1.mga10',
                 requires=[('typelib(GnomeDesktop)', EQ, '3.0')],
                 provides=[('gnome-characters', EQ, '46.0-1.mga10')]),
        ]
        with _rpmdb(headers):
            orphans = {o.name
                       for o in detector.find_erase_orphans(['gnome-shell'])}

        assert orphans == {'lib64gnome-desktop-gir4.0'}

    def test_the_iterative_path_agrees(self, detector):
        headers = _gnome_rpmdb() + [
            _hdr('gnome-characters', '46.0', '1.mga10',
                 requires=[('typelib(GnomeDesktop)', EQ, '3.0')],
                 provides=[('gnome-characters', EQ, '46.0-1.mga10')]),
        ]
        with _rpmdb(headers):
            orphans = {o.name
                       for o in detector._find_orphans_iterative({'gnome-shell'})}

        assert orphans == {'lib64gnome-desktop-gir4.0'}


class TestTheRescueOfWhatIsKept:
    """``needed_by_kept``, the interactive triage's safety net."""

    def test_a_kept_package_rescues_its_own_version_only(self, detector):
        headers = _gnome_rpmdb() + [
            _hdr('gnome-characters', '46.0', '1.mga10',
                 requires=[('typelib(GnomeDesktop)', EQ, '3.0')],
                 provides=[('gnome-characters', EQ, '46.0-1.mga10')]),
        ]
        candidates = ['lib64gnome-desktop-gir3.0', 'lib64gnome-desktop-gir4.0']
        with _rpmdb(headers):
            rescued = detector.needed_by_kept(candidates, ['gnome-characters'])

        assert rescued == {'lib64gnome-desktop-gir3.0'}
