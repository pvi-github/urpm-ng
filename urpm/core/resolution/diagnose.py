"""Human-readable diagnostics for unsatisfied dependencies.

This module turns raw resolver-error payloads (libsolv ``Problem`` objects
or rpmlib ``ts.check()`` tuples) into a small classified record that
explains *why* a dependency is unsatisfied, by cross-referencing the
synthesis metadata of the active media:

* ``missing`` — no package in any active medium provides the capability.
* ``version_mismatch`` — providers exist but none match the version
  constraint; the available EVRs are collected for the message.
* ``excluded`` — a provider is present in raw synthesis but is filtered
  out at pool-construction time (excludemedia / arch / Mageia release).
  Only detectable when a libsolv pool is supplied.
* ``unknown`` — fallback when none of the above applies.

The classifier intentionally takes a ``PackageDatabase`` (always
available) and an optional ``pool`` (only available when the caller is
the libsolv resolver). Callers reached from the rpmlib path
(:mod:`urpm.core.transaction_queue`) get the first three kinds via the
synthesis-only path; the optional pool refines the ``excluded`` case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ...i18n import _, ngettext
from ..rpm import decode_rpmdep_sense, decode_rpmsense_flags
from .orphans import _SYNTHESIS_SENSE_MAP, _provider_satisfies


@dataclass
class DepIssue:
    """Classified payload for an unsatisfied dependency.

    Attributes:
        kind: One of ``"missing"``, ``"version_mismatch"``,
            ``"excluded"``, ``"unknown"``.
        dep_name: Capability name that could not be satisfied.
        dep_op: Comparison operator (``""``, ``"<"``, ``"<="``, ``"="``,
            ``">="``, ``">"``); empty for an unversioned require.
        dep_version: EVR string the require asked for; empty for
            unversioned.
        sense_label: rpmlib sense label (``"requires"``, ``"conflicts"``,
            …) when classifying an rpmlib tuple; empty for libsolv.
        requester: NEVRA of the package whose dependency is unsatisfied,
            when known.
        available_versions: For ``version_mismatch``, the list of EVRs
            actually provided by the active media (sorted, deduplicated).
        reason: Free-form explanation for ``excluded`` (e.g. medium
            excluded, arch filtered).
    """

    kind: str
    dep_name: str
    dep_op: str = ""
    dep_version: str = ""
    sense_label: str = ""
    requester: str = ""
    available_versions: List[str] = field(default_factory=list)
    reason: str = ""


def _evr_of(provider: dict) -> str:
    """Build an ``epoch:version-release`` string from a provider dict.

    The DB rows returned by :meth:`PackageDatabase.whatprovides` carry
    ``version`` and ``release`` but no separate ``epoch`` column; epoch
    defaults to ``0`` and is normalised by :func:`_provider_satisfies`.
    """
    version = provider.get("version", "") or ""
    release = provider.get("release", "") or ""
    if release:
        return f"{version}-{release}"
    return version


def classify_unsatisfied_dep(
    db,
    dep_name: str,
    dep_op: str = "",
    dep_version: str = "",
    *,
    pool=None,
    sense_label: str = "",
    requester: str = "",
) -> DepIssue:
    """Classify a single unsatisfied dependency against active media.

    Args:
        db: A :class:`PackageDatabase` (read-only access to
            ``whatprovides`` is enough).
        dep_name: Capability name (e.g. ``kwin-x11`` or
            ``perl(Foo::Bar)``).
        dep_op: Comparison operator string. Empty when unversioned.
        dep_version: EVR string the require asked for. Empty when
            unversioned.
        pool: Optional ``solv.Pool`` used to detect the ``excluded``
            kind by comparing raw synthesis providers against the
            pool-filtered set. When ``None``, ``excluded`` is never
            returned.
        sense_label: Optional rpmlib sense label, propagated into the
            returned :class:`DepIssue` so the formatter can phrase the
            error correctly (``requires`` vs ``conflicts``).
        requester: Optional NEVRA of the package whose dep is broken.

    Returns:
        A :class:`DepIssue` describing the failure mode.
    """
    raw = db.whatprovides(dep_name)

    if not raw:
        return DepIssue(
            kind="missing",
            dep_name=dep_name,
            dep_op=dep_op,
            dep_version=dep_version,
            sense_label=sense_label,
            requester=requester,
        )

    sense = _SYNTHESIS_SENSE_MAP.get(dep_op, 0) if dep_op else 0
    if sense:
        satisfied_evrs = [
            _evr_of(p)
            for p in raw
            if _provider_satisfies(_evr_of(p), sense, dep_version)
        ]
        if not satisfied_evrs:
            seen = []
            for p in raw:
                evr = _evr_of(p)
                if evr and evr not in seen:
                    seen.append(evr)
            return DepIssue(
                kind="version_mismatch",
                dep_name=dep_name,
                dep_op=dep_op,
                dep_version=dep_version,
                sense_label=sense_label,
                requester=requester,
                available_versions=seen,
            )
    else:
        satisfied_evrs = [_evr_of(p) for p in raw]

    if pool is not None:
        try:
            filtered = pool.whatprovides(pool.Dep(dep_name))
            if not list(filtered):
                return DepIssue(
                    kind="excluded",
                    dep_name=dep_name,
                    dep_op=dep_op,
                    dep_version=dep_version,
                    sense_label=sense_label,
                    requester=requester,
                    reason="provider filtered out at pool construction",
                )
        except Exception:
            pass

    return DepIssue(
        kind="unknown",
        dep_name=dep_name,
        dep_op=dep_op,
        dep_version=dep_version,
        sense_label=sense_label,
        requester=requester,
    )


# ---------------------------------------------------------------------------
# Human-readable rendering and adapters
# ---------------------------------------------------------------------------


def _dep_clause(issue: DepIssue) -> str:
    """Render the ``name [op version]`` clause for a DepIssue."""
    if issue.dep_op and issue.dep_version:
        return f"{issue.dep_name} {issue.dep_op} {issue.dep_version}"
    return issue.dep_name


def format_dependency_issue(issue: DepIssue) -> str:
    """Render a :class:`DepIssue` as a single human, i18n string.

    The wording is intentionally short and self-contained so it can be
    surfaced verbatim by both the libsolv resolver and the rpmlib
    transaction layer; the requester (when known) is prepended so the
    user sees *which* package raised the error.
    """
    clause = _dep_clause(issue)
    is_conflict = issue.sense_label == "conflicts"

    if is_conflict:
        body = _("conflits avec {clause}").format(clause=clause)
    elif issue.kind == "missing":
        body = _("requiert {clause} ; ce paquet n'existe dans aucun media activé").format(clause=clause)
    elif issue.kind == "version_mismatch":
        if issue.available_versions:
            avail = ", ".join(issue.available_versions)
            body = ngettext(
                "requiert {clause} ; seule la version {avail} est disponible",
                "requiert {clause} ; seules les versions {avail} sont disponibles",
                len(issue.available_versions),
            ).format(clause=clause, avail=avail)
        else:
            body = _("requiert {clause} ; aucune version disponible ne satisfait la contrainte").format(clause=clause)
    elif issue.kind == "excluded":
        reason = issue.reason or _("filtré par la configuration des media")
        body = _("requiert {clause} ; provider présent mais {reason}").format(clause=clause, reason=reason)
    else:
        body = _("requiert {clause} ; raison non déterminée").format(clause=clause)

    if issue.requester:
        return f"{issue.requester} {body}"
    return body


def from_rpmlib_tuple(t, db=None, pool=None) -> DepIssue:
    """Build a :class:`DepIssue` from an ``rpm.ts.check()`` tuple.

    The tuple shape is ``((N, V, R), (depN, depV), flags, suggest, sense)``.
    ``flags`` is an RPMSENSE bitmask (version compare bits) and ``sense``
    is an RPMDEP_SENSE label.

    Args:
        t: The tuple as produced by ``rpm.TransactionSet.check()``.
        db: Optional :class:`PackageDatabase`; when supplied the dep is
            classified against active synthesis media. Without it the
            issue defaults to ``kind="unknown"``.
        pool: Optional ``solv.Pool`` for ``excluded``-kind refinement.
    """
    try:
        (n, v, r), (dep_name, dep_version), flags, _suggest, sense = t
    except (TypeError, ValueError):
        return DepIssue(kind="unknown", dep_name=str(t))

    requester = f"{n}-{v}-{r}" if n else ""
    sense_label = decode_rpmdep_sense(sense)
    dep_op = decode_rpmsense_flags(flags) if flags else ""

    if db is None:
        return DepIssue(
            kind="unknown",
            dep_name=dep_name or "",
            dep_op=dep_op,
            dep_version=dep_version or "",
            sense_label=sense_label,
            requester=requester,
        )
    return classify_unsatisfied_dep(
        db,
        dep_name or "",
        dep_op,
        dep_version or "",
        pool=pool,
        sense_label=sense_label,
        requester=requester,
    )


def from_libsolv_problem(problem, pool, db=None) -> DepIssue:
    """Build a :class:`DepIssue` from a libsolv ``Problem``.

    Uses :meth:`Problem.findproblemrule` and :meth:`ProblemRule.info`
    when available to extract the requester and the unsatisfied
    dependency; falls back to ``str(problem)`` otherwise. The pool is
    forwarded to :func:`classify_unsatisfied_dep` so the ``excluded``
    kind can be detected.
    """
    requester = ""
    dep_name = ""
    dep_op = ""
    dep_version = ""

    try:
        rule = problem.findproblemrule()
        info = rule.info() if rule is not None else None
    except Exception:
        info = None

    if info is not None:
        try:
            solvable = getattr(info, "solvable", None)
            if solvable is not None:
                requester = str(solvable)
        except Exception:
            pass
        try:
            dep = getattr(info, "dep", None)
            if dep is not None:
                dep_str = str(dep)
                for op in (">=", "<=", "==", "=", ">", "<"):
                    if f" {op} " in dep_str:
                        head, version = dep_str.split(f" {op} ", 1)
                        dep_name = head.strip()
                        dep_op = "=" if op == "==" else op
                        dep_version = version.strip()
                        break
                else:
                    dep_name = dep_str.strip()
        except Exception:
            pass

    if not dep_name:
        return DepIssue(kind="unknown", dep_name=str(problem), requester=requester)

    if db is None:
        return DepIssue(
            kind="unknown",
            dep_name=dep_name,
            dep_op=dep_op,
            dep_version=dep_version,
            requester=requester,
        )
    return classify_unsatisfied_dep(
        db,
        dep_name,
        dep_op,
        dep_version,
        pool=pool,
        requester=requester,
    )
