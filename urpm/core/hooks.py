"""Declarative actions run once an operation is over.

What defines this mechanism is **when** it acts: after the transaction is
committed, recorded, and the caller has been answered.  Not during.  That
property, and only it, makes an action safe even when it targets the very
process carrying it out.

None of rpm's own hooks can do this.  ``%post``, ``%posttrans`` and file
triggers all run *inside* the transaction, so a scriptlet can neither act
on the process driving it nor rely on the transaction having finished.
The incident that made the gap visible: the ``%post`` of
``urpm-ng-packagekit-backend`` restarted ``urpm-dbus.service``, which is
the very service executing the transaction when the update comes from
Discover.  The package decapitated itself, rpm was cut off halfway, and
the packages ordered after it were never installed.

The model is APT's ``DPkg::Post-Invoke``: actions run by the *package
manager* once dpkg has returned, not by dpkg.  We diverge on one point,
deliberately: APT runs arbitrary command lines, we accept only a closed
vocabulary of verbs (see :class:`Action`), because a drop-in directory
whose contents run as root is a privilege surface.

This module is the reading and matching half only.  It loads rules,
validates them, and says which ones a finished operation satisfies.  It
performs nothing: carrying an action out belongs to the caller.

Rules live in two directories, vendor and administrator, following the
convention Mageia already uses for tmpfiles, sysusers and udev rules::

    /usr/lib/urpm/hooks.d/*.cfg     shipped by packages
    /etc/urpm/hooks.d/*.cfg         administrator overrides

A file in ``/etc`` masks the vendor file of the same name entirely, and
the survivors are read in alphabetical order of file name.  Keeping
shipped rules out of ``/etc`` also keeps them out of
``%config(noreplace)``, so rpm stops leaving ``.rpmnew`` files behind for
something nobody ever edits.

Format, one section per rule::

    [on-completion:declared-restarts]
    watch  = restart-on-completion
    action = report

``watch`` names a capability, and the subject of the action is taken
from its value: a package writes ``Provides: restart-on-completion =
urpm-dbus`` in its own spec and says both "something wants a restart"
and "of that service". One generic rule therefore covers every package
that declares it, and **without that rule the capability does nothing**.
That is the line between declaring a need and instructing an action, and
it is deliberate: restarting sshd or a display manager underneath a live
session is how machines break.

A rule may name a ``service`` of its own, which then wins over the
value. That is how an administrator writes a local policy about a
package that declares nothing, such as restarting a web server when a
TLS library moves.

``on-completion`` says when, and it is carried by the section header
because that is what a reader sees first.  ``posttrans`` and
``post-transaction`` are both avoided: rpm owns the first for something
that runs *inside* the transaction, and our unit is not the rpm
transaction anyway but the whole urpm operation, which may contain
several (Tx A and Tx B of a distupgrade).
"""

from __future__ import annotations

import configparser
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Rules shipped by packages.  Plain files, upgraded cleanly by rpm.
VENDOR_HOOKS_DIR = Path("/usr/lib/urpm/hooks.d")

#: Administrator overrides.  A file here masks the vendor file of the
#: same name.
ADMIN_HOOKS_DIR = Path("/etc/urpm/hooks.d")

#: Prefix of the section header naming the phase.  Only one phase exists
#: today; TX-Shield will bring the entry side, and the header is what
#: will tell the two apart at a glance.
COMPLETION_PREFIX = "on-completion:"

_SUFFIX = ".cfg"


class Operation:
    """The closed vocabulary of what an operation is.

    A rule may narrow itself to some of these with ``on``.  The names
    are the ones the transaction layer already records as
    ``operation_id``, with one addition: a distupgrade goes through
    ``execute_install`` like an upgrade does, so nothing distinguished
    the two until this vocabulary existed.

    The unit is the whole urpm operation, not the rpm transaction: a
    distupgrade runs several (Phase A, Tx A, Tx B) and a rule reasons
    about the release change, not about its internal steps.

    Not every name is reachable yet.  Rules are evaluated after an
    install, an upgrade and a distupgrade; the removal verbs are in the
    vocabulary because the operation type has to name them the day
    those paths evaluate rules too, and freezing the spelling now
    matters more than the wait: third-party packages will write these
    in their own rule files.  A rule narrowed to a verb nothing
    produces simply never fires.
    """

    INSTALL = "install"
    UPGRADE = "upgrade"
    DISTUPGRADE = "distupgrade"
    ERASE = "erase"
    AUTOREMOVE = "autoremove"
    CLEANDEPS = "cleandeps"
    UNDO = "undo"
    ROLLBACK = "rollback"

    ALL = (INSTALL, UPGRADE, DISTUPGRADE, ERASE, AUTOREMOVE, CLEANDEPS,
           UNDO, ROLLBACK)


