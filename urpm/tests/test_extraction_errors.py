"""Payload-extraction failure detection in the rpm transaction callback.

Regression this locks down : a user's ``urpm image make`` reported
« 10 paquets installés » with exit code 0 while ``filesystem`` was
absent from the resulting rpmdb.  The chain was :

1. ``filesystem`` ships ``/bin``, ``/lib``, ``/lib64``, ``/sbin`` as
   UsrMove *symlinks*, prepared by its ``%pretrans``.
2. An earlier ``--noscripts`` pass skipped that pretrans, so ``glibc``
   extracted into ``/lib`` and ``/lib64`` as real *directories*.
3. rpm then had to replace a non-empty directory with a symlink,
   which fails.
4. rpm signalled it through ``RPMCALLBACK_UNPACK_ERROR`` and returned
   an **empty** ``problems`` list from ``ts.run()``.
5. urpm-ng only listened for ``RPMCALLBACK_CPIO_ERROR``, saw nothing,
   and reported a full success.

The empty-``problems`` behaviour is the crux : when payload extraction
fails there is no other signal.  If the callback misses it, nothing
downstream can recover it.

The integration test reproduces the exact conflict against real rpm.
It is skipped off Mageia and when the ``filesystem`` package is not
available on disk.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

rpm = pytest.importorskip("rpm")


FILESYSTEM_RPM = Path(
    "/var/lib/urpm/medias/official/10/x86_64/media/core/release/"
    "filesystem-2.1.9-41.mga10.x86_64.rpm"
)


def is_mageia() -> bool:
    """Detect if running on Mageia Linux."""
    try:
        with open("/etc/mageia-release"):
            return True
    except FileNotFoundError:
        return False


class TestExtractionErrorConstants:
    """Cheap, always-runnable guards on the constants themselves."""

    def test_both_error_constants_exist_in_rpm(self):
        """rpm exposes two distinct payload-failure reasons.  If a future
        rpm drops one, this test says so before the behaviour silently
        changes underneath us."""
        assert hasattr(rpm, "RPMCALLBACK_UNPACK_ERROR")
        assert hasattr(rpm, "RPMCALLBACK_CPIO_ERROR")
        assert rpm.RPMCALLBACK_UNPACK_ERROR != rpm.RPMCALLBACK_CPIO_ERROR

    def test_transaction_queue_handles_both(self):
        """The callback must branch on BOTH constants.  Listening to
        ``CPIO_ERROR`` alone is exactly the bug : rpm 4.20 fires
        ``UNPACK_ERROR`` for the common directory/symlink conflict."""
        src = (
            Path(__file__).parent.parent / "core" / "transaction_queue.py"
        ).read_text()
        assert "RPMCALLBACK_UNPACK_ERROR" in src, (
            "UNPACK_ERROR is unhandled — payload failures will be "
            "reported as success"
        )
        assert "RPMCALLBACK_CPIO_ERROR" in src

    def test_diag_map_tracks_both(self):
        """The ``pkg_callback_seen`` diagnostic buckets must cover both
        too, otherwise post-mortem dumps under ``URPM_DUP_DIAG`` lose
        the very signal we are chasing."""
        src = (
            Path(__file__).parent.parent / "core" / "transaction_queue.py"
        ).read_text()
        assert '"unpack_error": set()' in src
        assert '"cpio_error": set()' in src


@pytest.mark.skipif(not is_mageia(), reason="needs Mageia rpm layout")
@pytest.mark.skipif(
    not FILESYSTEM_RPM.exists(),
    reason=f"{FILESYSTEM_RPM} not present — run 'urpm download filesystem'",
)
class TestExtractionErrorIntegration:
    """Reproduce the real failure against real rpm.

    Uses ``unshare --user --map-root-user`` so the test needs no root :
    the chroot is built and the transaction driven inside a throwaway
    user namespace.
    """

    # Driven as a subprocess script because the transaction must run
    # inside the unshared namespace, not in the pytest process.
    PROBE = r"""
