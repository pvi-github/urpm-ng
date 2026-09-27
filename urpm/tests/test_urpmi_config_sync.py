"""Tests for keeping urpmi's media on the release the machine now runs.

urpmi and urpm-ng coexist, and a distupgrade used to leave urpmi
naming the release the machine just left: its URL-direct media carry
the number in the path, and nothing rewrote it.  The operator met that
the first time they typed ``urpmi``, which is the kind of leftover QA
sends a release back for.

Four things are covered here, in the order they happen:

* the rewrite itself, which must move the release and nothing that
  merely looks like it;
* the ``on`` key, which is what keeps this rule to a distupgrade;
* the refusal to restart a service while a release change is only half
  in place, which is a policy and is reported as one;
* the wiring, so that no path evaluates rules without saying which
  operation it was.  A behaviour test would not have caught the three
  call sites that used to pass none.
"""

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from urpm.core import hooks, init_system, urpmi_config
from urpm.core.distupgrade.version import ReleaseIdentity, identity_of
from urpm.core.hooks import (
    Action, Hook, Operation, OperationOutcome, hooks_for, load_hooks,
)
from urpm.core.init_system import Result
from urpm.core.operations import PackageOperations
from urpm.core.urpmi_config import SyncReport, sync_urpmi_config

REPO = Path(__file__).resolve().parents[2]

#: A configuration with the shapes that matter: a plain medium, a
#: source medium, a medium the administrator switched off, one whose
#: name carries a number, a mirrorlist entry, and a mirror that
#: publishes Mageia under a directory of its own numbering.
URPMI_CFG_SAMPLE = r"""{
}

Core\ Release http://mir.example.org/pub/Mageia/distrib/9/x86_64/media/core/release {
  key-ids: 80420f66
}

Core\ Sources http://mir.example.org/pub/Mageia/distrib/9/SRPMS/core/release {
  ignore
}

Nonfree\ 9\ Updates http://mir.example.org/pub/Mageia/distrib/9/x86_64/media/nonfree/updates {
  key-ids: 80420f66
  update
}

Tainted\ Release {
  mirrorlist: $MIRRORLIST
  with-dir: media/tainted/release
}

Local\ Extras http://build.example.org/mirrors/9/rpms/ {
  ignore
}
"""


@pytest.fixture
def cfg(tmp_path):
    """A urpmi.cfg on a mga9 machine."""
    path = tmp_path / "urpmi.cfg"
    path.write_text(URPMI_CFG_SAMPLE, encoding="utf-8")
    return path


