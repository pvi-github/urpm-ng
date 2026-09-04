"""``urpm build`` : ``rpmbuild`` runs network-isolated by default,
opt-out via ``--with-network``.

Purpose : the spec's ``%prep`` / ``%build`` / ``%install`` /
``%check`` runs in a network-less namespace so a stray ``curl`` or
``pip install`` cannot sneak unaudited content into the final RPM.

Two layers, and the split matters — the first shipped alone once and
that is exactly how a broken wrapper reached users :

* :class:`TestBuildNetworkIsolation` drives the chain against a fake
  container and asserts the *shape* of the issued command.  Fast, no
  podman needed, but it proves only that we emit what we meant to.
* :class:`TestWrapperActuallyWorks` runs the wrapper for real inside
  the build image and asserts it does what it claims.  The earlier
  wrapper was a plain ``unshare -n``, which passed every shape test
  and then died with « unshare failed: Operation not permitted »
  because a rootless podman container has no CAP_SYS_ADMIN.  Shape
  tests cannot catch that class of bug ; only execution can.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from urpm.cli.helpers.build_chain import (
    NET_ISOLATION_WRAP,
    _build_one_spec_in_container,
)


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

    def test_default_wraps_rpmbuild(self, tmp_path):
        """Default (network off) : every rpmbuild command must carry
        the isolation prefix.  Asserted against the constant rather
        than a literal, so changing the wrapper cannot leave the test
        passing against a stale string."""
        calls = _run_chain_with(with_network=False, tmp_path=tmp_path)
        assert calls, "no rpmbuild call captured — test fixture broken"
        for cmd in calls:
            assert f"{NET_ISOLATION_WRAP}rpmbuild" in cmd, (
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
            assert NET_ISOLATION_WRAP.strip() in cmd, cmd


# ── Execution layer : does the wrapper actually work? ──────────────


def _build_image_available() -> str | None:
    """Name of a usable build image, or None to skip.

    Prefers ``mageia:10-build`` (what ``urpm build`` uses by default) ;
    any locally-present tag would do, but pinning keeps the skip
    message actionable.
    """
    if not shutil.which("podman"):
        return None
    probe = subprocess.run(
        ["podman", "image", "exists", "mageia:10-build"],
        capture_output=True, timeout=30,
    )
    return "mageia:10-build" if probe.returncode == 0 else None


_IMAGE = _build_image_available()


@pytest.mark.skipif(
    _IMAGE is None,
    reason="needs podman and the mageia:10-build image "
           "(urpm image make -r 10 -t mageia:10-build)",
)
class TestWrapperActuallyWorks:
    """Run ``NET_ISOLATION_WRAP`` for real inside the build image.

    Regression this exists for : the wrapper was once a plain
    ``unshare -n``.  It satisfied every shape assertion above and then
    failed at build time with « unshare failed: Operation not
    permitted », because a rootless podman container's ``CapEff``
    lacks CAP_SYS_ADMIN (bit 21) and ``unshare -n`` requires it.
    Every build broke before ``%build`` even started.

    These tests are the ones that can catch that.  They are slow and
    skipped without podman, which is the price of testing the claim
    rather than the string.
    """

    def _in_container(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["podman", "run", "--rm", _IMAGE, "bash", "-c", script],
            capture_output=True, text=True, timeout=180,
        )

    def test_wrapper_does_not_fail_to_start(self):
        """The bare minimum the previous wrapper could not clear."""
        res = self._in_container(f"{NET_ISOLATION_WRAP}true")
        assert res.returncode == 0, (
            f"isolation wrapper cannot start in the build container.\n"
            f"stderr: {res.stderr}"
        )
        assert "Operation not permitted" not in res.stderr

    def test_network_is_really_cut(self):
        """Isolation has to isolate — name resolution must fail inside
        the wrapper while it works outside it, in the same container."""
        res = self._in_container(
            "getent hosts mirrors.mageia.org >/dev/null 2>&1 "
            "&& echo OUTSIDE_OK || echo OUTSIDE_KO; "
            f"{NET_ISOLATION_WRAP}"
            "sh -c 'getent hosts mirrors.mageia.org >/dev/null 2>&1 "
            "&& echo INSIDE_OK || echo INSIDE_KO'"
        )
        assert "OUTSIDE_OK" in res.stdout, (
            "the container itself has no network — test proves nothing.\n"
            f"stdout: {res.stdout}"
        )
        assert "INSIDE_KO" in res.stdout, (
            f"network still reachable inside the wrapper.\n"
            f"stdout: {res.stdout}"
        )

    def test_identity_is_preserved(self):
        """rpmbuild must see uid 0 inside the wrapper exactly as it
        does outside, otherwise the buildroot ends up owned by nobody
        and the resulting RPM carries wrong ownership."""
        res = self._in_container(
            f"{NET_ISOLATION_WRAP}sh -c 'id -u'"
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "0", (
            f"uid inside wrapper is {res.stdout.strip()!r}, expected 0 — "
            "file ownership in the buildroot would be wrong"
        )

    def test_writes_keep_root_ownership(self):
        """Companion to the identity check : an actual write, since
        uid mapping and on-disk ownership can diverge."""
        res = self._in_container(
            f"{NET_ISOLATION_WRAP}"
            "sh -c 'mkdir -p /tmp/netiso && echo x > /tmp/netiso/f "
            "&& stat -c %U /tmp/netiso/f'"
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "root", (
            f"file written as {res.stdout.strip()!r}, expected root"
        )