class Action:
    """The closed vocabulary of things a rule may ask for.

    Reporting is the default on purpose.  ``needrestart`` behaves the
    same way, and for the same reason: restarting dbus, a display manager
    or sshd underneath a live session is a proven way to break a machine.
    """

    REPORT = "report"
    RESTART_SERVICE = "restart-service"
    SYNC_URPMI_CONFIG = "sync-urpmi-config"

    ALL = (REPORT, RESTART_SERVICE, SYNC_URPMI_CONFIG)

    #: Verbs that must not be carried out while a release change is
    #: only half in place.  Restarting a service under a session that
    #: still runs the previous release is the incident this module was
    #: written for; a distupgrade ends in a reboot anyway.  The rule
    #: still fires and still reports, so the operator reads that
    #: something wanted a restart and why it did not happen.
    NOT_DURING_DISTUPGRADE = (RESTART_SERVICE,)


@dataclass(frozen=True)
class Hook:
    """One rule: what to watch, and what to do about it.

    ``watch`` is a capability, never a package name: a name gets renamed,
    a ``Provides`` is a stable contract.

    The subject of the action comes from one of two places, and it is one
    model read two ways. Normally it is **the value of the watched
    capability**, so a package states its own subject:
    ``restart-on-completion = urpm-dbus``. When the rule names a
    ``service`` of its own, that one wins instead, which is how an
    administrator writes a local policy about a package that declares
    nothing.
    """

    identifier: str
    watch: str
    action: str = Action.REPORT
    service: str = ""
    only_on_full_success: bool = False
    on: FrozenSet[str] = frozenset()
    source: Optional[Path] = None

    def matches(self, outcome: "OperationOutcome") -> bool:
        """Does this rule fire for that finished operation?

        ``on`` is a restriction a rule puts on itself, not a whitelist
        it has to be on: an empty one matches every operation.  Safety
        during a release change comes from declining the restart, not
        from hiding the rule, so that the operator still reads that
        something wanted one.
        """
        if self.only_on_full_success and not outcome.fully_successful:
            return False
        if self.on and outcome.operation and outcome.operation not in self.on:
            return False
        return self.watch in outcome.provides

    def subjects(self, outcome: "OperationOutcome") -> FrozenSet[str]:
        """What the action applies to for that operation.

        Empty when the rule names no service and the capability was
        declared without a value: the rule fired but says nothing about
        what to act on. The caller reports that rather than guessing.

        """
        if self.service:
            return frozenset({self.service})
        return frozenset(outcome.provides.get(self.watch, frozenset()))


@dataclass(frozen=True)
class OperationOutcome:
    """What an operation actually did, as the rules get to see it.

    ``provides`` maps every capability the packages **really installed**
    bring to the values seen for it, never what the plan intended.  A
    partly applied transaction therefore needs no special flag: a package
    that did not reach the disk brings nothing.  The per-package verdict
    is already in the history, written by
    :meth:`~urpm.core.db.history.HistoryMixin.record_action_end` as
    ``done``, ``failed`` or ``skipped``.

    The values are kept because a capability can carry its own subject,
    which is what lets a package say *which* service to restart without
    anyone shipping a rule for it.

    ``fully_successful`` covers the rarer need of a rule whose action
    only makes sense if the whole operation went through.  APT draws the
    same line with ``DPkg::Post-Invoke`` against
    ``DPkg::Post-Invoke-Success``.

    ``operation`` names the whole urpm command, from :class:`Operation`.
    A rule narrows itself to some of them with ``on``; the acting side
    reads it too, to decline a service restart while a release change
    is only half in place.  Empty when the caller did not say, which
    every rule then matches.
    """

    provides: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    fully_successful: bool = True
    operation: str = ""


@dataclass(frozen=True)
class TriggeredHook:
    """A rule that fired, together with what it applies to.

    Pairing the two is what keeps the deciding half pure: the subjects
    are read off the outcome at matching time, so whoever acts later
    needs neither the outcome nor the rpmdb, and whoever only displays
    can name the services without reading anything either.

    ``subjects`` is empty when the rule named no service and the watched
    capability carried no value.  The rule fired but says nothing to act
    on, and that is reported rather than guessed.

    ``operation`` travels along for the same reason: whoever acts needs
    to know a release change is under way, and asking the outcome again
    would restore the coupling this class exists to remove.
    """

    hook: Hook
    subjects: FrozenSet[str] = frozenset()
    operation: str = ""


@dataclass
class LoadReport:
    """Rules kept, and why the others were not.

    Returned rather than logged only, so a future ``urpm hooks --list``
    can show an administrator what was ignored without re-reading
    anything.
    """

    hooks: List[Hook] = field(default_factory=list)
    rejected: List[Tuple[Path, str]] = field(default_factory=list)