class TestWhatTheRewriteMoves:
    """The release segment, and only where a release can be."""

    def test_binary_media_reach_the_new_release(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        text = cfg.read_text(encoding="utf-8")
        assert "distrib/10/x86_64/media/core/release" in text
        assert "distrib/9/x86_64" not in text

    def test_source_media_reach_the_new_release(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        assert "distrib/10/SRPMS/core/release" in cfg.read_text(
            encoding="utf-8")

    def test_a_medium_the_admin_switched_off_keeps_its_marker(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        text = cfg.read_text(encoding="utf-8")
        assert text.count("ignore") == URPMI_CFG_SAMPLE.count("ignore")

    def test_key_ids_and_update_flags_survive(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        text = cfg.read_text(encoding="utf-8")
        assert text.count("key-ids: 80420f66") == 2
        assert "\n  update\n" in text

    def test_a_mirrorlist_entry_is_left_alone(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        # urpmi resolves $MIRRORLIST through /etc/product.id, which the
        # distupgrade replaced.  Those entries follow on their own.
        assert "mirrorlist: $MIRRORLIST" in cfg.read_text(encoding="utf-8")

    def test_a_medium_name_carrying_the_number_is_not_renamed(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        assert r"Nonfree\ 9\ Updates" in cfg.read_text(encoding="utf-8")

    def test_a_mirrors_own_numbered_directory_is_not_renamed(self, cfg):
        sync_urpmi_config("9", "10", path=cfg)

        # /mirrors/9/rpms/ is that mirror's own layout, not a release:
        # nothing that follows it has the Mageia shape.
        assert "http://build.example.org/mirrors/9/rpms/" in cfg.read_text(
            encoding="utf-8")

    def test_community_prefixed_segments_move_too(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Core http://mir.example.org/linux/mga9/x86_64/media/core/release"
            " {\n}\n", encoding="utf-8")

        sync_urpmi_config("9", "10", path=path)

        assert "/mga10/x86_64/media/" in path.read_text(encoding="utf-8")

    def test_the_count_is_what_actually_moved(self, cfg):
        report = sync_urpmi_config("9", "10", path=cfg)

        assert report.rewritten == 3
        assert report.changed

    def test_a_freeze_target_keeps_its_identity(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Core http://mir.example.org/distrib/10/x86_64/media/core/release"
            " {\n}\n", encoding="utf-8")

        sync_urpmi_config("10", "cauldron", path=path)

        assert "/distrib/cauldron/x86_64/media/" in path.read_text(
            encoding="utf-8")


class TestTheMirrorListForm:
    """The release inside the mirror API filename.

    ``urpmi.addmedia --distrib`` leaves every official medium as a
    ``mirrorlist:`` holding a literal versioned URL, so this is the
    form an ordinary install is made of.  The path-segment rule cannot
    see it: the release sits inside a file name, not between two
    slashes.  It was missed on a real mga9 to mga10, where four
    URL-direct entries moved and forty-odd mirrorlist ones did not.
    """

    def test_the_api_filename_moves(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Core\\ Release  {\n"
            "  mirrorlist: http://mirrors.mageia.org/api/mageia.9.x86_64.list\n"
            "  with-dir: media/core/release\n}\n", encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)

        assert "mageia.10.x86_64.list" in path.read_text(encoding="utf-8")
        assert report.rewritten == 1

    def test_the_symbolic_form_is_left_alone(self, tmp_path):
        # ``$MIRRORLIST`` is the one value that really does follow the
        # installed release, through /etc/product.id.
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Auto  {\n  mirrorlist: $MIRRORLIST\n"
            "  with-dir: media/core/release\n}\n", encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)

        assert "$MIRRORLIST" in path.read_text(encoding="utf-8")
        assert report.unhandled == []

    def test_a_cauldron_identity_moves_too(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Core  {\n  mirrorlist: "
            "http://mirrors.mageia.org/api/mageia.10.x86_64.list\n}\n",
            encoding="utf-8")

        sync_urpmi_config("10", "cauldron", path=path)

        assert "mageia.cauldron.x86_64.list" in path.read_text(
            encoding="utf-8")

    def test_the_key_bounds_the_rule(self, tmp_path):
        # The same string under another key is not a mirror list and
        # must not be touched.
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Core  {\n  with-dir: "
            "http://mirrors.mageia.org/api/mageia.9.x86_64.list\n}\n",
            encoding="utf-8")

        sync_urpmi_config("9", "10", path=path)

        assert "mageia.9.x86_64.list" in path.read_text(encoding="utf-8")

    def test_with_dir_never_carries_a_release(self, tmp_path):
        # Measured on a real file, including the 32-bit form.
        path = tmp_path / "urpmi.cfg"
        body = ("Core\\ 32bit  {\n"
                "  mirrorlist: http://mirrors.mageia.org/api/mageia.9.x86_64.list\n"
                "  with-dir: media/../../i586/media/core/release\n}\n")
        path.write_text(body, encoding="utf-8")

        sync_urpmi_config("9", "10", path=path)

        assert "media/../../i586/media/core/release" in path.read_text(
            encoding="utf-8")


class TestAnUnknownFormIsNamed:
    """Strict when rewriting, wide when reporting."""

    def test_it_is_left_alone_and_reported(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Exotique https://exemple.org/mageia-9-rpms/x86_64/ {\n"
            "  key-ids: deadbeef\n}\n"
            "Core https://mir.org/distrib/9/x86_64/media/core/release {\n}\n",
            encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)

        assert "mageia-9-rpms" in path.read_text(encoding="utf-8")
        assert report.unhandled == ["Exotique"]

    def test_a_name_with_spaces_comes_back_readable(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Truc\\ Bidule\\ Release https://exemple.org/mageia-9/x86_64/ {\n}\n"
            "Core https://mir.org/distrib/9/x86_64/media/core/release {\n}\n",
            encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)

        assert report.unhandled == ["Truc Bidule Release"]

    def test_a_medium_we_did_move_is_never_reported(self, tmp_path):
        # The mirror publishes Mageia under its own numbered directory:
        # the leading 9 survives on purpose.  Reporting an entry we
        # handled would teach the operator to ignore the message.
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Miroir https://mir.org/mirrors/9/distrib/9/x86_64/media/core/"
            "release {\n}\n", encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)

        assert report.rewritten == 1
        assert report.unhandled == []

    def test_a_block_that_moved_something_is_not_reported(self, tmp_path):
        # Two URLs in one entry: the mirror list moves, the header URL
        # is in a shape no rule knows.  Reporting on « some URL here
        # is still stale » would name an entry that is now half right
        # and half wrong, which helps nobody; the operator is told
        # about entries where nothing at all could be done.
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Mixte https://exemple.org/mageia-9-rpms/x86_64/ {\n"
            "  mirrorlist: http://mirrors.mageia.org/api/mageia.9.x86_64.list\n"
            "}\n", encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)

        assert report.rewritten == 1
        assert report.unhandled == []

    def test_nothing_reported_when_every_entry_moved(self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(
            "Core https://mir.org/distrib/9/x86_64/media/core/release {\n}\n",
            encoding="utf-8")

        assert sync_urpmi_config("9", "10", path=path).unhandled == []


class TestTheRealConfigurationOfATestMachine:
    """The mga9 file that exposed the gap, kept as a fixture."""

    FIXTURE = Path(__file__).parent / "fixtures" / "urpmi.cfg.mga9"

    def test_every_known_form_moves_and_nothing_else_is_touched(
            self, tmp_path):
        path = tmp_path / "urpmi.cfg"
        path.write_text(self.FIXTURE.read_text(encoding="utf-8"),
                        encoding="utf-8")

        report = sync_urpmi_config("9", "10", path=path)
        after = path.read_text(encoding="utf-8")

        # Three URL-direct entries and three mirror lists.
        assert report.rewritten == 6
        # The one shape no rule knows, named rather than lost.
        assert report.unhandled == ["Exotique"]
        assert "$MIRRORLIST" in after
        assert "mageia-9-rpms" in after
        # Nothing of the operator's own choices disappeared.
        before = self.FIXTURE.read_text(encoding="utf-8")
        for marker in ("ignore", "update", "key-ids: c186ac23",
                       "with-dir: media/../../i586/media/core/release"):
            assert after.count(marker) == before.count(marker)


class TestTheOriginalIsKept:
    """A rewrite an operator can undo by hand."""

    def test_a_backup_is_written_before_the_rewrite(self, cfg):
        report = sync_urpmi_config("9", "10", path=cfg)

        assert report.backup is not None
        assert report.backup.read_text(encoding="utf-8") == URPMI_CFG_SAMPLE

    def test_no_backup_when_nothing_moves(self, cfg):
        report = sync_urpmi_config("11", "12", path=cfg)

        assert report.backup is None
        assert cfg.read_text(encoding="utf-8") == URPMI_CFG_SAMPLE


class TestNothingHereFailsAnOperation:
    """This runs after a committed upgrade and may only report."""

    def test_a_missing_configuration_is_not_an_error(self, tmp_path):
        report = sync_urpmi_config("9", "10", path=tmp_path / "absent.cfg")

        assert report.errors == []
        assert report.skipped_reason
        assert not report.changed

    def test_the_same_release_twice_does_nothing(self, cfg):
        report = sync_urpmi_config("9", "9", path=cfg)

        assert not report.changed
        assert cfg.read_text(encoding="utf-8") == URPMI_CFG_SAMPLE

    def test_an_empty_release_pair_does_nothing(self, cfg):
        assert not sync_urpmi_config("", "10", path=cfg).changed
        assert not sync_urpmi_config("9", "", path=cfg).changed
        assert cfg.read_text(encoding="utf-8") == URPMI_CFG_SAMPLE

    def test_an_unwritable_file_is_reported_not_raised(self, cfg,
                                                       monkeypatch):
        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(urpmi_config.Path, "write_text", refuse)

        report = sync_urpmi_config("9", "10", path=cfg)

        assert report.errors
        assert not report.changed


class TestIdentityOfAFreezeTarget:
    """``version_to`` in the state is what ``display()`` wrote."""

    def test_a_plain_identity_passes_through(self):
        assert identity_of("10") == "10"

    def test_a_freeze_pair_gives_back_the_identity(self):
        display = ReleaseIdentity("cauldron", "11").display()

        assert display == "cauldron:11"
        assert identity_of(display) == "cauldron"

    def test_an_empty_value_is_empty(self):
        assert identity_of("") == ""
        assert identity_of(None) == ""


# ---------------------------------------------------------------------------
# The ``on`` key
# ---------------------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path):
    vendor = tmp_path / "usr" / "lib" / "urpm" / "hooks.d"
    admin = tmp_path / "etc" / "urpm" / "hooks.d"
    vendor.mkdir(parents=True)
    admin.mkdir(parents=True)
    return vendor, admin


@pytest.fixture
def trusted(monkeypatch):
    """Vouch for every file, since tests cannot own files as root."""
    monkeypatch.setattr(hooks, "_refuse_untrusted", lambda path: None)


class TestRulesNarrowThemselves:
    """``on`` is a restriction a rule puts on itself."""

    def test_a_rule_without_on_matches_every_operation(self):
        hook = Hook("any", "urpmi")
        outcome = OperationOutcome(provides={"urpmi": frozenset()},
                                   operation=Operation.INSTALL)

        assert hook.matches(outcome)

    def test_a_distupgrade_rule_ignores_an_install(self):
        hook = Hook("sync", "urpmi", on=frozenset({Operation.DISTUPGRADE}))
        outcome = OperationOutcome(provides={"urpmi": frozenset()},
                                   operation=Operation.INSTALL)

        assert not hook.matches(outcome)

    def test_a_distupgrade_rule_fires_on_a_distupgrade(self):
        hook = Hook("sync", "urpmi", on=frozenset({Operation.DISTUPGRADE}))
        outcome = OperationOutcome(provides={"urpmi": frozenset()},
                                   operation=Operation.DISTUPGRADE)

        assert hook.matches(outcome)

    def test_a_caller_that_says_nothing_matches_every_rule(self):
        # An empty operation is « not distinguished », never « none of
        # them »: a caller not yet naming its operation must not
        # silently switch rules off.
        hook = Hook("sync", "urpmi", on=frozenset({Operation.DISTUPGRADE}))
        outcome = OperationOutcome(provides={"urpmi": frozenset()})

        assert hook.matches(outcome)

    def test_the_operation_travels_to_the_triggered_rule(self):
        hook = Hook("sync", "urpmi")
        outcome = OperationOutcome(provides={"urpmi": frozenset()},
                                   operation=Operation.DISTUPGRADE)

        assert hooks_for(outcome, [hook])[0].operation == Operation.DISTUPGRADE


class TestReadingTheOnKey:
    """Parsing, and what an unknown value costs."""

    def _write(self, directory, body):
        (directory / "50-rule.cfg").write_text(
            "[on-completion:sync]\nwatch = urpmi\n" + body, encoding="utf-8")

    def test_a_single_operation_is_read(self, dirs, trusted):
        vendor, admin = dirs
        self._write(vendor, "on = distupgrade\n")

        assert load_hooks(vendor, admin).hooks[0].on == frozenset(
            {Operation.DISTUPGRADE})

    def test_several_operations_separate_on_spaces_or_commas(self, dirs,
                                                             trusted):
        vendor, admin = dirs
        self._write(vendor, "on = install, upgrade distupgrade\n")

        assert load_hooks(vendor, admin).hooks[0].on == frozenset(
            {Operation.INSTALL, Operation.UPGRADE, Operation.DISTUPGRADE})

    def test_an_unknown_operation_drops_the_rule(self, dirs, trusted):
        vendor, admin = dirs
        self._write(vendor, "on = dist-upgrade\n")

        report = load_hooks(vendor, admin)

        # Widening to « every operation », which is what an empty ``on``
        # means, is the one outcome a typo must not produce.
        assert report.hooks == []
        assert report.rejected
        assert "dist-upgrade" in report.rejected[0][1]


# ---------------------------------------------------------------------------
# Restarts during a release change
# ---------------------------------------------------------------------------


@pytest.fixture
def ops(monkeypatch):
    """Operations over a stand-in database."""
    instance = PackageOperations.__new__(PackageOperations)
    instance.db = SimpleNamespace()
    instance.audit = None
    return instance


class TestRestartsAreDeclinedDuringADistupgrade:
    """The running session is still on the previous release."""

    def test_the_service_is_never_asked(self, ops, monkeypatch):
        asked = []
        monkeypatch.setattr(PackageOperations, "_restart_for_hook",
                            staticmethod(lambda hook, service:
                                         asked.append(service)))
        entry = hooks.TriggeredHook(
            Hook("restart", "restart-on-completion",
                 action=Action.RESTART_SERVICE),
            frozenset({"dbus"}), Operation.DISTUPGRADE)

        results = ops.run_hooks([entry])

        assert asked == []
        assert [o.result for _entry, outcomes in results
                for o in outcomes] == [Result.DECLINED]

    def test_the_same_rule_acts_on_a_plain_upgrade(self, ops, monkeypatch):
        asked = []

        def restart(hook, service):
            asked.append(service)
            return init_system.ServiceOutcome(service, Result.RESTARTED)

        monkeypatch.setattr(PackageOperations, "_restart_for_hook",
                            staticmethod(restart))
        entry = hooks.TriggeredHook(
            Hook("restart", "restart-on-completion",
                 action=Action.RESTART_SERVICE),
            frozenset({"dbus"}), Operation.UPGRADE)

        ops.run_hooks([entry])

        assert asked == ["dbus"]

    def test_the_declined_service_is_named_to_the_operator(self):
        from urpm.cli.helpers.hook_report import _restart_line

        line = _restart_line(
            init_system.ServiceOutcome("dbus", Result.DECLINED))

        # Not asserting on the wording, which is translated: what must
        # hold is that the service is named and that the line differs
        # from a plain failure the operator would go and investigate.
        assert "dbus" in line
        assert line != _restart_line(
            init_system.ServiceOutcome("dbus", Result.FAILED))

    def test_declined_is_not_one_of_the_acting_results(self):
        assert Result.DECLINED not in (Result.RESTARTED, Result.NOT_RUNNING,
                                       Result.FAILED, Result.NO_SERVICE_TOOL)


class TestTheSyncVerbIsDispatched:
    """``sync-urpmi-config`` reaches the module that does the work."""

    def test_the_action_is_part_of_the_vocabulary(self):
        assert Action.SYNC_URPMI_CONFIG in Action.ALL

    def test_run_hooks_returns_a_sync_report(self, ops, monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.state.read_state",
            lambda db: {"version_from": "9", "version_to": "10"})
        seen = {}

        def fake_sync(source, target, path=None):
            seen["pair"] = (source, target)
            return SyncReport(path=Path("/etc/urpmi/urpmi.cfg"),
                              source_release=source, target_release=target,
                              rewritten=3)

        monkeypatch.setattr("urpm.core.urpmi_config.sync_urpmi_config",
                            fake_sync)
        entry = hooks.TriggeredHook(
            Hook("urpmi-config", "urpmi",
                 action=Action.SYNC_URPMI_CONFIG),
            frozenset(), Operation.DISTUPGRADE)

        [(_entry, outcomes)] = ops.run_hooks([entry])

        assert seen["pair"] == ("9", "10")
        assert outcomes[0].rewritten == 3

    def test_a_freeze_pair_is_reduced_to_identities(self, ops, monkeypatch):
        monkeypatch.setattr(
            "urpm.core.distupgrade.state.read_state",
            lambda db: {"version_from": "10", "version_to": "cauldron:11"})
        seen = {}

        def fake_sync(source, target, path=None):
            seen["pair"] = (source, target)
            return SyncReport(path=Path("/etc/urpmi/urpmi.cfg"))

        monkeypatch.setattr("urpm.core.urpmi_config.sync_urpmi_config",
                            fake_sync)
        entry = hooks.TriggeredHook(
            Hook("urpmi-config", "urpmi",
                 action=Action.SYNC_URPMI_CONFIG),
            frozenset(), Operation.DISTUPGRADE)

        ops.run_hooks([entry])

        assert seen["pair"] == ("10", "cauldron")

    def test_no_state_means_no_release_change_to_follow(self, ops,
                                                        monkeypatch):
        monkeypatch.setattr("urpm.core.distupgrade.state.read_state",
                            lambda db: None)
        entry = hooks.TriggeredHook(
            Hook("urpmi-config", "urpmi",
                 action=Action.SYNC_URPMI_CONFIG),
            frozenset(), Operation.DISTUPGRADE)

        [(_entry, outcomes)] = ops.run_hooks([entry])

        assert not outcomes[0].changed
        assert outcomes[0].errors == []

    def test_an_action_that_raises_is_reported_not_propagated(self, ops,
                                                              monkeypatch):
        def explode(db):
            raise RuntimeError("state unreadable")

        monkeypatch.setattr("urpm.core.distupgrade.state.read_state", explode)
        entry = hooks.TriggeredHook(
            Hook("urpmi-config", "urpmi",
                 action=Action.SYNC_URPMI_CONFIG),
            frozenset(), Operation.DISTUPGRADE)

        [(_entry, outcomes)] = ops.run_hooks([entry])

        assert outcomes[0].errors


class TestTheShippedRule:
    """What the package drops in ``/usr/lib/urpm/hooks.d``."""

    RULE = REPO / "data/usr/lib/urpm/hooks.d/60-urpmi-config.cfg"

    def test_the_file_is_shipped(self):
        assert self.RULE.is_file()

    def test_it_loads_and_asks_for_the_sync(self, tmp_path, trusted):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / self.RULE.name).write_text(
            self.RULE.read_text(encoding="utf-8"), encoding="utf-8")

        report = load_hooks(vendor, tmp_path / "absent")

        assert report.rejected == []
        [hook] = report.hooks
        assert hook.watch == "urpmi"
        assert hook.action == Action.SYNC_URPMI_CONFIG
        assert hook.on == frozenset({Operation.DISTUPGRADE})

    def test_every_shipped_rule_is_installed_by_the_spec(self):
        spec = (REPO / "rpmbuild/SPECS/urpm-ng.spec").read_text(
            encoding="utf-8")
        shipped = sorted(p.name for p in self.RULE.parent.glob("*.cfg"))

        # Either the spec names each file, or it installs the directory
        # in one pass.  What must not happen is a rule that ships in the
        # tree and never reaches the package, which is silent: %files
        # globs the directory, so the build would not complain.
        installed_wholesale = "data/usr/lib/urpm/hooks.d/*.cfg" in spec
        assert installed_wholesale or all(
            f"hooks.d/{name}" in spec for name in shipped)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _calls_named(tree, name):
    """Every ``Call`` node whose callee ends in ``name``."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            found.append(node)
        elif isinstance(func, ast.Name) and func.id == name:
            found.append(node)
    return found


def _sources(*relative):
    return {rel: ast.parse((REPO / rel).read_text(encoding="utf-8"))
            for rel in relative}


class TestEveryPathNamesItsOperation:
    """A rule cannot narrow itself if nobody says what happened.

    These assert on wiring rather than on behaviour, deliberately: the
    defect they guard against is a call site that keeps working and
    silently matches every rule.  No output changes, so no behaviour
    test fails.
    """

    def test_the_install_pipeline_passes_an_operation(self):
        tree = ast.parse(
            (REPO / "urpm/cli/commands/_install_pipeline.py").read_text(
                encoding="utf-8"))

        calls = _calls_named(tree, "run_post_operation_hooks")

        assert calls, "the install pipeline no longer evaluates rules"
        for call in calls:
            assert len(call.args) >= 3 or any(
                kw.arg == "operation" for kw in call.keywords), (
                "run_post_operation_hooks called without an operation")

    def test_no_caller_anywhere_omits_the_operation(self):
        # Both entry points, the CLI helper and the decision method it
        # forwards to: the D-Bus service calls the second one directly,
        # and checking only the first is how that path was missed.
        wanted = {"run_post_operation_hooks": 3,
                  "hooks_triggered_by": 2,
                  "_hook_advice": 2}
        offenders = []
        for path in (REPO / "urpm").rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, arity in wanted.items():
                for call in _calls_named(tree, name):
                    if len(call.args) < arity and not any(
                            kw.arg == "operation" for kw in call.keywords):
                        offenders.append(
                            f"{path.relative_to(REPO)}:{call.lineno} {name}")

        assert offenders == [], (
            "these evaluate rules without naming the operation: "
            + ", ".join(offenders))

    def test_the_dbus_service_tells_install_from_upgrade(self):
        tree = ast.parse((REPO / "urpm/dbus/service.py").read_text(
            encoding="utf-8"))

        named = set()
        for call in _calls_named(tree, "_hook_advice"):
            named.update(ast.unparse(arg) for arg in call.args)

        assert "Operation.INSTALL" in named
        assert "Operation.UPGRADE" in named

    def test_stage_four_evaluates_rules_as_a_distupgrade(self):
        tree = ast.parse(
            (REPO / "urpm/core/distupgrade/stages_4_5.py").read_text(
                encoding="utf-8"))

        calls = _calls_named(tree, "hooks_triggered_by")

        assert calls, "stage 4 no longer evaluates the post-operation rules"
        for call in calls:
            named = [ast.unparse(a) for a in call.args] + [
                ast.unparse(kw.value) for kw in call.keywords]
            assert any("DISTUPGRADE" in value for value in named)

    def test_stage_four_runs_them_before_the_state_goes(self):
        source = (REPO / "urpm/core/distupgrade/stages_4_5.py").read_text(
            encoding="utf-8")

        # The sync action reads the release pair from the distupgrade
        # state, so the order is load-bearing, not cosmetic.
        assert source.index("hooks_triggered_by") < source.index(
            "delete_state(db)")

    def test_the_distupgrade_report_prints_what_the_rules_did(self):
        source = (REPO / "urpm/cli/commands/distupgrade.py").read_text(
            encoding="utf-8")

        # Once on the nominal path, once on the Stage 4 retry: a rule
        # that fired with nobody reading it is the same as no rule.
        assert source.count('"hook_lines"') == 2
