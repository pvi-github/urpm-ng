"""Tests for the per-package verdict written to the history.

Every transaction records its packages at ``planned`` and, until now,
left them there: ``record_action_end`` had no caller anywhere in the
tree, so on a real machine all 1453 rows read ``planned`` and nothing
that keys on ``done`` could ever be true.  That is what kept the
post-operation rules inert.

The verdict comes from the rpm database rather than from rpm's
callback, and that is the point worth testing: the callback fires
``INST_STOP`` even when the cpio payload failed to extract, so a
package can be announced installed and be absent.  The callback still
says *why* something is missing, which is what separates ``skipped``
from ``failed``.

The other thing worth testing is that nothing is written when no
transaction ran.  A ``--test`` builds the rpm transaction and commits
nothing; judging its rows against the database would find none of the
packages and mark every one of them failed.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from urpm.core import rpmdb
from urpm.core.operations import PackageOperations
from urpm.core.rpmdb import InstalledPkg, installed_nevras

REPO = Path(__file__).resolve().parents[2]


def _pkg(name, version="1.0", release="1.mga10", arch="x86_64", epoch=""):
    return InstalledPkg(name=name, epoch=epoch, version=version,
                        release=release, arch=arch)


class TestInstalledNevras:
    """The primitive the verdict rests on."""

    @pytest.fixture
    def rpmdb_holding(self, monkeypatch):
        """Pretend the database holds exactly these builds."""
        def install(*packages):
            by_name = {}
            for pkg in packages:
                by_name.setdefault(pkg.name, []).append(pkg)
            monkeypatch.setattr(rpmdb, "query_by_name",
                                lambda name, root="/": by_name.get(name, []))
        return install

    def test_an_installed_build_is_found(self, rpmdb_holding):
        rpmdb_holding(_pkg("bash", "5.3", "2.mga10"))

        assert installed_nevras((("bash", "bash-5.3-2.mga10.x86_64"),)) == {
            "bash-5.3-2.mga10.x86_64"}

    def test_the_epoch_may_be_spelled_either_way(self, rpmdb_holding):
        # RPM omits a zero epoch in filenames and prints it in some
        # queries; the planner and the database do not always agree.
        rpmdb_holding(_pkg("bash", "5.3", "2.mga10"))

        assert installed_nevras((("bash", "bash-0:5.3-2.mga10.x86_64"),)) == {
            "bash-0:5.3-2.mga10.x86_64"}

    def test_a_real_epoch_is_honoured(self, rpmdb_holding):
        rpmdb_holding(_pkg("glibc", "2.42", "9.mga10", epoch="6"))

        assert installed_nevras((("glibc", "glibc-6:2.42-9.mga10.x86_64"),))

    def test_another_version_of_the_same_name_is_not_a_match(self,
                                                             rpmdb_holding):
        # The whole reason this is not ``is_installed``: an upgrade puts
        # a precise build on disk while the previous one may still be
        # there, so the name alone answers the wrong question.
        rpmdb_holding(_pkg("bash", "5.3", "2.mga10"))

        assert installed_nevras((("bash", "bash-5.2-1.mga9.x86_64"),)) == set()

    def test_a_package_that_is_not_there_at_all(self, rpmdb_holding):
        rpmdb_holding()

        assert installed_nevras((("ghost", "ghost-1-1.mga10.noarch"),)) == set()

    def test_several_builds_of_one_name_coexisting(self, rpmdb_holding):
        # kernels, java: the lookup must scan every build of the name.
        rpmdb_holding(_pkg("kernel", "6.6", "1.mga10"),
                      _pkg("kernel", "6.12", "2.mga10"))

        assert installed_nevras((("kernel", "kernel-6.12-2.mga10.x86_64"),)) \
            == {"kernel-6.12-2.mga10.x86_64"}

    def test_an_incomplete_pair_is_skipped(self, rpmdb_holding):
        rpmdb_holding(_pkg("bash"))

        assert installed_nevras((("", ""), ("bash", ""))) == set()

    def test_the_lookup_is_targeted_not_a_full_walk(self, monkeypatch):
        # 0.19 ms per package against 300 ms for a complete scan on a
        # 3000-package install: the name is passed precisely so the
        # database is asked about one package at a time.
        asked = []
        monkeypatch.setattr(rpmdb, "query_by_name",
                            lambda name, root="/": asked.append(name) or [])

        installed_nevras((("bash", "bash-5.3-2.mga10.x86_64"),
                          ("zsh", "zsh-5.9-1.mga10.x86_64")))

        assert asked == ["bash", "zsh"]

    def test_the_install_root_reaches_the_database(self, monkeypatch):
        # A chroot transaction must be judged against its own database.
        roots = []
        monkeypatch.setattr(
            rpmdb, "query_by_name",
            lambda name, root="/": roots.append(root) or [])

        installed_nevras((("bash", "bash-5.3-2.mga10.x86_64"),),
                         root="/mnt/chroot")

        assert roots == ["/mnt/chroot"]


class TestTheVerdict:
    """What ``record_action_outcomes`` writes, and when."""

    @pytest.fixture
    def ops(self, monkeypatch):
        """Operations over a stand-in database that records the writes."""
        self.rows = []
        self.written = []

        def record_action_end(transaction_id, pkg_nevra, status,
                              error_message=None):
            self.written.append((pkg_nevra, status, error_message))

        db = SimpleNamespace(
            get_transaction=lambda tid: {"packages": self.rows},
            record_action_end=record_action_end,
        )
        instance = PackageOperations.__new__(PackageOperations)
        instance.db = db
        instance.audit = None
        return instance

    def _row(self, name, nevra, action="install"):
        return {"pkg_name": name, "pkg_nevra": nevra, "action": action,
                "status": "planned"}

    def _result(self, attempted=(), erases=(), reasons=None):
        return SimpleNamespace(operations=[SimpleNamespace(
            attempted=list(attempted),
            attempted_erases=list(erases),
            callback_reasons=dict(reasons or {}),
        )])

    def _present(self, monkeypatch, *nevras):
        monkeypatch.setattr("urpm.core.rpmdb.installed_nevras",
                            lambda pairs, root="/": set(nevras))

    def test_an_install_that_landed_is_done(self, ops, monkeypatch):
        self.rows = [self._row("bash", "bash-5.3-2.mga10.x86_64")]
        self._present(monkeypatch, "bash-5.3-2.mga10.x86_64")

        ops.record_action_outcomes(1, self._result(attempted=["bash"]))

        assert self.written == [("bash-5.3-2.mga10.x86_64", "done", None)]

    def test_an_install_rpm_never_started_is_skipped(self, ops, monkeypatch):
        self.rows = [self._row("bash", "bash-5.3-2.mga10.x86_64")]
        self._present(monkeypatch)

        ops.record_action_outcomes(1, self._result(
            attempted=["bash"], reasons={"bash": "never-started"}))

        assert self.written == [("bash-5.3-2.mga10.x86_64", "skipped",
                                 "never-started")]

    def test_an_install_lost_to_extraction_is_failed(self, ops, monkeypatch):
        # The case the whole design exists for: rpm fired INST_STOP and
        # counted it a success, and the package is not on the disk.
        self.rows = [self._row("bash", "bash-5.3-2.mga10.x86_64")]
        self._present(monkeypatch)

        ops.record_action_outcomes(1, self._result(
            attempted=["bash"], reasons={"bash": "cpio-error"}))

        assert self.written == [("bash-5.3-2.mga10.x86_64", "failed",
                                 "cpio-error")]

    def test_an_install_missing_without_a_reason_is_failed(self, ops,
                                                           monkeypatch):
        # Claiming ``skipped`` without knowing would dress a failure up
        # as a deliberate omission.
        self.rows = [self._row("bash", "bash-5.3-2.mga10.x86_64")]
        self._present(monkeypatch)

        ops.record_action_outcomes(1, self._result(attempted=["bash"]))

        assert self.written == [("bash-5.3-2.mga10.x86_64", "failed", None)]

    def test_a_removal_that_went_through_is_done(self, ops, monkeypatch):
        self.rows = [self._row("nginx", "nginx-1.2-3.mga10.x86_64",
                               action="remove")]
        self._present(monkeypatch)

        ops.record_action_outcomes(1, self._result(erases=["nginx"]))

        assert self.written == [("nginx-1.2-3.mga10.x86_64", "done", None)]

    def test_a_removal_still_on_the_disk_is_failed(self, ops, monkeypatch):
        self.rows = [self._row("nginx", "nginx-1.2-3.mga10.x86_64",
                               action="remove")]
        self._present(monkeypatch, "nginx-1.2-3.mga10.x86_64")

        ops.record_action_outcomes(1, self._result(erases=["nginx"]))

        assert self.written == [("nginx-1.2-3.mga10.x86_64", "failed", None)]

    def test_both_directions_in_one_transaction(self, ops, monkeypatch):
        self.rows = [self._row("apache", "apache-2-1.mga10.x86_64"),
                     self._row("nginx", "nginx-1.2-3.mga10.x86_64",
                               action="remove")]
        self._present(monkeypatch, "apache-2-1.mga10.x86_64")

        ops.record_action_outcomes(1, self._result(attempted=["apache"],
                                                   erases=["nginx"]))

        assert sorted(self.written) == [
            ("apache-2-1.mga10.x86_64", "done", None),
            ("nginx-1.2-3.mga10.x86_64", "done", None),
        ]

    def test_a_dry_run_writes_nothing(self, ops, monkeypatch):
        # ``--test`` builds the transaction and commits nothing, so the
        # child reports no attempt.  Judging the rows here would find
        # none of the packages and fail every one of them.
        self.rows = [self._row("bash", "bash-5.3-2.mga10.x86_64")]
        self._present(monkeypatch)

        ops.record_action_outcomes(1, self._result())

        assert self.written == []

    def test_a_transaction_without_rows_writes_nothing(self, ops,
                                                       monkeypatch):
        self.rows = []
        self._present(monkeypatch)

        ops.record_action_outcomes(1, self._result(attempted=["bash"]))

        assert self.written == []

    def test_the_install_root_is_forwarded(self, ops, monkeypatch):
        seen = {}

        def fake(pairs, root="/"):
            seen["root"] = root
            return set()

        monkeypatch.setattr("urpm.core.rpmdb.installed_nevras", fake)
        self.rows = [self._row("bash", "bash-5.3-2.mga10.x86_64")]

        ops.record_action_outcomes(1, self._result(attempted=["bash"]),
                                   root="/mnt/chroot")

        assert seen["root"] == "/mnt/chroot"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _calls_in(path):
    """Every called name in a module, as plain strings."""
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (getattr(node.func, "attr", None)
                    or getattr(node.func, "id", None))
            if name:
                found.add(name)
    return found


#: Everywhere a committed transaction is turned into history.  The
#: scriptlet output and the per-package verdict are written at the same
#: moment, from the same queue result, so one list covers both.
HISTORY_SITES = (
    "urpm/core/distupgrade/stage3.py",
    "urpm/cli/commands/remove.py",
    "urpm/cli/commands/_install_pipeline.py",
)


class TestEveryHistoryWriterWritesTheVerdict:
    """A missing call here produces no output and no failure.

    Which is exactly why it is checked mechanically: the rows stay at
    ``planned``, the rules stop matching, and nothing says so.
    """

    @pytest.mark.parametrize("path", HISTORY_SITES)
    def test_the_verdict_is_written_where_the_scriptlets_are(self, path):
        calls = _calls_in(path)

        assert "record_scriptlet_output" in calls, \
            f"{path} no longer records the transaction at all"
        assert "record_action_outcomes" in calls, \
            f"{path} records scriptlet output but no per-package verdict"

    def test_no_other_module_records_scriptlets_alone(self):
        offenders = []
        for path in (REPO / "urpm").rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            relative = str(path.relative_to(REPO))
            if relative.endswith("operations.py"):
                continue  # the definitions themselves
            calls = _calls_in(relative)
            if ("record_scriptlet_output" in calls
                    and "record_action_outcomes" not in calls):
                offenders.append(relative)

        assert offenders == [], (
            "these write scriptlet output without writing the "
            "per-package verdict: " + ", ".join(offenders))


class TestTheDistupgradeRecordsItsPlan:
    """Tx A and Tx B used to open a transaction with no package rows."""

    def test_both_sides_record_their_plan(self):
        source = (REPO / "urpm/core/distupgrade/stage3.py").read_text(
            encoding="utf-8")

        # One call in the shared Tx A / Tx B body, one in the retry
        # pass, which opens a transaction of its own.
        assert source.count("_record_plan_rows(") == 3  # def + two calls

    def test_the_name_map_crosses_the_execvp(self):
        # The plan travels as bare NEVRA strings, so the names have to
        # travel too or the rows cannot be written on the far side.
        stage2 = (REPO / "urpm/core/distupgrade/stage2.py").read_text(
            encoding="utf-8")
        cli = (REPO / "urpm/cli/commands/distupgrade.py").read_text(
            encoding="utf-8")

        assert "nevra_to_name" in stage2
        assert '"nevra_to_name": dict(nevra_to_name or {})' in cli

    def test_the_name_is_never_guessed_from_the_nevra(self):
        # Picking the name out of the NEVRA with a pattern is the
        # shortcut this map exists to avoid.
        source = (REPO / "urpm/core/distupgrade/stage3.py").read_text(
            encoding="utf-8")
        body = source[source.index("def _record_plan_rows"):]
        body = body[:body.index("\ndef ")]

        assert "re." not in body
        assert "_canonical_nevra" not in body
