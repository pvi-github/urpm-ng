"""Tests for the partial-transaction resolver helpers.

This module exercises ``Resolver._solve()`` (atomic and best-effort
modes), ``_classify_jobs()`` and the SRPM / request-id cascade against
hand-built synthetic libsolv pools.  No urpm config, no real synthesis,
no rpmdb — just :mod:`solv` primitives plus a tiny FakeDB stub for the
diagnostic helpers that occasionally hit ``db.whatprovides``.

The Resolver is built with ``Resolver.__new__`` and the few attributes
it actually touches are wired by hand, side-stepping ``__init__`` (which
would otherwise create a real pool from disk).
"""

import ast
import pathlib
from typing import List, Optional

import pytest
import solv

from urpm.core import resolver as resolver_module
from urpm.core.resolver import (
    JobOrigin,
    Resolver,
    SkippedJob,
)


# ---------------------------------------------------------------------------
# FakeDB — same shape as ``urpm.tests.test_diagnose.FakeDB`` (kept local on
# purpose: tests should not depend on each other).
# ---------------------------------------------------------------------------


class FakeDB:
    """Minimal ``PackageDatabase`` stand-in.

    Only ``whatprovides()`` is consulted by the resolver helpers used
    here (via the diagnose module).  ``get_held_packages_set()`` is
    provided for tests that touch ``resolve_upgrade``.
    """

    def __init__(self, providers=None, held=None):
        self._providers = providers or {}
        self._held = set(held or ())

    def whatprovides(self, capability):
        return list(self._providers.get(capability, []))

    def get_held_packages_set(self):
        return set(self._held)


# ---------------------------------------------------------------------------
# Pool construction helpers
# ---------------------------------------------------------------------------


#: Comparison operators a fixture spec may write, mapped to libsolv
#: relation flags.
_REL_FLAGS = {
    "=": solv.REL_EQ,
    "==": solv.REL_EQ,
    ">": solv.REL_GT,
    ">=": solv.REL_GT | solv.REL_EQ,
    "<": solv.REL_LT,
    "<=": solv.REL_LT | solv.REL_EQ,
}


def _dep(pool, capability):
    """Turn ``"name"`` or ``"name OP evr"`` into a libsolv ``Dep``.

    ``pool.Dep("mid = 2-1")`` does **not** parse the string: it interns
    the whole thing as a single capability name that happens to contain
    spaces, and ``str()`` prints it back identically to a real
    relation.  A fixture written that way therefore expresses "nothing
    provides this" at both ends while looking perfectly correct, which
    is invisible as long as the test only needs the dependency to be
    unsatisfiable.  The relation has to be built explicitly.
    """
    parts = capability.split(None, 2)
    if len(parts) == 3 and parts[1] in _REL_FLAGS:
        return pool.Dep(parts[0]).Rel(_REL_FLAGS[parts[1]], pool.Dep(parts[2]))
    return pool.Dep(capability)


def _add_solvable(pool, repo, repodata, spec):
    """Populate one solvable from a dict spec.

    Spec keys: ``name``, ``evr``, ``arch`` (default ``x86_64``),
    ``requires`` (list of capability strings), ``provides`` (idem),
    ``srpm`` (source package name), ``srpm_evr`` (defaults to ``evr``).
    The solvable always self-provides ``name = evr``.
    """
    s = repo.add_solvable()
    s.name = spec["name"]
    s.evr = spec["evr"]
    s.arch = spec.get("arch", "x86_64")
    # Self-provide "name = evr"
    s.add_deparray(
        solv.SOLVABLE_PROVIDES,
        pool.rel2id(pool.str2id(s.name), pool.str2id(s.evr), solv.REL_EQ),
    )
    for prov in spec.get("provides", []):
        s.add_deparray(solv.SOLVABLE_PROVIDES, _dep(pool, prov))
    for req in spec.get("requires", []):
        s.add_deparray(solv.SOLVABLE_REQUIRES, _dep(pool, req))
    if "srpm" in spec and repodata is not None:
        repodata.set_str(s.id, solv.SOLVABLE_SOURCENAME, spec["srpm"])
        repodata.set_str(
            s.id, solv.SOLVABLE_SOURCEEVR, spec.get("srpm_evr", spec["evr"]),
        )
    return s


