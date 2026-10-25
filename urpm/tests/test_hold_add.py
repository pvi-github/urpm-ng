"""A hold has to freeze a build that is there.

``urpm hold`` freezes an installed package in the state it is in, and
``_add_hold_locks`` implements that by posting a solver lock for every
held name that is **currently installed**; a hold on an absent name is
left inert on purpose, since forbidding an install is ``urpm skip``'s
job and not this one.

What was missing sat between the two: ``hold add`` recorded whatever
string it was handed.  ``urpm hold add $(rpm -q firefox)`` answered
"Held: firefox-153.3.0-1.mga10", ``hold list`` showed it, and nothing
was frozen, because the rpmdb knows no package by that name — the
spelling is the one ``rpm -q`` prints, not a Name.  On a command whose
only purpose is to protect, that is a silent no-op.

So a spec is resolved to the installed build it names before anything
is written, and the build's version-release is recorded with it: the
list can then say what was pinned, and say so when the installed build
no longer matches.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from urpm.core import rpmdb
from urpm.core.database import PackageDatabase
from urpm.cli.commands.cleanup import _hold_targets

pytestmark = pytest.mark.skipif(
    shutil.which("rpm") is None,
    reason="no rpm on this machine",
)


def _a_real_package() -> rpmdb.InstalledPkg:
    """One installed build, derived from the machine's own database."""
    for name in ("filesystem", "rpm", "glibc"):
        found = rpmdb.query_by_name(name)
        if len(found) == 1 and found[0].version and found[0].arch:
            return found[0]
    pytest.skip("no single-build package found")


