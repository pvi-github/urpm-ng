"""Tests for the installed-vs-available split used by PackageKit.

``get_packages_by_names()`` backs the PackageKit ``Resolve`` verb, and
``search()`` backs ``SearchNames``.  Both used to answer "is this
installed?" by package *name*, then hand back a **media** row's version
under that flag.  Since a medium routinely carries several versions of a
name, Discover ended up displaying an arbitrary old media row as the
version the user has: ``firefox 140.12.0 → 153.2.0`` on a machine
running 153.1.0.

These tests pin the two properties that fix requires:

* the version reported as installed comes from the rpmdb, never from a
  medium;
* the version offered as available is the highest one the media carry in
  *RPM* ordering, not the first row SQLite happens to return.
"""

import tempfile
from pathlib import Path

import pytest

from urpm.core.database import InstalledRpm, PackageDatabase


@pytest.fixture
def rpmdb_generation(monkeypatch):
    """Stand in for the real rpmdb signature, under test control.

    ``_get_installed_index`` keys its cache on the mtimes of the rpmdb
    backing files. Pinning that to a value the test owns keeps the suite
    off the host's ``/var/lib/rpm`` and lets a test simulate "someone ran
    a transaction from a terminal" by bumping it.
    """
    generation = [0]
    monkeypatch.setattr('urpm.core.database.rpmdb_signature',
                        lambda root="/": (generation[0],))
    return generation


@pytest.fixture
def db(monkeypatch, rpmdb_generation):
    """Temporary database whose media pass the system version filter."""
    monkeypatch.setattr(
        'urpm.core.config.get_system_version', lambda root=None: '9')

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as handle:
        db_path = Path(handle.name)

    database = PackageDatabase(db_path)
    # Pre-seed the rpmdb cache so no test ever shells out to the real
    # ``rpm -qa`` of the machine running the suite.
    database._installed_cache = {}
    database._installed_cache_signature = (rpmdb_generation[0],)
    yield database

    database.close()
    db_path.unlink(missing_ok=True)


def _media(db, name, path):
    """Register one medium and return its id."""
    return db.add_media(
        name=name,
        short_name=name.lower().replace(' ', '_'),
        mageia_version="9",
        architecture="x86_64",
        relative_path=path,
    )


def _package(name, version, release, arch="x86_64", summary="A package"):
    """Build one synthesis-shaped package dict."""
    return {
        'name': name,
        'version': version,
        'release': release,
        'epoch': 0,
        'arch': arch,
        'nevra': f"{name}-{version}-{release}.{arch}",
        'summary': summary,
        'provides': [name],
        'requires': [],
        'filesize': 1000,
    }


def _installed(db, name, version, release, arch="x86_64", summary="A package"):
    """Declare ``name`` installed at that exact build, rpmdb-side."""
    db._installed_cache.setdefault(name, []).append(
        InstalledRpm(version=version, release=release, arch=arch,
                     summary=summary))


def _sees_installed(db, name, version, release, arch="x86_64"):
    """Does the cached rpmdb view hold that exact build right now?"""
    return (name, version, release, arch) in db._installed_build_keys()