import os, sys, rpm
ROOT, PKG = sys.argv[1], sys.argv[2]
extraction_errors, open_fds = [], {}
def cb(reason, amount, total, key, data):
    if reason in (rpm.RPMCALLBACK_UNPACK_ERROR, rpm.RPMCALLBACK_CPIO_ERROR):
        extraction_errors.append(str(key)); return None
    if reason == rpm.RPMCALLBACK_INST_OPEN_FILE:
        fd = os.open(key, os.O_RDONLY); open_fds[key] = fd; return fd
    if reason == rpm.RPMCALLBACK_INST_CLOSE_FILE:
        fd = open_fds.pop(key, None)
        if fd is not None: os.close(fd)
    return None
ts = rpm.TransactionSet(ROOT)
ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
fd = rpm.fd.open(PKG, 'r'); hdr = ts.hdrFromFdno(fd); fd.close()
ts.addInstall(hdr, PKG, 'i')
ts.setProbFilter(rpm.RPMPROB_FILTER_DISKSPACE | rpm.RPMPROB_FILTER_REPLACEPKG)
ts.order()
problems = ts.run(cb, '')
print("PROBLEMS_EMPTY=%d" % (0 if problems else 1))
print("EXTRACTION_ERRORS=%d" % len(extraction_errors))
"""

    def _run_conflict(self, tmp_path: Path) -> dict:
        """Build a chroot whose ``/lib`` and ``/lib64`` are non-empty
        real directories, then install ``filesystem`` (which wants them
        to be symlinks) and report what rpm signalled."""
        chroot = tmp_path / "chroot"
        probe = tmp_path / "probe.py"
        probe.write_text(self.PROBE)
        chroot.mkdir()

        script = f"""
rpm --root '{chroot}' --initdb 2>/dev/null
mkdir -p '{chroot}/lib' '{chroot}/lib64'
echo fake > '{chroot}/lib/libc.so.6'
echo fake > '{chroot}/lib64/libc.so.6'
{sys.executable} '{probe}' '{chroot}' '{FILESYSTEM_RPM}'
"""
        proc = subprocess.run(
            ["unshare", "--user", "--map-root-user", "bash", "-c", script],
            capture_output=True, text=True, timeout=120,
        )
        out = {}
        for line in proc.stdout.splitlines():
            if "=" in line and line.split("=")[0].isupper():
                k, v = line.split("=", 1)
                out[k] = int(v)
        out["_chroot"] = chroot
        out["_stdout"] = proc.stdout
        out["_stderr"] = proc.stderr
        return out

    def test_rpm_returns_empty_problems_on_extraction_failure(self, tmp_path):
        """The crux : rpm's ``problems`` list is EMPTY even though the
        package failed to install.  This is why the callback is the only
        possible detection point."""
        res = self._run_conflict(tmp_path)
        assert res.get("PROBLEMS_EMPTY") == 1, (
            "rpm returned problems — the premise of the fix no longer "
            f"holds.\nstdout:\n{res['_stdout']}\nstderr:\n{res['_stderr']}"
        )

    def test_callback_catches_the_failure(self, tmp_path):
        """With both constants handled, the failure IS detected."""
        res = self._run_conflict(tmp_path)
        assert res.get("EXTRACTION_ERRORS", 0) >= 1, (
            "payload failure went undetected — this is the silent-success "
            f"bug.\nstdout:\n{res['_stdout']}\nstderr:\n{res['_stderr']}"
        )

    def test_package_really_is_absent_afterwards(self, tmp_path):
        """Confirms the failure is real and not a false alarm : the
        package is genuinely missing from the rpmdb, so reporting
        success would be a lie."""
        res = self._run_conflict(tmp_path)
        chroot = res["_chroot"]
        proc = subprocess.run(
            ["rpm", "--root", str(chroot), "-q", "filesystem"],
            capture_output=True, text=True,
        )
        assert proc.returncode != 0, (
            "filesystem IS installed — the conflict did not reproduce, "
            f"so this test proves nothing.\nstdout: {proc.stdout}"
        )
