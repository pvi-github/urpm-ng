"""Driving services through the wrapper the distribution already provides.

urpm-ng carries no systemd dependency: init integrations are optional and
working across init systems is a requirement. So a hook rule says *"this
service must restart"* and never *"run systemctl"*.

We do not translate that ourselves. Mageia ships ``service``, from
``initscripts``, which ``basesystem-minimal`` requires, so it is there
even on a minimal install. It runs the SysV script when there is one and
redirects to systemd otherwise (``/usr/sbin/service``)::

    if ! is_ignored_file "$service" && [[ -f $servicedir/$service ]]; then
        ... "$servicedir/$service" $options
    elif [[ -f /lib/systemd/system/"$service".service ]] && [ -d /run/systemd/system/ ]; then
        exec /bin/systemctl ${options} ${service}.service

Letting it decide beats detecting the init ourselves: the answer stays
the distribution's, and it keeps working the day that answer changes.

The verb is ``condrestart``, which systemd accepts as its ``try-restart``
(it says so itself when it refuses: *"Failed to try-restart …"*). It
carries the rule that matters: **a service that was not running is never
started**. Restarting a display manager or sshd underneath a live session
is a proven way to break a machine, and task #105, sshd dying during a
distupgrade, says what that costs.

Three limits of the wrapper, read in the script and worth knowing before
someone wonders why an exotic case misbehaves:

* its systemd branch only looks in ``/lib/systemd/system``, so a unit
  defined solely under ``/etc/systemd/system`` by an administrator is
  invisible to it;
* it appends ``.service`` unconditionally, so a ``.timer`` or a
  ``.socket`` cannot be targeted;
* a service that does not exist and a service that is stopped both come
  back non-zero, so this module reports both as "was not running".

None of the three touches what this is for: a package shipping a rule for
its own service, whose unit lives in ``/usr/lib/systemd/system``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

#: Mageia's own cross-init wrapper, from ``initscripts``.
_SERVICE = "service"

#: Asking a service whether it runs is a question, and a question that
#: takes this long has already failed.  Same order as the existing
#: ``auto_upgrade_policy._systemctl`` helper.
_QUERY_TIMEOUT = 10

#: A restart blocks until the service is back, which is legitimately
#: slower.  Bounded all the same: hooks run after the operation, so
#: hanging here would keep a finished ``urpm install`` from returning.
_RESTART_TIMEOUT = 120


class Result:
    """What became of a restart request."""

    RESTARTED = "restarted"
    #: Was not running, so deliberately left alone.  Also what a service
    #: that does not exist looks like through the wrapper.
    NOT_RUNNING = "not-running"
    #: No wrapper to drive services with.
    NO_SERVICE_TOOL = "no-service-tool"
    #: It was asked, and it refused or timed out.
    FAILED = "failed"


@dataclass(frozen=True)
class ServiceOutcome:
    """The verdict on one service, for the caller to phrase and log.

    ``result`` is one of :class:`Result`'s constants rather than a
    sentence, so the wording and its translation belong to the layer that
    talks to the user, not to this one.
    """

    service: str
    result: str
    message: str = ""

    @property
    def acted(self) -> bool:
        """Did the machine actually change because of this?"""
        return self.result == Result.RESTARTED


def available() -> bool:
    """Is there a wrapper here to drive services with?

    Asked so a machine without one is told as much, instead of being
    handed a failure that looks like the service's fault.
    """
    return shutil.which(_SERVICE) is not None


def is_running(service: str) -> bool:
    """Is that service running right now?

    Asked before restarting so the report can tell a service we brought
    back from one that was not up in the first place. The restart keeps
    its own guard, so the gap between the two costs nothing.
    """
    if not available():
        return False
    return _run([_SERVICE, service, "status"], _QUERY_TIMEOUT) == 0


def try_restart(service: str) -> ServiceOutcome:
    """Restart a service, but only if it was already running.

    Never starts anything. A rule asking to restart a service the
    operator had stopped must not quietly bring it back.
    """
    if not available():
        return ServiceOutcome(service, Result.NO_SERVICE_TOOL,
                              f"{_SERVICE} is not available")

    if not is_running(service):
        return ServiceOutcome(service, Result.NOT_RUNNING)

    command = [_SERVICE, service, "condrestart"]
    code = _run(command, _RESTART_TIMEOUT)
    if code == 0:
        return ServiceOutcome(service, Result.RESTARTED)
    return ServiceOutcome(service, Result.FAILED,
                          f"{' '.join(command)} exited with {code}")


def _run(command: Sequence[str], timeout: int) -> int:
    """Run a command, returning its exit code and never raising.

    A hook runs once an operation is over and recorded; nothing it does
    may take that operation down, so every failure becomes a non-zero
    code the caller can report.
    """
    try:
        completed = subprocess.run(list(command), capture_output=True,
                                   timeout=timeout)
        return completed.returncode
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out after %ds", command[0], timeout)
        return 124  # the shell's own convention for a timeout
    except OSError as exc:
        logger.warning("cannot run %s: %s", command[0], exc)
        return 127  # the shell's own convention for "not found"
