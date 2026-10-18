"""Boolean dependencies, which have a grammar and not a shape.

Reported from the field: ``urpm build python-gitpython.spec`` on
Mageia 10 failed on a dependency that was perfectly satisfiable.
``%generate_buildrequires`` had emitted

    (python3dist(gitdb) < 5~~ with python3dist(gitdb) >= 4.0.1)

and ``python3-gitdb-4.0.11-3.mga10`` provides ``python3dist(gitdb)``
in Core Release, which is inside that range.

Two of our own regexes were to blame, both taking a dependency string
for a shape rather than a grammar.  The install target loop split on
the first operator with a non-greedy ``(.+?)``, leaving
``(python3dist(gitdb)`` to look up; and the want/have diagnostic
removed version constraints with a greedy ``\\S+`` that swallowed the
closing parenthesis too, reporting « No package provides
'(python3dist(gitdb) with python3dist(gitdb)' » — a string nobody had
asked for, which made the missing constraints look like the cause.

libsolv ships the parser for this grammar.  These tests pin that every
site that turns a dependency string into a job or a query uses it.
"""

from __future__ import annotations

import pytest

solv = pytest.importorskip("solv", reason="libsolv bindings unavailable")

from urpm.core.buildrequires import rpm_dep_to_solver_format
from urpm.core.resolver import is_rich_dep, parse_capability, parse_rich_dep


#: The dependency from the report, verbatim.
GITDB = "(python3dist(gitdb) < 5~~ with python3dist(gitdb) >= 4.0.1)"


@pytest.fixture
def pool():
    pool = solv.Pool()
    pool.setarch()
    return pool


def add_gitdb(pool, version):
    """A package providing ``python3dist(gitdb) = <version>``.

    ``pool.repos`` starts with a ``None`` — libsolv numbers repos from
    1 — so the existing one is looked up rather than indexed.
    """
    repo = next((r for r in pool.repos if r), None) or pool.add_repo("test")
    s = repo.add_solvable()
    s.name = "python3-gitdb"
    s.evr = f"{version}-3.mga10"
    s.arch = "noarch"
    s.add_deparray(
        solv.SOLVABLE_PROVIDES,
        pool.Dep("python3dist(gitdb)").Rel(solv.REL_EQ, pool.Dep(version)))
    return s


# ── Recognising one ─────────────────────────────────────────────────────


@pytest.mark.parametrize("dep", [
    GITDB,
    "(pkgA or pkgB)",
    "(pkgA if pkgB)",
    "(pkgA without pkgB)",
    "  (pkgA and pkgB)",          # leading space, as rpm -qp can emit
])
def test_a_boolean_dependency_is_recognised(dep):
    assert is_rich_dep(dep)


@pytest.mark.parametrize("dep", [
    "python3dist(gitdb)",         # a parenthesis, but not at the front
    "python3dist(gitdb) >= 4.0.1",
    "pkgconfig(glib-2.0)",
    "plain-name",
])
def test_an_ordinary_capability_is_not(dep):
    assert not is_rich_dep(dep)


# ── Parsing it ──────────────────────────────────────────────────────────


def test_the_expression_survives_the_round_trip(pool):
    """What goes in comes out, operators and versions included."""
    dep = parse_rich_dep(pool, GITDB)

    assert dep is not None
    assert str(dep) == GITDB


def test_an_unparseable_expression_is_reported_not_guessed(pool):
    """A caller's request has to fail loudly.

    The lenient path exists for media import, where one bad dependency
    must not abort a whole medium; a request is the opposite case.
    """
    assert parse_rich_dep(pool, "(python3dist(gitdb) frobnicate 4)") is None


def test_media_import_stays_lenient(pool):
    """One malformed dependency must not stop a medium from loading."""
    dep = parse_capability(pool, "(python3dist(gitdb) frobnicate 4)")

    assert dep is not None          # opaque, provided by nothing


# ── Resolving it ────────────────────────────────────────────────────────


def test_the_constraint_picks_the_candidate_in_range(pool):
    """The whole point: 4.0.11 is in range, 5.1.0 is not.

    ``pool.select`` has no flag that evaluates a boolean dependency,
    so the job is built from the parsed Dep with
    ``SOLVER_SOLVABLE_PROVIDES``.
    """
    add_gitdb(pool, "4.0.11")
    add_gitdb(pool, "5.1.0")
    pool.createwhatprovides()

    dep = parse_rich_dep(pool, GITDB)
    solver = pool.Solver()
    problems = solver.solve([pool.Job(
        solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE_PROVIDES,
        dep.id)])

    assert not problems, [str(p) for p in problems]
    chosen = [s.name + "-" + s.evr
              for s in solver.transaction().newsolvables()]
    assert chosen == ["python3-gitdb-4.0.11-3.mga10"]


def test_a_selection_cannot_do_it(pool):
    """Why the job is built from a Dep and not from a selection.

    This is the call the install loop used to make, and it comes back
    empty — which is how a satisfiable dependency was reported as
    « Package not found ».
    """
    add_gitdb(pool, "4.0.11")
    pool.createwhatprovides()

    sel = pool.select(GITDB,
                      solv.Selection.SELECTION_NAME
                      | solv.Selection.SELECTION_CANON
                      | solv.Selection.SELECTION_DOTARCH
                      | solv.Selection.SELECTION_REL)

    assert sel.isempty()


def test_an_out_of_range_only_pool_reports_a_problem(pool):
    """Not « not found »: the capability exists, the version does not.

    The distinction is the whole value of the diagnostic — a Mageia
    mirror that has not rebuilt a package yet looks exactly like this.
    """
    add_gitdb(pool, "5.1.0")
    pool.createwhatprovides()

    dep = parse_rich_dep(pool, GITDB)
    solver = pool.Solver()
    problems = solver.solve([pool.Job(
        solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE_PROVIDES,
        dep.id)])

    assert problems


# ── The sites that must leave it alone ──────────────────────────────────


def test_the_solver_format_conversion_passes_it_through():
    """Its name pattern cannot start with a parenthesis, by design."""
    assert rpm_dep_to_solver_format(GITDB) == GITDB


def test_the_build_requires_extraction_keeps_it_whole():
    """Splitting on commas and stripping versions both destroy it."""
    from urpm.cli.commands.build import _is_rich_dep

    assert _is_rich_dep(GITDB)


def test_the_diagnostic_names_each_capability_in_the_expression():
    """Instead of one mangled string that means nothing.

    The tokens are *found* in the expression, never interpreted: the
    operators and versions around them stay libsolv's business.
    """
    from urpm.cli.commands.build import (_CAPABILITY_TOKEN, _RICH_KEYWORDS)

    tokens = [m.group(0) for m in _CAPABILITY_TOKEN.finditer(GITDB)]
    capabilities = [t for t in tokens if t not in _RICH_KEYWORDS]

    assert "python3dist(gitdb)" in capabilities
    assert "with" not in capabilities
    # The versions are not capabilities and must not be queried as if
    # they were.  ``5~~`` and ``4.0.1`` start with a digit, which the
    # token pattern excludes.
    assert not any(t[0].isdigit() for t in capabilities)