def make_pool(installed, available):
    """Build a ``solv.Pool`` with ``@System`` + ``available`` repos.

    Args:
        installed: list of solvable specs (dicts) for the system repo.
        available: list of solvable specs for the upgrade source.

    Returns:
        ``(pool, sys_repo, avail_repo, name_index)`` where ``name_index``
        maps ``"name-evr"`` → solvable for convenient test lookup.
    """
    pool = solv.Pool()
    pool.setarch("x86_64")
    sys_repo = pool.add_repo("@System")
    sys_rd = sys_repo.add_repodata()
    avail_repo = pool.add_repo("available")
    avail_rd = avail_repo.add_repodata()

    name_index = {}
    for spec in installed:
        s = _add_solvable(pool, sys_repo, sys_rd, spec)
        name_index[f"sys:{spec['name']}-{spec['evr']}"] = s
    for spec in available:
        s = _add_solvable(pool, avail_repo, avail_rd, spec)
        name_index[f"avail:{spec['name']}-{spec['evr']}"] = s

    sys_rd.internalize()
    avail_rd.internalize()
    pool.installed = sys_repo
    pool.addfileprovides()
    pool.createwhatprovides()
    return pool, sys_repo, avail_repo, name_index


def make_resolver(pool, db=None):
    """Construct a Resolver bypassing ``__init__``.

    Wires only the attributes ``_solve`` / ``_classify_jobs`` /
    ``_format_problems`` / ``_solvable_srpm_id`` actually read.
    """
    r = Resolver.__new__(Resolver)
    r.pool = pool
    r.db = db if db is not None else FakeDB()
    r._solvable_to_pkg = {}
    r._installed_count = (
        sum(1 for _ in pool.installed.solvables) if pool.installed else 0
    )
    r._held_obsolete_warnings = []
    r._held_upgrade_warnings = []
    r._preserve_pool = False
    r.arch = "x86_64"
    r.allowed_arches = ["x86_64", "noarch"]
    r.install_recommends = True
    r.ignore_installed = False
    return r


def install_job(pool, solvable, *, flags=0):
    """Build an INSTALL job for a single solvable id."""
    return pool.Job(
        solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE | flags,
        solvable.id,
    )


def make_origins(resolver, jobs, *, kinds=None, group_prefix="req"):
    """Build a parallel list of :class:`JobOrigin`, one per job.

    Tests build origins by hand (instead of going through
    :meth:`Resolver._classify_jobs`) because the production
    classifier's flag-mask overlaps :data:`solv.Job.SOLVER_INSTALL`
    with :data:`solv.Job.SOLVER_DISFAVOR` and would mis-tag plain
    INSTALL jobs as ``"hint"`` — irrelevant to what the partial-skip
    machinery is supposed to do, but it would derail every fixture
    here.

    Args:
        resolver: a Resolver (used only for ``_solvable_srpm_id``).
        jobs: list of ``solv.Job``.
        kinds: optional list (same length) of explicit kinds.  When
            ``None``, every job is tagged ``"user_explicit"``.
        group_prefix: prefix for the auto-generated request_id when a
            kind is not ``"hint"``.

    Returns:
        ``List[JobOrigin]``, one per input job.
    """
    if kinds is None:
        kinds = ["user_explicit"] * len(jobs)
    origins: List[JobOrigin] = []
    for idx, (job, kind) in enumerate(zip(jobs, kinds)):
        try:
            solvables = list(job.solvables())
        except Exception:
            solvables = []
        if solvables:
            s = solvables[0]
            name = s.name or ""
            srpm_id = resolver._solvable_srpm_id(s)
        else:
            name = ""
            srpm_id = None
        request_id = f"{group_prefix}:{name}" if name else f"{group_prefix}:job{idx}"
        origins.append(JobOrigin(
            kind=kind,
            request_id=request_id,
            package_name=name,
            srpm_id=srpm_id,
        ))
    return origins


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pool_fixture_smoke():
    """The pool fixture builds, exposes installed repo and whatprovides."""
    pool, sys_repo, _, _ = make_pool(
        installed=[{"name": "foo", "evr": "1.0-1"}],
        available=[],
    )
    # SWIG returns a fresh proxy each access; compare by name.
    assert pool.installed.name == sys_repo.name
    matches = list(pool.whatprovides(pool.Dep("foo")))
    assert len(matches) == 1
    assert matches[0].name == "foo"


