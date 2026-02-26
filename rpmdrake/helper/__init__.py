"""Transaction helper for rpmdrake-ng.

This module provides a privileged helper that runs via pkexec
to execute package operations (install, remove, upgrade).
"""

from .transaction_helper import TransactionHelper, run_helper

__all__ = ["TransactionHelper", "run_helper"]
