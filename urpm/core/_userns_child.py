"""Entry point for the child process spawned under ``podman unshare``.

``_execute_with_userns`` in :mod:`urpm.core.transaction_queue` pickles the
queue state to a temp file, then runs a tiny bootstrap in a subprocess
that scrubs ``sys.path`` (to avoid picking up a dev checkout via
``python3 -c``'s implicit ``sys.path[0] == ""``) and calls
:func:`run_child` here.

Keeping the child body in a real module -- rather than a heredoc
f-string invoked with ``python3 -c`` -- means it can be linted,
type-checked, and unit-tested like any other code.  The bootstrap is
the only piece that has to remain inline in the subprocess invocation
because the ``sys.path`` scrub has to happen *before* the first
``urpm`` import.
"""

from __future__ import annotations

import pickle

__all__ = ["run_child"]


def run_child(state_file: str) -> None:
    """Rehydrate the queue state and drive the standalone child loop.

    Called from the ``podman unshare python3 -c ...`` bootstrap after
    ``sys.path[0]`` has been scrubbed.  The state file is the pickle
    dumped by :meth:`TransactionQueue._execute_with_userns` — it holds
    the queue root, the ordered operation list, and the parent pipe
    write-fd (which the standalone loop uses for progress reporting).

    Never returns normally: the child process finishes when its loop
    finishes and its stdout pipe closes.
    """
    with open(state_file, "rb") as f:
        state = pickle.load(f)

    # Imported lazily so any ``sys.path`` scrub in the bootstrap
    # applies before the first ``urpm`` import.
    from urpm.core.transaction_queue import TransactionQueue

    queue = TransactionQueue(root=state["root"])
    queue.operations = state["operations"]
    queue._child_process_standalone()