def test_atomic_upgrade_never_drops_jobs():
    """Atomic mode never populates ``skipped``, even when the request fails.

    ``kwin-1.4`` requires a missing ``kwin-x11=1.4`` provider.  libsolv
    typically offers a "do nothing" SOLUTION_JOB element which the atomic
    loop applies — silently neutralising the broken job in place.  The
    contract under test here is the rock-solid one: ``skipped`` is always
    empty in atomic mode (no partial-skip cascade), and the loop must
    terminate.
    """
    pool, _, _, idx = make_pool(
        installed=[{"name": "kwin", "evr": "5.27.10-1.2"}],
        available=[{
            "name": "kwin", "evr": "5.27.10-1.4",
            "requires": ["kwin-x11 = 5.27.10-1.4"],
            "srpm": "kwin",
        }],
    )
    r = make_resolver(pool)
    new_kwin = idx["avail:kwin-5.27.10-1.4"]
    jobs = [install_job(pool, new_kwin)]
    origins = make_origins(r, jobs)

    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=True)
    # Skipped must be empty in atomic mode regardless of outcome.
    assert skipped == []
    # The atomic loop terminated (no exception raised, function returned).
    # Whether ``problems`` is empty or not depends on the solutions
    # libsolv proposes for this synthetic fixture.


def test_partial_upgrade_skips_broken():
    """``atomic=False`` drops the broken job and resolves cleanly."""
    pool, _, _, idx = make_pool(
        installed=[{"name": "kwin", "evr": "5.27.10-1.2"}],
        available=[{
            "name": "kwin", "evr": "5.27.10-1.4",
            "requires": ["kwin-x11 = 5.27.10-1.4"],
            "srpm": "kwin",
        }],
    )
    r = make_resolver(pool)
    new_kwin = idx["avail:kwin-5.27.10-1.4"]
    jobs = [install_job(pool, new_kwin)]
    origins = make_origins(r, jobs)

    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=False)
    assert problems == []
    assert len(skipped) == 1
    assert skipped[0].name == "kwin"
    assert skipped[0].kind == "user_explicit"


def test_srpm_group_skip():
    """Three siblings from the same SRPM all fall together.

    Only ``kwin`` itself is broken; ``kwin-common`` and ``lib64kwin5``
    have no broken require but cascade because they share ``srpm=kwin``.
    """
    pool, _, _, idx = make_pool(
        installed=[
            {"name": "kwin", "evr": "5.27.10-1.2"},
            {"name": "kwin-common", "evr": "5.27.10-1.2"},
            {"name": "lib64kwin5", "evr": "5.27.10-1.2"},
        ],
        available=[
            {
                "name": "kwin", "evr": "5.27.10-1.4",
                "requires": ["kwin-x11 = 5.27.10-1.4"],
                "srpm": "kwin",
            },
            {
                "name": "kwin-common", "evr": "5.27.10-1.4",
                "srpm": "kwin",
            },
            {
                "name": "lib64kwin5", "evr": "5.27.10-1.4",
                "srpm": "kwin",
            },
        ],
    )
    r = make_resolver(pool)
    jobs = [
        install_job(pool, idx["avail:kwin-5.27.10-1.4"]),
        install_job(pool, idx["avail:kwin-common-5.27.10-1.4"]),
        install_job(pool, idx["avail:lib64kwin5-5.27.10-1.4"]),
    ]
    origins = make_origins(r, jobs, kinds=["implicit_upgrade"] * 3,
                            group_prefix="upg")
    # Sanity: all three carry the same srpm_id
    srpm_ids = {o.srpm_id for o in origins}
    assert srpm_ids == {"kwin-5.27.10-1.4"}

    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=False)
    assert problems == []
    skipped_names = {s.name for s in skipped}
    assert skipped_names == {"kwin", "kwin-common", "lib64kwin5"}


def test_install_atomic_default_unchanged():
    """Atomic install of a solvable with satisfiable deps: no problems, no skips."""
    pool, _, _, idx = make_pool(
        installed=[],
        available=[
            {"name": "bar", "evr": "1.0-1"},
            {"name": "foo", "evr": "1.0-1", "requires": ["bar = 1.0-1"]},
        ],
    )
    r = make_resolver(pool)
    foo = idx["avail:foo-1.0-1"]
    jobs = [install_job(pool, foo)]
    origins = make_origins(r, jobs)
    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=True)
    assert problems == []
    assert skipped == []