class TestResolveInstalledFace:
    """The entry flagged ``installed`` must mirror the rpmdb."""

    def test_installed_version_comes_from_rpmdb_not_media(self, db):
        """The oldest media row must not be passed off as installed.

        This is the reported bug, reduced: the media carry three builds,
        the machine runs the newest, and the first row SQLite returns is
        the oldest one.
        """
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('firefox', '140.12.0', '1.mga9'),
            _package('firefox', '153.1.0', '1.mga9'),
        ]), media_id=media_id)
        _installed(db, 'firefox', '153.1.0', '1.mga9')

        rows = db.get_packages_by_names(['firefox'])

        installed = [r for r in rows if r['installed']]
        assert len(installed) == 1
        assert installed[0]['version'] == '153.1.0'
        assert installed[0]['release'] == '1.mga9'

    def test_installed_and_candidate_both_returned(self, db):
        """A client needs both faces to render "from → to"."""
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('firefox', '153.1.0', '1.mga9'),
            _package('firefox', '153.2.0', '1.mga9'),
        ]), media_id=media_id)
        _installed(db, 'firefox', '153.1.0', '1.mga9')

        rows = db.get_packages_by_names(['firefox'])

        assert len(rows) == 2
        assert [r['version'] for r in rows if r['installed']] == ['153.1.0']
        assert [r['version'] for r in rows if not r['installed']] == ['153.2.0']

    def test_no_candidate_when_installed_build_is_the_newest(self, db):
        """Nothing to offer means one entry, not a duplicate pair."""
        media_id = _media(db, "Core Release", "core/release")
        db.import_packages(iter([
            _package('urpm-ng', '0.9.9', '3.mga9'),
        ]), media_id=media_id)
        _installed(db, 'urpm-ng', '0.9.9', '3.mga9')

        rows = db.get_packages_by_names(['urpm-ng'])

        assert len(rows) == 1
        assert rows[0]['installed'] is True
        assert rows[0]['version'] == '0.9.9'

    def test_installed_package_absent_from_media_is_still_reported(self, db):
        """A locally built package used to vanish from Resolve entirely."""
        _media(db, "Core Release", "core/release")
        _installed(db, 'my-local-tool', '1.0', '1', summary='Home grown')

        rows = db.get_packages_by_names(['my-local-tool'])

        assert len(rows) == 1
        assert rows[0]['installed'] is True
        assert rows[0]['version'] == '1.0'
        assert rows[0]['summary'] == 'Home grown'

    def test_installed_entry_carries_no_repository(self, db):
        """No medium backs an installed build, so ``media_name`` is empty.

        The PackageKit backend stamps the ``package_id`` data field with
        the literal ``installed`` in that case, so an empty repository is
        the accurate answer rather than a missing one.
        """
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('firefox', '153.1.0', '1.mga9'),
            _package('firefox', '153.2.0', '1.mga9'),
        ]), media_id=media_id)
        _installed(db, 'firefox', '153.1.0', '1.mga9')

        rows = db.get_packages_by_names(['firefox'])

        installed = next(r for r in rows if r['installed'])
        available = next(r for r in rows if not r['installed'])
        assert installed['media_name'] == ''
        assert available['media_name'] == "Core Updates"

    def test_each_installed_architecture_gets_its_own_entry(self, db):
        """A name is not a unique key in the rpmdb."""
        media_id = _media(db, "Core Release", "core/release")
        db.import_packages(iter([
            _package('libfoo', '1.0', '1.mga9', arch='x86_64'),
        ]), media_id=media_id)
        _installed(db, 'libfoo', '1.0', '1.mga9', arch='x86_64')
        _installed(db, 'libfoo', '1.0', '1.mga9', arch='i586')

        rows = db.get_packages_by_names(['libfoo'])

        assert sorted(r['arch'] for r in rows) == ['i586', 'x86_64']
        assert all(r['installed'] for r in rows)


class TestResolveCandidateOrdering:
    """The candidate is the highest EVR in RPM ordering."""

    def test_candidate_uses_rpm_ordering_not_insertion_order(self, db):
        """Insert the low version first so rowid order gives the wrong answer."""
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('brltty', '7.6.7.2', '1.mga9'),
            _package('brltty', '24.2.7.2', '1.mga9'),
        ]), media_id=media_id)

        rows = db.get_packages_by_names(['brltty'])

        assert len(rows) == 1
        assert rows[0]['version'] == '24.2.7.2'

    def test_candidate_uses_rpm_ordering_on_release_too(self, db):
        """``10.mga9`` outranks ``2.mga9``, lexicographic order says otherwise."""
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('zlib', '1.3', '10.mga9'),
            _package('zlib', '1.3', '2.mga9'),
        ]), media_id=media_id)

        rows = db.get_packages_by_names(['zlib'])

        assert rows[0]['release'] == '10.mga9'

    def test_candidate_spans_media(self, db):
        """The newest build wins whichever medium carries it."""
        release_id = _media(db, "Core Release", "core/release")
        updates_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('firefox', '153.2.0', '1.mga9'),
        ]), media_id=updates_id)
        db.import_packages(iter([
            _package('firefox', '140.11.0', '1.mga9'),
        ]), media_id=release_id)

        rows = db.get_packages_by_names(['firefox'])

        assert rows[0]['version'] == '153.2.0'
        assert rows[0]['media_name'] == "Core Updates"


class TestResolveContract:
    """Shape of the reply, which the C backend parses positionally."""

    def test_not_installed_yields_the_candidate_alone(self, db):
        media_id = _media(db, "Core Release", "core/release")
        db.import_packages(iter([
            _package('inkscape', '1.3', '1.mga9'),
        ]), media_id=media_id)

        rows = db.get_packages_by_names(['inkscape'])

        assert len(rows) == 1
        assert rows[0]['installed'] is False

    def test_unknown_name_is_absent(self, db):
        _media(db, "Core Release", "core/release")

        assert db.get_packages_by_names(['no-such-package']) == []

    def test_empty_input_short_circuits(self, db):
        assert db.get_packages_by_names([]) == []

    def test_results_follow_the_requested_order(self, db):
        media_id = _media(db, "Core Release", "core/release")
        db.import_packages(iter([
            _package('alpha', '1.0', '1.mga9'),
            _package('beta', '1.0', '1.mga9'),
            _package('gamma', '1.0', '1.mga9'),
        ]), media_id=media_id)

        rows = db.get_packages_by_names(['gamma', 'alpha', 'beta'])

        assert [r['name'] for r in rows] == ['gamma', 'alpha', 'beta']


