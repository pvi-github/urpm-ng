"""Distupgrade state persistence — backed by :class:`PackageDatabase.config`.

The state is the source of durable truth : it survives SIGKILL, power
cut, and reboot.  Persistence delegates to the SQLite config table
(WAL journal + atomic per-row writes) — no dedicated file, no
mkstemp/fsync/rename dance to maintain.

Consumers :

- :mod:`urpm.core.distupgrade.stage0` writes the initial state after
  Phase A/B pass.
- :mod:`urpm.core.distupgrade.stage3` bumps ``stage`` at every step
  boundary + persists ``tx_a_plan_ordered`` / ``tx_b_plan_ordered``.
- :mod:`urpm.cli.commands.distupgrade` `--resume` reads it to decide
  where to restart.
- The CLI mesh helper (`urpm/cli/helpers/distupgrade_mesh.py`)
  observes its presence to refuse write verbs.

Public API :

- :func:`read_state`  — return the dict or ``None`` when unset.
- :func:`write_state` — replace the dict wholesale.
- :func:`bump_stage`  — update the ``stage`` field only.
- :func:`delete_state` — clear the config row.

Every function takes ``db`` as a **required** argument.  urpm-ng's
convention is one :class:`PackageDatabase` per process (thread-safe
via thread-local connections) — a lazy singleton here would create
a second connection on the same SQLite file, defeating the WAL
serialisation the CLI relies on and re-introducing the concurrent-
writer lock contention we've spent effort designing away
(PackageKit + urpmd + CLI + distupgrade all through one instance).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..database import PackageDatabase


STATE_CONFIG_KEY = "distupgrade.state"


class DistupgradeStateError(Exception):
    """Raised for corrupted state (malformed JSON)."""


def read_state(db: "PackageDatabase") -> Optional[Dict[str, Any]]:
    """Return the persisted state dict, or ``None`` when unset.

    Malformed JSON (should never happen — writers only ever go
    through :func:`write_state` — but defense in depth) raises
    :class:`DistupgradeStateError`.
    """
    raw = db.get_config(STATE_CONFIG_KEY)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DistupgradeStateError(
            f"cannot parse distupgrade state from config: {exc}"
        ) from exc


def write_state(new_state: Dict[str, Any],
                db: "PackageDatabase") -> None:
    """Replace the state row wholesale.

    Serialised with ``sort_keys=True`` so a `cat`-style diff between
    two config dumps stays readable — the payload is user-facing at
    debug time even though it doesn't live in a file anymore.
    """
    payload = json.dumps(new_state, sort_keys=True)
    db.set_config(STATE_CONFIG_KEY, payload)


def bump_stage(stage: str, db: "PackageDatabase") -> None:
    """Update the ``stage`` field only, keeping the rest intact.

    A missing state row is a hard error — a stage cannot bump into
    something that was never opened.
    """
    current = read_state(db)
    if current is None:
        raise DistupgradeStateError(
            "cannot bump stage: state row is absent")
    current["stage"] = stage
    write_state(current, db)


def delete_state(db: "PackageDatabase") -> None:
    """Clear the state row.  No-op when absent."""
    db.set_config(STATE_CONFIG_KEY, None)