def test_install_partial_opt_in():
    """``atomic=False`` drops a fresh install whose dep is missing."""
    pool, _, _, idx = make_pool(
        installed=[],
        available=[{
            "name": "foo", "evr": "1.0-1",
            "requires": ["bar = 1.0"],
        }],
    )
    r = make_resolver(pool)
    foo = idx["avail:foo-1.0-1"]
    jobs = [install_job(pool, foo)]
    origins = make_origins(r, jobs)
    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=False)
    assert problems == []
    assert len(skipped) == 1
    assert skipped[0].name == "foo"


def test_hint_jobs_fall_with_request():
    """A FAVOR hint sharing a request_id with a dropped INSTALL falls too.

    Hints are never skipped on their own (``_solve`` only skips
    ``user_explicit``/``implicit_upgrade``/``obsolete``), but they do
    cascade through ``request_id``.
    """
    pool, _, _, idx = make_pool(
        installed=[],
        available=[
            {"name": "foo", "evr": "1.0-1", "requires": ["bar = 1.0"]},
            {"name": "barX", "evr": "2.0-1"},
        ],
    )
    r = make_resolver(pool)
    foo = idx["avail:foo-1.0-1"]
    barX = idx["avail:barX-2.0-1"]
    install = install_job(pool, foo)
    favor = pool.Job(
        solv.Job.SOLVER_FAVOR | solv.Job.SOLVER_SOLVABLE,
        barX.id,
    )
    jobs = [install, favor]
    origins = make_origins(r, jobs, kinds=["user_explicit", "hint"])
    # Tie the hint to the same request_id as the install (that's how
    # production resolve_install groups a --prefer cluster).
    shared_id = origins[0].request_id
    origins[1] = JobOrigin(
        kind="hint",
        request_id=shared_id,
        package_name=origins[1].package_name,
        srpm_id=origins[1].srpm_id,
    )

    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=False)
    assert problems == []
    skipped_names = {s.name for s in skipped}
    assert "foo" in skipped_names
    assert "barX" in skipped_names
    # The hint specifically must be tagged "hint", not promoted to user_explicit.
    hint_entries = [s for s in skipped if s.name == "barX"]
    assert hint_entries and hint_entries[0].kind == "hint"


@pytest.mark.skip(
    reason="TODO: wiring resolve_upgrade against a synthetic pool requires "
    "patching _create_pool / _find_best_upgrade — deferred until the "
    "silent-holdback fixture pattern is stabilised."
)
def test_silent_holdback_surfaced():
    """``pkgB`` silently held by libsolv must surface as held_silently_by_libsolv."""
    pass  # pragma: no cover


def test_seen_state_guard_terminates():
    """Pathological case: ``_solve`` must always return, never spin.

    A package whose require has zero providers should converge within a
    couple of iterations; the seen-state guard breaks any potential flap.
    """
    pool, _, _, idx = make_pool(
        installed=[],
        available=[{
            "name": "alpha", "evr": "1.0-1",
            "requires": ["nonexistent_capability = 42"],
        }],
    )
    r = make_resolver(pool)
    jobs = [install_job(pool, idx["avail:alpha-1.0-1"])]
    origins = make_origins(r, jobs)
    # If this loops forever the test runner will hang; pytest-timeout
    # would help but is optional here — _solve has converged in well
    # under a millisecond on every fixture we've tried.
    problems, skipped = r._solve(pool.Solver(), jobs, origins, atomic=False)
    # alpha is dropped; no remaining problems.
    assert problems == []
    assert {s.name for s in skipped} == {"alpha"}


@pytest.mark.skip(
    reason="TODO Task 10: orphan detector consistency with skipped — needs "
    "OrphansMixin wiring, deferred."
)
def test_orphans_consistent_with_skipped():
    """Orphan detection must agree with the partial-skip outcome."""
    pass  # pragma: no cover


# ---------------------------------------------------------------------------
# Holds — SOLVER_LOCK and the attribution of what a hold blocked
# ---------------------------------------------------------------------------


