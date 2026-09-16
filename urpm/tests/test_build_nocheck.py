"""``urpm build --nocheck`` : skip the spec's ``%check`` section.

rpm has no notion of an individual test, so this is all or nothing.
What matters, and what these tests pin down, is *where* the flag
lands: on the final ``rpmbuild -ba`` and nowhere else.

The build makes several rpmbuild passes.  ``rpmbuild -br`` runs
``%prep`` plus ``%generate_buildrequires`` and stops there, so it never
reaches ``%check`` — passing ``--nocheck`` to it would be noise in the
command line and in the log, suggesting a decision that pass cannot
act on.

Both build paths are covered.  ``urpm build`` chains specs in one
shared container by default, and gives each spec its own container
under ``--parallel``; a flag wired into only one of them is the kind
of gap nobody notices until a build behaves differently depending on
an unrelated option.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from urpm.cli.helpers.build_chain import _build_one_spec_in_container


class _FakeContainer:
    """Records every command it is asked to run."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def _record(self, cmd):
        self.calls.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    def exec(self, cid, cmd, **kw):
        return self._record(cmd)

    def exec_stream(self, cid, cmd, **kw):
        self._record(cmd)
        return 0

    def cp(self, *a, **kw):
        return True


def _rpmbuild_calls(container: _FakeContainer) -> list[str]:
    """The ``bash -c '<cmd>'`` strings that invoke rpmbuild."""
    return [call[2] for call in container.calls
            if len(call) >= 3 and call[0] == "bash" and call[1] == "-c"
            and "rpmbuild" in call[2]]


def _chain_calls(tmp_path: Path, nocheck: bool) -> list[str]:
    container = _FakeContainer()
    spec = tmp_path / "foo.spec"
    spec.write_text("Name: foo\nVersion: 1\nRelease: 1\n")
    _build_one_spec_in_container(
        container=container,
        cid="fakecid",
        source_path=spec,
        output_dir=tmp_path / "out",
        _find_workspace_fn=lambda p: (tmp_path, tmp_path, True),
        _diagnose_fn=lambda *a, **kw: None,
        limits=None,
        bcond_args="",
        net_wrap="",
        nocheck=nocheck,
    )
    return _rpmbuild_calls(container)


def _of_pass(calls: list[str], flag: str) -> list[str]:
    """The rpmbuild commands of one pass, ``-ba`` or ``-br``."""
    return [cmd for cmd in calls if f" {flag} " in cmd]


class TestNocheckInSharedChain:
    """Default path: every spec built in one shared container."""

    def test_flag_reaches_the_build_pass(self, tmp_path):
        built = _of_pass(_chain_calls(tmp_path, nocheck=True), "-ba")
        assert built, "no rpmbuild -ba call captured — fixture broken"
        for cmd in built:
            assert "--nocheck" in cmd, cmd

    def test_flag_stays_off_the_buildrequires_pass(self, tmp_path):
        """``-br`` stops before %build, so %check is out of its reach."""
        probed = _of_pass(_chain_calls(tmp_path, nocheck=True), "-br")
        assert probed, "no rpmbuild -br call captured — fixture broken"
        for cmd in probed:
            assert "--nocheck" not in cmd, cmd

    def test_absent_by_default(self, tmp_path):
        """Not asked for, not passed: the %check runs."""
        for cmd in _chain_calls(tmp_path, nocheck=False):
            assert "--nocheck" not in cmd, cmd

    def test_flag_precedes_the_build_mode(self, tmp_path):
        """rpmbuild reads options before the mode selector ; a flag
        landing after ``-ba`` would be taken for a source path."""
        for cmd in _of_pass(_chain_calls(tmp_path, nocheck=True), "-ba"):
            assert cmd.index("--nocheck") < cmd.index(" -ba "), cmd


class TestNocheckInSinglePackagePath:
    """``--parallel`` path: one fresh container per spec.

    Driven through the CLI signature rather than the internals, since
    that function starts a container of its own.
    """

    def test_signature_accepts_the_flag(self):
        """A parameter the caller passes but the callee ignores would
        silently run the tests under ``--parallel``."""
        import inspect

        from urpm.cli.commands.build import _build_single_package

        signature = inspect.signature(_build_single_package)
        assert "nocheck" in signature.parameters
        assert signature.parameters["nocheck"].default is False

    def test_source_applies_it_to_the_build_pass_only(self):
        """Read the call sites: the flag is interpolated into the
        ``-ba`` command and absent from the ``-br`` one."""
        import inspect

        from urpm.cli.commands import build

        source = inspect.getsource(build._build_single_package)
        ba_lines = [line for line in source.splitlines()
                    if "-ba {spec_path}" in line]
        br_lines = [line for line in source.splitlines()
                    if "-br {spec_path}" in line]
        assert ba_lines and br_lines, "call sites not found"
        assert all("{nocheck_arg}" in line for line in ba_lines), ba_lines
        assert all("{nocheck_arg}" not in line for line in br_lines), br_lines
