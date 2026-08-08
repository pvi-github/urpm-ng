"""Tests for `_apply_transaction_niceness` (SPEC_DISTUPGRADE §3.E0).

Minimal ionice + os.nice hook applied post-fork on the transaction
child so long transactions don't monopolize the machine.
"""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

from urpm.core.transaction_queue import _apply_transaction_niceness


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