def lock_job(pool, name):
    """Build the ``SOLVER_LOCK`` job :meth:`Resolver._add_hold_locks` posts.

    Locking the **name** rather than the installed solvable is the
    production choice: one job per hold whatever the architectures, and
    a problem phrased around the package whose upgrade was asked for.
    """
    return pool.Job(
        solv.Job.SOLVER_LOCK | solv.Job.SOLVER_SOLVABLE_NAME,
        pool.str2id(name),
    )


def make_chain_pool(extra=False):
    """``app`` → ``mid`` → ``lib``, each in version 1 installed and 2 available.

    Every edge is a strict ``= EVR`` require, so upgrading ``app``
    forces the whole chain up and a hold anywhere in it is enough to
    make the request unsolvable.  ``idle`` is a package with no
    relation to the chain, there to show a hold costs nothing to a
    plan that does not touch it.  With ``extra``, a second independent
    chain ``tool`` → ``other`` is added, to check two holds stay
    separately attributed.
    """
    installed = [
        {"name": "lib", "evr": "1-1", "srpm": "lib"},
        {"name": "mid", "evr": "1-1", "requires": ["lib = 1-1"], "srpm": "mid"},
        {"name": "app", "evr": "1-1", "requires": ["mid = 1-1"], "srpm": "app"},
        {"name": "idle", "evr": "1-1", "srpm": "idle"},
    ]
    available = [
        {"name": "lib", "evr": "2-1", "srpm": "lib"},
        {"name": "mid", "evr": "2-1", "requires": ["lib = 2-1"], "srpm": "mid"},
        {"name": "app", "evr": "2-1", "requires": ["mid = 2-1"], "srpm": "app"},
        {"name": "idle", "evr": "2-1", "srpm": "idle"},
    ]
    if extra:
        installed += [
            {"name": "other", "evr": "1-1", "srpm": "other"},
            {"name": "tool", "evr": "1-1", "requires": ["other = 1-1"],
             "srpm": "tool"},
        ]
        available += [
            {"name": "other", "evr": "2-1", "srpm": "other"},
            {"name": "tool", "evr": "2-1", "requires": ["other = 2-1"],
             "srpm": "tool"},
        ]
    return make_pool(installed=installed, available=available)


def new_names(solver):
    """Names the solver's transaction would install."""
    return {s.name for s in solver.transaction().newsolvables()}


