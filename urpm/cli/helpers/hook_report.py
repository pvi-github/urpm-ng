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


def run_post_operation_hooks(ops, transaction_id: int) -> None:
    """Apply the rules a finished transaction satisfied, and say so.

    Called once the transaction is committed and recorded.  Nothing here
    may fail the operation, so a rule that goes wrong is reported like
    any other outcome.

    Args:
        ops: The :class:`~urpm.core.operations.PackageOperations` that
            ran the transaction.
        transaction_id: The transaction, as recorded when it began.
    """
    triggered = ops.hooks_triggered_by(transaction_id)
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


def _restart_line(outcome) -> str:
    """One line per service, phrasing a :class:`Result` for a human."""
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
