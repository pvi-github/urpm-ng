"""Rendering for :mod:`urpm.core.audit` outcomes.

Lives in ``helpers/`` because three callers share it and none owns it :
the ``urpm audit`` verb, the ``--check`` tail of the install/erase
pipelines, and distupgrade's Stage 4 summary.  Keeping the layout in
one place means a finding looks identical wherever the operator meets
it, which matters when the same broken symlink shows up first in a
distupgrade report and again later under ``urpm audit``.

Two entry points, differing only in how much they say when there is
nothing to report :

* :func:`render_outcomes` always speaks — used by ``urpm audit``,
  where silence would leave the operator wondering whether anything
  ran at all.
* :func:`render_if_findings` stays quiet on a clean result — used at
  the tail of an install, where a clean check should not add noise to
  output the operator is already reading for other reasons.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from .. import colors
from ...i18n import _, ngettext
from ...core.audit import CheckOutcome, Finding, parse_selection, run_checks


def run_requested_checks(args) -> int:
    """Run whatever ``--check`` asked for, print only what was found.

    The shared tail for the install / upgrade / erase pipelines.  A
    no-op when the flag was absent, so callers can invoke it
    unconditionally instead of each guarding on ``getattr``.

    Findings are reported but never propagated as a failure : the
    transaction itself succeeded, and an inherited dangling symlink is
    a separate matter for the operator to weigh.  A malformed
    ``--check`` value is likewise reported and swallowed here rather
    than raised — the packages are already installed, and turning that
    into a traceback after the fact would be worse than a warning.

    Returns:
        Number of findings printed ; ``0`` when the flag was absent,
        the selection was invalid, or everything came back clean.
    """
    spec = getattr(args, 'check', None)
    if not spec:
        return 0

    try:
        names = parse_selection(spec)
    except ValueError as exc:
        print(colors.warning(f"  {exc}"))
        return 0

    return render_if_findings(run_checks(names))


def render_outcomes(
    outcomes: Sequence[CheckOutcome],
    *,
    show_clean: bool = True,
) -> int:
    """Print *outcomes*, return the total number of findings.

    Args:
        outcomes: What :func:`urpm.core.audit.run_checks` returned.
        show_clean: When True, checks that found nothing still get a
            « clean » line.  When False they are skipped entirely.

    Returns:
        Total findings across every outcome — ``0`` means the system
        is clean, which callers map to their exit code.
    """
    total = sum(len(o.findings) for o in outcomes)

    for outcome in outcomes:
        if outcome.clean:
            if show_clean:
                print(colors.success(
                    _("  {check}: clean").format(check=outcome.name)
                ))
            continue
        _render_one(outcome)

    return total


def render_if_findings(outcomes: Sequence[CheckOutcome]) -> int:
    """Print only the outcomes that found something.

    The post-transaction form : an operator who asked for ``--check``
    and got a clean system does not need a paragraph saying so.
    """
    return render_outcomes(outcomes, show_clean=False)


def _render_one(outcome: CheckOutcome) -> None:
    """Print a single non-clean outcome, grouped by remedy."""
    print()
    print(colors.warning(ngettext(
        "{check}: {n} finding",
        "{check}: {n} findings",
        len(outcome.findings),
    ).format(check=outcome.name, n=len(outcome.findings))))
    print()

    for remedy, findings in _group_by_remedy(outcome.findings).items():
        owner = findings[0].owner
        header = colors.bold(owner) if owner else colors.error(
            _("not owned by any package")
        )
        count_hint = ngettext(
            "{n} item", "{n} items", len(findings),
        ).format(n=len(findings))
        print(f"{header}  {colors.dim(count_hint)}")

        for finding in findings:
            print(f"    {finding.subject} "
                  f"{colors.dim('— ' + finding.detail)}")

        if remedy:
            print(colors.dim(_("    fix: {remedy}").format(remedy=remedy)))
        print()


def _group_by_remedy(findings: List[Finding]) -> Dict[object, List[Finding]]:
    """Bundle findings that share a remedy, unactionable ones last.

    Grouping on the remedy rather than the owner is what lets one
    ``urpm i --reinstall <pkg>`` line stand for a whole cluster of
    broken symlinks instead of repeating per file.  Findings with no
    remedy collapse into a single trailing group : nothing can be
    suggested for them, so they belong after everything the operator
    can actually act on.
    """
    grouped: Dict[object, List[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.remedy, []).append(finding)

    actionable = sorted(
        (k for k in grouped if k is not None), key=str.lower,
    )
    ordered: Dict[object, List[Finding]] = {
        key: grouped[key] for key in actionable
    }
    if None in grouped:
        ordered[None] = grouped[None]
    return ordered
