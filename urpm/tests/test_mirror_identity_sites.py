"""Every site that addresses a mirror must use the identity, not the number.

A fresh cauldron install came back with five media, all reading
``11/x86_64/media/…`` and all listed « no server ».  The first fix went
to ``cmd_init``'s fallback, and changed nothing: that is not the code
path an install takes.

The actual cause was ``autoconfig_servers``, which asked the mirror API
for ``mageia.11.x86_64.list``.  That list does not exist — the API
serves the development branch as ``mageia.cauldron.x86_64.list`` — so no
mirror came back, no server was recorded, and no server could then be
attached to any medium.

Hence this file, and its shape: rather than testing one call site, it
walks the tree and asserts that *no* site reads ``VERSION_ID`` to reach
a mirror.  One fix at a time is what let this ship.
"""

import ast
from pathlib import Path

import pytest

from urpm.core.config import get_system_identity


def _root():
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def cauldron(tmp_path):
    """A system root that announces 11 but lives on the cauldron branch."""
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "os-release").write_text('VERSION_ID=11\n', encoding="utf-8")
    (etc / "version").write_text("11 0.0.5 cauldron\n", encoding="utf-8")
    return str(tmp_path)


class TestTheMirrorApiIsAskedForTheRightList:
    """What the API is actually asked for, end to end."""

    def test_a_cauldron_asks_for_the_cauldron_list(self, cauldron):
        from urpm.core.mirrorlist import _API_URL

        url = _API_URL.format(release=get_system_identity(cauldron),
                              arch="x86_64")

        assert url.endswith("mageia.cauldron.x86_64.list")

    def test_a_release_still_asks_for_its_number(self, tmp_path):
        from urpm.core.mirrorlist import _API_URL

        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "os-release").write_text('VERSION_ID=10\n', encoding="utf-8")
        (etc / "version").write_text("10 4 official\n", encoding="utf-8")

        url = _API_URL.format(release=get_system_identity(str(tmp_path)),
                              arch="x86_64")

        assert url.endswith("mageia.10.x86_64.list")


class TestServerPoolDetection:
    """server_pool feeds both the pool expansion and the country backfill."""

    def test_it_goes_through_the_identity(self, monkeypatch):
        """Pinned on the seam rather than on a value.

        The suite runs on releases and cauldrons alike, so asserting a
        literal would only test the machine it runs on.
        """
        from urpm.core import server_pool

        monkeypatch.setattr("urpm.core.config.get_system_identity",
                            lambda root=None: "cauldron")

        assert server_pool._detect_version() == "cauldron"

    def test_an_undetectable_system_yields_an_empty_string(self, monkeypatch):
        """Its caller reads the empty string as "skip the network"."""
        from urpm.core import server_pool

        monkeypatch.setattr("urpm.core.config.get_system_identity",
                            lambda root=None: None)

        assert server_pool._detect_version() == ""


class TestNoSiteReadsVersionIdToReachAMirror:
    """The guard that would have caught the miss.

    ``VERSION_ID`` is legitimate for describing the running system —
    appstream metadata, a seeded stub in a chroot.  It is never right
    for building a mirror URL, and the two are easy to confuse, which
    is exactly what happened.
    """

    #: Modules allowed to read VERSION_ID, with why.
    _ALLOWED = {
        # The one place that parses it, and exposes it as an identity.
        "core/config.py",
        # Writes a stub os-release into a chroot; not a mirror lookup.
        "cli/commands/media.py",
        # Reads it back out of a built container to check what landed.
        "core/image_urpm_ng.py",
    }

    @staticmethod
    def _docstrings(tree):
        """The string constants that document rather than compute."""
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    found.add(doc)
        return found

    def test_version_id_is_read_in_known_places_only(self):
        offenders = []
        for path in sorted(_root().rglob("*.py")):
            if "tests" in path.parts:
                continue
            relative = str(path.relative_to(_root()))
            if relative in self._ALLOWED:
                continue

            tree = ast.parse(path.read_text(encoding="utf-8"))
            documentation = self._docstrings(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if not isinstance(node.value, str):
                    continue
                if "VERSION_ID" not in node.value:
                    continue
                if node.value in documentation:
                    continue  # prose, not a lookup
                offenders.append(f"{relative}:{node.lineno}: {node.value!r}")

        assert not offenders, (
            "VERSION_ID read outside the places that may:\n  "
            + "\n  ".join(offenders)
            + "\n\nTo reach a mirror use get_system_identity(); a cauldron "
              "announces the version it is becoming, not the one served."
        )

    def test_the_guard_is_looking_at_a_real_tree(self):
        """Guard the guard: an empty walk would pass silently."""
        scanned = [p for p in _root().rglob("*.py") if "tests" not in p.parts]

        assert len(scanned) > 50
