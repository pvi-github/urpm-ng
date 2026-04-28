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

    def find_upgrade_orphans(self, all_actions, obsoleted_names=None):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.find_upgrade_orphans(self, all_actions, obsoleted_names=obsoleted_names)

    def find_all_orphans(self):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.find_all_orphans(self)

    def is_orphan(self, name):
        from urpm.core.resolution.orphans import OrphansMixin
        return OrphansMixin.is_orphan(self, name)


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


def _fake_hdr(name, requires=(), recommends=(), suggests=(), provides=(),
              supplements=(),
              require_vers=(), require_flags=(),
              recommend_vers=(), recommend_flags=(),
              suggest_vers=(), suggest_flags=(),
              supplement_vers=(), supplement_flags=(),
              provide_vers=(),
              epoch=0, version='1', release='1', arch='noarch', size=0):
    """Build a minimal fake rpm header that supports ``hdr[rpm.RPMTAG_*]``.

    The ``*_vers`` / ``*_flags`` parallel arrays are optional and default
    to empty; ``find_upgrade_orphans`` tolerates short arrays by padding
    with empty strings / zero flags.  Tests that only care about the name
    graph can ignore them entirely (backward-compatible with pre-version
    tests).
    """
    tag_map = {
        rpm.RPMTAG_NAME: name,
        rpm.RPMTAG_REQUIRENAME: list(requires),
        rpm.RPMTAG_REQUIREVERSION: list(require_vers),
        rpm.RPMTAG_REQUIREFLAGS: list(require_flags),
        rpm.RPMTAG_RECOMMENDNAME: list(recommends),
        rpm.RPMTAG_RECOMMENDVERSION: list(recommend_vers),
        rpm.RPMTAG_RECOMMENDFLAGS: list(recommend_flags),
        rpm.RPMTAG_SUGGESTNAME: list(suggests),
        rpm.RPMTAG_SUGGESTVERSION: list(suggest_vers),
        rpm.RPMTAG_SUGGESTFLAGS: list(suggest_flags),
        rpm.RPMTAG_SUPPLEMENTNAME: list(supplements),
        rpm.RPMTAG_SUPPLEMENTVERSION: list(supplement_vers),
        rpm.RPMTAG_SUPPLEMENTFLAGS: list(supplement_flags),
        rpm.RPMTAG_PROVIDENAME: list(provides),
        rpm.RPMTAG_PROVIDEVERSION: list(provide_vers),
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
        """Patch rpm.TransactionSet and dispatch to find_upgrade_orphans.

        Returns ``(plan, TransactionType)`` where ``plan`` is the
        :class:`UpgradeOrphanPlan` returned by the resolver.
        """
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

        plan, _ = self._run(resolver, headers, actions)
        assert {o.name for o in plan.removes} == {'a'}
        # ``a`` is also being upgraded, so the new version must be
        # cancelled alongside the REMOVE of its old version.
        assert plan.cancelled_new_versions == {'a'}

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

        plan, _ = self._run(resolver, headers, actions)
        assert not plan, (
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

        plan, _ = self._run(resolver, headers, actions)
        assert {o.name for o in plan.removes} == {'hh'}
        # ``hh`` is NOT in the action list (not being upgraded or
        # installed), so no new-version cancellation is required.
        assert plan.cancelled_new_versions == set()

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

        plan, _ = self._run(resolver, headers, actions)
        assert not plan, (
            "stale was already orphan before the transaction — "
            "find_upgrade_orphans must leave it to autoremove"
        )

    # -- Family A regression tests -----------------------------------------
    #
    # The two scenarios below pin the Family A fix landed together with
    # the :class:`UpgradeOrphanPlan` refactor.  They cover the two
    # distinct bugs that were conflated under a single xfail pair in
    # ``test_install.py::TestOrphans`` (``test_auto_select_t`` and
    # ``test_auto_select_f``).

    def test_version_constraint_filters_stale_provider(self, resolver):
        """Version-aware reverse graph: stale provider is not a parent.

        Scenario (mirrors ``test_auto_select_t``):

        * ``t-1`` is installed explicitly and requires the virtual
          capability ``tt`` (unversioned in v1).
        * ``tt1-1`` is installed as a dep of ``t-1`` and provides
          ``tt = 1``.
        * The upgrade plan advances ``t`` to ``t-2`` which requires
          ``tt >= 2`` — now satisfied by the new ``tt2-2`` (``Provides:
          tt = 2``).  Libsolv installs ``tt2`` alongside.

        The expected post-transaction state has ``tt1-1`` orphaned:
        its only in-edge (via virtual ``tt``) has a version constraint
        that ``tt1`` cannot satisfy.  The old version-blind reverse
        graph credited ``tt1`` for the edge and masked the orphan.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'tt1', 'tt2'})
        resolver.db.get_package.side_effect = lambda name: {
            't': {
                'name': 't', 'epoch': 0, 'version': '2', 'release': '1',
                'requires': ['tt[>= 2]'], 'recommends': [],
                'provides': ['t[= 2-1]'],
            },
            'tt2': {
                'name': 'tt2', 'epoch': 0, 'version': '2', 'release': '1',
                'requires': [], 'recommends': [],
                'provides': ['tt2[= 2-1]', 'tt[= 2]'],
            },
        }.get(name)

        headers = [
            _fake_hdr(
                't',
                requires=['tt'],
                require_vers=[''],
                require_flags=[0],
                provides=['t'],
                provide_vers=['1-1'],
            ),
            _fake_hdr(
                'tt1',
                provides=['tt1', 'tt'],
                provide_vers=['1-1', '1'],
            ),
        ]
        actions = [
            _make_action('t', TransactionType.UPGRADE),
            _make_action('tt2', TransactionType.INSTALL),
        ]

        plan, _ = self._run(resolver, headers, actions)

        # ``tt1`` is in the rpmdb and becomes orphaned → removes.
        assert {o.name for o in plan.removes} == {'tt1'}
        # ``tt1`` is NOT being installed/upgraded, so it's not in
        # cancelled_new_versions.  And ``tt2`` has a legitimate parent
        # (``t-2`` requires ``tt >= 2`` satisfied by ``tt2``), so it's
        # not cancelled either.
        assert plan.cancelled_new_versions == set()

    def test_cancelled_new_install_not_emitted_as_remove(self, resolver):
        """Orphan-on-arrival: a new install whose parent is orphan.

        Scenario (mirrors ``test_auto_select_f`` with ``req_f``
        extension): a user had ``req_f-1`` requiring ``f-1`` requiring
        ``ff1``.  After upgrade, ``req_f-2`` no longer requires ``f``
        and ``f-2`` switches from ``ff1`` to ``ff2``.  Libsolv produces
        a plan that upgrades ``req_f`` and ``f``, and installs ``ff2``
        as a dep of ``f-2``.

        The post-state reasoning:

        * ``f-2`` is orphan — ``req_f-2`` no longer requires it and ``f``
          is unrequested.
        * ``ff2`` is transitively orphan — its only requester (``f-2``)
          is itself orphan.

        The old implementation emitted a ``REMOVE(ff2)`` PackageAction
        alongside an ``INSTALL(ff2)`` from libsolv.  rpm's transaction
        engine silently no-ops such install+remove pairs.  The new
        implementation must instead report ``ff2`` in
        ``cancelled_new_versions`` so the caller can drop the install
        action and the downloaded RPM.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'f', 'ff1', 'ff2'})
        resolver.db.get_package.side_effect = lambda name: {
            'req_f': {
                'name': 'req_f', 'epoch': 0, 'version': '2', 'release': '1',
                'requires': [], 'recommends': [],
                'provides': ['req_f[= 2-1]'],
            },
            'f': {
                'name': 'f', 'epoch': 0, 'version': '2', 'release': '1',
                'requires': ['ff2'], 'recommends': [],
                'provides': ['f[= 2-1]'],
            },
            'ff2': {
                'name': 'ff2', 'epoch': 0, 'version': '2', 'release': '1',
                'requires': [], 'recommends': [],
                'provides': ['ff2[= 2-1]'],
            },
        }.get(name)

        headers = [
            _fake_hdr('req_f', requires=['f'], provides=['req_f']),
            _fake_hdr('f', requires=['ff1'], provides=['f']),
            _fake_hdr('ff1', provides=['ff1']),
        ]
        actions = [
            _make_action('req_f', TransactionType.UPGRADE),
            _make_action('f', TransactionType.UPGRADE),
            _make_action('ff2', TransactionType.INSTALL),
        ]

        plan, _ = self._run(resolver, headers, actions)

        # Category 1: rpmdb-side removes.  ``f-1`` and ``ff1-1`` are
        # both in the rpmdb and both orphan in the post-state:
        # ``f-1`` because req_f-2 no longer requires it, ``ff1-1``
        # because f-2 no longer requires it.
        assert {o.name for o in plan.removes} == {'f', 'ff1'}

        # Category 2: ``ff2`` is being INSTALLED but is orphan-on-arrival
        # → must be cancelled, not emitted as a REMOVE action.
        # ``f`` is being UPGRADED and its old version is in removes, so
        # its new version must also be cancelled (otherwise we'd install
        # f-2 only for libsolv to wonder why).
        assert plan.cancelled_new_versions == {'ff2', 'f'}

        # And critically, ``ff2`` must NOT appear in ``removes`` — that
        # was the silent-no-op bug the refactor exists to fix.
        assert 'ff2' not in {o.name for o in plan.removes}

    # -- Supplements regression tests --------------------------------------
    #
    # The two scenarios below pin the fix for the
    # xdg-desktop-portal-kde-style bug: a plugin ``B`` carrying
    # ``Supplements: A`` must stay alive as long as ``A`` is installed and
    # must become orphan when ``A`` disappears.  Before the fix, Supplements
    # was silently ignored in the reverse-dep graph, so such plugins were
    # either kept forever (no in-edge ⇒ orphan in pre-state ⇒ excluded by
    # the pre-orphan clause) or removed on the wrong trigger.

    def test_supplements_removed_target_emits_orphan(self, resolver):
        """Target of Supplements is removed ⇒ supplementing package is orphan.

        Scenario: ``plugin`` Supplements ``app``.  Both installed,
        ``plugin`` unrequested.  The user explicitly removes ``app``.
        ``plugin`` must be emitted as a new orphan because in post-state
        it has no remaining supplements target (and no Requires /
        Recommends in-edge either).

        Without the fix, ``plugin`` had no in-edge in either pre or post
        state, was orphan in both, and the pre-orphan clause filtered it
        out — leaving it lingering forever.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'plugin'})
        resolver.db.get_package.side_effect = lambda name: {
            'app':    {'requires': [], 'recommends': [], 'provides': ['app']},
            'plugin': {
                'requires': [], 'recommends': [],
                'supplements': ['app'], 'provides': ['plugin'],
            },
        }.get(name)

        headers = [
            _fake_hdr('app', provides=['app']),
            _fake_hdr('plugin', supplements=['app'], provides=['plugin']),
        ]
        actions = [
            _make_action('app', TransactionType.REMOVE),
        ]

        plan, _ = self._run(resolver, headers, actions)
        assert {o.name for o in plan.removes} == {'plugin'}

    def test_supplements_target_kept_protects_package(self, resolver):
        """Target of Supplements stays installed ⇒ supplementing package is safe.

        Scenario: ``plugin`` Supplements ``app``.  Both installed,
        ``plugin`` unrequested.  ``app`` is upgraded in place.  ``plugin``
        must NOT be flagged as orphan in the post-state — the supplements
        edge onto the upgraded ``app`` keeps it alive.

        This mirrors the xdg-desktop-portal-kde case: on an LXQt system
        where ``plasma-workspace`` is installed, the portal pulled by
        ``Supplements: plasma-workspace`` must not be torn down by an
        unrelated upgrade.
        """
        from urpm.core.resolver import TransactionType

        resolver._save_unrequested_packages({'plugin'})
        resolver.db.get_package.side_effect = lambda name: {
            'app': {
                'name': 'app', 'epoch': 0, 'version': '2', 'release': '1',
                'requires': [], 'recommends': [],
                'provides': ['app[= 2-1]'],
            },
            'plugin': {
                'requires': [], 'recommends': [],
                'supplements': ['app'], 'provides': ['plugin'],
            },
        }.get(name)

        headers = [
            _fake_hdr('app', provides=['app']),
            _fake_hdr('plugin', supplements=['app'], provides=['plugin']),
        ]
        actions = [
            _make_action('app', TransactionType.UPGRADE),
        ]

        plan, _ = self._run(resolver, headers, actions)
        assert not plan, (
            "plugin is kept alive by Supplements: app — must not be "
            "flagged when app is merely upgraded"
        )


class TestOrphanCrossPathConsistency:
    """Ensure ``is_orphan`` and ``find_all_orphans`` agree, and that
    every weak-dep relationship (Recommends, Suggests, Supplements)
    counts as a protective edge per the unified spec.

    These tests pin the single-source-of-truth contract: any verb
    surfacing orphan status must call ``is_orphan`` (which is
    backed by ``find_all_orphans``) so a future refactor cannot
    re-introduce the divergence between ``urpm why`` /
    ``urpm autoremove`` / ``urpme --auto-orphans``.
    """

    def _run(self, resolver, headers):
        with patch('urpm.core.resolution.orphans.rpm.TransactionSet') as ts_cls:
            ts_cls.return_value.dbMatch.return_value = headers
            return resolver.find_all_orphans(), [
                resolver.is_orphan(h[rpm.RPMTAG_NAME]) for h in headers
            ]

    def test_suggests_protects_from_orphan(self, resolver):
        """A package pulled only by Suggests must NOT be orphan.

        Project policy: ``Suggests`` counts as a protective edge.  A
        runtime helper (``helper``) installed because some explicit
        package suggests it must not be auto-removed unless the user
        explicitly asks for weak-dep cleanup.
        """
        resolver._save_unrequested_packages({'helper'})
        headers = [
            _fake_hdr('app', suggests=['helper'], provides=['app']),
            _fake_hdr('helper', provides=['helper']),
        ]
        all_orphans, _per_name = self._run(resolver, headers)
        assert {o.name for o in all_orphans} == set(), (
            "helper is suggested by an explicit app — must not be flagged"
        )

    def test_recommends_protects_from_orphan(self, resolver):
        """A package pulled only by Recommends must NOT be orphan."""
        resolver._save_unrequested_packages({'helper'})
        headers = [
            _fake_hdr('app', recommends=['helper'], provides=['app']),
            _fake_hdr('helper', provides=['helper']),
        ]
        all_orphans, _per_name = self._run(resolver, headers)
        assert {o.name for o in all_orphans} == set()

    def test_truly_orphan_is_flagged(self, resolver):
        """Closed-cluster of unrequested with no explicit ancestor."""
        resolver._save_unrequested_packages({'a', 'b'})
        headers = [
            _fake_hdr('a', requires=['b'], provides=['a']),
            _fake_hdr('b', provides=['b']),
        ]
        all_orphans, _per_name = self._run(resolver, headers)
        assert {o.name for o in all_orphans} == {'a', 'b'}

    def test_is_orphan_agrees_with_find_all_orphans(self, resolver):
        """``is_orphan(X)`` ⇔ X ∈ find_all_orphans() for every X.

        The contract is bidirectional: a package returned by
        ``find_all_orphans`` must be ``is_orphan``-True, and a package
        not returned must be ``is_orphan``-False.  Any divergence is a
        regression of the single-source-of-truth invariant.
        """
        resolver._save_unrequested_packages({'a', 'b', 'helper'})
        headers = [
            _fake_hdr('explicit_root', requires=['a'], provides=['explicit_root']),
            _fake_hdr('a', requires=['b'], suggests=['helper'], provides=['a']),
            _fake_hdr('b', provides=['b']),
            _fake_hdr('helper', provides=['helper']),
            _fake_hdr('cluster_x', requires=['cluster_y'], provides=['cluster_x']),
            _fake_hdr('cluster_y', requires=['cluster_x'], provides=['cluster_y']),
        ]
        # cluster_x and cluster_y form a closed unrequested cycle → orphan
        # a/b/helper protected via explicit_root
        resolver._save_unrequested_packages(
            {'a', 'b', 'helper', 'cluster_x', 'cluster_y'}
        )
        all_orphans, per_name_results = self._run(resolver, headers)
        all_orphan_names = {o.name for o in all_orphans}
        for hdr, is_orph in zip(headers, per_name_results):
            name = hdr[rpm.RPMTAG_NAME]
            in_set = name in all_orphan_names
            assert is_orph == in_set, (
                f"is_orphan({name})={is_orph} but "
                f"find_all_orphans includes={in_set}"
            )
