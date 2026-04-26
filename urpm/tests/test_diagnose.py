"""Tests for urpm.core.resolution.diagnose."""

import rpm

from urpm.core.resolution.diagnose import (
    DepIssue,
    classify_unsatisfied_dep,
    format_dependency_issue,
    from_libsolv_problem,
    from_rpmlib_tuple,
)


class FakeDB:
    """Minimal stand-in for PackageDatabase.whatprovides."""

    def __init__(self, providers):
        # providers: {capability_name: [{name, version, release, ...}, ...]}
        self._providers = providers

    def whatprovides(self, capability):
        return list(self._providers.get(capability, []))


def _pkg(name, version, release="1.mga9"):
    return {"name": name, "version": version, "release": release, "arch": "x86_64",
            "nevra": f"{name}-{version}-{release}.x86_64"}


class TestClassifyMissing:
    def test_no_providers_returns_missing(self):
        db = FakeDB({})
        issue = classify_unsatisfied_dep(db, "kwin-x11", "=", "5.27.10-1.4.mga9")
        assert issue.kind == "missing"
        assert issue.dep_name == "kwin-x11"
        assert issue.dep_op == "="
        assert issue.dep_version == "5.27.10-1.4.mga9"

    def test_propagates_sense_label_and_requester(self):
        db = FakeDB({})
        issue = classify_unsatisfied_dep(
            db, "foo", sense_label="requires", requester="kwin-5.27.10-1.4"
        )
        assert issue.kind == "missing"
        assert issue.sense_label == "requires"
        assert issue.requester == "kwin-5.27.10-1.4"


class TestClassifyVersionMismatch:
    def test_only_old_versions_available(self):
        db = FakeDB({"libfoo": [_pkg("libfoo", "1.5"), _pkg("libfoo", "1.8")]})
        issue = classify_unsatisfied_dep(db, "libfoo", ">=", "2.0")
        assert issue.kind == "version_mismatch"
        assert sorted(issue.available_versions) == ["1.5-1.mga9", "1.8-1.mga9"]
        assert issue.dep_op == ">="
        assert issue.dep_version == "2.0"

    def test_dedupes_and_keeps_order(self):
        db = FakeDB({
            "libfoo": [
                _pkg("libfoo", "1.5"),
                _pkg("libfoo", "1.5"),
                _pkg("libfoo", "1.8"),
            ]
        })
        issue = classify_unsatisfied_dep(db, "libfoo", ">=", "2.0")
        assert issue.available_versions == ["1.5-1.mga9", "1.8-1.mga9"]


class TestClassifyUnknown:
    def test_provider_satisfies_falls_back_to_unknown(self):
        # Provider exists with a satisfying version and no pool to detect
        # an exclusion: we cannot explain the error, return "unknown".
        db = FakeDB({"libfoo": [_pkg("libfoo", "2.5")]})
        issue = classify_unsatisfied_dep(db, "libfoo", ">=", "2.0")
        assert issue.kind == "unknown"

    def test_unversioned_with_provider_returns_unknown(self):
        db = FakeDB({"libfoo": [_pkg("libfoo", "1.0")]})
        issue = classify_unsatisfied_dep(db, "libfoo")
        assert issue.kind == "unknown"


class TestClassifyExcluded:
    class _FakeDep:
        def __init__(self, name):
            self.name = name

    class _FakePool:
        """Pool whose whatprovides returns nothing — simulates filter exclusion."""

        def Dep(self, name):
            return TestClassifyExcluded._FakeDep(name)

        def whatprovides(self, dep):
            return []

    def test_provider_filtered_out_returns_excluded(self):
        db = FakeDB({"libfoo": [_pkg("libfoo", "1.0")]})
        issue = classify_unsatisfied_dep(
            db, "libfoo", pool=self._FakePool()
        )
        assert issue.kind == "excluded"
        assert issue.reason

    def test_no_pool_no_excluded_kind(self):
        db = FakeDB({"libfoo": [_pkg("libfoo", "1.0")]})
        issue = classify_unsatisfied_dep(db, "libfoo")
        assert issue.kind != "excluded"


