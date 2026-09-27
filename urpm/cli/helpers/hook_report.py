"""Running the post-operation rules and telling the operator about them.

Lives in ``helpers/`` because the three pipelines that finish an
operation share it and none owns it: install, upgrade and erase.  The
rules themselves live in :mod:`urpm.core.hooks`, which decides and
phrases nothing; the wording belongs here, with the rest of what the CLI
says.

Reporting names the services and hands over the command rather than
running it, because that is the prudent half of the mechanism and the
operator is the one who knows whether now is a good moment.  The command
we print is ``service <name> restart``: Mageia's own wrapper, which works
whatever the init is, so the advice does not quietly assume systemd.
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from .. import colors
from ...i18n import _, ngettext
from ...core.hooks import Action, TriggeredHook
from ...core.init_system import Result


def run_post_operation_hooks(ops, transaction_id: int,
                             operation: str = "") -> None:
    """Apply the rules a finished transaction satisfied, and say so.

    Called once the transaction is committed and recorded.  Nothing here
    may fail the operation, so a rule that goes wrong is reported like
    any other outcome.

    Args:
        ops: The :class:`~urpm.core.operations.PackageOperations` that
            ran the transaction.
        transaction_id: The transaction, as recorded when it began.
        operation: What the whole command was, from
            :class:`~urpm.core.hooks.Operation`.  Empty when the caller
            does not distinguish, which every rule then matches.
    """
    triggered = ops.hooks_triggered_by(transaction_id, operation)
    if not triggered:
        return  # the common case: no rule on the machine matched

    for line in render_hooks(triggered, ops.run_hooks(triggered)):
        print(line)


def render_hooks(triggered: Sequence[TriggeredHook],
                 results: Sequence[Tuple[TriggeredHook, List[Any]]]
                 ) -> List[str]:
    """Turn what the rules asked for, and what came of it, into lines.

    Kept apart from the printing so it can be tested on its text alone,
    and so distupgrade can fold the same wording into its own report.
    """
    lines: List[str] = []
    lines += _render_reports(triggered)
    for entry, outcomes in results:
        lines += _render_restarts(entry, outcomes)
        lines += _render_urpmi_sync(entry, outcomes)
    return lines


def _render_reports(triggered: Sequence[TriggeredHook]) -> List[str]:
    """Name the services running replaced code, and how to restart them.

    All the reporting rules are merged into one block: the operator
    cares about the list of services, not about which rule file named
    each of them.
    """
    services = sorted({service
                       for entry in triggered
                       if entry.hook.action == Action.REPORT
                       for service in entry.subjects})
    if not services:
        return []

    lines = ["  " + colors.warning(ngettext(
        "This service is running replaced code: {services}",
        "These services are running replaced code: {services}",
        len(services)).format(services=", ".join(services)))]
    lines.append("    " + ngettext(
        "Restart it when convenient:",
        "Restart them when convenient:",
        len(services)))
    lines += [f"      service {service} restart" for service in services]
    return lines


def _render_restarts(entry: TriggeredHook,
                     outcomes: Sequence[Any]) -> List[str]:
    """Say what became of a rule that asked for a restart."""
    if entry.hook.action != Action.RESTART_SERVICE:
        return []

    if not outcomes:
        # The rule fired, but neither it nor the package named anything.
        # Saying so beats guessing, and points at the rule to fix.
        return ["  " + colors.warning(_(
            "Rule {rule} asked for a restart without naming a service"
        ).format(rule=entry.hook.identifier))]

    return [_restart_line(outcome) for outcome in outcomes]


def _render_urpmi_sync(entry: TriggeredHook,
                       outcomes: Sequence[Any]) -> List[str]:
    """Say what became of a rule that asked for urpmi's config to follow.

    Silent when there was nothing to move: a configuration already on
    the installed release is not news, and this runs at the end of a
    report the operator is already reading for defects.
    """
    if entry.hook.action != Action.SYNC_URPMI_CONFIG:
        return []

    lines: List[str] = []
    for report in outcomes:
        if report.errors:
            lines.append("  " + colors.warning(_(
                "Could not move urpmi's media to Mageia {tgt}: {reason}"
            ).format(tgt=report.target_release or "?",
                     reason="; ".join(report.errors))))
            continue
        if not report.changed:
            continue
        lines.append("  " + colors.success(ngettext(
            "urpmi: {n} medium URL moved to Mageia {tgt}",
            "urpmi: {n} media URLs moved to Mageia {tgt}",
            report.rewritten).format(n=report.rewritten,
                                     tgt=report.target_release)))
        if report.backup:
            lines.append("    " + colors.dim(_(
                "urpmi's previous configuration kept as {path}"
            ).format(path=report.backup)))
        # Named rather than silently skipped: the shapes a release can
        # take in a URL are not enumerable, so an entry no rule could
        # move is handed to the operator instead of being lost.
        if report.unhandled:
            lines.append("    " + colors.warning(ngettext(
                "{n} entry still names Mageia {src} and was left alone:",
                "{n} entries still name Mageia {src} and were left alone:",
                len(report.unhandled)).format(n=len(report.unhandled),
                                              src=report.source_release)))
            for name in report.unhandled:
                lines.append("      " + name)
    return lines


def _restart_line(outcome) -> str:
    """One line per service, phrasing a :class:`Result` for a human."""
    if outcome.result == Result.DECLINED:
        # The rule is not at fault, it is out of context: the running
        # session is still on the previous release.  Naming the context
        # matters more than the refusal, so the operator knows this is
        # a deliberate policy and not a failure to look into.
        return "  " + colors.dim(_(
            "not restarting {service} (context = distupgrade)"
        ).format(service=outcome.service))

    if outcome.result == Result.RESTARTED:
        return "  " + colors.success(
            _("{service} restarted").format(service=outcome.service))

    if outcome.result == Result.NOT_RUNNING:
        # Deliberate: try-restart never starts what the operator stopped.
        return "  " + _("{service} was not running, left alone").format(
            service=outcome.service)

    if outcome.result == Result.NO_SERVICE_TOOL:
        return "  " + colors.warning(_(
            "Cannot restart {service}: no service command on this system"
        ).format(service=outcome.service))

    return "  " + colors.warning(_(
        "Could not restart {service}, try: service {service} restart"
    ).format(service=outcome.service))