def load_hooks(vendor_dir: Path = VENDOR_HOOKS_DIR,
               admin_dir: Path = ADMIN_HOOKS_DIR) -> LoadReport:
    """Read every rule file, keep the sound ones.

    Administrator files mask vendor files of the same name; what remains
    is read in alphabetical order of file name, as tmpfiles and sysusers
    do.

    A file that cannot be trusted or parsed is dropped with a reason, and
    never raises: a malformed rule left by a third party must not take a
    package operation down with it.
    """
    report = LoadReport()
    for path in _files_to_read(vendor_dir, admin_dir):
        refusal = _refuse_untrusted(path)
        if refusal:
            logger.warning("ignoring hook file %s: %s", path, refusal)
            report.rejected.append((path, refusal))
            continue
        _read_file(path, report)
    report.hooks.sort(key=lambda hook: hook.identifier)
    return report


def hooks_for(outcome: OperationOutcome,
              hooks: Iterable[Hook]) -> List[TriggeredHook]:
    """Select the rules a finished operation satisfies, in order.

    Each one comes back with its subjects already resolved, so nothing
    downstream has to hold on to the outcome.
    """
    return [TriggeredHook(hook, hook.subjects(outcome), outcome.operation)
            for hook in hooks if hook.matches(outcome)]


def _files_to_read(vendor_dir: Path, admin_dir: Path) -> List[Path]:
    """Apply the masking rule and return the files in reading order."""
    chosen: Dict[str, Path] = {}
    for directory in (vendor_dir, admin_dir):  # admin wins, read second
        for path in _cfg_files(directory):
            chosen[path.name] = path
    return [chosen[name] for name in sorted(chosen)]


def _cfg_files(directory: Path) -> List[Path]:
    """List the ``.cfg`` files of a directory, tolerating its absence.

    Each entry is examined on its own: the directory can be read while a
    single entry cannot, and one unreadable name must not cost us the
    rest of the directory.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    files = []
    for path in entries:
        if path.suffix != _SUFFIX:
            continue
        try:
            if path.is_file():
                files.append(path)
        except OSError:
            continue  # vanished or unreachable; _refuse_untrusted would too
    return files


def _refuse_untrusted(path: Path) -> Optional[str]:
    """Say why a file must not be trusted, or ``None`` when it is fine.

    Its contents drive actions taken as root, so ownership and write
    permissions are checked rather than assumed.
    """
    try:
        info = path.stat()
    except OSError as exc:
        return f"cannot be read ({exc.strerror})"
    if info.st_uid != 0:
        return f"not owned by root (uid {info.st_uid})"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"writable by group or others (mode {info.st_mode & 0o777:04o})"
    return None


def _read_file(path: Path, report: LoadReport) -> None:
    """Parse one file, appending its rules and its refusals."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    try:
        parser.read(str(path), encoding="utf-8")
    except (configparser.Error, OSError) as exc:
        logger.warning("ignoring hook file %s: %s", path, exc)
        report.rejected.append((path, f"unreadable ({exc})"))
        return

    for section in parser.sections():
        if not section.startswith(COMPLETION_PREFIX):
            # Another phase, or a typo.  Not ours to judge, not ours to
            # run: leave it alone rather than guess.
            continue
        identifier = section[len(COMPLETION_PREFIX):].strip()
        hook, refusal = _build_hook(identifier, parser[section], path)
        if hook is None:
            logger.warning("ignoring hook [%s] in %s: %s",
                           section, path, refusal)
            report.rejected.append((path, f"[{section}] {refusal}"))
        else:
            report.hooks.append(hook)


def _build_hook(identifier: str, values, source: Path
                ) -> Tuple[Optional[Hook], str]:
    """Turn one parsed section into a rule, or say why it cannot be."""
    if not identifier:
        return None, "no identifier in the section header"

    watch = values.get("watch", "").strip()
    if not watch:
        return None, "no watch, the rule could never fire"
    if len(watch.split()) > 1:
        # One capability per rule.  Watching several would make the
        # subject ambiguous as soon as it is taken from a value.
        return None, f"watch takes one capability, got {watch!r}"

    action = values.get("action", Action.REPORT).strip()
    if action not in Action.ALL:
        # Skipping rather than falling back: a rule asking for something
        # we cannot do must not silently become something else.
        return None, (f"unknown action {action!r}, expected one of "
                      + ", ".join(Action.ALL))

    raw_on = values.get("on", "").replace(",", " ").split()
    unknown = [o for o in raw_on if o not in Operation.ALL]
    if unknown:
        # Same reasoning as an unknown action: a rule meant for
        # something we do not know must not silently widen to
        # everything, which is what an empty ``on`` means.
        return None, (f"unknown operation {unknown[0]!r} in on, expected "
                      "one of " + ", ".join(Operation.ALL))

    # ``service`` stays optional even for a restart: the usual case takes
    # the subject from the watched capability's value.  A rule that ends
    # up with no subject at all is reported at run time, not refused
    # here, because whether a subject exists depends on the operation.
    return Hook(
        identifier=identifier,
        watch=watch,
        action=action,
        service=values.get("service", "").strip(),
        only_on_full_success=_as_bool(values.get("only-on-full-success")),
        on=frozenset(raw_on),
        source=source,
    ), ""


def _as_bool(raw: Optional[str]) -> bool:
    """Read a boolean the way configparser does, defaulting to false."""
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "yes", "true", "on")