class TestSearchInstalledFlag:
    """``search()`` flagged every version of a name as installed at once."""

    def test_only_the_installed_build_is_flagged(self, db):
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('firefox', '140.12.0', '1.mga9'),
            _package('firefox', '153.1.0', '1.mga9'),
            _package('firefox', '153.2.0', '1.mga9'),
        ]), media_id=media_id)
        _installed(db, 'firefox', '153.1.0', '1.mga9')

        flagged = {(p['version'], p['installed'])
                   for p in db.search('firefox') if p['name'] == 'firefox'}

        assert flagged == {
            ('140.12.0', False),
            ('153.1.0', True),
            ('153.2.0', False),
        }

    def test_nothing_is_flagged_when_the_package_is_absent(self, db):
        media_id = _media(db, "Core Release", "core/release")
        db.import_packages(iter([
            _package('firefox', '153.1.0', '1.mga9'),
        ]), media_id=media_id)

        assert all(not p['installed'] for p in db.search('firefox'))

    def test_a_different_architecture_is_not_flagged(self, db):
        """Same build, other arch: available, not installed."""
        media_id = _media(db, "Core Release", "core/release")
        db.import_packages(iter([
            _package('libfoo', '1.0', '1.mga9', arch='i586'),
        ]), media_id=media_id)
        _installed(db, 'libfoo', '1.0', '1.mga9', arch='x86_64')

        results = [p for p in db.search('libfoo') if p['name'] == 'libfoo']
        assert results and all(not p['installed'] for p in results)


class TestInstalledIndex:
    """Parsing of ``rpm -qa`` into the cached index."""

    @staticmethod
    def _fake_rpm(monkeypatch, stdout, returncode=0):
        class _Completed:
            def __init__(self):
                self.stdout = stdout
                self.returncode = returncode

        monkeypatch.setattr('subprocess.run',
                            lambda *args, **kwargs: _Completed())

    def test_gpg_pubkey_entries_are_dropped(self, db, monkeypatch):
        """Their arch is ``(none)``, which is illegal in a package_id."""
        db._installed_cache = None
        self._fake_rpm(monkeypatch,
                       "gpg-pubkey\t80420f66\t4d4fe123\t(none)\tgpg key\n"
                       "firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n")

        index = db._get_installed_index()

        assert 'gpg-pubkey' not in index
        assert index['firefox'][0].arch == 'x86_64'

    def test_a_tab_inside_the_summary_is_preserved(self, db, monkeypatch):
        """Summary comes last precisely because RPM does not escape it."""
        db._installed_cache = None
        self._fake_rpm(monkeypatch,
                       "weird\t1.0\t1.mga9\tx86_64\tone\ttwo\n")

        assert db._get_installed_index()['weird'][0].summary == 'one\ttwo'

    def test_a_short_line_is_ignored(self, db, monkeypatch):
        db._installed_cache = None
        self._fake_rpm(monkeypatch, "truncated\t1.0\n")

        assert db._get_installed_index() == {}

    def test_an_rpm_failure_degrades_to_an_empty_index(self, db, monkeypatch):
        """A package manager UI may show nothing; it may not crash."""
        db._installed_cache = None
        self._fake_rpm(monkeypatch, "", returncode=1)

        assert db._get_installed_index() == {}
        assert db._is_installed('firefox') is False

    def test_an_rpm_crash_degrades_to_an_empty_index(self, db, monkeypatch):
        db._installed_cache = None

        def _boom(*args, **kwargs):
            raise OSError("rpm is gone")

        monkeypatch.setattr('subprocess.run', _boom)

        assert db._get_installed_index() == {}

    def test_the_index_is_read_once(self, db, monkeypatch):
        db._installed_cache = None
        calls = []

        class _Completed:
            stdout = "firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n"
            returncode = 0

        def _counted(*args, **kwargs):
            calls.append(args)
            return _Completed()

        monkeypatch.setattr('subprocess.run', _counted)

        db._get_installed_index()
        db._get_installed_index()

        assert len(calls) == 1

    def test_invalidating_forces_a_re_read(self, db, monkeypatch):
        """The explicit path, for a caller that ran the transaction itself."""
        db._installed_cache = None
        stdout = ["firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n"]

        class _Completed:
            def __init__(self, text):
                self.stdout = text
                self.returncode = 0

        monkeypatch.setattr('subprocess.run',
                            lambda *a, **k: _Completed(stdout[0]))

        assert _sees_installed(db, 'firefox', '153.1.0', '1.mga9')

        stdout[0] = "firefox\t153.2.0\t1.mga9\tx86_64\tWeb browser\n"
        assert _sees_installed(db, 'firefox', '153.1.0', '1.mga9'), \
            "an untouched rpmdb must not cost a re-read"

        db.invalidate_installed_cache()
        assert _sees_installed(db, 'firefox', '153.2.0', '1.mga9')
        assert not _sees_installed(db, 'firefox', '153.1.0', '1.mga9')


