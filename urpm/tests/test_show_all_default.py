"""``[display] show_all`` makes the full output the default.

Three properties, and the second is the one that broke in review.

The key is read when the operator says nothing, and ignored when they
say something, either way round: that is what makes ``--no-show-all``
a way back rather than a second switch.

Every command asks the same question of the same place.  Before this,
seven modules read ``args.show_all`` directly, which answers ``None``
once the flag is a tri-state, so a config key wired only into
``display.init`` would have been honoured by the plan printers and
ignored by ``history``, ``find``, ``download`` and the skipped-job
reports.  The test that guards it is mechanical: no module outside
``main`` may read that attribute.

And the verb writes somewhere the loader actually reads back.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from urpm.cli.main import _resolve_show_all, create_parser
from urpm.core import settings as settings_module

SOURCE = Path(__file__).resolve().parents[1]


# ── The tri-state ───────────────────────────────────────────────────────


def _args(argv):
    return create_parser().parse_args(argv)


def test_the_flag_is_three_valued_not_two():
    """Absent has to be distinguishable from refused."""
    assert _args(["search", "foo"]).show_all is None
    assert _args(["search", "--show-all", "foo"]).show_all is True
    assert _args(["search", "--no-show-all", "foo"]).show_all is False


def test_the_short_alias_survives_the_pair():
    """``peer clean -a`` predates the config key and still means on."""
    assert _args(["peer", "clean", "host", "-a"]).show_all is True
    assert _args(["peer", "clean", "host", "--no-show-all"]).show_all is False


@pytest.mark.parametrize("key,flag,expected", [
    (False, None, False),
    (False, True, True),
    (False, False, False),
    (True, None, True),
    (True, True, True),
    # The case the whole feature exists for: the key is on and the
    # operator wants one truncated run.
    (True, False, False),
])
def test_the_operator_outranks_the_file(monkeypatch, key, flag, expected):
    settings = settings_module.Settings()
    settings.display.show_all = key
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)

    assert _resolve_show_all(argparse.Namespace(show_all=flag)) is expected


def test_a_broken_configuration_truncates_rather_than_crashes(monkeypatch):
    """An unreadable config must not take urpm down with it."""
    def explode():
        raise OSError("no")

    monkeypatch.setattr(settings_module, "get_settings", explode)

    assert _resolve_show_all(argparse.Namespace(show_all=None)) is False


# ── One question, one place ─────────────────────────────────────────────


def test_no_command_reads_the_flag_behind_the_resolver():
    """``args.show_all`` is ``main``'s business and nobody else's.

    A module that reads it directly sees ``None`` when the flag was not
    passed, treats that as false, and silently loses the config key.
    """
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path.parent.name == "tests" or path.name == "main.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"args\.show_all|args,\s*['\"]show_all['\"]", line):
                offenders.append(f"{path.relative_to(SOURCE)}:{number}")

    assert not offenders, (
        "these read args.show_all instead of display.get_show_all(): "
        + ", ".join(offenders))


# ── The verb and the loader agree ───────────────────────────────────────


def test_what_the_verb_writes_is_what_the_loader_reads(tmp_path):
    written = settings_module.set_setting(
        "display", "show_all", True, config_dir=tmp_path)

    assert written.name == settings_module.MANAGED_DROP_IN
    assert written.parent.name == "conf.d"
    assert settings_module.load_settings(tmp_path).display.show_all is True


def test_the_drop_in_wins_over_every_shipped_default(tmp_path):
    """A shipped file must not shadow what a command was told to set."""
    confdir = tmp_path / "conf.d"
    confdir.mkdir()
    (confdir / "20-daemon.cfg").write_text("[display]\nshow_all = true\n")
    settings_module.set_setting(
        "display", "show_all", False, config_dir=tmp_path)

    assert settings_module.load_settings(tmp_path).display.show_all is False


def test_two_verbs_do_not_undo_each_other(tmp_path):
    settings_module.set_setting("display", "show_all", True,
                                config_dir=tmp_path)
    settings_module.set_setting("server", "auto_add", False,
                                config_dir=tmp_path)

    loaded = settings_module.load_settings(tmp_path)
    assert loaded.display.show_all is True
    assert loaded.server.auto_add is False


def test_the_verb_is_offered_with_the_same_vocabulary_as_its_siblings():
    """``yes|no|true|false|on|off``, like the auto-upgrade verbs."""
    parsed = _args(["config", "show-all", "on"])

    assert parsed.config_cmd == "show-all"
    assert parsed.show_all_value == "on"
    assert _args(["config", "show-all"]).show_all_value is None


def test_the_effective_configuration_names_the_key():
    """``urpm config show`` has to list it, or nobody will find it."""
    settings = settings_module.Settings()
    settings.display.show_all = True

    assert "show_all = true" in settings_module.format_settings(settings)
