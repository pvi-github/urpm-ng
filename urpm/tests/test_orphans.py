"""Tests for builddep tracking in OrphansMixin."""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class FakeResolver:
    """Minimal stand-in that inherits OrphansMixin through Resolver."""

    def __init__(self, tmpdir):
        # OrphansMixin uses self.root
        self.root = str(tmpdir)
        self.db = MagicMock()

    # --- file helpers (from OrphansMixin) ---

    def _get_builddeps_file(self):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._get_builddeps_file(self)

    def _get_builddep_packages(self):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._get_builddep_packages(self)

    def _save_builddep_packages(self, packages):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._save_builddep_packages(self, packages)

    def mark_as_builddep(self, package_names, source):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.mark_as_builddep(self, package_names, source)

    def unmark_builddep_packages(self, package_names):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.unmark_builddep_packages(self, package_names)

    def _get_unrequested_file(self):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._get_unrequested_file(self)

    def _get_unrequested_packages(self):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._get_unrequested_packages(self)

    def _save_unrequested_packages(self, packages):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._save_unrequested_packages(self, packages)

    def mark_as_explicit(self, package_names):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.mark_as_explicit(self, package_names)

    def _extract_cap_name(self, cap):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin._extract_cap_name(self, cap)

    def find_upgrade_orphans(self, all_actions):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.find_upgrade_orphans(self, all_actions)


@pytest.fixture
def resolver(tmp_path):
    """Create a FakeResolver with a temp root directory."""
    # Ensure var/lib/rpm exists
    (tmp_path / 'var' / 'lib' / 'rpm').mkdir(parents=True)
    return FakeResolver(tmp_path)


class TestBuilddepTracking:
    """Tests for builddep file read/write operations."""

    def test_get_builddeps_file_path(self, resolver):
        """_get_builddeps_file returns the correct path."""
        bd_file = resolver._get_builddeps_file()
        assert bd_file.name == 'installed-through-builddeps.list'
        assert 'var/lib/rpm' in str(bd_file)

    def test_mark_as_builddep(self, resolver):
        """mark_as_builddep writes a TSV file with name and source."""
        # Packages must be in unrequested to be marked as builddep
        resolver._save_unrequested_packages({'gcc-c++', 'cmake', 'make'})

        result = resolver.mark_as_builddep(['gcc-c++', 'cmake', 'make'], 'foo.spec')
        assert result is True

        bd = resolver._get_builddep_packages()
        assert bd['gcc-c++'] == 'foo.spec'
        assert bd['cmake'] == 'foo.spec'
        assert bd['make'] == 'foo.spec'

    def test_builddep_source_stored(self, resolver):
        """The source (spec/srpm basename) is correctly stored and retrieved."""
        resolver._save_unrequested_packages({'cmake'})
        resolver.mark_as_builddep(['cmake'], 'bar.src.rpm')

        bd = resolver._get_builddep_packages()
        assert bd['cmake'] == 'bar.src.rpm'

    def test_builddep_file_format(self, resolver):
        """The file uses TSV format with name<TAB>source."""
        resolver._save_unrequested_packages({'cmake', 'gcc-c++'})
        resolver.mark_as_builddep(['cmake', 'gcc-c++'], 'foo.spec')

        content = resolver._get_builddeps_file().read_text()
        lines = content.strip().split('\n')
        assert len(lines) == 2
        for line in lines:
            parts = line.split('\t')
            assert len(parts) == 2
            assert parts[1] == 'foo.spec'

    def test_unmark_builddep_packages(self, resolver):
        """unmark_builddep_packages removes entries from the file."""
        resolver._save_unrequested_packages({'cmake', 'gcc-c++'})
        resolver.mark_as_builddep(['cmake', 'gcc-c++'], 'foo.spec')

        resolver.unmark_builddep_packages(['cmake'])
        bd = resolver._get_builddep_packages()
        assert 'cmake' not in bd
        assert 'gcc-c++' in bd

    def test_unmark_nonexistent_is_safe(self, resolver):
        """unmark_builddep_packages handles packages not in the list."""
        result = resolver.unmark_builddep_packages(['nonexistent'])
        assert result is True

    def test_empty_builddep_file(self, resolver):
        """_get_builddep_packages returns empty dict when file doesn't exist."""
        bd = resolver._get_builddep_packages()
        assert bd == {}


