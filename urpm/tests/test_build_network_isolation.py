"""``urpm build`` : ``rpmbuild`` is wrapped in ``unshare -n`` by
default, opt-out via ``--with-network``.

Purpose : the spec's ``%prep`` / ``%build`` / ``%install`` /
``%check`` runs in a network-less namespace so a stray ``curl`` or
``pip install`` cannot sneak unaudited content into the final RPM.

The tests here don't spin up a container ; they intercept every
``container.exec_stream`` / ``container.exec`` call, assert the
issued bash command contains ``unshare -n rpmbuild`` (isolated,
default) or ``rpmbuild`` unwrapped (``--with-network``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from urpm.cli.helpers.build_chain import _build_one_spec_in_container


class _FakeContainer:
    """Records every command it's asked to run so tests can inspect them."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def _rec(self, cmd, **kw):
        self.calls.append(list(cmd))
        rv = MagicMock()
        rv.returncode = 0
        rv.stdout = ""
        rv.stderr = ""
        return rv

    def exec(self, cid, cmd, **kw):
        return self._rec(cmd, **kw)

    def exec_stream(self, cid, cmd, **kw):
        self._rec(cmd, **kw)
        return 0

    def cp(self, *a, **kw):
        return True


def _rpmbuild_bash_calls(container: _FakeContainer) -> list[str]:
    """Extract the ``bash -c '<cmd>'`` strings that invoke rpmbuild.

    Every ``rpmbuild`` invocation in the build chain goes through
    ``bash -c 'set -o pipefail; …rpmbuild …'`` — grab those.
    """
    out = []
    for call in container.calls:
        if len(call) >= 3 and call[0] == "bash" and call[1] == "-c":
            if "rpmbuild" in call[2]:
                out.append(call[2])
    return out


def _run_chain_with(with_network: bool, tmp_path: Path) -> list[str]:
    """Drive ``_build_one_spec_in_container`` end-to-end against the
    fake container ; return the rpmbuild-bearing bash strings."""
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
        with_network=with_network,
    )
    return _rpmbuild_bash_calls(container)


class TestBuildNetworkIsolation:

    def test_default_wraps_rpmbuild_in_unshare_n(self, tmp_path):
        """Default (network off) : every rpmbuild command must be
        prefixed by ``unshare -n``."""
        calls = _run_chain_with(with_network=False, tmp_path=tmp_path)
        assert calls, "no rpmbuild call captured — test fixture broken"
        for cmd in calls:
            assert "unshare -n rpmbuild" in cmd, (
                f"rpmbuild not net-isolated : {cmd}")

    def test_with_network_leaves_rpmbuild_bare(self, tmp_path):
        """``--with-network`` opts out : rpmbuild runs with the
        container's ambient network (no ``unshare -n`` prefix)."""
        calls = _run_chain_with(with_network=True, tmp_path=tmp_path)
        assert calls, "no rpmbuild call captured — test fixture broken"
        for cmd in calls:
            assert "unshare" not in cmd, (
                f"rpmbuild unexpectedly net-isolated : {cmd}")
            assert "rpmbuild" in cmd

    def test_both_br_and_ba_get_wrapped(self, tmp_path):
        """Both the dynamic-BuildRequires probe (``rpmbuild -br``)
        and the actual build (``rpmbuild -ba``) must be isolated —
        %generate_buildrequires runs a spec-provided script and is
        just as untrusted as %build."""
        calls = _run_chain_with(with_network=False, tmp_path=tmp_path)
        seen_br = any(" -br " in cmd for cmd in calls)
        seen_ba = any(" -ba " in cmd for cmd in calls)
        assert seen_br, "no rpmbuild -br call captured"
        assert seen_ba, "no rpmbuild -ba call captured"
        for cmd in calls:
            assert "unshare -n" in cmd, cmd