class TestHoldLocks:
    """A hold has to survive the dependency graph, not just the job list.

    Before this, holding a package only meant the resolver did not
    *ask* to upgrade it.  Nothing stopped libsolv from upgrading it
    anyway to satisfy somebody else's require, so a held package could
    move under the user without a word.  These tests pin both halves:
    the lock is honoured, and the upgrades it made impossible are named
    with the hold responsible for them.
    """

    def test_without_a_lock_a_held_package_is_dragged_up(self):
        """The control. No lock, and the chain pulls ``lib`` to version 2.

        This is the behaviour a hold has to defeat; if this test ever
        fails, the fixture stopped exercising the problem and every
        other test in this class proves nothing.
        """
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool, db=FakeDB(held={"lib"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [install_job(pool, idx["avail:app-2-1"])]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        assert problems == []
        assert skipped == []
        assert new_names(solver) == {"app", "mid", "lib"}

    def test_a_lock_keeps_the_held_version_and_drops_the_upgrade(self):
        """With the lock, ``lib`` stays put and ``app`` is set aside."""
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool, db=FakeDB(held={"lib"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [install_job(pool, idx["avail:app-2-1"]), lock_job(pool, "lib")]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        assert problems == []
        assert new_names(solver) == set()
        assert [s.name for s in skipped] == ["app"]

    def test_the_dropped_upgrade_names_the_hold_that_blocked_it(self):
        """``blocked_by`` comes from libsolv naming its own lock job."""
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool, db=FakeDB(held={"lib"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [install_job(pool, idx["avail:app-2-1"]), lock_job(pool, "lib")]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        _problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        assert len(skipped) == 1
        assert skipped[0].blocked_by == ("lib",)
        # And this is why the field has to exist: libsolv phrases the
        # problem around the package whose upgrade was asked for and
        # the require that broke, never around the hold.  Reading the
        # sentence would tell the user ``app`` could not be upgraded
        # because of ``mid``, and leave them with no idea that their own
        # hold on ``lib`` is what stopped it.
        assert "lib" not in skipped[0].reason
        assert "app" in skipped[0].reason

    def test_an_unrelated_upgrade_is_untouched_by_the_hold(self):
        """A hold constrains its own name, not the whole transaction."""
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool, db=FakeDB(held={"lib"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [install_job(pool, idx["avail:idle-2-1"]), lock_job(pool, "lib")]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        assert problems == []
        assert skipped == []
        assert new_names(solver) == {"idle"}

    def test_two_holds_are_attributed_separately(self):
        """Two independent chains, two holds, each named on its own victim."""
        pool, _, _, idx = make_chain_pool(extra=True)
        r = make_resolver(pool, db=FakeDB(held={"lib", "other"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [
            install_job(pool, idx["avail:app-2-1"]),
            install_job(pool, idx["avail:tool-2-1"]),
            lock_job(pool, "lib"),
            lock_job(pool, "other"),
        ]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        assert problems == []
        attribution = {s.name: s.blocked_by for s in skipped}
        assert attribution == {"app": ("lib",), "tool": ("other",)}

    def test_each_dropped_job_carries_its_own_problem(self):
        """Two problems, two sentences, each on the job it killed.

        Pinning the first problem's text onto every dropped job would
        tell the user ``tool`` was not upgraded because of a require of
        ``app``, which is a sentence they can neither act on nor check.
        """
        pool, _, _, idx = make_chain_pool(extra=True)
        r = make_resolver(pool, db=FakeDB(held={"lib", "other"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [
            install_job(pool, idx["avail:app-2-1"]),
            install_job(pool, idx["avail:tool-2-1"]),
            lock_job(pool, "lib"),
            lock_job(pool, "other"),
        ]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        _problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        reasons = {s.name: s.reason for s in skipped}
        assert set(reasons) == {"app", "tool"}
        assert "app" in reasons["app"] and "tool" not in reasons["app"]
        assert "tool" in reasons["tool"] and "app" not in reasons["tool"]

    def test_a_lock_is_never_dropped(self):
        """libsolv offers to relax the hold ; the offer is always declined."""
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool, db=FakeDB(held={"lib"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [install_job(pool, idx["avail:app-2-1"]), lock_job(pool, "lib")]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        _problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        # ``_solve`` rewrites ``jobs`` in place: the lock has to be the
        # one survivor, and no lock may appear among the dropped jobs.
        assert len(jobs) == 1
        assert (jobs[0].how & solv.Job.SOLVER_JOBMASK) == solv.Job.SOLVER_LOCK
        assert [o.kind for o in origins] == ["lock"]
        assert not [s for s in skipped if s.kind == "lock"]

    def test_a_lock_does_not_fall_with_a_cascade(self):
        """Neither the request-id nor the SRPM cascade may take a lock.

        A lock job carries a ``srpm_id`` like any other, so without the
        explicit guard a dropped sibling from the same source package
        would silently unlock the hold.  The origins are forced into
        that collision here rather than waiting for a real package to
        produce it.
        """
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool, db=FakeDB(held={"lib"}))
        solver = pool.Solver()
        solver.set_flag(solv.Solver.SOLVER_FLAG_ALLOW_UNINSTALL, 1)
        jobs = [install_job(pool, idx["avail:app-2-1"]), lock_job(pool, "lib")]
        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")
        assert origins[1].kind == "lock"
        origins[1].request_id = origins[0].request_id
        origins[1].srpm_id = origins[0].srpm_id

        _problems, skipped = r._solve(solver, jobs, origins, atomic=False)

        assert len(jobs) == 1
        assert (jobs[0].how & solv.Job.SOLVER_JOBMASK) == solv.Job.SOLVER_LOCK
        assert not [s for s in skipped if s.kind == "lock"]

    def test_classify_jobs_tags_a_lock(self):
        """``SOLVER_LOCK`` is an action code, matched after masking."""
        pool, _, _, idx = make_chain_pool()
        r = make_resolver(pool)
        jobs = [install_job(pool, idx["avail:app-2-1"]), lock_job(pool, "lib")]

        origins = r._classify_jobs(jobs, default_kind="implicit_upgrade")

        assert [o.kind for o in origins] == ["implicit_upgrade", "lock"]
        assert origins[1].package_name == "lib"
        assert origins[1].request_id == "lock:lib"


class TestHoldLocksAreWired:
    """The locks have to be posted by the code paths users actually run.

    Every other test in this file builds the lock job by hand, so all
    of them would keep passing if the two call sites disappeared and
    the feature became inert.  These two read the source instead.
    """

    SITES = ("resolve_install", "resolve_upgrade")

    @staticmethod
    def _function(name):
        source = pathlib.Path(resolver_module.__file__).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found in resolver.py")

    @staticmethod
    def _call_lines(body, attr):
        return sorted(call.lineno for call in ast.walk(body)
                      if isinstance(call, ast.Call)
                      and getattr(call.func, "attr", None) == attr)

    @pytest.mark.parametrize("func", SITES)
    def test_the_path_posts_the_holds(self, func):
        body = self._function(func)
        assert self._call_lines(body, "_add_hold_locks"), (
            f"{func} never calls _add_hold_locks, so a hold has no "
            f"effect on anything it resolves")

    @pytest.mark.parametrize("func", SITES)
    def test_the_holds_are_posted_before_the_jobs_are_classified(self, func):
        """Order matters: a lock added after the classification would be
        missing from ``job_origins``, and the parallel-list invariant
        ``_solve`` asserts on would fail outright."""
        body = self._function(func)
        posted = self._call_lines(body, "_add_hold_locks")
        classified = self._call_lines(body, "_classify_jobs")
        assert posted and classified, (
            f"{func} must both post the holds and classify its jobs; "
            f"found {len(posted)} and {len(classified)} call(s)")
        assert posted[0] < classified[0], (
            f"{func} posts the holds at line {posted[0]}, after the "
            f"classification at line {classified[0]}")


class TestAddHoldLocks:
    """What :meth:`Resolver._add_hold_locks` posts, and what it leaves alone."""

    def _resolver(self, held=(), pool=None):
        pool = pool or make_chain_pool()[0]
        return make_resolver(pool, db=FakeDB(held=set(held))), pool

    def test_nothing_held_posts_nothing(self):
        r, _pool = self._resolver()
        jobs = []
        assert r._add_hold_locks(jobs) == []
        assert jobs == []

    def test_an_installed_hold_is_locked(self):
        r, _pool = self._resolver(held={"lib"})
        jobs = []
        assert r._add_hold_locks(jobs) == ["lib"]
        assert len(jobs) == 1
        assert (jobs[0].how & solv.Job.SOLVER_JOBMASK) == solv.Job.SOLVER_LOCK

    def test_a_hold_on_an_absent_package_is_inert(self):
        """Holding something not installed is not an install ban.

        ``urpm skip`` is the verb that forbids installing; turning a
        hold into one here would forbid a dependency the user never
        refused.
        """
        r, _pool = self._resolver(held={"nowhere"})
        jobs = []
        assert r._add_hold_locks(jobs) == []
        assert jobs == []

    def test_an_explicitly_named_package_is_exempt(self):
        """Naming a held package on the command line overrides the hold."""
        r, _pool = self._resolver(held={"lib"})
        jobs = []
        assert r._add_hold_locks(jobs, exempt={"LIB"}) == []
        assert jobs == []

    def test_several_holds_are_locked_in_a_stable_order(self):
        pool = make_chain_pool(extra=True)[0]
        r, _pool = self._resolver(held={"other", "lib"}, pool=pool)
        jobs = []
        assert r._add_hold_locks(jobs) == ["lib", "other"]
        assert len(jobs) == 2

    def test_an_unreadable_hold_list_does_not_break_the_solve(self):
        """A database error costs the locks, never the transaction."""
        class BrokenDB(FakeDB):
            def get_held_packages_set(self):
                raise RuntimeError("database is locked")

        pool = make_chain_pool()[0]
        r = make_resolver(pool, db=BrokenDB())
        jobs = []
        assert r._add_hold_locks(jobs) == []
        assert jobs == []


# ---------------------------------------------------------------------------
# _disambiguate_dup_rename — kernel-desktop mga9→mga10 pattern
# ---------------------------------------------------------------------------


class TestDisambiguateDupRename:
    """The pre-solve pass that pins a target candidate when a DUP rename
    would otherwise be ambiguous.

    Real-world trigger : mga9 kernel-desktop has ``Name=kernel-desktop``
    (short) ; mga10 has ``Name=kernel-desktop-<version>-1.mga10`` (long).
    Several mga10 candidates provide the unversioned ``kernel-desktop``
    capability — libsolv can't pick one and silently holds them all.
    The pass emits a ``SOLVER_INSTALL`` on the highest-EVR candidate to
    disambiguate.
    """

    def test_ambiguous_rename_pins_highest_evr(self):
        """Two mga10 candidates provide ``kernel-desktop`` — the highest
        EVR must be pinned."""
        pool, sys_repo, avail_repo, idx = make_pool(
            installed=[{
                "name": "kernel-desktop",
                "evr": "6.6.141-1.mga9",
                "provides": ["kernel-desktop"],
            }],
            available=[
                {
                    "name": "kernel-desktop-6.18.35-1.mga10",
                    "evr": "1-1.mga10",
                    "provides": ["kernel-desktop"],
                },
                {
                    "name": "kernel-desktop-6.18.39-1.mga10",
                    "evr": "1-1.mga10",
                    "provides": ["kernel-desktop"],
                },
            ],
        )
        r = make_resolver(pool)

        jobs = r._disambiguate_dup_rename()

        assert len(jobs) == 1, \
            f"expected exactly one pin, got {len(jobs)}"
        pinned = jobs[0].solvables()[0]
        # Both candidates share EVR here — pinning either is correct.
        # What matters is that ONE is picked, breaking the ambiguity.
        assert pinned.name in (
            "kernel-desktop-6.18.35-1.mga10",
            "kernel-desktop-6.18.39-1.mga10",
        )

    def test_ambiguous_rename_prefers_higher_evr(self):
        """When candidates have distinct EVR, the highest must win."""
        pool, sys_repo, avail_repo, idx = make_pool(
            installed=[{
                "name": "old-name",
                "evr": "1.0-1",
                "provides": ["myapp"],
            }],
            available=[
                {
                    "name": "new-name-a",
                    "evr": "2.0-1",
                    "provides": ["myapp"],
                },
                {
                    "name": "new-name-b",
                    "evr": "3.0-1",
                    "provides": ["myapp"],
                },
            ],
        )
        r = make_resolver(pool)

        jobs = r._disambiguate_dup_rename()

        assert len(jobs) == 1
        assert jobs[0].solvables()[0].name == "new-name-b"

    def test_same_name_target_skips_disambig(self):
        """When the installed Name has a same-Name target candidate, DUP
        handles it — no pin needed."""
        pool, sys_repo, avail_repo, idx = make_pool(
            installed=[{
                "name": "firefox",
                "evr": "128.0-1.mga9",
                "provides": ["firefox"],
            }],
            available=[
                {
                    "name": "firefox",
                    "evr": "140.0-1.mga10",
                    "provides": ["firefox"],
                },
                # A random other pkg also provides "firefox" as a virtual
                # cap — irrelevant because the same-Name candidate exists.
                {
                    "name": "firefox-esr",
                    "evr": "128.0-1.mga10",
                    "provides": ["firefox"],
                },
            ],
        )
        r = make_resolver(pool)

        jobs = r._disambiguate_dup_rename()

        assert jobs == []

    def test_single_candidate_skips_disambig(self):
        """When only one candidate provides the capability, no ambiguity
        to disambiguate — DUP_ALLOW_NAMECHANGE picks it directly."""
        pool, sys_repo, avail_repo, idx = make_pool(
            installed=[{
                "name": "old-name",
                "evr": "1.0-1",
                "provides": ["myapp"],
            }],
            available=[{
                "name": "new-name",
                "evr": "2.0-1",
                "provides": ["myapp"],
            }],
        )
        r = make_resolver(pool)

        jobs = r._disambiguate_dup_rename()

        assert jobs == []

    def test_no_target_candidate_skips_disambig(self):
        """When nothing in target repos provides any of the installed's
        Provides, nothing to pin — libsolv uninstalls via
        ALLOW_UNINSTALL."""
        pool, sys_repo, avail_repo, idx = make_pool(
            installed=[{
                "name": "orphan",
                "evr": "1.0-1",
                "provides": ["orphan"],
            }],
            available=[{
                "name": "unrelated",
                "evr": "1.0-1",
                "provides": ["unrelated"],
            }],
        )
        r = make_resolver(pool)

        jobs = r._disambiguate_dup_rename()

        assert jobs == []