class TestStaleAfterOutsideTransaction:
    """A long-lived service must notice ``urpm install`` run in a terminal.

    ``urpm-dbus.service`` and ``urpmd`` hold one ``PackageDatabase`` for
    hours. Nothing tells them when the user installs something from a
    shell, so the cached rpmdb view expires on the rpmdb's own mtimes
    rather than on an explicit call.
    """

    class _Completed:
        def __init__(self, text):
            self.stdout = text
            self.returncode = 0

    def _rpm_returning(self, monkeypatch, lines, calls=None):
        def _run(*args, **kwargs):
            if calls is not None:
                calls.append(args)
            return self._Completed(lines[0])

        monkeypatch.setattr('subprocess.run', _run)

    def test_a_transaction_elsewhere_expires_the_cache(
            self, db, monkeypatch, rpmdb_generation):
        db._installed_cache = None
        lines = ["firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n"]
        self._rpm_returning(monkeypatch, lines)

        assert _sees_installed(db, 'firefox', '153.1.0', '1.mga9')

        # Someone upgrades firefox from a shell: the rpmdb files move.
        lines[0] = "firefox\t153.2.0\t1.mga9\tx86_64\tWeb browser\n"
        rpmdb_generation[0] += 1

        assert _sees_installed(db, 'firefox', '153.2.0', '1.mga9')
        assert not _sees_installed(db, 'firefox', '153.1.0', '1.mga9')

    def test_an_untouched_rpmdb_is_not_re_read(
            self, db, monkeypatch, rpmdb_generation):
        db._installed_cache = None
        calls = []
        self._rpm_returning(
            monkeypatch,
            ["firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n"],
            calls,
        )

        for _ in range(5):
            db._get_installed_index()

        assert len(calls) == 1

    def test_resolve_reflects_a_transaction_run_elsewhere(
            self, db, monkeypatch, rpmdb_generation):
        """The property as a user of Discover would observe it."""
        media_id = _media(db, "Core Updates", "core/updates")
        db.import_packages(iter([
            _package('firefox', '153.1.0', '1.mga9'),
            _package('firefox', '153.2.0', '1.mga9'),
        ]), media_id=media_id)

        db._installed_cache = None
        lines = ["firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n"]
        self._rpm_returning(monkeypatch, lines)

        before = db.get_packages_by_names(['firefox'])
        assert [(r['version'], r['installed']) for r in before] == [
            ('153.1.0', True), ('153.2.0', False)]

        lines[0] = "firefox\t153.2.0\t1.mga9\tx86_64\tWeb browser\n"
        rpmdb_generation[0] += 1

        after = db.get_packages_by_names(['firefox'])
        assert [(r['version'], r['installed']) for r in after] == [
            ('153.2.0', True)]

    def test_a_transaction_landing_mid_read_is_not_swallowed(
            self, db, monkeypatch, rpmdb_generation):
        """The signature is sampled before the query, deliberately.

        Sampled after, a transaction committing while ``rpm -qa`` runs
        would stamp the fresh signature onto a pre-transaction read, and
        that staleness would never expire.
        """
        db._installed_cache = None
        calls = []

        def _run(*args, **kwargs):
            calls.append(args)
            # The rpmdb moves while we are reading it.
            rpmdb_generation[0] += 1
            return self._Completed(
                "firefox\t153.1.0\t1.mga9\tx86_64\tWeb browser\n")

        monkeypatch.setattr('subprocess.run', _run)

        db._get_installed_index()
        db._get_installed_index()

        assert len(calls) == 2, "the racing read must not be trusted"
