"""``urpm build`` : ``rpmbuild`` runs network-isolated by default,
controlled by ``--net-isolation``.

Purpose : the spec's ``%prep`` / ``%build`` / ``%install`` /
``%check`` runs in a network-less namespace so a stray ``curl`` or
``pip install`` cannot sneak unaudited content into the final RPM.

Two layers, and the split matters — the first shipped alone once and
that is exactly how a broken wrapper reached users :

* :class:`TestBuildNetworkIsolation` drives the chain against a fake
  container and asserts the *shape* of the issued command.  Fast, no
  podman needed, but it proves only that we emit what we meant to.
* :class:`TestWrapperActuallyWorks` runs the wrapper for real inside
  the build image and asserts it does what it claims.  Both wrappers
  that shipped broken passed every shape test :

  - a plain ``unshare -n`` died with « unshare failed: Operation not
    permitted », because a podman container has no CAP_SYS_ADMIN ;
  - ``unshare --user --map-root-user --net`` started fine, then broke
    every ``chown`` towards a non-root uid, because the nested user
    namespace maps uid 0 and nothing else.

  Shape tests cannot catch either ; only execution can.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from urpm.cli.helpers.build_chain import (
    NET_ISOLATION_CAPS,
    NET_ISOLATION_WRAP,
    NET_PING_CAPS,
    NetIsolationUnavailable,
    _build_one_spec_in_container,
    resolve_net_wrap,
    start_build_container,
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


def _run_chain_with(net_wrap: str, tmp_path: Path) -> list[str]:
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
        net_wrap=net_wrap,
    )
    return _rpmbuild_bash_calls(container)


class TestBuildNetworkIsolation:

    def test_default_wraps_rpmbuild(self, tmp_path):
        """Default (network off) : every rpmbuild command must carry
        the isolation prefix.  Asserted against the constant rather
        than a literal, so changing the wrapper cannot leave the test
        passing against a stale string."""
        calls = _run_chain_with(NET_ISOLATION_WRAP, tmp_path=tmp_path)
        assert calls, "no rpmbuild call captured — test fixture broken"
        for cmd in calls:
            assert f"{NET_ISOLATION_WRAP}rpmbuild" in cmd, (
                f"rpmbuild not net-isolated : {cmd}")

    def test_no_wrap_leaves_rpmbuild_bare(self, tmp_path):
        """``--net-isolation=off`` opts out : rpmbuild runs with the
        container's ambient network (no ``unshare`` prefix)."""
        calls = _run_chain_with("", tmp_path=tmp_path)
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
        calls = _run_chain_with(NET_ISOLATION_WRAP, tmp_path=tmp_path)
        seen_br = any(" -br " in cmd for cmd in calls)
        seen_ba = any(" -ba " in cmd for cmd in calls)
        assert seen_br, "no rpmbuild -br call captured"
        assert seen_ba, "no rpmbuild -ba call captured"
        for cmd in calls:
            assert NET_ISOLATION_WRAP.strip() in cmd, cmd

    def test_wrapper_creates_no_user_namespace(self):
        """The wrapper must not open a user namespace.

        ``unshare --user --map-root-user`` shipped once as a way to
        obtain CAP_SYS_ADMIN for free.  It writes a one-line uid_map,
        so uid 0 becomes the only translatable identity and every
        ``chown`` towards another uid answers EINVAL — python's %prep
        died unpacking a tarball owned by uid 1000.  The capabilities
        come from the container now (:data:`NET_ISOLATION_CAPS`).
        """
        assert "--user" not in NET_ISOLATION_WRAP, NET_ISOLATION_WRAP
        assert "--map-root-user" not in NET_ISOLATION_WRAP, NET_ISOLATION_WRAP


class _CapRecordingContainer(_FakeContainer):
    """Fake container that records ``run`` kwargs and can refuse caps.

    ``probe_rc`` is what ``unshare --net true`` will answer, so a test
    can drive the unavailable-isolation branches without podman.
    """

    def __init__(self, refuse_caps: bool = False, probe_rc: int = 0):
        super().__init__()
        self.refuse_caps = refuse_caps
        self.probe_rc = probe_rc
        self.run_kwargs: list[dict] = []

    def run(self, image, command=None, **kw):
        self.run_kwargs.append(kw)
        if self.refuse_caps and kw.get('cap_add'):
            raise RuntimeError("Container run failed: --cap-add refused")
        return "fakecid"

    def exec(self, cid, cmd, **kw):
        rv = super().exec(cid, cmd, **kw)
        if cmd[:2] == ['unshare', '--net']:
            rv.returncode = self.probe_rc
            rv.stderr = "" if self.probe_rc == 0 else "Operation not permitted"
        return rv