class TestExplicitPromotion:
    """Tests for explicit install promoting builddep packages."""

    def test_explicit_install_promotes(self, resolver):
        """mark_as_explicit removes the package from builddeps list."""
        resolver._save_unrequested_packages({'cmake', 'gcc-c++'})
        resolver.mark_as_builddep(['cmake', 'gcc-c++'], 'foo.spec')

        # Simulate explicit install of cmake
        resolver.mark_as_explicit(['cmake'])

        # cmake should be gone from builddeps
        bd = resolver._get_builddep_packages()
        assert 'cmake' not in bd
        assert 'gcc-c++' in bd

        # cmake should also be gone from unrequested
        unreq = resolver._get_unrequested_packages()
        assert 'cmake' not in unreq

    def test_already_explicit_not_demoted(self, resolver):
        """A package already explicit should not be added to builddeps."""
        # gcc-c++ is NOT in unrequested (= explicitly installed)
        resolver._save_unrequested_packages({'cmake'})

        resolver.mark_as_builddep(['cmake', 'gcc-c++'], 'foo.spec')

        bd = resolver._get_builddep_packages()
        # cmake was in unrequested → should be marked
        assert 'cmake' in bd
        # gcc-c++ was explicit → should NOT be demoted
        assert 'gcc-c++' not in bd


class TestCaseInsensitive:
    """Tests for case-insensitive handling."""

    def test_case_insensitive_mark(self, resolver):
        """Package names are normalized to lowercase."""
        resolver._save_unrequested_packages({'gcc-c++'})
        resolver.mark_as_builddep(['GCC-C++'], 'foo.spec')

        bd = resolver._get_builddep_packages()
        assert 'gcc-c++' in bd

    def test_case_insensitive_unmark(self, resolver):
        """Unmark works case-insensitively."""
        resolver._save_unrequested_packages({'cmake'})
        resolver.mark_as_builddep(['cmake'], 'foo.spec')
        resolver.unmark_builddep_packages(['CMAKE'])

        bd = resolver._get_builddep_packages()
        assert 'cmake' not in bd

    def test_case_insensitive_promote(self, resolver):
        """Explicit promotion works case-insensitively."""
        resolver._save_unrequested_packages({'cmake'})
        resolver.mark_as_builddep(['cmake'], 'foo.spec')
        resolver.mark_as_explicit(['CMAKE'])

        bd = resolver._get_builddep_packages()
        assert 'cmake' not in bd


# ---------------------------------------------------------------------------
# find_upgrade_orphans regression tests
#
# These tests implement the formal specification directly against a mocked
# rpm database, with no real RPMs involved.  They are the unit-level
# counterpart of TestOrphans in test_install.py and are designed to catch
# drifts in any of the three clauses of the spec:
#
#     new_orphans(T) = { P ∈ S_post |
#         unrequested(P) ∧ orphan(P, S_post)
#         ∧ ¬(P ∈ S_pre ∧ orphan(P, S_pre)) }
#
# with ``orphan(P, S) ≡ ∀Q ∈ S, P ∉ Requires(Q) ∪ Recommends(Q)``.
# ---------------------------------------------------------------------------

rpm = pytest.importorskip('rpm')


def _fake_hdr(name, requires=(), recommends=(), provides=(),
              epoch=0, version='1', release='1', arch='noarch', size=0):
    """Build a minimal fake rpm header that supports ``hdr[rpm.RPMTAG_*]``."""
    tag_map = {
        rpm.RPMTAG_NAME: name,
        rpm.RPMTAG_REQUIRENAME: list(requires),
        rpm.RPMTAG_RECOMMENDNAME: list(recommends),
        rpm.RPMTAG_PROVIDENAME: list(provides),
        rpm.RPMTAG_EPOCH: epoch,
        rpm.RPMTAG_VERSION: version,
        rpm.RPMTAG_RELEASE: release,
        rpm.RPMTAG_ARCH: arch,
        rpm.RPMTAG_SIZE: size,
    }
    hdr = MagicMock()
    hdr.__getitem__.side_effect = lambda tag: tag_map.get(tag)
    return hdr


def _make_action(name, action, evr='2-1', arch='noarch', size=0):
    """Build a minimal stand-in for PackageAction."""
    act = MagicMock()
    act.name = name
    act.action = action
    act.evr = evr
    act.arch = arch
    act.nevra = f"{name}-{evr}.{arch}"
    act.size = size
    return act


