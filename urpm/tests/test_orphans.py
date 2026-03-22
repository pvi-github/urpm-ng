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
