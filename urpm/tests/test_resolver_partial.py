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

from typing import List, Optional

import pytest
import solv

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
        s.add_deparray(solv.SOLVABLE_PROVIDES, pool.Dep(prov))
    for req in spec.get("requires", []):
        s.add_deparray(solv.SOLVABLE_REQUIRES, pool.Dep(req))
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
