"""The path an imported medium ends up with, on a cauldron.

This is the test that was missing while the bug survived four rounds of
fixes.  Each round checked a function in isolation and each one passed,
because the faulty site was always the next one down the chain.  What
nobody exercised was the chain itself: parse ``urpmi.cfg``, resolve an
identity, build a relative path.

The reported failure, on a fresh cauldron install::

    [x]  Core Release (distrib1)  11/x86_64/media/core/release  (no server)

``11/`` exists on no mirror — the development branch is served as
``cauldron/`` until release day.  The server probe HEADs
``<mirror>/<relative_path>/media_info/MD5SUM`` for every medium, so all
forty-five answered 404 and not a single server link was created.  The
symptom read « no server » while the servers were fine and the path was
wrong.
"""

from unittest.mock import patch

import pytest

from urpm.cli.commands.media import parse_urpmi_cfg
from urpm.core import config as core_config
from urpm.core.media_pipeline import _build_relative_path, _resolve_version

#: A mirrorlist-based urpmi.cfg, in the shape urpmi really writes:
#: escaped spaces in the name, no URL, a ``with-dir``.
URPMI_CFG = """{
}

Core\\ Release\\ (distrib1) {
  mirrorlist: $MIRRORLIST
  with-dir: media/core/release
}

Core\\ Updates\\ (distrib3) {
  mirrorlist: $MIRRORLIST
  update
  with-dir: media/core/updates
}

Tainted\\ Release\\ (distrib21) {
  mirrorlist: $MIRRORLIST
  ignore
  with-dir: media/tainted/release
}
"""


@pytest.fixture
def urpmi_cfg(tmp_path):
    path = tmp_path / "urpmi.cfg"
    path.write_text(URPMI_CFG, encoding="utf-8")
    return str(path)


def _system(tmp_path, name, version_line, version_id):
    """A system root: /etc/version plus /etc/os-release."""
    etc = tmp_path / name / "etc"
    etc.mkdir(parents=True)
    (etc / "version").write_text(version_line + "\n", encoding="utf-8")
    (etc / "os-release").write_text(f"VERSION_ID={version_id}\n",
                                    encoding="utf-8")
    return str(tmp_path / name)


def _imported_paths(cfg_path, system_root):
    """Replay what ``urpm media import`` does, from file to path.

    Mirrors ``cmd_media_import``: the identity becomes the hint, and the
    pipeline turns it into a relative path.  Mirrorlist entries carry no
    URL, hence the empty string.
    """
    real = core_config.get_system_identity
    with patch.object(core_config, "get_system_identity",
                      lambda root=None: real(system_root)):
        hint_version = core_config.get_system_identity()
        paths = {}
        for medium in parse_urpmi_cfg(cfg_path):
            section = medium.get("with_dir") or medium.get("relative_path")
            hint = {"version": hint_version, "arch": "x86_64"}
            version = _resolve_version(None, None, hint, "")
            paths[medium["name"]] = _build_relative_path(
                version, "x86_64", section, hint)
        return paths


def test_a_cauldron_imports_media_under_cauldron(tmp_path, urpmi_cfg):
    """The reported failure, end to end."""
    root = _system(tmp_path, "cauldron", "11 0.0.5 cauldron", "11")

    paths = _imported_paths(urpmi_cfg, root)

    assert paths["Core Release (distrib1)"] == \
        "cauldron/x86_64/media/core/release"
    assert paths["Core Updates (distrib3)"] == \
        "cauldron/x86_64/media/core/updates"


def test_no_imported_path_carries_the_future_number(tmp_path, urpmi_cfg):
    """`11/` is the exact string that made every probe 404."""
    root = _system(tmp_path, "cauldron", "11 0.0.5 cauldron", "11")

    paths = _imported_paths(urpmi_cfg, root)

    offenders = [name for name, path in paths.items()
                 if path.startswith("11/")]
    assert not offenders, f"still addressing a tree no mirror serves: {offenders}"


def test_a_disabled_medium_gets_the_same_treatment(tmp_path, urpmi_cfg):
    """It is probed too, so a later `media enable` finds it linked."""
    root = _system(tmp_path, "cauldron", "11 0.0.5 cauldron", "11")

    paths = _imported_paths(urpmi_cfg, root)

    assert paths["Tainted Release (distrib21)"] == \
        "cauldron/x86_64/media/tainted/release"


def test_a_released_system_still_imports_under_its_number(tmp_path,
                                                          urpmi_cfg):
    """Guard against overcorrecting: a release is served as `<n>/`."""
    root = _system(tmp_path, "released", "10 4 official", "10")

    paths = _imported_paths(urpmi_cfg, root)

    assert paths["Core Release (distrib1)"] == "10/x86_64/media/core/release"


def test_the_probe_url_the_linker_would_build(tmp_path, urpmi_cfg):
    """Spell out the URL, since that is what actually 404ed."""
    root = _system(tmp_path, "cauldron", "11 0.0.5 cauldron", "11")

    path = _imported_paths(urpmi_cfg, root)["Core Release (distrib1)"]
    probe = f"https://mirror.example/distrib/{path}/media_info/MD5SUM"

    assert probe == ("https://mirror.example/distrib/cauldron/x86_64"
                     "/media/core/release/media_info/MD5SUM")
