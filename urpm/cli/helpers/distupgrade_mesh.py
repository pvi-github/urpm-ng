"""Mesh de blocage des commandes urpm pendant un distupgrade en cours.

Contrat §2 de SPEC_DISTUPGRADE : toute commande urpm qui touche au
rpmdb ou aux media doit refuser d'agir tant que la config
``distupgrade.state`` (SQLite) est présente OU que
`/run/urpm/distupgrade.lock` est tenu par un process vivant qui est
bien un distupgrade (check argv, cf. §4.0 `_pid_is_distupgrade`).

Deux checks enchaînés :

1. **`distupgrade.state`** en base : source de vérité durable —
   survit SIGKILL / coupure secteur et signale « distupgrade à
   reprendre ».
2. **`fcntl` lock** sur `/run/urpm/distupgrade.lock` : refus quand un
   distupgrade est actuellement en train de tourner.

Une commande CLI top-level appelle :class:`check_distupgrade_mesh`
avant d'entrer dans son handler.  Si refusée, le handler n'est jamais
exécuté ; l'user reçoit un message actionnable pointant
``urpm distupgrade --resume`` / ``--abort``.

**Échappatoire install/erase** : `urpm install <paquet>` et
`urpm remove <paquet>` (opération ponctuelle) sont **autorisés
exceptionnellement** avec un warning explicite, pour dépanner un
distupgrade coincé.  Refuse toujours en cas de plusieurs paquets
ou de flags de résolution étendue (--auto, --all).  Voir §2.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional

from ...i18n import _
from .. import colors


DISTUPGRADE_LOCK_PATH = Path("/run/urpm/distupgrade.lock")


class DistupgradeMeshRefusal(Exception):
    """Raised when the mesh refuses a command.

    ``message`` porte le texte user-visible pointant `--resume` /
    `--abort` ou le PID détenteur.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _read_state_stage(db) -> Optional[str]:
    """Return the ``stage`` value from ``distupgrade.state`` in DB.

    Reads via the standard state helper (SQLite-backed) using the
    caller's ``db`` connection.  Returns ``None`` when unset or
    unreadable — the mesh treats absence as « no distupgrade in
    progress » and lets the command through.
    """
    try:
        from ...core.distupgrade.state import read_state
        state = read_state(db)
    except Exception:  # noqa: BLE001 — mesh must never crash startup
        return None
    if state is None:
        return None
    return state.get("stage")


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID exists.

    Pattern from `urpm/core/sync_lock.py:185` (`_pid_alive`) — sends
    signal 0 which does no work but errors on ProcessLookupError.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # EPERM implies the process exists but we can't signal it
        return True
    return True


def _pid_is_distupgrade(pid: int) -> bool:
    """Return True if PID is a live ``urpm distupgrade`` process.

    Checks ``/proc/<pid>/cmdline`` structurally (basename argv[i] ==
    'urpm', 'distupgrade' in argv[i+1:]) — tolerates the shebang
    interpreter case where argv[0] is 'python3' and 'urpm' is argv[2]
    (§4.0 `_pid_is_distupgrade`).
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return False
    if argv and argv[-1] == b"":
        argv = argv[:-1]
    idx = next(
        (i for i, a in enumerate(argv)
         if os.path.basename(a) == b"urpm"),
        None,
    )
    return idx is not None and b"distupgrade" in argv[idx + 1:]


def _read_lock_pid() -> Optional[int]:
    """Return the PID recorded in `distupgrade.lock`, or None.

    Handles the in-flight PID-write window (empty file or partial
    line without trailing ``\n``) by returning None — the caller
    treats it as « lock not held by a live distupgrade ».
    """
    try:
        with open(DISTUPGRADE_LOCK_PATH, "rb") as f:
            raw = f.read(32)
    except (FileNotFoundError, PermissionError):
        return None
    if not raw.endswith(b"\n"):
        return None
    try:
        return int(raw.decode().strip())
    except ValueError:
        return None


def check_distupgrade_mesh(command_name: str, db,
                          *, is_read_only: bool = False,
                          is_escape_hatch: bool = False) -> None:
    """Refuse commands while a distupgrade is in progress or interrupted.

    Args:
        command_name: The subcommand invoked (e.g. ``'upgrade'``,
            ``'install'``, ``'media add'``).  Used only for user
            messages.
        is_read_only: When True, this command doesn't mutate the
            rpmdb / media tables and is always allowed.  Applies to
            ``search``, ``show``, ``list``, ``depends``,
            ``whatrequires``, ``history``.
        is_escape_hatch: When True (single-package ``install`` /
            ``remove``), emit a warning but allow.  Refuses the
            escape hatch only if a live distupgrade actively holds
            the lock — the escape is meant for a suspended
            distupgrade (crash / power cut).

    Raises:
        DistupgradeMeshRefusal: if the command must be refused.
    """
    if is_read_only:
        return

    stage = _read_state_stage(db)
    lock_pid = _read_lock_pid()
    lock_alive = (
        lock_pid is not None
        and _pid_alive(lock_pid)
        and _pid_is_distupgrade(lock_pid)
    )

    if not stage and not lock_alive:
        return

    if is_escape_hatch:
        if lock_alive:
            # Distupgrade actively running — even the escape hatch
            # would race with it.  Refuse.
            raise DistupgradeMeshRefusal(_(
                "un distupgrade Mageia est actuellement en cours (PID {pid}).\n"
                "Attendez la fin ou utilisez `urpm distupgrade --abort` "
                "pour l'abandonner."
            ).format(pid=lock_pid))
        # `.state` present but no live PID — distupgrade is suspended
        # (SIGKILL / power cut).  Escape hatch is intended for exactly
        # this dépannage case.  Warn but allow.
        print(colors.warning(_(
            "ATTENTION : un distupgrade Mageia est en cours à l'état "
            "'{stage}' et actuellement suspendu (pas de process vivant).\n"
            "Cette opération va manipuler le rpmdb source pendant que "
            "le distupgrade est suspendu.\n\n"
            "L'échappatoire install/erase est prévue pour du dépannage "
            "ciblé (retirer un paquet qui a fait crasher Tx A, installer "
            "une dépendance manquante). Toute autre manipulation risque "
            "de rendre le distupgrade non-reprisible."
        ).format(stage=stage or "?")))
        return

    if lock_alive:
        raise DistupgradeMeshRefusal(_(
            "distupgrade Mageia en cours (PID {pid}).\n"
            "Attendez la fin ou utilisez `urpm distupgrade --abort` "
            "pour l'abandonner."
        ).format(pid=lock_pid))

    raise DistupgradeMeshRefusal(_(
        "distupgrade Mageia à l'état '{stage}' (transaction interrompue).\n"
        "Utilisez `urpm distupgrade --resume` pour reprendre ou "
        "`urpm distupgrade --abort` pour abandonner."
    ).format(stage=stage or "?"))
