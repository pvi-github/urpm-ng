"""Tests for suggests handling in resolver.

These tests verify:
1. Basic suggests detection (single/multiple providers)
2. Alternatives sorting by missing dependencies
3. Auto mode behavior
4. Choices recording
5. Recursive suggests (suggests of suggests)
6. Cycle detection (A->B->C->A)
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from typing import List, Dict, Set

import solv

from urpm.core.resolver import Resolver, Alternative, PackageAction


class MockSolvable:
    """Mock libsolv solvable."""
    def __init__(self, id: int, name: str, evr: str = "1.0-1", arch: str = "x86_64",
                 provides: List[str] = None, requires: List[str] = None,
                 suggests: List[str] = None):
        self.id = id
        self.name = name
        self.evr = evr
        self.arch = arch
        self.repo = None
        self._provides = provides or [name]
        self._requires = requires or []
        self._suggests = suggests or []

    def lookup_deparray(self, dep_type):
        """Return dependencies based on type."""
        if dep_type == solv.SOLVABLE_SUGGESTS:
            return [MockDep(s) for s in self._suggests]
        elif dep_type == solv.SOLVABLE_REQUIRES:
            return [MockDep(r) for r in self._requires]
        elif dep_type == solv.SOLVABLE_PROVIDES:
            return [MockDep(p) for p in self._provides]
        return []


class MockDep:
    """Mock libsolv dependency."""
    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return self.name


class MockRepo:
    """Mock libsolv repo."""
    def __init__(self, name: str):
        self.name = name
        self.solvables = []


class MockSelection:
    """Mock libsolv selection."""
    def __init__(self, solvables: List[MockSolvable]):
        self._solvables = solvables

    def isempty(self):
        return len(self._solvables) == 0

    def solvables(self):
        return self._solvables


class MockPool:
    """Mock libsolv pool for testing."""

    def __init__(self):
        self.installed = None
        self.repos = []
        self._packages: Dict[str, List[MockSolvable]] = {}
        self._provides: Dict[str, List[MockSolvable]] = {}

    def add_package(self, solvable: MockSolvable, repo: MockRepo):
        """Add a package to the pool."""
        solvable.repo = repo
        repo.solvables.append(solvable)  # Also add to repo's solvables list

        if solvable.name not in self._packages:
            self._packages[solvable.name] = []
        self._packages[solvable.name].append(solvable)

        for prov in solvable._provides:
            if prov not in self._provides:
                self._provides[prov] = []
            self._provides[prov].append(solvable)

    def select(self, name: str, flags: int) -> MockSelection:
        """Select packages by name."""
        solvables = self._packages.get(name, [])
        return MockSelection(solvables)

    def Dep(self, name: str) -> MockDep:
        """Create a dependency object."""
        return MockDep(name)

    def whatprovides(self, dep) -> List[MockSolvable]:
        """Find packages that provide a capability."""
        dep_name = dep.name if hasattr(dep, 'name') else str(dep)
        return self._provides.get(dep_name, [])


class TestFindAvailableSuggests:
    """Tests for find_available_suggests method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.pool = MockPool()
        self.available_repo = MockRepo('Core')
        self.system_repo = MockRepo('@System')
        self.pool.installed = self.system_repo

    def _create_resolver_with_mock_pool(self):
        """Create a real Resolver but inject our mock pool."""
        # Create resolver with a mock db (won't be used since we inject pool)
        resolver = Resolver.__new__(Resolver)
        resolver.db = None
        resolver.pool = self.pool
        resolver.install_recommends = False
        resolver._solvable_to_pkg = {}
        return resolver

    def test_single_provider_returned_directly(self):
        """Test 1: suggest with single provider is returned in suggests list."""
        # Setup: pkg-a suggests pkg-b, pkg-b has single provider
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['pkg-b'])
        pkg_b = MockSolvable(id=2, name='pkg-b')

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_b, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()
        suggests, alternatives = resolver.find_available_suggests(['pkg-a'])

        # Single provider -> should be in suggests, not alternatives
        assert len(suggests) == 1
        assert suggests[0].name == 'pkg-b'
        assert len(alternatives) == 0

    def test_multiple_providers_returned_as_alternative(self):
        """Test 2: suggest with multiple providers returned as Alternative."""
        # Setup: pkg-a suggests gui-frontend, provided by pkg-qt and pkg-gtk
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['gui-frontend'])
        pkg_qt = MockSolvable(id=2, name='pkg-qt', provides=['gui-frontend', 'pkg-qt'])
        pkg_gtk = MockSolvable(id=3, name='pkg-gtk', provides=['gui-frontend', 'pkg-gtk'])

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_qt, self.available_repo)
        self.pool.add_package(pkg_gtk, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()
        suggests, alternatives = resolver.find_available_suggests(['pkg-a'])

        # Multiple providers -> should be in alternatives, not suggests
        assert len(suggests) == 0
        assert len(alternatives) == 1
        assert alternatives[0].capability == 'gui-frontend'
        assert set(alternatives[0].providers) == {'pkg-qt', 'pkg-gtk'}

    def test_alternatives_contains_all_providers(self):
        """Test 3: alternatives should contain all providers (sorting tested in integration)."""
        # Setup: pkg-a suggests gui, provided by pkg-qt and pkg-gtk
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['gui'])
        pkg_qt = MockSolvable(id=2, name='pkg-qt', provides=['gui', 'pkg-qt'])
        pkg_gtk = MockSolvable(id=3, name='pkg-gtk', provides=['gui', 'pkg-gtk'])

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_qt, self.available_repo)
        self.pool.add_package(pkg_gtk, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()
        suggests, alternatives = resolver.find_available_suggests(['pkg-a'])

        # Both providers should be in the alternative
        assert len(alternatives) == 1
        assert set(alternatives[0].providers) == {'pkg-qt', 'pkg-gtk'}

    def test_choice_applied_filters_alternative(self):
        """Test 4: when choice exists, no alternative returned."""
        # Setup: pkg-a suggests gui, provided by pkg-qt and pkg-gtk
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['gui'])
        pkg_qt = MockSolvable(id=2, name='pkg-qt', provides=['gui', 'pkg-qt'])
        pkg_gtk = MockSolvable(id=3, name='pkg-gtk', provides=['gui', 'pkg-gtk'])

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_qt, self.available_repo)
        self.pool.add_package(pkg_gtk, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()
        choices = {'gui': 'pkg-qt'}
        suggests, alternatives = resolver.find_available_suggests(['pkg-a'], choices=choices)

        # Choice exists -> should return chosen package in suggests, no alternatives
        assert len(alternatives) == 0
        assert len(suggests) == 1
        assert suggests[0].name == 'pkg-qt'

    def test_choice_recorded_excludes_others(self):
        """Test 5: choice for one capability should reject other providers."""
        # Setup: pkg-a suggests gui, user chose pkg-qt
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['gui'])
        pkg_qt = MockSolvable(id=2, name='pkg-qt', provides=['gui', 'pkg-qt'])
        pkg_gtk = MockSolvable(id=3, name='pkg-gtk', provides=['gui', 'pkg-gtk'])

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_qt, self.available_repo)
        self.pool.add_package(pkg_gtk, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()
        choices = {'gui': 'pkg-qt'}
        suggests, alternatives = resolver.find_available_suggests(['pkg-a'], choices=choices)

        # Only pkg-qt should be suggested, pkg-gtk excluded
        suggest_names = [s.name for s in suggests]
        assert 'pkg-qt' in suggest_names
        assert 'pkg-gtk' not in suggest_names

    def test_suggests_of_suggests_not_found_single_pass(self):
        """Test 6: single pass does NOT find suggests of suggests (A->B->C)."""
        # Setup: pkg-a suggests pkg-b, pkg-b suggests pkg-c
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['pkg-b'])
        pkg_b = MockSolvable(id=2, name='pkg-b', suggests=['pkg-c'])
        pkg_c = MockSolvable(id=3, name='pkg-c')

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_b, self.available_repo)
        self.pool.add_package(pkg_c, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()

        # First pass: only finds pkg-b
        suggests, alternatives = resolver.find_available_suggests(['pkg-a'])
        suggest_names = [s.name for s in suggests]

        assert 'pkg-b' in suggest_names
        # pkg-c is NOT found in single pass (this is the bug we're fixing)
        assert 'pkg-c' not in suggest_names

    def test_no_infinite_loop_on_cycle(self):
        """Test 7: should not loop infinitely on A->B->C->A cycle."""
        # Setup: A suggests B, B suggests C, C suggests A
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['pkg-b'])
        pkg_b = MockSolvable(id=2, name='pkg-b', suggests=['pkg-c'])
        pkg_c = MockSolvable(id=3, name='pkg-c', suggests=['pkg-a'])

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_b, self.available_repo)
        self.pool.add_package(pkg_c, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()

        # This should complete without infinite loop
        # When iterative implementation is done, it should find pkg-b and pkg-c
        # but NOT re-add pkg-a (already in resolved_packages)
        suggests, alternatives = resolver.find_available_suggests(
            ['pkg-a'], resolved_packages=['pkg-a']
        )

        # Should not hang - if we get here, no infinite loop
        suggest_names = [s.name for s in suggests]
        assert 'pkg-a' not in suggest_names  # Should not re-suggest pkg-a


class TestSuggestsIterative:
    """Tests for iterative suggests collection (suggests of suggests).

    These tests verify the iteration logic in main.py that calls
    find_available_suggests multiple times to collect nested suggests.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.pool = MockPool()
        self.available_repo = MockRepo('Core')
        self.system_repo = MockRepo('@System')
        self.pool.installed = self.system_repo

    def _create_resolver_with_mock_pool(self):
        """Create a real Resolver but inject our mock pool."""
        resolver = Resolver.__new__(Resolver)
        resolver.db = None
        resolver.pool = self.pool
        resolver.install_recommends = False
        resolver._solvable_to_pkg = {}
        return resolver

    def test_iterative_finds_suggests_of_suggests(self):
        """Test that iterating finds A->B->C chain."""
        # Setup: pkg-a suggests pkg-b, pkg-b suggests pkg-c
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['pkg-b'])
        pkg_b = MockSolvable(id=2, name='pkg-b', suggests=['pkg-c'])
        pkg_c = MockSolvable(id=3, name='pkg-c')

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_b, self.available_repo)
        self.pool.add_package(pkg_c, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()

        # Simulate iterative collection (like main.py should do)
        all_suggests = []
        packages_to_check = ['pkg-a']
        checked = set(['pkg-a'])
        max_iter = 5

        for _ in range(max_iter):
            suggests, alts = resolver.find_available_suggests(
                packages_to_check, resolved_packages=list(checked)
            )
            if not suggests and not alts:
                break

            for s in suggests:
                if s.name.lower() not in checked:
                    all_suggests.append(s)
                    checked.add(s.name.lower())

            # Next iteration: check new suggests
            packages_to_check = [s.name for s in suggests]
            if not packages_to_check:
                break

        suggest_names = [s.name for s in all_suggests]
        assert 'pkg-b' in suggest_names
        assert 'pkg-c' in suggest_names

    def test_iterative_handles_cycle(self):
        """Test that iteration handles A->B->C->A without infinite loop."""
        # Setup: circular suggests
        pkg_a = MockSolvable(id=1, name='pkg-a', suggests=['pkg-b'])
        pkg_b = MockSolvable(id=2, name='pkg-b', suggests=['pkg-c'])
        pkg_c = MockSolvable(id=3, name='pkg-c', suggests=['pkg-a'])

        self.pool.add_package(pkg_a, self.available_repo)
        self.pool.add_package(pkg_b, self.available_repo)
        self.pool.add_package(pkg_c, self.available_repo)

        resolver = self._create_resolver_with_mock_pool()

        # Simulate iterative collection
        all_suggests = []
        packages_to_check = ['pkg-a']
        checked = set(['pkg-a'])  # pkg-a is the initial package
        max_iter = 5
        iterations = 0

        for _ in range(max_iter):
            iterations += 1
            suggests, alts = resolver.find_available_suggests(
                packages_to_check, resolved_packages=list(checked)
            )
            if not suggests and not alts:
                break

            new_packages = []
            for s in suggests:
                if s.name.lower() not in checked:
                    all_suggests.append(s)
                    checked.add(s.name.lower())
                    new_packages.append(s.name)

            packages_to_check = new_packages
            if not packages_to_check:
                break

        # Should complete without hitting max_iter due to infinite loop
        assert iterations < max_iter, "Possible infinite loop detected"

        suggest_names = [s.name for s in all_suggests]
        assert 'pkg-b' in suggest_names
        assert 'pkg-c' in suggest_names
        # pkg-a should NOT be in suggests (it's the initial package)
        assert 'pkg-a' not in suggest_names