class TestFormatDependencyIssue:
    def test_missing_with_requester(self):
        issue = DepIssue(
            kind="missing",
            dep_name="kwin-x11",
            dep_op="=",
            dep_version="5.27.10-1.4.mga9",
            requester="kwin-5.27.10-1.4",
        )
        msg = format_dependency_issue(issue)
        assert "kwin-5.27.10-1.4" in msg
        assert "kwin-x11 = 5.27.10-1.4.mga9" in msg
        assert "n'existe" in msg

    def test_version_mismatch_singular(self):
        issue = DepIssue(
            kind="version_mismatch",
            dep_name="libfoo",
            dep_op=">=",
            dep_version="2.0",
            available_versions=["1.5-1.mga9"],
        )
        msg = format_dependency_issue(issue)
        assert "1.5-1.mga9" in msg
        assert "libfoo >= 2.0" in msg

    def test_version_mismatch_plural(self):
        issue = DepIssue(
            kind="version_mismatch",
            dep_name="libfoo",
            dep_op=">=",
            dep_version="2.0",
            available_versions=["1.5-1.mga9", "1.8-1.mga9"],
        )
        msg = format_dependency_issue(issue)
        assert "1.5-1.mga9, 1.8-1.mga9" in msg

    def test_excluded(self):
        issue = DepIssue(kind="excluded", dep_name="foo", reason="medium disabled")
        msg = format_dependency_issue(issue)
        assert "medium disabled" in msg

    def test_unknown_falls_back_cleanly(self):
        issue = DepIssue(kind="unknown", dep_name="foo")
        msg = format_dependency_issue(issue)
        assert "foo" in msg

    def test_conflict_phrasing(self):
        issue = DepIssue(
            kind="missing",
            dep_name="bar",
            sense_label="conflicts",
            requester="foo-1.0",
        )
        msg = format_dependency_issue(issue)
        assert "conflit" in msg.lower()


class TestFromRpmlibTuple:
    def test_decodes_kwin_like_tuple(self):
        db = FakeDB({})
        flags = rpm.RPMSENSE_EQUAL
        sense = rpm.RPMDEP_SENSE_REQUIRES
        t = (("kwin", "5.27.10", "1.4.mga9"),
             ("kwin-x11", "5.27.10-1.4.mga9"),
             flags, None, sense)
        issue = from_rpmlib_tuple(t, db=db)
        assert issue.kind == "missing"
        assert issue.dep_name == "kwin-x11"
        assert issue.dep_op == "="
        assert issue.dep_version == "5.27.10-1.4.mga9"
        assert issue.requester == "kwin-5.27.10-1.4.mga9"
        assert issue.sense_label == "requires"

    def test_malformed_tuple_falls_back(self):
        issue = from_rpmlib_tuple("not a tuple")
        assert issue.kind == "unknown"

    def test_no_db_returns_unknown_with_fields(self):
        t = (("a", "1", "1"), ("b", "2"), rpm.RPMSENSE_EQUAL, None,
             rpm.RPMDEP_SENSE_REQUIRES)
        issue = from_rpmlib_tuple(t)
        assert issue.kind == "unknown"
        assert issue.dep_name == "b"
        assert issue.dep_op == "="


class TestFromLibsolvProblem:
    class _FakeDep:
        def __init__(self, s):
            self._s = s

        def __str__(self):
            return self._s

    class _FakeInfo:
        def __init__(self, dep_str, solvable_str=""):
            self.dep = TestFromLibsolvProblem._FakeDep(dep_str)
            self.solvable = solvable_str or None

    class _FakeRule:
        def __init__(self, info):
            self._info = info

        def info(self):
            return self._info

    class _FakeProblem:
        def __init__(self, rule):
            self._rule = rule

        def findproblemrule(self):
            return self._rule

        def __str__(self):
            return "fake-problem"

    def test_parses_versioned_dep(self):
        info = self._FakeInfo("kwin-x11 = 5.27.10-1.4.mga9", "kwin-5.27.10-1.4.mga9.x86_64")
        problem = self._FakeProblem(self._FakeRule(info))
        issue = from_libsolv_problem(problem, pool=None, db=None)
        assert issue.dep_name == "kwin-x11"
        assert issue.dep_op == "="
        assert issue.dep_version == "5.27.10-1.4.mga9"
        assert issue.requester == "kwin-5.27.10-1.4.mga9.x86_64"

    def test_unversioned_dep(self):
        info = self._FakeInfo("foo")
        problem = self._FakeProblem(self._FakeRule(info))
        issue = from_libsolv_problem(problem, pool=None, db=None)
        assert issue.dep_name == "foo"
        assert issue.dep_op == ""

    def test_no_rule_falls_back_to_str(self):
        class NoRule:
            def findproblemrule(self):
                return None

            def __str__(self):
                return "raw-problem"

        issue = from_libsolv_problem(NoRule(), pool=None)
        assert issue.kind == "unknown"
        assert issue.dep_name == "raw-problem"