class TestIsolationCapabilities:
    """Where the capabilities come from, and what happens without them."""

    def test_container_asks_for_the_capabilities(self):
        container = _CapRecordingContainer()
        start_build_container(container, "img", None, 'auto')
        asked = container.run_kwargs[0]['cap_add']
        assert set(asked) == set(NET_ISOLATION_CAPS) | set(NET_PING_CAPS)

    def test_off_still_asks_for_the_ping_capability(self):
        """No isolation wanted, so no isolation privilege — but ICMP is
        not an isolation matter and a ``%check`` that pings needs it in
        either mode."""
        container = _CapRecordingContainer()
        start_build_container(container, "img", None, 'off')
        asked = container.run_kwargs[0]['cap_add']
        assert set(asked) == set(NET_PING_CAPS)
        assert not set(asked) & set(NET_ISOLATION_CAPS)

    def test_refused_capabilities_still_start_the_container(self):
        """A runtime that will not grant them must not break the build
        outright — ``resolve_net_wrap`` is what reports the downgrade."""
        container = _CapRecordingContainer(refuse_caps=True)
        cid = start_build_container(container, "img", None, 'auto')
        assert cid == "fakecid"
        assert len(container.run_kwargs) == 2
        assert 'cap_add' not in container.run_kwargs[1]

    def test_auto_downgrades_when_the_probe_fails(self, capsys):
        container = _CapRecordingContainer(probe_rc=1)
        assert resolve_net_wrap(container, "fakecid", 'auto') == ""
        assert "OPEN" in capsys.readouterr().out

    def test_strict_refuses_when_the_probe_fails(self):
        container = _CapRecordingContainer(probe_rc=1)
        with pytest.raises(NetIsolationUnavailable):
            resolve_net_wrap(container, "fakecid", 'strict')

    def test_off_never_probes(self):
        """Nothing to check when isolation is not wanted."""
        container = _CapRecordingContainer(probe_rc=1)
        assert resolve_net_wrap(container, "fakecid", 'off') == ""
        assert container.calls == []


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

    Two regressions this exists for, both of which passed every shape
    assertion above and broke real builds :

    * a plain ``unshare -n`` failed at build time with « unshare
      failed: Operation not permitted », because a podman container's
      ``CapEff`` lacks CAP_SYS_ADMIN (bit 21) ;
    * ``unshare --user --map-root-user --net`` cleared that, then
      broke ownership restoration (see
      :meth:`test_non_root_ownership_is_restorable`).

    These tests are the ones that can catch them.  They are slow and
    skipped without podman, which is the price of testing the claim
    rather than the string.

    The container is started the way ``urpm build`` starts it, caps
    included : without them the wrapper cannot work, and a test that
    dropped them would be testing something we never ship.
    """

    def _in_container(self, script: str) -> subprocess.CompletedProcess:
        caps = []
        for capability in (*NET_ISOLATION_CAPS, *NET_PING_CAPS):
            caps += ["--cap-add", capability]
        return subprocess.run(
            ["podman", "run", "--rm", *caps, _IMAGE, "bash", "-c", script],
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

    def test_loopback_is_usable(self):
        """The wrapper cuts the network, not the machine's ability to
        talk to itself.

        A fresh network namespace contains ``lo``, but it is created
        DOWN, so ``127.0.0.1`` is unreachable.  Every ``%check`` that
        binds a local socket fails on it (Test::TCP, Plack, anything
        starting a server or a daemon), with an error that never
        mentions networking : ``perl-Web-ID`` reported « Can't call
        method "uri_object" on an undefined value », and two Perl
        packages were nearly patched to work around it.

        Paired with :meth:`test_network_is_really_cut`, this is what
        makes the guarantee precise: cut outward, intact inward.

        The check is a real TCP connection, which is what those suites
        actually open.  ICMP gets its own test below.
        """
        res = self._in_container(
            f"{NET_ISOLATION_WRAP}sh -c 'ip -br link show lo'\n"
            f"{NET_ISOLATION_WRAP}python3 - <<'PY'\n"
            "import socket, threading\n"
            "srv = socket.socket()\n"
            "srv.bind(('127.0.0.1', 0))\n"
            "srv.listen(1)\n"
            "threading.Thread(target=lambda: srv.accept()[0].sendall(b'pong'),\n"
            "                 daemon=True).start()\n"
            "with socket.create_connection(srv.getsockname(), timeout=5) as c:\n"
            "    print('LOOPBACK_' + ('OK' if c.recv(4) == b'pong' else 'KO'))\n"
            "PY\n"
        )
        assert "LOOPBACK_OK" in res.stdout, (
            f"127.0.0.1 unreachable inside the wrapper — every test "
            f"suite binding a local socket will fail.\n"
            f"stdout: {res.stdout}\nstderr: {res.stderr}"
        )
        assert "UP" in res.stdout, (
            f"lo is present but not up.\nstdout: {res.stdout}"
        )

    def test_loopback_answers_icmp(self):
        """``ping 127.0.0.1`` has to work inside the wrapper.

        It needs a raw socket: a fresh network namespace resets
        ``net.ipv4.ping_group_range`` to ``65534 65534`` and
        ``/proc/sys`` is read-only in the container, so the unprivileged
        ICMP datagram path is closed.  podman dropped ``NET_RAW`` from
        its defaults, hence :data:`NET_PING_CAPS`.  The nested user
        namespace used to supply it by accident, handing the build
        every capability; specs that ping noticed when it went away.
        """
        res = self._in_container(
            f"{NET_ISOLATION_WRAP}"
            "sh -c 'ping -c1 -W1 127.0.0.1 >/dev/null 2>&1 "
            "&& echo PING_OK || echo PING_KO'"
        )
        assert "PING_OK" in res.stdout, (
            f"ICMP unusable inside the wrapper — a %check that pings "
            f"localhost will fail.\nstdout: {res.stdout}\n"
            f"stderr: {res.stderr}"
        )

    def test_icmp_still_cannot_leave_the_namespace(self):
        """Companion to the previous one: a raw socket must not become
        a way out.  It is not, there being no route off the namespace,
        but granting NET_RAW is exactly the kind of change that
        deserves the claim checked rather than assumed."""
        res = self._in_container(
            f"{NET_ISOLATION_WRAP}"
            "sh -c 'ping -c1 -W1 9.9.9.9 >/dev/null 2>&1 "
            "&& echo OUT_OK || echo OUT_KO'"
        )
        assert "OUT_KO" in res.stdout, (
            f"an external address answers inside the wrapper — the "
            f"network is not isolated.\nstdout: {res.stdout}"
        )

    def test_exit_status_survives_the_wrapper(self):
        """``exec "$@"`` has to keep rpmbuild's exit code intact.

        The call sites read it to tell « needs more BuildRequires »
        (11) from a real failure, through a ``set -o pipefail`` and a
        ``tee``.  A wrapper that swallowed or replaced the status would
        turn a dynamic-BuildRequires pass into a build error.
        """
        res = self._in_container(
            f"set -o pipefail; {NET_ISOLATION_WRAP}"
            "sh -c 'exit 11' 2>&1 | tee /dev/null"
        )
        assert res.returncode == 11, (
            f"exit status lost: expected 11, got {res.returncode}"
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

    def test_non_root_ownership_is_restorable(self):
        """Seeing uid 0 is not enough : other uids must stay usable.

        ``unshare --user --map-root-user`` satisfied every check above
        and still broke builds, because its uid_map has a single line
        (``0 0 1``). Inside it uid 0 is the only translatable identity
        and ``chown`` towards any other answers EINVAL.  python's
        %prep unpacks a documentation tarball whose files belong to
        uid 1000 and died on it::

            tar: …/genindex-T.html: Cannot change ownership to
                 uid 1000, gid 1000: Invalid argument

        Any spec unpacking an archive owned by a non-root user, or
        chowning to a system user in %install, hits the same wall.
        """
        res = self._in_container(
            f"{NET_ISOLATION_WRAP}"
            "sh -c 'echo x > /tmp/owned && chown 1000:1000 /tmp/owned "
            "&& stat -c %u:%g /tmp/owned'"
        )
        assert res.returncode == 0, (
            f"chown to a non-root uid failed inside the wrapper — every "
            f"spec restoring archive ownership in %prep will break.\n"
            f"stderr: {res.stderr}"
        )
        assert res.stdout.strip() == "1000:1000", (
            f"ownership is {res.stdout.strip()!r}, expected 1000:1000"
        )

    def test_tar_restores_archive_ownership(self):
        """The same claim through the tool that actually broke.

        ``chown`` succeeding is the mechanism ; ``tar`` restoring what
        an archive carries is what %prep does.
        """
        res = self._in_container(
            "mkdir -p /tmp/src/d && echo x > /tmp/src/d/f && "
            "tar --owner=1000 --group=1000 -cf /tmp/a.tar -C /tmp/src d && "
            f"{NET_ISOLATION_WRAP}"
            "sh -c 'mkdir -p /tmp/dst && tar xf /tmp/a.tar -C /tmp/dst "
            "&& stat -c %u:%g /tmp/dst/d/f'"
        )
        assert res.returncode == 0, (
            f"tar could not restore ownership inside the wrapper.\n"
            f"stderr: {res.stderr}"
        )
        assert res.stdout.strip() == "1000:1000", (
            f"extracted file is {res.stdout.strip()!r}, expected 1000:1000"
        )