class TestFindUpgradeOrphansSpec:
    """Regression tests enforcing the three clauses of the new_orphans spec.

    Each test pins one fix so that a future refactor cannot silently
    reintroduce the regression that commit 3bec30b brought (and that
    commit fixing this docstring undid).
    """

    def _run(self, resolver, headers, actions):
        """Patch rpm.TransactionSet and dispatch to find_upgrade_orphans."""
        from urpm.core.resolver import TransactionType
        with patch('urpm.core.resolution.orphans.rpm.TransactionSet') as ts_cls:
            ts_cls.return_value.dbMatch.return_value = headers
            return resolver.find_upgrade_orphans(actions), TransactionType

    def test_upgraded_package_can_become_orphan(self, resolver):
        """Fix #1: a package being upgraded must still be considered.

        Scenario: ``req_a`` and ``a`` are both available in v2 of a medium.
        Libsolv gratuitously upgrades ``a`` alongside ``req_a``, even
        though ``req_a-2`` no longer requires ``a``.  The pre-3bec30b
        skip ``if name in upgraded_names: continue`` hid ``a`` from
        detection; the spec requires flagging it as a new orphan.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'a'})
        resolver.db.get_package.side_effect = lambda name: {
            'req_a': {'requires': [], 'recommends': [], 'provides': ['req_a']},
            'a':     {'requires': [], 'recommends': [], 'provides': ['a']},
        }.get(name)

        headers = [
            _fake_hdr('req_a', requires=['a'], provides=['req_a']),
            _fake_hdr('a', provides=['a']),
        ]
        actions = [
            _make_action('req_a', TransactionType.UPGRADE),
            _make_action('a', TransactionType.UPGRADE),
        ]

        orphans, _ = self._run(resolver, headers, actions)
        assert {o.name for o in orphans} == {'a'}

    def test_newly_installed_dep_counted_as_unrequested(self, resolver):
        """Fix #2: newly installed packages feed ``effective_unrequested``.

        Scenario: an upgrade installs a new dep ``new_dep``, which in
        turn pulls ``leaf`` (also new, also a dep).  ``leaf`` is only
        required by ``new_dep``.  Without fix #2, ``new_dep`` would be
        treated as "explicit" during graph traversal, masking any
        orphan whose chain passes through it.  Here we verify the
        complementary case: ``leaf`` is NOT flagged because it has a
        real parent — and the parent's traversal correctly walks
        through ``new_dep`` instead of stopping at it.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages(set())
        resolver.db.get_package.side_effect = lambda name: {
            'user_app': {
                'requires': ['new_dep'], 'recommends': [],
                'provides': ['user_app'],
            },
            'new_dep': {
                'requires': ['leaf'], 'recommends': [],
                'provides': ['new_dep'],
            },
            'leaf': {
                'requires': [], 'recommends': [], 'provides': ['leaf'],
            },
        }.get(name)

        headers = [
            _fake_hdr('user_app', provides=['user_app']),
        ]
        actions = [
            _make_action('user_app', TransactionType.UPGRADE),
            _make_action('new_dep', TransactionType.INSTALL),
            _make_action('leaf', TransactionType.INSTALL),
        ]

        orphans, _ = self._run(resolver, headers, actions)
        assert orphans == [], (
            "new_dep is a legitimate dep of user_app and leaf is a "
            "legitimate dep of new_dep — none must be flagged"
        )

    def test_recommends_counted_as_dependency_edge(self, resolver):
        """Fix #3: Recommends are real edges in the orphan graph.

        Scenario: ``h-1`` Recommends ``hh``.  After upgrading ``h`` to
        ``h-2`` which no longer recommends ``hh``, ``hh`` becomes a new
        orphan.  Without fix #3, ``hh`` has no in-edge in either pre or
        post state, is seen as orphan in both, and the ``pre ∧
        orphan(pre)`` clause silently skips it forever.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'hh'})
        resolver.db.get_package.side_effect = lambda name: {
            'h':  {'requires': [], 'recommends': [], 'provides': ['h']},
            'hh': {'requires': [], 'recommends': [], 'provides': ['hh']},
        }.get(name)

        headers = [
            _fake_hdr('h', recommends=['hh'], provides=['h']),
            _fake_hdr('hh', provides=['hh']),
        ]
        actions = [
            _make_action('h', TransactionType.UPGRADE),
        ]

        orphans, _ = self._run(resolver, headers, actions)
        assert {o.name for o in orphans} == {'hh'}

    def test_preexisting_orphan_not_flagged(self, resolver):
        """Spec clause ``¬(P ∈ S_pre ∧ orphan(P, S_pre))``.

        Scenario: ``stale`` is already an orphan before the
        transaction (in rpmdb, in the unrequested set, no reverse
        dep).  An unrelated upgrade happens.  ``stale`` must NOT be
        reported — it was already orphan, it is autoremove's job.
        This test guards the drift that 3bec30b was meant to prevent.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'stale'})
        resolver.db.get_package.side_effect = lambda name: {
            'user_app': {
                'requires': [], 'recommends': [], 'provides': ['user_app'],
            },
        }.get(name)

        headers = [
            _fake_hdr('user_app', provides=['user_app']),
            _fake_hdr('stale', provides=['stale']),
        ]
        actions = [
            _make_action('user_app', TransactionType.UPGRADE),
        ]

        orphans, _ = self._run(resolver, headers, actions)
        assert orphans == [], (
            "stale was already orphan before the transaction — "
            "find_upgrade_orphans must leave it to autoremove"
        )