PKG = _a_real_package()


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file."""
    with tempfile.TemporaryDirectory(prefix="hold_add_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            yield database
        finally:
            database.close()


class TestWhatAHoldWouldFreeze:
    """``_hold_targets`` answers before anything is written."""

    def test_a_plain_name(self):
        assert _hold_targets([PKG.name]) == [(PKG.name, PKG.name, PKG.evr)]

    def test_what_rpm_q_prints(self):
        # The spelling that used to be stored verbatim and froze
        # nothing.
        spec = f"{PKG.name}-{PKG.version}-{PKG.release}"

        assert _hold_targets([spec]) == [(spec, PKG.name, PKG.evr)]

    def test_a_full_nevra(self):
        assert _hold_targets([PKG.nevra]) == [(PKG.nevra, PKG.name, PKG.evr)]

    def test_a_name_in_the_wrong_case(self):
        spec = PKG.name.upper()

        assert _hold_targets([spec]) == [(spec, PKG.name, PKG.evr)]

    def test_nothing_installed_answers_to_it(self):
        assert _hold_targets(["no-such-package"]) == [
            ("no-such-package", None, None)]

    def test_a_build_that_is_not_installed(self):
        spec = f"{PKG.name}-0.0.0-0.mga1"

        assert _hold_targets([spec]) == [(spec, None, None)]

    def test_several_specs_keep_their_order(self):
        targets = _hold_targets([PKG.name, "no-such-package"])

        assert [t[1] for t in targets] == [PKG.name, None]

    def test_nothing_to_resolve(self):
        assert _hold_targets([]) == []


class TestTheHoldTable:
    def test_the_frozen_build_is_recorded(self, db):
        assert db.add_hold("firefox", "a reason", evr="153.3.0-1.mga10")

        held = db.list_holds()
        assert [h["package_name"] for h in held] == ["firefox"]
        assert held[0]["held_evr"] == "153.3.0-1.mga10"

    def test_a_hold_without_a_build_is_still_a_hold(self, db):
        # A name with several builds installed (``kernel``) is locked
        # by name, so pinning one of them in the record would
        # misrepresent what the lock covers.
        assert db.add_hold("kernel-desktop")

        assert db.list_holds()[0]["held_evr"] is None

    def test_holding_twice_is_refused(self, db):
        assert db.add_hold("firefox", evr="1-1")
        assert not db.add_hold("firefox", evr="2-1")

    def test_the_reason_still_travels(self, db):
        db.add_hold("firefox", "pinned for the plugin", evr="1-1")

        assert db.list_holds()[0]["reason"] == "pinned for the plugin"


class TestWhatHoldAddDoes:
    """The command, with the privilege gate stepped over."""

    @pytest.fixture(autouse=True)
    def _no_privilege_check(self, monkeypatch):
        from urpm.auth import privileges
        monkeypatch.setattr(privileges, "require_privileges",
                            lambda *a, **k: None)

    def _run(self, db, packages, capsys):
        from argparse import Namespace
        from urpm.cli.commands.cleanup import _hold_add

        code = _hold_add(Namespace(packages=packages, reason=None), db)
        return code, capsys.readouterr().out

    def test_a_pasted_spelling_is_held_under_its_name(self, db, capsys):
        spec = f"{PKG.name}-{PKG.version}-{PKG.release}"

        code, out = self._run(db, [spec], capsys)

        assert code == 0
        assert [h["package_name"] for h in db.list_holds()] == [PKG.name]
        assert db.list_holds()[0]["held_evr"] == PKG.evr
        assert PKG.name in out

    def test_a_spec_naming_nothing_is_refused(self, db, capsys):
        code, out = self._run(db, ["no-such-package"], capsys)

        assert code == 1
        assert db.list_holds() == []
        assert "no-such-package" in out

    def test_the_good_ones_are_held_even_beside_a_bad_one(self, db, capsys):
        code, out = self._run(db, [PKG.name, "no-such-package"], capsys)

        assert code == 0
        assert [h["package_name"] for h in db.list_holds()] == [PKG.name]


class TestWhatTheListSays:
    """The report, which is where the silent no-op used to hide."""

    def _render(self, holds, installed, monkeypatch, capsys):
        from urpm.cli.commands.cleanup import _hold_list

        monkeypatch.setattr(
            rpmdb, "query_by_name",
            lambda name, root="/": [
                rpmdb.InstalledPkg(name=name, epoch="", version=evr.split("-")[0],
                                   release=evr.split("-", 1)[1], arch="x86_64")
                for evr in installed.get(name, [])])

        class Db:
            def list_holds(self):
                return holds

        _hold_list(object(), Db())
        return capsys.readouterr().out

    def test_the_frozen_build_is_named(self, monkeypatch, capsys):
        out = self._render(
            [{"package_name": "firefox", "reason": None,
              "held_evr": "153.3.0-1.mga10", "added_timestamp": 0}],
            {"firefox": ["153.3.0-1.mga10"]}, monkeypatch, capsys)

        assert "153.3.0-1.mga10" in out
        assert "no longer installed" not in out

    def test_a_package_that_moved_anyway_is_reported(self, monkeypatch, capsys):
        # An ``rpm -U`` behind urpm's back, or a hold older than a
        # release upgrade.
        out = self._render(
            [{"package_name": "firefox", "reason": None,
              "held_evr": "153.3.0-1.mga10", "added_timestamp": 0}],
            {"firefox": ["154.0.0-1.mga10"]}, monkeypatch, capsys)

        assert "154.0.0-1.mga10" in out

    def test_a_hold_on_a_package_that_is_gone_says_so(self, monkeypatch,
                                                      capsys):
        out = self._render(
            [{"package_name": "firefox", "reason": None,
              "held_evr": "153.3.0-1.mga10", "added_timestamp": 0}],
            {}, monkeypatch, capsys)

        assert "protects nothing" in out

    def test_a_hold_without_a_recorded_build_prints_plainly(self, monkeypatch,
                                                            capsys):
        out = self._render(
            [{"package_name": "kernel-desktop", "reason": None,
              "held_evr": None, "added_timestamp": 0}],
            {"kernel-desktop": ["6.18.20-1.mga10", "6.18.22-1.mga10"]},
            monkeypatch, capsys)

        assert "kernel-desktop" in out
        assert "frozen at" not in out
        assert "protects nothing" not in out


class TestADatabaseTheReaderCannotMigrate:
    """The file is root-owned in mode 644.

    A normal user reads it and cannot move its schema, which happens
    on the first privileged command.  Between the package upgrade and
    that command, the code is ahead of the database it queries, and
    both halves of that have to behave: the list still answers, and
    the operator is told why the build is unknown.
    """

    def test_the_list_answers_without_the_column(self, db):
        db.add_hold("firefox", "a reason", evr="153.3.0-1.mga10")
        # The shape of a database that has not seen v39 yet.
        db.conn.execute("ALTER TABLE held_packages DROP COLUMN held_evr")
        db.conn.commit()

        held = db.list_holds()

        assert [h["package_name"] for h in held] == ["firefox"]
        assert held[0]["reason"] == "a reason"
        assert held[0]["held_evr"] is None

    def test_a_reader_is_told_the_upgrade_is_pending(self, tmp_path, capsys):
        path = tmp_path / "packages.db"
        PackageDatabase(db_path=path).close()
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE schema_info SET version = 38")
        conn.commit()
        conn.close()
        capsys.readouterr()

        reader = PackageDatabase(db_path=path, read_only=True)
        try:
            err = capsys.readouterr().err
        finally:
            reader.close()

        assert "pending" in err

    def test_nothing_is_said_when_the_schema_is_current(self, tmp_path,
                                                        capsys):
        path = tmp_path / "packages.db"
        PackageDatabase(db_path=path).close()
        capsys.readouterr()

        reader = PackageDatabase(db_path=path, read_only=True)
        try:
            err = capsys.readouterr().err
        finally:
            reader.close()

        assert "pending" not in err

    def test_a_writer_migrates_instead_of_warning(self, tmp_path, capsys):
        # The warning belongs to readers only: a writer has just
        # applied the migration, so saying it is pending would be
        # false.
        path = tmp_path / "packages.db"
        PackageDatabase(db_path=path).close()
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE schema_info SET version = 38")
        conn.commit()
        conn.close()
        capsys.readouterr()

        writer = PackageDatabase(db_path=path)
        try:
            err = capsys.readouterr().err
            version = writer.conn.execute(
                "SELECT version FROM schema_info").fetchone()[0]
        finally:
            writer.close()

        assert "pending" not in err
        from urpm.core.database import SCHEMA_VERSION
        assert version == SCHEMA_VERSION


class TestTheSchemaMoved:
    def test_an_older_database_gains_the_column(self, tmp_path):
        """A database created before v39 is migrated, not recreated.

        The hold table is user data: an operator's pins must survive
        the upgrade that adds the column.
        """
        import sqlite3

        path = tmp_path / "packages.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE schema_info (version INTEGER);
            INSERT INTO schema_info (version) VALUES (38);
            CREATE TABLE held_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_name TEXT NOT NULL UNIQUE,
                reason TEXT,
                added_timestamp INTEGER NOT NULL
            );
            INSERT INTO held_packages (package_name, reason, added_timestamp)
            VALUES ('firefox', 'from before', 1);
        """)
        conn.commit()
        conn.close()

        database = PackageDatabase(db_path=path)
        try:
            held = database.list_holds()
            assert [h["package_name"] for h in held] == ["firefox"]
            assert held[0]["reason"] == "from before"
            assert held[0]["held_evr"] is None
        finally:
            database.close()
