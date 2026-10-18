"""Tests for `_apply_transaction_niceness` (SPEC_DISTUPGRADE §3.E0).

The hook is applied post-fork on the transaction child so a long
transaction does not monopolize the machine.  It now delegates to
:func:`urpm.core.txresources.lower_priority`, which prefers a cgroup
v2 weight — the only one of the two that governs writeback — and falls
back to the ``ionice`` + ``os.nice`` pair these tests cover.

The cgroup root is pinned to an empty directory throughout, so the
fallback is tested because it was *asked for* and not because the
machine running the suite happens to lack the privilege to create a
cgroup.  Without that, the same tests would fail when run as root on
a host that delegates the ``io`` controller.
"""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest

from urpm.core import txresources
from urpm.core.transaction_queue import _apply_transaction_niceness


@pytest.fixture(autouse=True)
def without_cgroups(tmp_path, monkeypatch):
    monkeypatch.setattr(txresources, "CGROUP_ROOT", tmp_path / "absent")


def test_ionice_invoked_with_current_pid_when_available():
    with patch("shutil.which", return_value="/usr/bin/ionice"), \
         patch("subprocess.run") as run, \
         patch("os.nice"):
        _apply_transaction_niceness()
        run.assert_called_once()
        args, _kw = run.call_args
        cmd = args[0]
        assert cmd[0] == "ionice"
        assert "-c3" in cmd
        assert cmd[cmd.index("-p") + 1] == str(os.getpid())


def test_ionice_skipped_when_binary_missing():
    with patch("shutil.which", return_value=None), \
         patch("subprocess.run") as run, \
         patch("os.nice"):
        _apply_transaction_niceness()
        run.assert_not_called()


def test_nice_increment_applied():
    with patch("shutil.which", return_value=None), \
         patch("subprocess.run"), \
         patch("os.nice") as nice:
        _apply_transaction_niceness()
        nice.assert_called_once_with(5)


def test_nice_oserror_swallowed():
    """A failing os.nice must not raise — nothing worse than a
    non-niced transaction, we still want the commit to proceed."""
    with patch("shutil.which", return_value=None), \
         patch("subprocess.run"), \
         patch("os.nice", side_effect=OSError("EPERM")):
        # Must not raise
        _apply_transaction_niceness()


def test_ionice_check_false():
    """`ionice` failure must be silent — same rationale as nice."""
    with patch("shutil.which", return_value="/usr/bin/ionice"), \
         patch("subprocess.run") as run, \
         patch("os.nice"):
        run.return_value = MagicMock(returncode=1)
        # Must not raise
        _apply_transaction_niceness()
        # Verify check=False was used
        _, kwargs = run.call_args
        assert kwargs.get("check") is False
