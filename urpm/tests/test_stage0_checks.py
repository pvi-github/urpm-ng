"""Tests for TS0.5 Phase B checks (SPEC_DISTUPGRADE §4.0).

- check_boot_space : /boot ≥ Σ(kernel) × 2 + 50 MB
- check_min_kernel : glibc's ABI-tag vs uname -r
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.distupgrade.checks import (
    BOOT_MARGIN_BYTES,
    BootSpaceError,
    MinKernelError,
    boot_files_size,
    check_boot_space,
    check_min_kernel,
    parse_min_kernel_from_readelf,
    parse_running_kernel,
)


# ── /boot ───────────────────────────────────────────────────────────


class TestBootFilesSize:
    def _mock_hdr(self, names, sizes):
        """Build a rpm-hdr-like mapping returning names + sizes."""
        import rpm as _rpm
        hdr = {
            _rpm.RPMTAG_FILENAMES: list(names),
            _rpm.RPMTAG_FILESIZES: list(sizes),
        }
        return hdr

    def test_zero_when_rpm_absent(self, tmp_path):
        # An unreadable path returns 0 without raising.
        assert boot_files_size(tmp_path / "does-not-exist.rpm") == 0

    def test_sums_boot_files_only(self, tmp_path):
        rpm_path = tmp_path / "kernel.rpm"
        rpm_path.write_bytes(b"")  # readable file, header parse mocked
        hdr = self._mock_hdr(
            names=[
                "/boot/vmlinuz-6.6.0",
                "/boot/System.map",
                "/usr/lib/modules/6.6.0/foo",
                "/boot/config-6.6.0",
            ],
            sizes=[12345678, 100, 999, 200],
        )
        ts_mock = MagicMock()
        ts_mock.hdrFromFdno.return_value = hdr
        with patch("rpm.TransactionSet", return_value=ts_mock):
            assert boot_files_size(rpm_path) == 12345678 + 100 + 200

    def test_returns_zero_when_no_boot_payload(self, tmp_path):
        rpm_path = tmp_path / "kmod.rpm"
        rpm_path.write_bytes(b"")
        hdr = self._mock_hdr(
            names=["/usr/lib/modules/6.6.0/kmod.ko"],
            sizes=[500000],
        )
        ts_mock = MagicMock()
        ts_mock.hdrFromFdno.return_value = hdr
        with patch("rpm.TransactionSet", return_value=ts_mock):
            assert boot_files_size(rpm_path) == 0

    def test_returns_zero_on_header_parse_error(self, tmp_path):
        import rpm as _rpm
        rpm_path = tmp_path / "corrupt.rpm"
        rpm_path.write_bytes(b"")
        ts_mock = MagicMock()
        ts_mock.hdrFromFdno.side_effect = _rpm.error("bad header")
        with patch("rpm.TransactionSet", return_value=ts_mock):
            assert boot_files_size(rpm_path) == 0


class TestCheckBootSpace:
    def test_passes_with_ample_room(self, tmp_path):
        with patch("urpm.core.distupgrade.checks.boot_files_size",
                   return_value=10 * 1024 * 1024):  # 10 MB
            with patch("urpm.core.distupgrade.checks._boot_free_bytes",
                       return_value=1024 * 1024 * 1024):  # 1 GB
                check_boot_space([Path("k.rpm")])

    def test_raises_when_tight(self, tmp_path):
        with patch("urpm.core.distupgrade.checks.boot_files_size",
                   return_value=200 * 1024 * 1024):  # 200 MB kernel
            with patch("urpm.core.distupgrade.checks._boot_free_bytes",
                       return_value=100 * 1024 * 1024):  # 100 MB free
                with pytest.raises(BootSpaceError) as exc:
                    check_boot_space([Path("k.rpm")])
        msg = str(exc.value)
        assert "urpm autoremove --oldkernels" in msg
        assert "dracut" in msg

    def test_no_kernel_files_returns_early(self):
        with patch("urpm.core.distupgrade.checks.boot_files_size",
                   return_value=0):
            # Should not even query _boot_free_bytes
            with patch("urpm.core.distupgrade.checks._boot_free_bytes") as f:
                check_boot_space([Path("k.rpm")])
                f.assert_not_called()

    def test_margin_included(self):
        # 100 MB kernel, need 100 * 2 + 50 = 250 MB
        with patch("urpm.core.distupgrade.checks.boot_files_size",
                   return_value=100 * 1024 * 1024):
            # 200 MB free is not enough — margin bites here.
            with patch("urpm.core.distupgrade.checks._boot_free_bytes",
                       return_value=200 * 1024 * 1024):
                with pytest.raises(BootSpaceError):
                    check_boot_space([Path("k.rpm")])
            # 260 MB free is fine.
            with patch("urpm.core.distupgrade.checks._boot_free_bytes",
                       return_value=260 * 1024 * 1024):
                check_boot_space([Path("k.rpm")])


# ── MIN_KERNEL ─────────────────────────────────────────────────────


class TestParseHelpers:
    def test_parse_min_kernel_standard(self):
        out = (
            "Displaying notes found in: .note.ABI-tag\n"
            "  Owner                Data size   Description\n"
            "  GNU                  0x00000010   NT_GNU_ABI_TAG\n"
            "    OS: Linux, ABI: 3.2.0\n"
        )
        assert parse_min_kernel_from_readelf(out) == (3, 2, 0)

    def test_parse_min_kernel_missing_line(self):
        assert parse_min_kernel_from_readelf("nothing here") is None

    def test_parse_running_kernel(self):
        assert parse_running_kernel("6.6.0-1.mga10") == (6, 6, 0)

    def test_parse_running_kernel_bogus(self):
        assert parse_running_kernel("unknown") is None


class TestCheckMinKernel:
    _READELF_OUT = (
        "Displaying notes found in: .note.ABI-tag\n"
        "    OS: Linux, ABI: 3.2.0\n"
    )

    def test_pass_when_running_newer(self, tmp_path):
        libc = tmp_path / "libc.so.6"
        libc.write_bytes(b"")
        with patch("shutil.which", return_value="/usr/bin/readelf"), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout=self._READELF_OUT, stderr="")
            check_min_kernel(libc, running_kernel="6.6.0-1.mga10")

    def test_raise_when_running_older(self, tmp_path):
        libc = tmp_path / "libc.so.6"
        libc.write_bytes(b"")
        with patch("shutil.which", return_value="/usr/bin/readelf"), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout=self._READELF_OUT, stderr="")
            with pytest.raises(MinKernelError) as exc:
                check_min_kernel(libc, running_kernel="3.0.0-1.mga9")
        msg = str(exc.value)
        assert "urpm upgrade kernel" in msg

    def test_silent_when_readelf_missing(self, tmp_path):
        with patch("shutil.which", return_value=None):
            check_min_kernel(tmp_path / "libc.so.6",
                             running_kernel="6.0.0-1")

    def test_silent_when_readelf_fails(self, tmp_path):
        libc = tmp_path / "libc.so.6"
        libc.write_bytes(b"")
        with patch("shutil.which", return_value="/usr/bin/readelf"), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="",
                                          stderr="corrupt")
            check_min_kernel(libc, running_kernel="6.6.0")

    def test_silent_on_locale_mangled_output(self, tmp_path):
        """A stale env leak (LANG=fr) would produce translated
        readelf output that doesn't match our regex — must not
        raise, just skip."""
        libc = tmp_path / "libc.so.6"
        libc.write_bytes(b"")
        with patch("shutil.which", return_value="/usr/bin/readelf"), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0,
                stdout="Système : Linux, ABI : 3.2.0\n",  # translated
                stderr="")
            check_min_kernel(libc, running_kernel="6.6.0")
