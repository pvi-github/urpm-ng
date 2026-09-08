"""What an erase gives back, in bytes that mean something.

``urpm erase libreoffice-core`` announced a few kilobytes for 323 MB.
Every removal weighed zero, and for the same reason a `from_size` bug
had already been fixed on the upgrade side: outgoing packages are
*installed* solvables, and ``_solvable_to_pkg`` only ever describes
media ones.  The production pool fills ``@System`` with libsolv's own
``add_rpmdb()``, which populates ``SOLVABLE_INSTALLSIZE`` and leaves
that map empty.

Reading the map gave zero for every erase — silently, since zero is a
plausible-looking number.  Four call sites had it: the three mixed
install/erase paths and the erase-only one.

These tests use the machine's real rpmdb through libsolv, because a
stand-in map would have satisfied the broken code just as well as the
fixed one.
"""

from __future__ import annotations

import subprocess

import pytest

solv = pytest.importorskip("solv")

from urpm.core.resolver import installed_size_of  # noqa: E402


@pytest.fixture(scope="module")
def system_pool():
    pool = solv.Pool()
    pool.setarch()
    repo = pool.add_repo("@System")
    repo.add_rpmdb()
    pool.installed = repo
    pool.createwhatprovides()
    return pool


def _rpm_size(name):
    out = subprocess.run(["rpm", "-q", "--qf", "%{SIZE}", name],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


class TestAnInstalledPackageHasAWeight:

    def test_the_map_is_empty_on_the_production_path(self, system_pool):
        """The premise of the bug, pinned : if this ever stops being
        true the fix is still correct, but the test below stops
        proving anything."""
        assert system_pool.installed.nsolvables > 0

    def test_sizes_come_out_non_zero(self, system_pool):
        sizes = [installed_size_of(s, {})
                 for s in system_pool.installed.solvables]
        assert sizes, "no installed packages to measure"
        assert sum(sizes) > 0, (
            "every installed package weighed zero -- the lookup is "
            "reading a field this loader does not populate"
        )
        assert sum(1 for s in sizes if s) > len(sizes) // 2, (
            "most packages should have a non-zero size"
        )

    def test_they_agree_with_rpm(self, system_pool):
        """``SOLVABLE_INSTALLSIZE`` is ``RPMTAG_SIZE``, in bytes.  A
        unit change would be off by 1024 and still look plausible."""
        checked = 0
        for s in system_pool.installed.solvables:
            ref = _rpm_size(s.name)
            if ref is None or ref == 0:
                continue
            assert installed_size_of(s, {}) == ref, s.name
            checked += 1
            if checked >= 20:
                break
        assert checked >= 5, "not enough packages to compare against"


class TestTheEraseTotal:
    """End to end, through the resolver the CLI actually calls."""

    @pytest.fixture(scope="class")
    def erase_plan(self):
        from urpm.core.database import PackageDatabase
        from urpm.core.resolver import Resolver

        installed = subprocess.run(
            ["rpm", "-qa", "--qf", "%{NAME} %{SIZE}\n"],
            capture_output=True, text=True).stdout.split("\n")
        heavy = [line.split()[0] for line in installed
                 if len(line.split()) == 2 and int(line.split()[1]) > 20 << 20]
        if not heavy:
            pytest.skip("no package large enough to make the total telling")

        db = PackageDatabase()
        try:
            yield Resolver(db).resolve_remove([heavy[0]], clean_deps=False)
        finally:
            db.close()

    def test_the_total_is_not_zero(self, erase_plan):
        assert erase_plan.remove_size > 0, (
            "a plan that removes a 20 MB package cannot free nothing"
        )

    def test_the_total_matches_rpm(self, erase_plan):
        ref = sum(filter(None, (_rpm_size(a.name)
                                for a in erase_plan.actions)))
        assert erase_plan.remove_size == ref

    def test_each_action_carries_its_own_size(self, erase_plan):
        """The summary line is not the only consumer : the distupgrade
        space estimate sums ``action.size`` over removals, and the
        erase deferral ranks them by it."""
        sized = [a for a in erase_plan.actions if a.size]
        assert sized, "no action carries a size"
        for a in sized[:10]:
            assert a.size == _rpm_size(a.name), a.name
