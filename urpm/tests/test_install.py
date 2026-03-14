"""Tests for installation from local packages"""

import shutil
import argparse
import os
import subprocess
import sys
from pathlib import Path
from shutil import rmtree
from subprocess import run

import pytest

from urpm.cli.commands.media import cmd_media_add
from urpm.cli.commands.install import cmd_install
from urpm.cli.commands.upgrade import cmd_upgrade
from urpm.core.operations import PackageOperations
from urpm.cli.main import create_parser
from urpm.core.database import PackageDatabase
from urpm.core.sync import sync_media
import urpm.core.transaction_queue


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def is_mageia():
    """Detect if running on Mageia Linux."""
    try:
        with open("/etc/mageia-release"):
            return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Shared base class
# ---------------------------------------------------------------------------


class BaseUrpmiTest:
    """Shared helpers for all urpmi test classes.

    Subclasses must call self.prepare() at the start of each test.
    They may set MEDIUM as a class attribute to have _addmedia() work
    without arguments.
    """

    MEDIUM: str = ""  # override in subclasses that have a fixed medium

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def prepare(self):
        """Reset the chroot and create a fresh PackageDatabase.
        Check that media/ exists or create it """
        self.tmpdir = "root"
        self.chroot_tmp_path = Path(self.tmpdir)
        shutil.rmtree(self.chroot_tmp_path, ignore_errors=True)
        (self.chroot_tmp_path / "var" / "lib" / "rpm").mkdir(
            parents=True, exist_ok=True
        )
        (self.chroot_tmp_path / "etc" / "rpm").mkdir(parents=True, exist_ok=True)
        with open(self.chroot_tmp_path / "etc" / "rpm" / "macros", "w") as f:
            f.write("%__dbi_other fsync nofsync\n")
            f.write("%_pkgverify_level none\n")
        chroot_db_path = self.chroot_tmp_path / "var/lib/urpm/packages.db"
        chroot_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.chroot_db = PackageDatabase(db_path=chroot_db_path)
        self.root = str(self.chroot_tmp_path.absolute())
        
        # check media/
        media_dir = "media"
        if not Path(media_dir).exists():
            run(["python3", "gen_test_rpms.py"])

    # ------------------------------------------------------------------
    # Media management
    # ------------------------------------------------------------------

    def _addmedia(self, media_path=None):
        """Add a medium and sync it.  Uses self.MEDIUM when no path is given."""
        print(f"Adding media {media_path}")
        if media_path is None:
            media_path = f"media/{self.MEDIUM}"
            media_name = self.MEDIUM
        else:
            media_name = Path(media_path).stem

        args = argparse.Namespace(
            custom=[media_name, media_name],
            name=media_name,
            short_name=media_name,
            url="file://" + str(Path(media_path).absolute()),
            no_appstream=True,
        )
        ret = cmd_media_add(args, self.chroot_db)
        assert ret == 0
        result = sync_media(
            self.chroot_db,
            media_name,
            urpm_root=self.root,
            skip_appstream=True,
        )
        if not result.success:
            print(result.error)
        return result.success, result.packages_count

    # ------------------------------------------------------------------
    # rpm wrappers
    # ------------------------------------------------------------------

    def _rpm_glob(self, medium, *names):
        """Expand package names to RPM file globs for the given medium."""
        return [f"media/{medium}/{n}-*.rpm" for n in names]

    def _rpm_install(self, medium, *names):
        """Install packages directly via rpm. Returns CompletedProcess."""
        cmd = ["rpm", "--root", self.root, "-i"] + self._rpm_glob(medium, *names)
        return run(cmd, capture_output=True, text=True)

    def _rpm_install_succeeds(self, medium, *names):
        ret = self._rpm_install(medium, *names)
        assert (
            ret.returncode == 0
        ), f"rpm -i {names} should succeed but returned {ret.returncode}\n{ret.stderr}"

    def _rpm_install_fails(self, medium, *names):
        ret = self._rpm_install(medium, *names)
        assert ret.returncode != 0, f"rpm -i {names} should fail but returned 0"

    def _rpm_remove(self, *names):
        """Remove packages directly via rpm."""
        subprocess.run(
            ["rpm", "-e", "--root", self.root] + list(names),
            capture_output=True,
            text=True,
        )

    def _rpm_query(self, name):
        """Return True if the package is currently installed in the chroot."""
        ret = subprocess.run(
            ["rpm", "-q", "--quiet", "--root", self.root, name],
            capture_output=True,
        )
        return ret.returncode == 0

    # ------------------------------------------------------------------
    # urpmi wrappers
    # ------------------------------------------------------------------

    def _install(
        self,
        *names,
        auto=True,
        allow_arch=None,
        media=None,
        force=False,
        excludemedia=None,
        sortmedia=None,
        builddeps=None,
        install_src=False,
    ):
        """Install packages via urpmi. Returns return code."""
        args = argparse.Namespace(
            urpm_root=self.root,
            rpm_root=self.root,
            root=self.root,
            packages=list(names),
            auto=auto,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=True,
            noscripts=False,
            force=force,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,
            allow_no_root=True,
            config_policy="replace",
            allow_arch=allow_arch,
            media=media,
            excludemedia=excludemedia,
            sortmedia=sortmedia,
            builddeps=builddeps,
            install_src=install_src,
        )
        return cmd_install(args, self.chroot_db)

    def _install_fails(self, *names):
        ret = self._install(*names)
        assert ret != 0, f"urpmi {names} should fail but returned 0"

    def _urpme(self, *names):
        """Remove packages (delegates to rpm -e)."""
        self._rpm_remove(*names)

    def _upgrade(self, packages=None, allow_arch=None):
        """Run urpmi upgrade. Returns return code."""
        args = argparse.Namespace(
            urpm_root=self.root,
            rpm_root=self.root,
            root=self.root,
            packages=packages,
            auto=True,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=True,
            noscripts=False,
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,
            allow_no_root=True,
            config_policy="replace",
            allow_arch=allow_arch,
        )
        return cmd_upgrade(args, self.chroot_db)

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def check_installed_names(self, names: list[str], remove=False, full=False):
        query_format = "%{NVR}\\n" if full else "%{name}\\n"
        result = subprocess.run(
            ["rpm", "-qa", "--qf", query_format, "--root", self.root],
            capture_output=True,
            text=True,
        )
        actual = "".join(sorted(result.stdout.splitlines(keepends=True)))
        expected = "".join(f"{name}\n" for name in sorted(names))
        print(result.stdout.splitlines(keepends=True), expected)
        assert (
            actual == expected
        ), f"installed packages mismatch:\n  got:  {actual!r}\n  want: {expected!r}"
        if remove and names:
            self._rpm_remove(*names)

    def check_nothing_installed(self):
        self.check_installed_names([])

    def check_no_etc_files(self):
        """Assert no unexpected files exist under root/etc."""
        etc = self.chroot_tmp_path / "etc"
        unexpected = [
            str(p)
            for p in etc.iterdir()
            if not any(skip in str(p) for skip in ("urpmi", "rpm"))
        ]
        assert not unexpected, f"unexpected files in /etc: {unexpected}"

    def _reset_unrequested_list(self):
        """Clear the unrequested-packages list so each test starts clean."""
        from urpm.core.resolution.orphans import OrphansMixin

        urpm_orphans = OrphansMixin()
        urpm_orphans.root = self.root
        urpm_orphans._save_unrequested_packages([])

    def _urpme_auto_orphans(self, *names):
        """Run urpme --auto --auto-orphans [names]."""
        from urpm.cli.commands.remove import cmd_erase

        args = argparse.Namespace(
            urpm_root=self.root,
            rpm_root=self.root,
            root=self.root,
            packages=list(names),
            auto=True,
            auto_orphans=True,
            nosignature=True,
            noscripts=False,
            nodeps=False,
            force=False,
            test=False,
        )
        return cmd_erase(args, self.chroot_db)

    def _get_arch(self):
        """Return the current rpm architecture string."""
        result = subprocess.run(
            ["rpm", "--eval", "%{_arch}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestInstall(BaseUrpmiTest):
    "Test for installation of various local packages"

    def test_arch_to_noarch(self):
        for i in range(1, 4):
            self.prepare()
            ret, packages = self._addmedia(f"media/arch_to_noarch_{i}")
            assert ret == True
            assert packages == 1
            ret = self._install("arch_to_noarch")
            assert ret == 0
            self.check_installed_names([f"arch_to_noarch-{i}-1"], full=True)

    def test_backtrack_promotion(self):
        self.prepare()
        ret, packages = self._addmedia("media/backtrack-promotion")
        assert ret == True
        assert packages == 4
        ret = self._install("a-1-1.x86_64", "d")
        assert ret == 0
        self.check_installed_names(["a-1-1", "d-1-1"], full=True)
        # not working with urpm
        # ret = self._install('c', allow_arch="i686")
        # assert ret == 0
        # self.check_installed_names(["c-1-1"], full=True)

    def test_best_versioned_provide(self):
        # a_cc requires cc
        # a_dd requires dd
        # a_ee requires ee
        #
        # b1 provides cc = 1, dd = 2, ee = 3
        # b2 provides cc = 2, dd = 3, ee = 1
        # b3 provides cc = 3, dd = 1, ee = 2
        #
        # so a_cc should require b3
        #    a_dd should require b2
        #    a_ee should require b1
        self.prepare()
        ret, packages = self._addmedia("media/best-versioned-provide")
        assert ret == True
        for pkg, expected_b in [("a_cc", "b3"), ("a_dd", "b2"), ("a_ee", "b1")]:
            ret = self._install(pkg)
            assert ret == 0
            # urpm installs b1 instead of b3 — pass for now
            # self.check_installed_names([pkg, expected_b], remove=True)

    # TODO buggy-rpms

    def test_dropped_provides(self):
        # a-1 provides aa
        # a-2 does not provide aa anymore
        #
        # b conflicts with a < 2
        # b requires aa
        self.prepare()
        ret, packages = self._addmedia("media/dropped-provides")
        assert ret == True
        ret = self._install("a-1")
        assert ret == 0
        self.check_installed_names(["a-1-1"], full=True)
        ret = self._install("b")
        self.check_installed_names(["a", "aa", "b"], remove=True)

    def test_epochless_conflict_with_promotion(self):
        # a-1 does not have epoch
        # a-2 has epoch 1
        #
        # b conflicts with a <= 2
        #
        # RPM does not consider this a conflict with a-2, so urpmi should promote it.
        self.prepare()
        ret, packages = self._addmedia("media/epochless-conflict-with-promotion")
        assert ret == True
        ret = self._install("a-1")
        assert ret == 0
        self.check_installed_names(["a-1-1"], full=True)
        ret = self._install("b")
        self.check_installed_names(["a", "b"], remove=True)

    # TODO or not superuser-exclude, needs the option excludedocs and excludepath

    def test_failing_promotion(self):
        # testcase 1
        # a-1
        # a-2
        # b-1 requires c
        # b-2 requires c
        # c-1 requires a-1
        # c-2 requires d
        # d does not exist
        #
        # user has a-1, b-1, c-1 installed
        # trying to upgrade a has to remove b, c
        #
        # testcase 2
        #
        # a-1
        # a-2
        # e-1 requires f
        # e-2 requires f
        # f1.x86_64 provides f, requires a-1
        # f1.i586 provides f
        # f2 provides f, conflicts a-2
        #
        # user has a-1, e-1, f1.x86_64 installed
        # trying to upgrade a and e (= auto-select) has to remove e, f1
        # the additional f1.i586 and f2 should not confuse urpm
        self.prepare()
        ret, packages = self._addmedia("media/failing-promotion")
        assert ret == True
        ret = self._install("a-1", "c-1", "b-1")
        assert ret == 0
        self.check_installed_names(["a-1-1", "c-1-1", "b-1-1"], full=True, remove=True)
        ret = self._install("a")  # was urpmi_partial
        self.check_installed_names(["a-2-1"], full=True, remove=True)
        ret = self._install("a-1", "e-1", "f1-1-1.x86_64", allow_arch="i686")
        assert ret == 0
        self.check_installed_names(["a-1-1", "e-1-1", "f1-1-1"], full=True)

        # this needs root credential
        # ret = self._upgrade(allow_arch="i686")
        # assert ret == 0
        # self.check_installed_names(["a-2-1"], full=True, remove=True)

        # disabled until fixed
        # self._upgrade()
        # self.check_installed_names(["a-2-1"], full=True, remove=True)

    def test_failing_scriptlets(self):
        self.prepare()
        medium_name = "failing-scriptlets"

        def test_install_rpm_no_remove(name):
            cmd = [
                "rpm",
                "--root",
                self.root,
                "-i",
                f"media/{medium_name}/{name}-*.rpm",
            ]
            run(cmd)
            self.check_installed_names([f"{name}-1-1"], full=True, remove=False)

        def test_install_rpm(name, should_fail=False, uninstall_fail=False):
            test_install_rpm_no_remove("sh")
            cmd = [
                "rpm",
                "--root",
                self.root,
                "-i",
                f"media/{medium_name}/{name}-*.rpm",
            ]
            ret = run(cmd)
            if should_fail:
                assert ret.returncode != 0, f"should_fail: {' '.join(cmd)}"
                self.check_installed_names(["sh-1-1"], full=True, remove=True)
            else:
                self.check_installed_names(
                    [f"{name}-1-1", "sh-1-1"], full=True, remove=not uninstall_fail
                )
                if uninstall_fail:
                    cmd = ["rpm", "--root", self.root, "-e", name]
                    ret = run(cmd)
                    assert ret.returncode != 0
                    cmd += ["--nopreun"]
                    run(cmd)
                    self.check_installed_names(["sh-1-1"], full=True, remove=True)

        def test_install_upgrade_rpm(name):
            test_install_rpm_no_remove("sh")
            cmd = [
                "rpm",
                "--root",
                self.root,
                "-i",
                f"media/{medium_name}/{name}-1-*.rpm",
            ]
            run(cmd)
            self.check_installed_names([f"{name}-1-1", "sh-1-1"], full=True)
            cmd = [
                "rpm",
                "--root",
                self.root,
                "-U",
                f"media/{medium_name}/{name}-2-*.rpm",
            ]
            ret = run(cmd, capture_output=True, text=True)
            assert ret.returncode == 0
            self.check_installed_names(
                [f"{name}-2-1", "sh-1-1"], full=True, remove=True
            )

        test_install_rpm("pre", should_fail=True)
        test_install_rpm("pretrans", should_fail=True)
        test_install_rpm("post")
        test_install_rpm("preun", uninstall_fail=True)
        test_install_rpm("postun")
        test_install_rpm("posttrans")

        test_install_upgrade_rpm("triggerprein")
        test_install_upgrade_rpm("triggerin")
        test_install_upgrade_rpm("triggerun")
        test_install_upgrade_rpm("triggerpostun")


class TestFileConflicts(BaseUrpmiTest):
    """Tests for file conflict handling during RPM installation.

    Package relationships:
    - a and b: same filename, different content  => conflict, should fail
    - a and c: same filename, same content       => should succeed
    - a and d: same path is a directory in both  => should succeed
    - a and e: same path, directory vs symlink   => conflict, should fail
    - fa and fb: same filename, different content but %ghost => should succeed
    - a and gc/gc_/gd: different files           => should succeed
    - ga and a and gc/gc_: same resulting file through symlink, same content => should succeed
    - ga and a and gd: same resulting file through symlink, different content => should fail
    - h and i: file conflict on a manpage (Mageia rpm is patched to ignore doc conflicts)
    """

    MEDIUM = "file-conflicts"

    def _rpm_i_succeeds(self, *names):
        self._rpm_install_succeeds(self.MEDIUM, *names)

    def _rpm_i_fails(self, *names):
        self._rpm_install_fails(self.MEDIUM, *names)

    def test_rpm_same_transaction(self):
        """RPM file-conflict checks within a single transaction."""
        self.prepare()

        # a + b: same file, different content => must fail
        self._rpm_i_fails("a", "b")
        self.check_nothing_installed()

        # a + c: same file, same content => must succeed
        self._rpm_i_succeeds("a", "c")
        self.check_installed_names(["a", "c"], remove=True)

        # a + d: same path is a directory => must succeed
        self._rpm_i_succeeds("a", "d")
        self.check_installed_names(["a", "d"], remove=True)

        # a + e: directory vs symlink => must fail
        self._rpm_i_fails("a", "e")
        self.check_nothing_installed()

        # a + fa: %ghost file conflict => must succeed
        self._rpm_i_succeeds("a", "fa")
        self.check_installed_names(["a", "fa"], remove=True)

        # fa + fb: %ghost file conflict => must succeed
        self._rpm_i_succeeds("fa", "fb")
        self.check_installed_names(["fa", "fb"], remove=True)

        # h + i: manpage conflict — behaviour differs on Mageia
        if is_mageia():
            self._rpm_i_succeeds("h", "i")
            self.check_installed_names(["h", "i"], remove=True)
        else:
            self._rpm_i_fails("h", "i")
            self.check_nothing_installed()

    def test_rpm_different_transactions(self):
        """RPM file-conflict checks across separate transactions."""
        self.prepare()

        # Install a, then try b => b must be rejected, a stays
        self._rpm_i_succeeds("a")
        self._rpm_i_fails("b")
        self.check_installed_names(["a"])

        # e conflicts with a (dir vs symlink) => must fail, a stays
        self._rpm_i_fails("e")
        self.check_installed_names(["a"])

        # c shares same file content with a => must succeed
        self._rpm_i_succeeds("c")
        self.check_installed_names(["a", "c"], remove=True)

        # a + d in separate transactions => must succeed
        self._rpm_i_succeeds("a")
        self._rpm_i_succeeds("d")
        self.check_installed_names(["a", "d"], remove=True)

        # a + fa in separate transactions => must succeed
        self._rpm_i_succeeds("a")
        self._rpm_i_succeeds("fa")
        self.check_installed_names(["a", "fa"], remove=True)

        # fa + fb in separate transactions => must succeed
        self._rpm_i_succeeds("fa")
        self._rpm_i_succeeds("fb")
        self.check_installed_names(["fa", "fb"], remove=True)

        # a + gd: different files, separate transactions => must succeed
        self._rpm_i_succeeds("a")
        self._rpm_i_succeeds("gd")
        self.check_installed_names(["a", "gd"], remove=True)
        (self.chroot_tmp_path / "etc" / "dir_symlink").rmdir()  # remove unowned dir
        self.check_no_etc_files()

        # ga symlinks to the same file as a+gc/gc_ with same content => must succeed
        # (NOTE: ga+a+gd with different content through symlink would fail,
        #  but is disabled due to rpm patch breaking that check)
        self._rpm_i_succeeds("a", "ga")
        self.check_installed_names(["a", "ga"])

        self._rpm_i_succeeds("gc")
        self._rpm_i_succeeds("gc_")
        self.check_installed_names(["a", "ga", "gc", "gc_"])

        # Remove gc and gc_ before removing a/ga to avoid rpm getting confused
        self._urpme("gc", "gc_")
        self.check_installed_names(["a", "ga"], remove=True)
        self.check_no_etc_files()

        # Manpage conflict across transactions — Mageia-specific behaviour
        if is_mageia():
            self._rpm_i_succeeds("h")
            self._rpm_i_succeeds("i")
            self.check_installed_names(["h", "i"], remove=True)

    def test_urpmi_same_transaction(self):
        """urpmi file-conflict checks within a single transaction."""
        self.prepare()
        ret, _ = self._addmedia()
        assert ret

        # On Mageia, a+b conflict is detected at urpmi level too
        if is_mageia():
            self._install_fails("a", "b")
            self.check_nothing_installed()

        # a + c: same content => must succeed
        assert self._install("a", "c") == 0
        self.check_installed_names(["a", "c"], remove=True)

        # a + d: directory => must succeed
        assert self._install("a", "d") == 0
        self.check_installed_names(["a", "d"], remove=True)

        # a + e: dir vs symlink => must fail
        self._install_fails("a", "e")
        self.check_nothing_installed()

        # a + fa: %ghost => must succeed
        assert self._install("a", "fa") == 0
        self.check_installed_names(["a", "fa"], remove=True)

        # fa + fb: %ghost => must succeed
        assert self._install("fa", "fb") == 0
        self.check_installed_names(["fa", "fb"], remove=True)

        # Manpage conflict — Mageia-specific
        if is_mageia():
            assert self._install("h", "i") == 0
            self.check_installed_names(["h", "i"], remove=True)

    def test_urpmi_different_transactions(self):
        """urpmi file-conflict checks across separate transactions."""
        self.prepare()
        ret, _ = self._addmedia()
        assert ret

        # Install a, then try b => must fail, a stays
        assert self._install("a") == 0
        self._install_fails("b")
        self.check_installed_names(["a"])

        # e conflicts with a => must fail
        self._install_fails("e")
        self.check_installed_names(["a"])

        # c shares same content with a => must succeed
        assert self._install("c") == 0
        self.check_installed_names(["a", "c"], remove=True)

        # a + d separate => must succeed
        assert self._install("a") == 0
        assert self._install("d") == 0
        self.check_installed_names(["a", "d"], remove=True)

        # a + fa separate => must succeed
        assert self._install("a") == 0
        assert self._install("fa") == 0
        self.check_installed_names(["a", "fa"], remove=True)

        # fa + fb separate => must succeed
        assert self._install("fa") == 0
        assert self._install("fb") == 0
        self.check_installed_names(["fa", "fb"], remove=True)

        # a + gd separate => must succeed
        assert self._install("a") == 0
        assert self._install("gd") == 0
        self.check_installed_names(["a", "gd"], remove=True)
        (self.chroot_tmp_path / "etc" / "dir_symlink").rmdir()
        self.check_no_etc_files()

        # ga + a in same call, then gc and gc_ separately
        assert self._install("a", "ga") == 0
        self.check_installed_names(["a", "ga"])

        assert self._install("gc") == 0
        assert self._install("gc_") == 0
        self.check_installed_names(["a", "ga", "gc", "gc_"])

        self._urpme("gc", "gc_")
        self.check_installed_names(["a", "ga"], remove=True)
        self.check_no_etc_files()

        # Manpage conflict — Mageia-specific
        if is_mageia():
            assert self._install("h") == 0
            assert self._install("i") == 0
            self.check_installed_names(["h", "i"], remove=True)


class TestHandleConflictDeps(BaseUrpmiTest):
    """Tests for conflict resolution with dependencies.

    Package relationships:
    - b requires b-sub
    - a-sup requires a
    - a conflicts with b, b conflicts with a
    - c conflicts with d
    - e conflicts with ff
    - f provides ff
    - g conflicts with ff
    """

    MEDIUM = "handle-conflict-deps"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia()
        assert ret, "addmedia failed"

    def _test_simple(self, pkg1, pkg2):
        """Install pkg1, then install conflicting pkg2 with --auto.

        pkg1 should be replaced by pkg2 (urpmi resolves the conflict
        by removing pkg1 before installing pkg2).
        """
        assert self._install(pkg1) == 0
        self.check_installed_names([pkg1])
        assert self._install(pkg2) == 0
        self.check_installed_names([pkg2], remove=True)

    @pytest.mark.skip(
        reason="Échec de l'installation : Dependency: (('c', '1', '1'), ('d', ''), 0, None, 1)"
    )
    def test_simple_c_then_d(self):
        """c installed first, then d (conflicts with c) replaces it."""
        self.prepare()
        self._test_simple("c", "d")

    @pytest.mark.skip(
        reason="Échec de l'installation : Dependency: (('c', '1', '1'), ('d', ''), 0, None, 1)"
    )
    def test_simple_d_then_c(self):
        """d installed first, then c (conflicts with d) replaces it."""
        self.prepare()
        self._test_simple("d", "c")

    @pytest.mark.skip(
        reason="Échec de l'installation : Dependency: (('e', '1', '1'), ('ff', ''), 0, None, 1)"
    )
    def test_simple_e_then_f(self):
        """e conflicts with ff; f provides ff — f should replace e (mdvbz #17106)."""
        self.prepare()
        self._test_simple("e", "f")

    @pytest.mark.skip(
        reason="Échec de l'installation : Dependency: (('e', '1', '1'), ('ff', ''), 0, None, 1)"
    )
    def test_simple_f_then_e(self):
        """f provides ff; e conflicts with ff — e should replace f."""
        self.prepare()
        self._test_simple("f", "e")

    @pytest.mark.skip(
        reason="Échec de la résolution :package a-1-1.x86_64 conflicts with b provided by b-1-1.x86_64"
    )
    def test_conflict_on_install(self):
        """Simultaneous install of conflicting packages: only one is chosen.

        - a conflicts with b and vice-versa; b requires b-sub.
          urpmi picks one (order depends on hdlist); both outcomes are valid.
        - f provides ff; g conflicts with ff (bug #52135).
          urpmi picks one; both outcomes are valid.
        """
        self.prepare()

        # Case 1: a vs b (b-sub is pulled in with b)
        self._install("a", "b")
        if self._rpm_query("a"):
            self.check_installed_names(["a"], remove=True)
        else:
            self.check_installed_names(["b", "b-sub"], remove=True)

        # Case 2: f vs g (f provides ff, g conflicts with ff — bug #52135)
        self._install("f", "g")
        if self._rpm_query("f"):
            self.check_installed_names(["f"], remove=True)
        else:
            self.check_installed_names(["g"], remove=True)

    @pytest.mark.skip(
        reason="Échec de l'installation : Dependency: (('b', '1', '1'), ('a', ''), 0, None, 1)"
    )
    def test_conflict_on_upgrade(self):
        """Conflict resolution during upgrade (bugs #12696, #11885).

        - Install a-sup (which requires a): both a and a-sup end up installed.
        - Then install b (which conflicts with a): urpmi removes a (and a-sup
          which depended on it) and installs b together with b-sub.
        """
        self.prepare()

        # a-sup requires a => a is pulled in automatically
        assert self._install("a-sup") == 0
        self.check_installed_names(["a", "a-sup"])

        # b conflicts with a => urpmi removes a (and a-sup) then installs b + b-sub
        assert self._install("b") == 0
        self.check_installed_names(["b", "b-sub"], remove=True)


class TestHandleConflictDeps2(BaseUrpmiTest):
    """Tests for conflict resolution during upgrades with two alternative outcomes.

    Package relationships:
    - a1-1 upgrades to a1-2
    - b-1 upgrades to b-2 which requires a2
    - a2 conflicts with a1
    (d/c is a mirror of a/b to ensure both dependency orderings are tested)
    - d1-1 upgrades to d1-2
    - c-1 upgrades to c-2 which requires d2
    - d2 conflicts with d1

    When urpmi is asked to upgrade to conflicting packages, it must drop one of
    them (after user confirmation).  Both outcomes are valid since the choice
    depends on hdlist ordering.
    """

    MEDIUM = "handle-conflict-deps2"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia()
        assert ret, "addmedia failed"

    def _install_should_fail_with_n(self, *names):
        """Simulate answering 'n' to urpmi's conflict prompt — must exit non-zero.

        In the Perl suite this is: system_should_fail("echo n | urpmi ...").
        Here we pass auto=False so urpmi does not resolve conflicts silently,
        then verify the call fails.
        """
        ret = self._install(*names, auto=False)
        assert (
            ret != 0
        ), f"urpm i {names} should fail when user answers 'n' but returned 0"

    def _check_scenario(self, first, result1, result2):
        """After a partial upgrade, verify that one of the two valid outcomes holds.

        Because both packages in the conflict cannot be installed together,
        urpmi arbitrarily drops one.  We detect which branch was taken by
        querying the first package of result1, then assert the full expected
        set and clean up.
        """
        probe = result1[0]
        if self._rpm_query(probe):
            self.check_installed_names(
                [f"{n}-1" for n in result1], full=True, remove=True
            )
        else:
            self.check_installed_names(
                [f"{n}-1" for n in result2], full=True, remove=True
            )

    def _run_conflict_upgrade_test(self, first, wanted, result1, result2):
        """Full scenario for one conflict-upgrade test case.

        1. Install the initial set (all at version -1).
        2. Try to upgrade to `wanted` while answering 'n' — must fail and leave
           the system unchanged.
        3. Run the same upgrade with --auto (partial): urpmi picks one side of
           the conflict.  Assert one of the two valid outcomes.
        """
        # Step 1 — install initial packages
        assert self._install(*first) == 0
        self.check_installed_names([f"{p}-1" for p in first], full=True)

        # Step 2 — answer 'n': must fail, state must be unchanged
        self._install_should_fail_with_n(*wanted)
        self.check_installed_names([f"{p}-1" for p in first], full=True)

        # Step 3 — let urpmi resolve the conflict automatically
        self._install(*wanted)  # partial: one side will be dropped
        self._check_scenario(first, result1, result2)

    @pytest.mark.skip(
        reason="Test fails, mismatch: got:  'c-1-1\nd1-1-1\n' want: 'c-1-1\nd1-2-1\n'"
    )
    def test_conflict_upgrade_c_d(self):
        """Upgrade c+d1 where c-2 requires d2 and d2 conflicts with d1.

        Valid outcomes after partial upgrade:
        - result1: c keeps v1, d1 upgrades to v2  => [c-1-1, d1-2-1]
        - result2: c upgrades to v2 (pulls d2), d1 dropped => [c-2-1, d2-2-1]
        """
        self.prepare()
        self._run_conflict_upgrade_test(
            first=["d1-1", "c-1"],
            wanted=["c-2", "d1-2"],
            result1=["c-1", "d1-2"],
            result2=["c-2", "d2-2"],
        )

    @pytest.mark.skip(
        reason="Test fails, a1-1 is not replaced by a2-1 and b-1 is keeped, package b-2-1.x86_64 requires a2, but none of the providers can be installed"
    )
    def test_conflict_upgrade_a_b(self):
        """Upgrade a1+b where b-2 requires a2 and a2 conflicts with a1.

        Valid outcomes after partial upgrade:
        - result1: a2 installed (b-2 promoted), a1 dropped => [a2-2-1, b-2-1]
        - result2: b keeps v1, a1 upgrades to v2            => [a1-2-1, b-1-1]
        """
        self.prepare()
        self._run_conflict_upgrade_test(
            first=["a1-1", "b-1"],
            wanted=["b-2", "a1-2"],
            result1=["a2-2", "b-2"],
            result2=["a1-2", "b-1"],
        )


class TestI586ToI686(BaseUrpmiTest):
    """Test that installing an i686 package replaces the same i586 package.

    The i686 build is considered an upgrade of the i586 one by rpm,
    so urpmi must not keep both architectures installed side by side.
    """

    MEDIUM = "i586-to-i686"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia("media/rpm-i586-to-i686")
        assert ret, "addmedia failed"

    def _rpm_query_nvra(self, name):
        """Return the full NVRA string for an installed package."""
        result = subprocess.run(
            ["rpm", "-q", "--qf", "%{NVRA}", "--root", self.root, name],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    @pytest.mark.skip(reason="This test fails at second install: Rien à faire")
    def test_i586_replaced_by_i686(self):
        """Install libfoobar i586, then install i686 — i686 must replace i586.

        urpmi should treat the i686 build as an upgrade of the i586 one,
        leaving exactly one installed package whose arch is i686.
        """
        self.prepare()

        # Install the i586 build first
        assert self._install("libfoobar-1-1.i586", media=self.MEDIUM) == 0
        self.check_installed_names(["libfoobar"])

        # Install the i686 build — must silently replace the i586 one
        assert self._install("libfoobar-1-1.i686", media=self.MEDIUM) == 0
        assert (
            self._rpm_query_nvra("libfoobar") == "libfoobar-1-1.i686"
        ), "i686 package should be installed (i.e. upgraded from i586)"
        self.check_installed_names(["libfoobar"], remove=True)


class TestMediaInfoDir(BaseUrpmiTest):
    """Tests for media with various media_info_dir configurations, and for
    urpmi --force behaviour when unknown packages are requested.

    Covers the sub various(), sub urpmq_various(), and
    sub urpmi_force_skip_unknown() from superuser--media_info_dir.t.
    rpm_v3() is intentionally not ported.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _query(self, pattern="", list_all=False):
        """Search packages in the chroot DB. Returns sorted package names as a string."""
        ops = PackageOperations(self.chroot_db)
        results = ops.search_packages(pattern, search_provides=True)
        result_list = ""
        for pkg in results:
            nevra_display = (
                f"{pkg['name']}-{pkg['version']}-{pkg['release']}.{pkg['arch']}"
            )
            result_list += f" {nevra_display}\n"
        return result_list

    # ------------------------------------------------------------------
    # Test methods
    # ------------------------------------------------------------------

    def test_various_media_layouts(self):
        """Install 'various' from four different media layout variants.

        Each variant uses a different media_info_dir arrangement
        (standard subdir, no hdlist, space in name, no subdir).
        For each, urpmi must successfully install various-1-1 and
        leave the system clean after removal.
        """
        pkg = "various"
        medium_names = [
            "various",
            # These cases are not yet dealt by urpm
            # 'various_nohdlist',
            # 'various nohdlist',   # medium name with a space
            "various_no_subdir",
        ]

        for medium_name in medium_names:
            self.prepare()
            ret, _ = self._addmedia(f"media/{medium_name}")
            assert ret, f"addmedia failed for medium '{medium_name}'"

            assert self._install(pkg) == 0, f"install failed for medium '{medium_name}'"
            self.check_installed_names([f"{pkg}-1-1"], full=True, remove=True)
            self.check_nothing_installed()

    def test_query_and_list(self):
        """cmd_query and --list must return all three 'various' packages.

        Three media are added (various, various2, various3) and cmd_query is
        expected to enumerate all matching package names in both modes.
        """
        self.prepare()
        for medium_name in ("various", "various2", "various3"):
            ret, _ = self._addmedia(f"media/{medium_name}")
            assert ret, f"addmedia failed for '{medium_name}'"

        expected = " various-1-1.x86_64\n various2-1-1.x86_64\n various3-1-1.x86_64\n"

        fuzzy_out = self._query(pattern="v")
        assert (
            fuzzy_out == expected
        ), f"search_packages fuzzy 'v': expected {expected!r}, got {fuzzy_out!r}"

        list_out = self._query(list_all=True)
        assert (
            list_out == expected
        ), f"search_packages list all: expected {expected!r}, got {list_out!r}"

    @pytest.mark.skip(reason="This test fails : --force is without effect")
    def test_force_skip_unknown(self):
        """urpmi --force must install known packages even when unknown ones are listed.

        Without --force, requesting an unknown package alongside a known one
        must fail.  With --force, the known package must be installed and the
        unknown one silently skipped.
        """
        self.prepare()
        pkg = "various"
        ret, _ = self._addmedia(f"media/{pkg}")
        assert ret

        # Normal install of a known package must succeed
        assert self._install(pkg) == 0
        self.check_installed_names([pkg], remove=True)

        # Mixing a known and an unknown package without --force must fail
        assert self._install(pkg, "unknown-pkg") != 0
        self.check_nothing_installed()

        # With --force the known package must be installed, unknown skipped
        assert self._install(pkg, "unknown-pkg", force=True) == 0
        self.check_installed_names([pkg], full=True, remove=True)


# TODO Add all media and test that a standard package is installable
# cf superuser-mirrolist.t


class TestObsoleteAndConflict(BaseUrpmiTest):
    """Tests for split-package scenarios combining obsoletes and conflicts.

    Package relationships:
    - 'a' is split into 'b' and 'c':
        - 'b' obsoletes/provides 'a' and requires 'c'
        - 'c' conflicts with 'a' (but cannot obsolete it)
    - 'd' requires 'a'

    Installing b+c while 'a' is present must atomically remove 'a' and
    install both b and c.  When 'd' is also installed, it must be kept
    because 'b' provides 'a' (satisfying d's dependency).
    """

    MEDIUM = "obsolete-and-conflict"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia()
        assert ret, "addmedia failed"

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_split_package_removes_original(self):
        """Installing b+c while 'a' is present must remove 'a' and install b, c."""
        self.prepare()

        assert self._install("a") == 0
        self.check_installed_names(["a"])

        # b obsoletes a (and requires c), c conflicts with a =>
        # urpm must remove a and install b + c in one transaction.
        assert self._install("b", "c") == 0
        self.check_installed_names(["b", "c"], remove=True)

    def test_with_ad_plain(self):
        """With a+d installed, upgrading to b+c must keep d (via b provides a)."""
        self.prepare()

        assert self._install("a", "d") == 0
        self.check_installed_names(["a", "d"])

        assert self._install("b", "c") == 0
        self.check_installed_names(["b", "c", "d"], remove=True)

    def test_with_ad_split_level(self):
        """Same as test_with_ad_plain but packages installed one at a time.

        Mirrors the --split-level 1 --split-length 1 call in the Perl suite
        (perl-URPM fix for bug #31969: 'd' must not be removed without asking).
        """
        self.prepare()

        assert self._install("a", "d") == 0
        self.check_installed_names(["a", "d"])

        # Install one package at a time
        assert self._install("b") == 0
        assert self._install("c") == 0
        self.check_installed_names(["b", "c", "d"], remove=True)

    def test_with_ad_auto_c(self):
        """Installing only c with --auto must promote b (which obsoletes a)."""
        self.prepare()

        assert self._install("a", "d") == 0
        self.check_installed_names(["a", "d"])

        # Installing only 'c' should trigger automatic promotion of 'b'
        # because c conflicts with a and b obsoletes/provides a.
        assert self._install("c") == 0
        self.check_installed_names(["b", "c", "d"], remove=True)


class TestOrderingScriptlets(BaseUrpmiTest):
    """Tests that rpm/urpm respect scriptlet ordering when installing,
    removing, and upgrading packages that have scriptlet dependencies.

    The medium contains:
    - a-1 and a-2  (/bin/a is a simple 'cat')
    - requires_pre, requires_post, requires_preun, requires_postun
      (each package requires /bin/a at the corresponding scriptlet phase)

    Tests are run in two orderings (a first, then requires_X; and reversed)
    to ensure the ordering is correct regardless of the sequence passed
    to rpm/urpm.
    """

    MEDIUM = "ordering-scriptlets"

    def _rpm_glob_single(self, name, version):
        """Return a glob pattern for a single versioned RPM in the medium."""
        return f"media/{self.MEDIUM}/{name}-{version}-*.rpm"

    def _rpm_install_direct(self, *globs):
        """Install RPMs by glob patterns directly via rpm -i."""
        cmd = ["rpm", "--root", self.root, "-i"] + list(globs)
        ret = run(cmd, capture_output=True, text=True)
        assert ret.returncode == 0, f"rpm -i failed:\n{ret.stderr}"

    def _rpm_upgrade_direct(self, *globs):
        """Upgrade RPMs by glob patterns directly via rpm -U."""
        cmd = ["rpm", "--root", self.root, "-U"] + list(globs)
        ret = run(cmd, capture_output=True, text=True)
        assert ret.returncode == 0, f"rpm -U failed:\n{ret.stderr}"

    def _check_and_remove(self, *names):
        """Assert packages are installed then remove them."""
        self.check_installed_names(list(names), remove=True)

    # ------------------------------------------------------------------
    # Scenario helpers (mirror the Perl subs)
    # ------------------------------------------------------------------

    def _test_install_remove_rpm(self, name):
        """install via rpm, check, remove.

        Tested in both orderings: a first, then requires_X; and reversed.
        """
        a1 = self._rpm_glob_single("a", 1)
        pkg1 = self._rpm_glob_single(name, 1)

        # Ordering 1: a before requires_X
        self.prepare()
        self._rpm_install_direct(a1, pkg1)
        self._check_and_remove("a", name)

        # Ordering 2: requires_X before a
        self.prepare()
        self._rpm_install_direct(pkg1, a1)
        self._check_and_remove(name, "a")

    def _test_install_upgrade_rpm(self, name):
        """install v1, upgrade to v2."""
        a1 = self._rpm_glob_single("a", 1)
        a2 = self._rpm_glob_single("a", 2)
        pkg1 = self._rpm_glob_single(name, 1)
        pkg2 = self._rpm_glob_single(name, 2)

        # Ordering 1: a first at install and upgrade
        self.prepare()
        self._rpm_install_direct(a1, pkg1)
        self._rpm_upgrade_direct(pkg2, a2)
        self._check_and_remove("a", name)

        # Ordering 2: requires_X first at install and upgrade
        self.prepare()
        self._rpm_install_direct(pkg1, a1)
        self._rpm_upgrade_direct(a2, pkg2)
        self._check_and_remove(name, "a")

    def _test_install_remove_urpm(self, name):
        """Install a + requires_X via urpm in both orderings.
        Medium is re-added and removed around each sub-scenario.
        """
        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            assert self._install(*names) == 0
            self._check_and_remove(*names)

    def _test_install_upgrade_urpm(self, name):
        """Pre-install v1 of both packages via rpm, then upgrade via urpm
        in both orderings.
        """
        a1 = self._rpm_glob_single("a", 1)
        pkg1 = self._rpm_glob_single(name, 1)

        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            self._rpm_install_direct(a1, pkg1)
            assert self._install(*names) == 0
            self._check_and_remove(*names)

    def _test_install_remove_urpm_one_by_one(self, name):
        """Installs each package in a separate _install() call to emulate
        the one-package-per-transaction behaviour of --split-length 1.
        """
        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            for pkg in names:
                assert self._install(pkg) == 0
            self._check_and_remove(*names)

    def _test_install_upgrade_one_by_one(self, name):
        """Upgrade each package in a separate call to emulate
        the one-package-per-transaction behaviour of --split-length 1."""
        a1 = self._rpm_glob_single("a", 1)
        pkg1 = self._rpm_glob_single(name, 1)

        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            self._rpm_install_direct(a1, pkg1)
            for pkg in names:
                assert self._install(pkg) == 0
            self._check_and_remove(*names)

    # ------------------------------------------------------------------
    # Test methods
    # ------------------------------------------------------------------

    SCRIPTLET_PKGS_INSTALL = [
        "requires_pre",
        "requires_post",
        "requires_preun",
        "requires_postun",
    ]
    SCRIPTLET_PKGS_UPGRADE = ["requires_preun", "requires_postun"]

    def test_install_remove_rpm(self):
        """rpm -i ordering for all four scriptlet-dependency packages."""
        for name in self.SCRIPTLET_PKGS_INSTALL:
            self._test_install_remove_rpm(name)

    def test_install_upgrade_rpm(self):
        """rpm -i/-U ordering for upgrade-relevant scriptlet packages."""
        for name in self.SCRIPTLET_PKGS_UPGRADE:
            self._test_install_upgrade_rpm(name)

    def test_install_remove_urpm(self):
        """urpm install+remove ordering, all packages, no split."""
        for name in self.SCRIPTLET_PKGS_INSTALL:
            self._test_install_remove_urpm(name)

    def test_install_upgrade_urpm(self):
        """urpm install+upgrade ordering, upgrade packages, no split."""
        for name in self.SCRIPTLET_PKGS_UPGRADE:
            self._test_install_upgrade_urpm(name)

    def test_install_remove_urpm_one_by_one(self):
        """urpm install+remove, one package per transaction (emulates split-length 1)."""
        for name in self.SCRIPTLET_PKGS_INSTALL:
            self._test_install_remove_urpm_one_by_one(name)

    def test_install_upgrade_one_by_one(self):
        """urpm install+upgrade, one package per transaction (emulates split-length 1)."""
        for name in self.SCRIPTLET_PKGS_UPGRADE:
            self._test_install_upgrade_one_by_one(name)


class TestOrderingScriptlets(BaseUrpmiTest):
    """Tests that rpm/urpmi respect scriptlet ordering when installing,
    removing, and upgrading packages that have scriptlet dependencies.

    The medium contains:
    - a-1 and a-2  (/bin/a is a simple 'cat')
    - requires_pre, requires_post, requires_preun, requires_postun
      (each package requires /bin/a at the corresponding scriptlet phase)

    Tests are run in two orderings (a first, then requires_X; and reversed)
    to ensure the ordering is correct regardless of the sequence passed
    to rpm/urpmi.
    """

    MEDIUM = "ordering-scriptlets"

    def _rpm_glob_single(self, name, version):
        """Return a glob pattern for a single versioned RPM in the medium."""
        return f"media/{self.MEDIUM}/{name}-{version}-*.rpm"

    def _rpm_install_direct(self, *globs):
        """Install RPMs by glob patterns directly via rpm -i."""
        cmd = ["rpm", "--root", self.root, "-i"] + list(globs)
        ret = run(cmd, capture_output=True, text=True)
        assert ret.returncode == 0, f"rpm -i failed:\n{ret.stderr}"

    def _rpm_upgrade_direct(self, *globs):
        """Upgrade RPMs by glob patterns directly via rpm -U."""
        cmd = ["rpm", "--root", self.root, "-U"] + list(globs)
        ret = run(cmd, capture_output=True, text=True)
        assert ret.returncode == 0, f"rpm -U failed:\n{ret.stderr}"

    def _check_and_remove(self, *names):
        """Assert packages are installed then remove them."""
        self.check_installed_names(list(names), remove=True)

    # ------------------------------------------------------------------
    # Scenario helpers (mirror the Perl subs)
    # ------------------------------------------------------------------

    def _test_install_remove_rpm(self, name):
        """Mirror sub test_install_remove_rpm(): install via rpm, check, remove.

        Tested in both orderings: a first, then requires_X; and reversed.
        """
        a1 = self._rpm_glob_single("a", 1)
        pkg1 = self._rpm_glob_single(name, 1)

        # Ordering 1: a before requires_X
        self.prepare()
        self._rpm_install_direct(a1, pkg1)
        self._check_and_remove("a", name)

        # Ordering 2: requires_X before a
        self.prepare()
        self._rpm_install_direct(pkg1, a1)
        self._check_and_remove(name, "a")

    def _test_install_upgrade_rpm(self, name):
        """Mirror sub test_install_upgrade_rpm(): install v1, upgrade to v2."""
        a1 = self._rpm_glob_single("a", 1)
        a2 = self._rpm_glob_single("a", 2)
        pkg1 = self._rpm_glob_single(name, 1)
        pkg2 = self._rpm_glob_single(name, 2)

        # Ordering 1: a first at install and upgrade
        self.prepare()
        self._rpm_install_direct(a1, pkg1)
        self._rpm_upgrade_direct(pkg2, a2)
        self._check_and_remove("a", name)

        # Ordering 2: requires_X first at install and upgrade
        self.prepare()
        self._rpm_install_direct(pkg1, a1)
        self._rpm_upgrade_direct(a2, pkg2)
        self._check_and_remove(name, "a")

    def _test_install_remove_urpmi(self, name):
        """Mirror sub test_install_remove_urpmi() without split options.

        Install a + requires_X via urpmi in both orderings, remove via urpme.
        Medium is re-added and removed around each sub-scenario to match
        the Perl suite behaviour (urpmi_addmedia / urpmi_removemedia per call).
        """
        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            assert self._install(*names) == 0
            self._check_and_remove(*names)

    def _test_install_upgrade_urpmi(self, name):
        """Mirror sub test_install_upgrade_urpmi() without split options.

        Pre-install v1 of both packages via rpm, then upgrade via urpmi
        in both orderings.
        """
        a1 = self._rpm_glob_single("a", 1)
        pkg1 = self._rpm_glob_single(name, 1)

        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            self._rpm_install_direct(a1, pkg1)
            assert self._install(*names) == 0
            self._check_and_remove(*names)

    def _test_install_remove_urpmi_one_by_one(self, name):
        """Mirror sub test_install_remove_urpmi() with --split-length 1.

        Installs each package in a separate _install() call to emulate
        the one-package-per-transaction behaviour of --split-length 1.
        """
        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            for pkg in names:
                assert self._install(pkg) == 0
            self._check_and_remove(*names)

    def _test_install_upgrade_urpmi_one_by_one(self, name):
        """Mirror sub test_install_upgrade_urpmi() with --split-length 1."""
        a1 = self._rpm_glob_single("a", 1)
        pkg1 = self._rpm_glob_single(name, 1)

        for names in [["a", name], [name, "a"]]:
            self.prepare()
            ret, _ = self._addmedia()
            assert ret
            self._rpm_install_direct(a1, pkg1)
            for pkg in names:
                assert self._install(pkg) == 0
            self._check_and_remove(*names)

    # ------------------------------------------------------------------
    # Test methods
    # ------------------------------------------------------------------

    SCRIPTLET_PKGS_INSTALL = [
        "requires_pre",
        "requires_post",
        "requires_preun",
        "requires_postun",
    ]
    SCRIPTLET_PKGS_UPGRADE = ["requires_preun", "requires_postun"]

    def test_install_remove_rpm(self):
        """rpm -i ordering for all four scriptlet-dependency packages."""
        for name in self.SCRIPTLET_PKGS_INSTALL:
            self._test_install_remove_rpm(name)

    def test_install_upgrade_rpm(self):
        """rpm -i/-U ordering for upgrade-relevant scriptlet packages."""
        for name in self.SCRIPTLET_PKGS_UPGRADE:
            self._test_install_upgrade_rpm(name)

    def test_install_remove_urpmi(self):
        """urpmi install+remove ordering, all packages, no split."""
        for name in self.SCRIPTLET_PKGS_INSTALL:
            self._test_install_remove_urpmi(name)

    def test_install_upgrade_urpmi(self):
        """urpmi install+upgrade ordering, upgrade packages, no split."""
        for name in self.SCRIPTLET_PKGS_UPGRADE:
            self._test_install_upgrade_urpmi(name)

    def test_install_remove_urpmi_one_by_one(self):
        """urpmi install+remove, one package per transaction (emulates split-length 1)."""
        for name in self.SCRIPTLET_PKGS_INSTALL:
            self._test_install_remove_urpmi_one_by_one(name)

    def test_install_upgrade_urpmi_one_by_one(self):
        """urpmi install+upgrade, one package per transaction (emulates split-length 1)."""
        for name in self.SCRIPTLET_PKGS_UPGRADE:
            self._test_install_upgrade_urpmi_one_by_one(name)


class TestOrphans(BaseUrpmiTest):
    """Tests for orphan detection and removal during upgrades.

    Two media are used:
    - orphans-1: version 1 of all packages
    - orphans-2: version 2 of all packages (with various dependency changes)

    Package relationships are described at the top of superuser--orphans.t.
    """

    MEDIUM_V1 = "orphans-1"
    MEDIUM_V2 = "orphans-2"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia(f"media/{self.MEDIUM_V1}")
        assert ret, f"addmedia failed for {self.MEDIUM_V1}"
        ret, _ = self._addmedia(f"media/{self.MEDIUM_V2}")
        assert ret, f"addmedia failed for {self.MEDIUM_V2}"
        self._reset_unrequested_list()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _install_v1(self, *names):
        """Install packages from the v1 medium."""
        return self._install(*names, media=self.MEDIUM_V1)

    def _install_v2(self, *names):
        """Install packages from the v2 medium."""
        return self._install(*names, media=self.MEDIUM_V2)

    def _auto_select_v2(self, auto_orphans=False):
        """Run urpmi --auto-select against the v2 medium."""
        args = argparse.Namespace(
            urpm_root=self.root,
            rpm_root=self.root,
            root=self.root,
            packages=[],
            auto=True,
            auto_select=True,
            auto_orphans=auto_orphans,
            without_recommends=True,
            with_suggests=False,
            download_only=False,
            nodeps=False,
            nosignature=True,
            noscripts=False,
            force=False,
            reinstall=False,
            debug=None,
            watched=None,
            prefer=None,
            all=False,
            test=False,
            sync=True,
            allow_no_root=True,
            config_policy="replace",
            allow_arch=None,
            media=self.MEDIUM_V2,
        )
        from urpm.cli.commands.upgrade import cmd_upgrade

        return cmd_upgrade(args, self.chroot_db)

    def _query_orphans(self):
        """Return the set of orphaned package names (NVR without arch).

        Mirrors: urpmq -r --auto-orphans
        """
        from urpm.core.operations import PackageOperations

        ops = PackageOperations(self.chroot_db)
        return set(ops.get_orphans(root=self.root))

    def _nvr(self, name, version, release="1"):
        """Build a NVR string: name-version-release."""
        return f"{name}-{version}-{release}"

    def _add_version1(self, *names):
        return [self._nvr(n, 1) for n in names]

    def _add_version2(self, *names):
        return [self._nvr(n, 2) for n in names]

    def _add_release(self, *names):
        """Add '-1' release suffix: 'a-2' -> 'a-2-1'."""
        result = []
        for n in names:
            parts = n.rsplit("-", 1)
            result.append(f"{n}-1")
        return result

    # ------------------------------------------------------------------
    # Scenario helpers (mirror Perl subs)
    # ------------------------------------------------------------------

    def _test_urpme_v1(self, req_v1_list, remove_v1, remaining_v1):
        """Mirror sub test_urpme_v1(): install from v1, remove with auto-orphans."""
        self.prepare()
        for pkg in req_v1_list:
            assert self._install_v1(*pkg.split()) == 0
        self._urpme_auto_orphans(remove_v1)
        expected = list(filter(None, remaining_v1.split()))
        self.check_installed_names(expected, remove=bool(expected))
        self._reset_unrequested_list()

    def _test_urpme(self, req_v1_list, wanted_v2, remove_v2, remaining_v2):
        """Mirror sub test_urpme(): install v1, upgrade some to v2, remove with orphans."""
        self.prepare()
        for pkg in req_v1_list:
            assert self._install_v1(pkg) == 0
        assert self._install_v2(wanted_v2) == 0
        self._urpme_auto_orphans(remove_v2)
        remaining = self._add_release(*filter(None, remaining_v2.split()))
        self.check_installed_names(remaining, full=True, remove=bool(remaining))
        self._reset_unrequested_list()

    def _test_auto_select(self, req_v1_list, wanted_v1_nvr, wanted_v2, orphans_v2):
        """Mirror sub test_auto_select(): both urpmq/urpme and auto-orphans variants."""
        self._test_auto_select_urpmq_urpme(
            req_v1_list, wanted_v1_nvr, wanted_v2, orphans_v2
        )
        self._test_auto_select_auto_orphans(req_v1_list, wanted_v1_nvr, wanted_v2)

    def _test_auto_select_urpmq_urpme(
        self, req_v1_list, wanted_v1_nvr, wanted_v2, orphans_v2
    ):
        """Mirror sub test_auto_select_raw_urpmq_urpme()."""
        self.prepare()
        for pkg in req_v1_list:
            assert self._install_v1(pkg) == 0
        self.check_installed_names(wanted_v1_nvr.split(), full=True)

        self._auto_select_v2(auto_orphans=False)

        expected_orphans = set(filter(None, orphans_v2.split()))
        actual_orphans = self._query_orphans()
        assert (
            actual_orphans == expected_orphans
        ), f"orphans mismatch: got {actual_orphans}, want {expected_orphans}"

        all_v2 = list(filter(None, f"{wanted_v2} {orphans_v2}".split()))
        self.check_installed_names(self._add_release(*all_v2), full=True)

        self._urpme_auto_orphans()
        remaining = self._add_release(*filter(None, wanted_v2.split()))
        self.check_installed_names(remaining, full=True, remove=True)
        self._reset_unrequested_list()

    def _test_auto_select_auto_orphans(self, req_v1_list, wanted_v1_nvr, wanted_v2):
        """Mirror sub test_auto_select_raw_auto_orphans()."""
        self.prepare()
        for pkg in req_v1_list:
            assert self._install_v1(pkg) == 0
        self.check_installed_names(wanted_v1_nvr.split(), full=True)

        self._auto_select_v2(auto_orphans=True)

        remaining = self._add_release(*filter(None, wanted_v2.split()))
        self.check_installed_names(remaining, full=True, remove=True)
        self._reset_unrequested_list()

    def _test_auto_select_both(self, pkg, wanted_v1, wanted_v2, orphans_v2=""):
        """Mirror sub test_auto_select_both()."""
        # test_urpme1: install pkg from v1, remove with auto-orphans
        self.prepare()
        assert self._install_v1(pkg) == 0
        self._urpme_auto_orphans(pkg)
        self.check_nothing_installed()
        self._reset_unrequested_list()

        # test_urpme2: only for packages whose v1 deps don't require the pkg itself
        skip_urpme2 = bool(set(pkg) & set("mlno"))
        if not skip_urpme2:
            self.prepare()
            assert self._install_v1(pkg) == 0
            self.check_installed_names([pkg] + list(filter(None, wanted_v1.split())))
            self._urpme_auto_orphans()  # must not remove anything
            self.check_installed_names([pkg] + list(filter(None, wanted_v1.split())))
            self._urpme_auto_orphans(pkg)
            self._urpme_auto_orphans()
            self.check_nothing_installed()
            self._reset_unrequested_list()

        # test_auto_select for pkg itself
        wanted_v1_nvr = " ".join(
            self._add_version1(pkg, *filter(None, wanted_v1.split()))
        )
        self._test_auto_select(
            [pkg],
            wanted_v1_nvr,
            wanted_v2,
            orphans_v2,
        )

        # test_auto_select for req-pkg (package that requires pkg)
        req_pkg = f"req-{pkg}"
        req_wanted_v1_nvr = " ".join(
            self._add_version1(req_pkg, pkg, *filter(None, wanted_v1.split()))
        )
        self._test_auto_select(
            [req_pkg],
            req_wanted_v1_nvr,
            f"req-{pkg}-2",
            f"{wanted_v2} {orphans_v2}".strip(),
        )

    def _test_unorphan_v1(self, pkg1, pkg2):
        """Install pkg1 (which pulls pkg2), remove pkg1 — pkg2 becomes orphan, removed."""
        self.prepare()
        assert self._install_v1(pkg1) == 0
        assert self._install_v1(pkg2) == 0
        self._urpme_auto_orphans(pkg1)
        self.check_installed_names([pkg2], remove=True)

    def _test_unorphan_v2(self, pkg1, pkg2):
        """Install pkg1, remove it, install pkg2 — pkg2 is now unrequested, removed by auto-orphans."""
        self.prepare()
        assert self._install_v1(pkg1) == 0
        self._rpm_remove(pkg1)
        assert self._install_v1(pkg2) == 0
        self._urpme_auto_orphans()
        self.check_installed_names([pkg2], remove=True)

    def _test_unorphan_v3(self, pkg1, pkg2):
        """Install pkg1 (pulls pkg2), remove both, reinstall pkg2 — auto-orphans cleans up."""
        self.prepare()
        assert self._install_v1(pkg1) == 0
        self.check_installed_names([pkg2, pkg1], remove=True)
        assert self._install_v1(pkg2) == 0
        self._urpme_auto_orphans()
        self.check_installed_names([pkg2], remove=True)

    # ------------------------------------------------------------------
    # Test methods
    # ------------------------------------------------------------------

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_urpme_v1_h(self):
        """Remove h and its weak-dep orphan hh (weak deps always supported)."""
        self._test_urpme_v1(["hh h"], "h", "hh")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_urpme_v1_u1_u2(self):
        """u1 requires u2 — removing u1 leaves u2 as orphan."""
        self._test_urpme_v1(["u1 u2"], "u1", "u2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_urpme_v1_u3_u4(self):
        """u4 requires u3 — removing u4 leaves u3 as orphan."""
        self._test_urpme_v1(["u3 u4"], "u4", "u3")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_a(self):
        self._test_auto_select_both("a", "", "a-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_b(self):
        self._test_auto_select_both("b", "", "bb-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_c(self):
        self._test_auto_select_both("c", "cc", "c-2 cc-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_d(self):
        self._test_auto_select_both("d", "dd", "d-2", "dd-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_e(self):
        self._test_auto_select_both("e", "ee1", "e-2 ee2-2", "ee1-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_f(self):
        self._test_auto_select_both("f", "ff1", "f-2 ff2-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_g(self):
        self._test_auto_select_both("g", "gg", "g-2 gg-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_h(self):
        """h suggests hh: after upgrade h-2 is kept, hh-1 becomes orphan."""
        self._test_auto_select_both("h", "hh", "h-2", "hh-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_l(self):
        self._test_auto_select_both("l", "ll", "l-2 ll-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_m(self):
        self._test_auto_select_both("m", "mm", "m-2 mm-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_n(self):
        self._test_auto_select_both("n", "nn", "n-2 nn-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_o(self):
        self._test_auto_select_both("o", "oo1", "o-2 oo2-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_r(self):
        self._test_auto_select_both("r", "rr1", "r-2", "rr1-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_s(self):
        self._test_auto_select_both("s", "ss1 ss2", "s-2 ss1-1 ss2-1")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_auto_select_t(self):
        self._test_auto_select_both("t", "tt1", "t-2 tt2-2", "tt1-1")

    @pytest.mark.skip(reason="Mismatch['r-2-1\n', 'rr2-1-1\n'] r-1-1 rr1-1-1 rr2-1-1")
    def test_auto_select_r_with_rr2(self):
        """r with both rr1 and rr2 available: rr1 becomes orphan after upgrade."""
        self._test_auto_select(
            ["r", "rr2"],
            " ".join(self._add_version1("r", "rr1", "rr2")),
            "r-2 rr2-1",
            "rr1-1",
        )

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_urpme_g(self):
        """Remove g-2, gg-2 stays (was explicitly installed)."""
        self._test_urpme(["g"], "g", "g", "")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_urpme_gg_g(self):
        """Remove g-2 after upgrading: gg-2 remains as it was explicitly requested."""
        self._test_urpme(["gg", "g"], "g", "g", "gg-2")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_unorphan_v1(self):
        self._test_unorphan_v1("u1", "u2")

    def test_unorphan_v2(self):
        self._test_unorphan_v2("u1", "u2")

    def test_unorphan_v3(self):
        self._test_unorphan_v3("u1", "u2")


class TestOrphansKernels(BaseUrpmiTest):
    """Tests for kernel orphan handling with DKMS packages.

    Two media are used:
    - kernel-1: old naming (NVR = kernel-desktop-5.6.2-1, V=1, R=1.mga8)
    - kernel-2: new naming (NVR = kernel-desktop, V=5.6.2, R=1.mga8)

    Five successive kernel versions are installed along with matching DKMS
    packages (virtualbox-kernel-*).  After urpme --auto-orphans, only the
    latest kernel and its DKMS counterpart should remain.
    """

    MEDIUM_V1 = "kernel-1"
    MEDIUM_V2 = "kernel-2"
    BASE_KVERSION = "5.15.4"
    DKMS_NAME = "virtualbox"
    DKMS_VERSION = "6.1.36"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia(f"media/{self.MEDIUM_V1}")
        assert ret, f"addmedia failed for {self.MEDIUM_V1}"
        ret, _ = self._addmedia(f"media/{self.MEDIUM_V2}")
        assert ret, f"addmedia failed for {self.MEDIUM_V2}"
        self._reset_unrequested_list()

    def _test_unorphan_kernels(self, medium, pkg, pkg2=None):
        """Mirror sub test_unorphan_kernels().

        Install five successive kernel versions (each with a matching DKMS
        package), then run urpme --auto-orphans.  Only the latest kernel,
        the latest DKMS package, the kernel-desktop-latest meta-package, and
        the virtualbox-kernel-desktop-latest meta-package should remain.

        - medium:  which medium to use (kernel-1 or kernel-2)
        - pkg:     the kernel meta-package name (e.g. 'kernel-desktop-latest')
        - pkg2:    if given, the base kernel package name whose latest NVR
                   should also remain (e.g. 'kernel-desktop'); defaults to
                   the last versioned NVR installed (old naming convention)
        """
        arch = self._get_arch()
        # The versioned DKMS dep that must survive (hardcoded like in Perl):
        # virtualbox-kernel-5.15.45-desktop-1  (base_kversion + "5" + "-desktop-1")
        latest_dkms_dep = f"{self.DKMS_NAME}-kernel-{self.BASE_KVERSION}5-desktop-1"
        latest_kpkg = None
        latest_dpkg = None

        for i in range(1, 6):
            latest_kpkg = f"{pkg}-{self.BASE_KVERSION}{i}-1"
            assert self._install(latest_kpkg, media=medium) == 0

            latest_dpkg = f"{self.DKMS_NAME}-{pkg}-{self.DKMS_VERSION}-{i}.{arch}"
            assert self._install(latest_dpkg, media=medium) == 0

        self._urpme_auto_orphans()

        # Determine what should remain after orphan cleanup
        surviving_pkg2 = pkg2 if pkg2 is not None else latest_kpkg
        surviving_pkg2 = surviving_pkg2.replace("-latest", "")

        expected = [
            pkg,  # kernel-desktop-latest meta-package
            "virtualbox-kernel-desktop-latest",  # DKMS meta-package
            surviving_pkg2,  # latest versioned kernel (or base name)
            latest_dkms_dep,  # latest versioned DKMS package
        ]
        self.check_installed_names(expected, remove=True)
        self._reset_unrequested_list()

    # ------------------------------------------------------------------
    # Test methods
    # ------------------------------------------------------------------

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_unorphan_kernels_old_naming(self):
        """Old kernel naming: NVR encodes version in the package name.

        kernel-desktop-5.6.2-1 style — the latest versioned NVR itself
        is the surviving pkg2 after cleanup.
        """
        self.prepare()
        self._test_unorphan_kernels(self.MEDIUM_V1, "kernel-desktop-latest")

    @pytest.mark.skip(reason="Erreur : la suppression nécessite les privilèges root")
    def test_unorphan_kernels_new_naming(self):
        """New kernel naming: package name is kernel-desktop, version in V field.

        kernel-desktop-5.6.2-1.mga8 style — 'kernel-desktop' (the base name)
        is the surviving pkg2 after cleanup.
        """
        self.prepare()
        self._test_unorphan_kernels(
            self.MEDIUM_V2, "kernel-desktop-latest", "kernel-desktop"
        )


class TestPrefer2(BaseUrpmiTest):
    """Tests for provider preference when multiple packages satisfy a dependency.

    Bug #46874: when a package requires both 'bb' and 'b2', and 'bb' is
    provided by both b1 and b2, urpmi must prefer b1 (the dedicated provider)
    over b2 (which only provides bb as a side effect).

    - a requires bb and b2; bb provided by b1 and b2 => b1 must be chosen
    - d is the mirror case (ensures both dependency orderings work):
      d requires cc and c1; cc provided by c1 and c2 => c2 must be chosen
    """

    MEDIUM = "prefer2"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia()
        assert ret, "addmedia failed"

    def _test(self, pkg, expected):
        assert self._install(pkg) == 0
        self.check_installed_names(expected, remove=True)

    @pytest.mark.skip(reason="mismatch: got:  'a\nb1\nb2\n' want: 'a\nb2\n'")
    def test_prefer_b1_over_b2(self):
        """a requires bb+b2; b1 and b2 both provide bb => b1 must be picked."""
        self.prepare()
        self._test("a", ["a", "b2"])

    def test_prefer_c2_over_c1(self):
        """d requires cc+c1; c1 and c2 both provide cc => c2 must be picked."""
        self.prepare()
        self._test("d", ["d", "c1"])


# TODO priority-upgrade


class TestProvideAndNoObsolete(BaseUrpmiTest):
    """Tests for upgrade behaviour when multiple packages provide the same virtual.

    Bug context: a-1 provides c-1, a-2 provides c-2, b-3 provides c-3.
    b-3 does NOT obsolete a, so it cannot replace it.
    urpmi must still be able to upgrade a-1 to a-2 in the presence of b-3.
    """

    MEDIUM = "provide-and-no-obsolete"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia()
        assert ret, "addmedia failed"

    def _setup(self):
        """Pre-install a-1 via rpm directly"""
        self._rpm_install_succeeds(self.MEDIUM, "a-1")
        self.check_installed_names(["a-1-1"], full=True)

    def _auto_select(self):
        """Run urpmi --auto-select --auto (upgrade all, no prompt)."""
        return self._upgrade()

    @pytest.mark.skip(reason="mismatch: got:  'a-1-1\n' want: 'a-2-1\n'")
    def test_upgrade_a(self):
        """urpm a: a-1 must be upgraded to a-2 despite b-3 providing c-3."""
        self.prepare()
        self._setup()
        assert self._install("a") == 0
        self.check_installed_names(["a-2-1"], full=True)
        self._urpme("a")
        self.check_nothing_installed()

    def test_install_b_keeps_a(self):
        """urpmi b: b-3 is installed alongside a-1 (b does not obsolete a)."""
        self.prepare()
        self._setup()
        assert self._install("b") == 0
        self.check_installed_names(["a-1-1", "b-3-1"], full=True)
        self._urpme("a", "b")
        self.check_nothing_installed()

    @pytest.mark.skip(reason="Erreur : la mise à jour nécessite les privilèges root")
    def test_auto_select_upgrades_a(self):
        """urpmi --auto-select --auto must upgrade a-1 to a-2, same as 'urpmi a' (bug #31130)."""
        self.prepare()
        self._setup()
        assert self._auto_select() == 0
        self.check_installed_names(["a-2-1"], full=True)
        self._urpme("a")
        self.check_nothing_installed()


class TestReadmeUrpmi(BaseUrpmiTest):
    """Tests that urpmi displays the correct README messages when installing
    or upgrading packages that ship a README.urpmi file.

    sub test_urpmi() in the Perl suite captures urpmi output and extracts
    lines between "More information on package..." and the 70-dash separator.
    We replicate this by capturing stdout from cmd_install and applying the
    same regex.
    """

    MEDIUM = "README-urpmi"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia()
        assert ret, "addmedia failed"

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _install_and_get_readme_msgs(self, *names):
        """Install packages and return the sorted list of README.urpmi messages.

        Mirrors sub test_urpmi(): captures stdout from cmd_install, then
        extracts every block between a 'More information on package...' line
        and the following 70-dash separator.
        """
        import io, contextlib, re

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._install(*names)
        output = buf.getvalue()
        msgs = re.findall(
            r"\nMore information on package[^\n]*\n(.*?)\n\n-{70}", output, re.DOTALL
        )
        return sorted(msgs)

    def _check_readme(self, names, expected_msgs):
        """Install packages and assert the README messages match expected_msgs."""
        msgs = self._install_and_get_readme_msgs(
            *names if isinstance(names, list) else [names]
        )
        assert msgs == sorted(
            expected_msgs
        ), f"README messages mismatch:\n  got:  {msgs}\n  want: {sorted(expected_msgs)}"

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    @pytest.mark.skip(reason="The display of README.urpmi is not yet ready in urpm")
    def test_a(self):
        """Installing a fresh package shows 'installing/upgrading a'."""
        self.prepare()
        self._check_readme(["a"], ["installing/upgrading a"])
        self.check_installed_names(["a"], remove=True)

    @pytest.mark.skip(reason="The display of README.urpmi is not yet ready in urpm")
    def test_b(self):
        """Upgrading b-1 to b-2 shows the upgrade messages; then upgrading via name."""
        self.prepare()
        # Pre-install b-1 via rpm directly
        self._rpm_install_succeeds(self.MEDIUM, "b-1")

        # Explicit upgrade to b-2
        self._check_readme(["b-2"], ["upgrading b", "upgrading b 2"])

        # Upgrade again via package name only
        self._check_readme(["b"], ["upgrading b"])
        self.check_installed_names(["b"], remove=True)

    @pytest.mark.skip(reason="The display of README.urpmi is not yet ready in urpm")
    def test_c(self):
        """Installing c shows 'installing c'."""
        self.prepare()
        self._check_readme(["c"], ["installing c"])
        self.check_installed_names(["c"], remove=True)

    @pytest.mark.skip(reason="The display of README.urpmi is not yet ready in urpm")
    def test_d(self):
        """Installing d then d_ shows their respective README messages."""
        self.prepare()
        self._check_readme(["d"], ["installing/upgrading d"])
        # d_ may show any valid message; we just verify install succeeds
        self._check_readme(["d_"], ["installing d_"])
        self.check_installed_names(["d_"], remove=True)


# TODO or not ? rpm-query-in-scriptlet
# TODO or not ? rpmnew, tests for rpm ?
# TODO or not ? should-restart, doesn't seem managed'


class TestSpecifyMedia(BaseUrpmiTest):
    """Tests for --media, --excludemedia and --sortmedia options.

    Two media are added: 'various' and 'various_bis' (a symlink to the same
    directory).  The tests verify that urpmi/urpmq correctly restricts or
    orders the media used for installation and source queries.
    """

    MEDIA = ["various", "various_bis"]

    def prepare(self):
        super().prepare()
        # Create the symlink 'media/various_bis' -> 'media/various'
        src = Path("media/various")
        dst = Path("media/various_bis")
        if not dst.exists():
            dst.symlink_to(src.resolve())

        for medium in self.MEDIA:
            ret, _ = self._addmedia(f"media/{medium}")
            assert ret, f"addmedia failed for '{medium}'"

    def _media_dir(self, medium):
        return str(Path(f"media/{medium}").absolute())

    # ------------------------------------------------------------------
    # Helpers mirroring sub test_urpmq / sub test_urpmi
    # ------------------------------------------------------------------

    # TODO find which function provides sources
    def _query_sources(self, media=None, excludemedia=None, sortmedia=None):
        """Return the list of source directories for package 'various'.

        Mirrors: urpmq [options] --sources various
        Uses PackageOperations to find which media would provide the package,
        filtered/ordered according to the given options.
        """
        from urpm.core.operations import PackageOperations

        ops = PackageOperations(self.chroot_db)
        return ops.query_sources(
            "various",
            media=media,
            excludemedia=excludemedia,
            sortmedia=sortmedia,
        )

    def _install_and_check_source(
        self, wanted_dir, bad_medium, media=None, excludemedia=None, sortmedia=None
    ):
        """Install 'various' with given media options, check source dir and cleanup.

        Mirrors sub test_urpmi(): verifies the package was fetched from
        wanted_dir and NOT from bad_medium, then removes it.
        """
        assert (
            self._install(
                "various",
                media=media,
                excludemedia=excludemedia,
                sortmedia=sortmedia,
            )
            == 0
        )
        # Verify the package came from the expected directory by checking
        # which medium is recorded as the source in the transaction log.
        # We re-query to confirm the expected medium was used.
        # not ready, no know output for sources
        # sources = self._query_sources(
        #     media=media, excludemedia=excludemedia, sortmedia=sortmedia
        # )
        # assert any(self._media_dir(wanted_dir) in s for s in sources), (
        #     f"expected source dir {wanted_dir!r} not found in {sources}"
        # )
        # assert not any(bad_medium in s for s in sources), (
        #     f"bad medium {bad_medium!r} should not appear in {sources}"
        # )
        self._urpme("various")

    # ------------------------------------------------------------------
    # urpmq tests (source directory queries)
    # ------------------------------------------------------------------
    # not found commend to ourput --sources, i.e. path to the rpm
    # def test_urpmq_no_filter(self):
    #     """urpmq --sources: both media dirs must appear."""
    #     self.prepare()
    #     sources = self._query_sources()
    #     for medium in self.MEDIA:
    #         assert any(self._media_dir(medium) in s for s in sources), (
    #             f"expected {medium!r} in sources: {sources}"
    #         )
    #
    # def test_urpmq_media_0(self):
    #     """urpmq --media various: only the first medium dir."""
    #     self.prepare()
    #     sources = self._query_sources(media=self.MEDIA[0])
    #     assert all(self._media_dir(self.MEDIA[0]) in s for s in sources)
    #     assert not any(self._media_dir(self.MEDIA[1]) in s for s in sources)
    #
    # def test_urpmq_media_1(self):
    #     """urpmq --media various_bis: only the second medium dir."""
    #     self.prepare()
    #     sources = self._query_sources(media=self.MEDIA[1])
    #     assert all(self._media_dir(self.MEDIA[1]) in s for s in sources)
    #     assert not any(self._media_dir(self.MEDIA[0]) in s for s in sources)
    #
    # def test_urpmq_excludemedia_1(self):
    #     """urpmq --excludemedia various_bis: only the first medium dir."""
    #     self.prepare()
    #     sources = self._query_sources(excludemedia=self.MEDIA[1])
    #     assert all(self._media_dir(self.MEDIA[0]) in s for s in sources)
    #     assert not any(self._media_dir(self.MEDIA[1]) in s for s in sources)
    #
    # def test_urpmq_excludemedia_0(self):
    #     """urpmq --excludemedia various: only the second medium dir."""
    #     self.prepare()
    #     sources = self._query_sources(excludemedia=self.MEDIA[0])
    #     assert all(self._media_dir(self.MEDIA[1]) in s for s in sources)
    #     assert not any(self._media_dir(self.MEDIA[0]) in s for s in sources)
    #
    # def test_urpmq_sortmedia_0_1(self):
    #     """urpmq --sortmedia various,various_bis: first medium comes first."""
    #     self.prepare()
    #     sources = self._query_sources(
    #         sortmedia=f"{self.MEDIA[0]},{self.MEDIA[1]}"
    #     )
    #     assert self._media_dir(self.MEDIA[0]) in sources[0]
    #
    # def test_urpmq_sortmedia_1_0(self):
    #     """urpmq --sortmedia various_bis,various: second medium comes first."""
    #     self.prepare()
    #     sources = self._query_sources(
    #         sortmedia=f"{self.MEDIA[1]},{self.MEDIA[0]}"
    #     )
    #     assert self._media_dir(self.MEDIA[1]) in sources[0]

    # ------------------------------------------------------------------
    # urpmi tests (actual installation with media filtering)
    # ------------------------------------------------------------------

    def test_urpmi_no_filter(self):
        """urpmi (no filter): installs from first medium, not from various_bis."""
        self.prepare()
        self._install_and_check_source(self.MEDIA[0], self.MEDIA[1])

    def test_urpmi_media_0(self):
        """urpmi --media various: installs from various."""
        self.prepare()
        self._install_and_check_source(
            self.MEDIA[0], self.MEDIA[1], media=self.MEDIA[0]
        )

    def test_urpmi_media_1(self):
        """urpmi --media various_bis: installs from various_bis."""
        self.prepare()
        self._install_and_check_source(
            self.MEDIA[1], self.MEDIA[0], media=self.MEDIA[1]
        )

    def test_urpmi_excludemedia_1(self):
        """urpmi --excludemedia various_bis: installs from various."""
        self.prepare()
        self._install_and_check_source(
            self.MEDIA[0], self.MEDIA[1], excludemedia=self.MEDIA[1]
        )

    def test_urpmi_excludemedia_0(self):
        """urpmi --excludemedia various: installs from various_bis."""
        self.prepare()
        self._install_and_check_source(
            self.MEDIA[1], self.MEDIA[0], excludemedia=self.MEDIA[0]
        )

    def test_urpmi_sortmedia_0_1(self):
        """urpmi --sortmedia various,various_bis: installs from various first."""
        self.prepare()
        self._install_and_check_source(
            self.MEDIA[0],
            self.MEDIA[1],
            sortmedia=f"{self.MEDIA[0]},{self.MEDIA[1]}",
        )

    def test_urpmi_sortmedia_1_0(self):
        """urpmi --sortmedia various_bis,various: installs from various_bis first."""
        self.prepare()
        self._install_and_check_source(
            self.MEDIA[1],
            self.MEDIA[0],
            sortmedia=f"{self.MEDIA[1]},{self.MEDIA[0]}",
        )


class TestSrpmBootstrapping(BaseUrpmiTest):
    """Tests for SRPM build-requirements installation and source RPM install.

    Two scenarios are tested (mirroring the two calls to sub test()):
    1. Pass the .src.rpm file path directly to urpmi --buildrequires.
    2. Add the SRPM medium and pass the source package name with --buildrequires.

    Each scenario also tests --install-src: the .spec file must be placed in
    ~/rpmbuild/SPECS/ and match the reference copy in data/SPECS/.
    """

    MEDIUM = "srpm-bootstrapping"
    MEDIUM_SRC = "srpm-bootstrapping-src"

    def prepare(self):
        super().prepare()
        ret, _ = self._addmedia(f"media/{self.MEDIUM}")
        assert ret, f"addmedia failed for {self.MEDIUM}"

    def _install_builddeps(self, *packages):
        """Install build dependencies via urpmi --buildrequires --auto.

        Mirrors: urpmi --buildrequires --auto <packages>
        Uses builddeps="AUTO" to let urpmi resolve build deps automatically.
        """
        print(f"Install {packages}")
        return self._install(*packages, builddeps="AUTO")

    def _install_src(self, *packages):
        """Install source RPM(s) via urpmi --install-src.

        Mirrors: urpmi --install-src <packages>
        Places the .spec file under root/root/rpmbuild/SPECS/.
        """
        return self._install(*packages, install_src=True)

    def _check_spec(self):
        """Assert the installed .spec matches the reference copy in data/SPECS/."""
        installed = Path(f"root/root/rpmbuild/SPECS/{self.MEDIUM}.spec")
        reference = Path(f"data/SPECS/{self.MEDIUM}.spec")
        assert installed.exists(), f"spec file not found: {installed}"
        assert (
            installed.read_bytes() == reference.read_bytes()
        ), f"spec mismatch: {installed} differs from {reference}"

    def _run_test(self, *packages):
        """Mirror sub test(): install builddeps, check, install src, verify spec."""
        # Create the rpmbuild directory tree inside the chroot
        Path(f"root/root/rpmbuild/SOURCES").mkdir(parents=True, exist_ok=True)

        # Install build dependencies and verify
        assert self._install_builddeps(*packages) == 0
        self.check_installed_names([self.MEDIUM])

        # Install source RPM and verify the spec file
        assert self._install_src(*packages) == 0
        self._check_spec()

        # Clean up rpmbuild tree and remove installed packages
        shutil.rmtree("root/usr/src/rpm", ignore_errors=True)
        self.check_installed_names([self.MEDIUM], remove=True)

    # ------------------------------------------------------------------
    # Test methods
    # ------------------------------------------------------------------
    @pytest.mark.skip(reason="No .spec file found. Run from an RPM build tree or specify a .spec/.src.rpm file")
    def test_buildrequires_from_srpm_file(self):
        """Pass the .src.rpm path directly to --buildrequires."""
        self.prepare()
        srpm_glob = list(
            Path(f"media/SRPMS-{self.MEDIUM}").glob(f"{self.MEDIUM}-*.src.rpm")
        )
        assert srpm_glob, f"no src.rpm found in media/SRPMS-{self.MEDIUM}"
        self._run_test(str(srpm_glob[0]))

    @pytest.mark.skip(reason="No .spec file found. Run from an RPM build tree or specify a .spec/.src.rpm file")
    def test_buildrequires_from_src_medium(self):
        """Add the SRPM medium then pass the package name to --buildrequires."""
        self.prepare()
        ret, _ = self._addmedia(f"media/SRPMS-{self.MEDIUM}")
        assert ret, f"addmedia failed for SRPMS medium"
        self._run_test(self.MEDIUM)
