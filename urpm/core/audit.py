"""System integrity checks — registry, checks, and the ``links`` check.

Package operations can leave a system subtly wrong in ways rpm itself
does not consider an error.  The motivating case : a mga9→mga10
distupgrade left ``/usr/lib64/libproxy.so.1`` pointing at
``libproxy.so.1.0.0``, a name mga10's ``lib64proxy1`` no longer ships.
rpm's transaction succeeded — the dangling symlink is a *consequence*
of the upgrade, not a failure of it — and firefox simply died at
startup with ``libproxy.so.1: cannot open shared object file``.  The
user had no diagnostic short of running ``ldd`` by hand.

This module is a **registry of named checks** rather than a single
scanner, so the same machinery serves three callers without either of
them knowing what a check actually does :

* ``urpm audit`` runs every registered check on demand.
* ``urpm install|upgrade|erase --check <names>`` runs a selection
  after the transaction.
* ``urpm distupgrade`` runs them unconditionally in Stage 4 — a
  cross-release upgrade is exactly where this class of breakage
  appears, so it is not left to the operator to remember.

Adding a check means writing one class and calling :func:`register`.
Nothing else in the codebase changes : the CLI flag, the ``all``
selector, and the report rendering pick it up automatically.

Checks report, they do not repair.  Each finding carries a *remedy*
string — the command the operator would run — so the decision to act
stays with them.  An automated ``--fix`` is deliberately out of scope
for now.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol

from ..i18n import _


# ── Data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One thing a check found wrong.

    Attributes:
        subject: What is at fault — an absolute path for the ``links``
            check, but checks are free to use any stable identifier.
        detail: Short description of what is wrong with *subject*,
            already translated.  Rendered next to it.
        owner: Name of the rpm package that owns *subject*, or ``None``
            when nothing claims it (« orphan » — created by hand, or
            its package was removed without cleanup).
        remedy: Command the operator can run to fix this finding, or
            ``None`` when the check has no suggestion.  Findings that
            share a remedy are grouped under it when rendered.
    """

    subject: str
    detail: str
    owner: Optional[str] = None
    remedy: Optional[str] = None


@dataclass(frozen=True)
class CheckOutcome:
    """What one check produced on one run.

    A check that ran and found nothing yields an outcome with an empty
    ``findings`` list — distinct from a check that was never selected,
    which produces no outcome at all.  Callers rendering a report use
    that difference to say « links: clean » rather than stay silent.
    """

    name: str
    findings: List[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when the check found nothing to report."""
        return not self.findings


class Check(Protocol):
    """What a registered check has to provide.

    Deliberately minimal : a name for the CLI selector, a one-line
    summary for ``--help`` and report headers, and a ``run`` that
    returns findings.  A check must never raise for an ordinary
    « system looks odd » condition — that is what findings are for.
    Genuine internal errors may propagate ; :func:`run_checks` does
    not swallow them, because a check that cannot run is a bug worth
    surfacing rather than a silent no-op.
    """

    name: str
    summary: str

    def run(self) -> CheckOutcome: ...


# ── Registry ───────────────────────────────────────────────────────


_REGISTRY: Dict[str, Check] = {}

#: Selector meaning « every registered check ».  Accepted anywhere a
#: check name is, including inside a comma-separated list.
ALL = "all"


def register(check: Check) -> None:
    """Add *check* to the registry, keyed on its ``name``.

    Raises:
        ValueError: if the name is already taken, or collides with the
            :data:`ALL` selector.  Both would make a CLI selection
            ambiguous, so they fail loudly at import time rather than
            silently shadow.
    """
    if check.name == ALL:
        raise ValueError(f"check name {ALL!r} is reserved for the selector")
    if check.name in _REGISTRY:
        raise ValueError(f"check {check.name!r} is already registered")
    _REGISTRY[check.name] = check


def available() -> List[Check]:
    """Every registered check, sorted by name for stable output."""
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]


def available_names() -> List[str]:
    """Names of every registered check, sorted."""
    return sorted(_REGISTRY)


def parse_selection(spec: str) -> List[str]:
    """Turn a ``--check`` argument into a list of check names.

    Accepts a comma-separated list (``links,orphans``), the
    :data:`ALL` selector, or a mix (``all`` anywhere in the list wins).
    Whitespace around names is tolerated and empty segments are
    ignored, so ``--check "links, "`` behaves.

    Args:
        spec: Raw value the user passed to ``--check``.

    Returns:
        Check names in registry order, deduplicated.

    Raises:
        ValueError: on an unknown name, with the valid ones listed —
            a typo should tell the operator what they meant, not fail
            with a bare traceback.
    """
    requested = [part.strip() for part in spec.split(",")]
    requested = [part for part in requested if part]
    if not requested or ALL in requested:
        return available_names()

    unknown = [name for name in requested if name not in _REGISTRY]
    if unknown:
        raise ValueError(
            _("Unknown check(s): {unknown}. Available: {available}, {all_}")
            .format(
                unknown=", ".join(sorted(unknown)),
                available=", ".join(available_names()),
                all_=ALL,
            )
        )
    # Registry order rather than user order : report layout stays
    # identical whichever way the operator typed the list.
    return [name for name in available_names() if name in requested]


def run_checks(names: Iterable[str]) -> List[CheckOutcome]:
    """Run the named checks in registry order, return their outcomes.

    Unknown names are skipped rather than raising : callers that went
    through :func:`parse_selection` have already validated, and the
    internal callers (Stage 4) pass :func:`available_names` directly.
    """
    wanted = [n for n in available_names() if n in set(names)]
    return [_REGISTRY[name].run() for name in wanted]


def run_all() -> List[CheckOutcome]:
    """Run every registered check.  Used by ``urpm audit`` and by
    distupgrade Stage 4, both of which check everything."""
    return run_checks(available_names())


# ── The ``links`` check ────────────────────────────────────────────


# Roots the links check walks.  Covers the two failure modes seen in
# practice :
#
# * shared library soname deps :
#   ``/usr/lib``, ``/usr/lib64`` — the libproxy case
# * ``update-alternatives`` chains :
#   ``/usr/bin``, ``/usr/sbin``, ``/usr/libexec`` — e.g.
#   ``/usr/bin/gcc → /etc/alternatives/gcc → /usr/bin/gcc-15``
#   with the alternative left stale by a mid-upgrade crash.
#
# ``/etc/alternatives`` itself is intentionally out : those symlinks
# are administrative state and a raw scan there would flag every
# not-yet-selected alternative slot.
DEFAULT_ROOTS: tuple[Path, ...] = (
    Path("/usr/lib"),
    Path("/usr/lib64"),
    Path("/usr/bin"),
    Path("/usr/sbin"),
    Path("/usr/libexec"),
)


class BrokenLinksCheck:
    """Flag symlinks in system directories whose target is missing.

    Reinstalling the owning package replays its payload, which restores
    the symlink to whatever the RPM originally shipped — hence the
    per-package ``urpm i --reinstall`` remedy.  Findings that share an
    owner therefore share a remedy and collapse into one suggestion
    when rendered.
    """

    name = "links"
    summary = _("Broken symlinks in system directories")

    def __init__(self, roots: Iterable[Path] = DEFAULT_ROOTS):
        self.roots = tuple(roots)

    def run(self) -> CheckOutcome:
        dangling = find_broken_symlinks(self.roots)
        owners = attribute_owners([str(p) for p, _t in dangling])
        findings = [
            Finding(
                subject=str(path),
                detail=_("target missing: {target}").format(target=target),
                owner=owners.get(str(path)),
                remedy=(
                    f"urpm i --reinstall {owners[str(path)]}"
                    if owners.get(str(path)) else None
                ),
            )
            for path, target in dangling
        ]
        return CheckOutcome(name=self.name, findings=findings)


def find_broken_symlinks(
    roots: Iterable[Path] = DEFAULT_ROOTS,
) -> List[tuple[Path, str]]:
    """Walk *roots* recursively, return ``(path, target)`` per dangling
    symlink.

    A symlink is « broken » when :func:`os.path.exists` returns
    ``False`` on it — that call follows the whole chain, so multi-hop
    cases (``foo → bar → baz`` with any link missing) are caught by the
    same predicate.

    *target* is what ``readlink`` returned verbatim, relative or
    absolute as the RPM shipped it, so the diagnostic shows the
    operator what the package actually declared rather than a resolved
    path they would then have to map back.

    Roots absent from the current system are skipped silently — not
    every installation has ``/usr/libexec``.
    """
    hits: List[tuple[Path, str]] = []
    for root in roots:
        if not root.exists() and not root.is_symlink():
            continue
        try:
            walker = os.walk(root, followlinks=False)
        except OSError:
            continue
        for dirpath, _dirnames, filenames in walker:
            for name in filenames:
                full = Path(dirpath) / name
                if not full.is_symlink() or os.path.exists(full):
                    continue
                try:
                    hits.append((full, os.readlink(full)))
                except OSError:
                    continue
    return hits


def attribute_owners(paths: List[str]) -> Dict[str, str]:
    """Map each path in *paths* to the rpm package that owns it.

    Paths owned by no package are simply absent from the result, so
    callers distinguish « orphan » with a plain ``.get()``.

    Uses the rpm bindings through :mod:`urpm.core.rpmdb` — one
    ``TransactionSet`` opened, queried per path, closed cleanly.
    Per-file queries are the right shape here : one match per unique
    absolute path, no fanout, and the alignment is exact.  Shelling out
    to ``rpm -qf`` on a batch would interleave success and error lines
    across stdout and stderr, making the path→owner mapping guesswork.

    Returns an empty mapping when the rpm bindings are unavailable —
    the findings are still worth reporting without owner attribution.
    """
    if not paths:
        return {}
    try:
        from .rpmdb import open_ts
        import rpm  # noqa: PLC0415 — local, matches the module contract
    except ImportError:
        return {}

    owners: Dict[str, str] = {}
    with open_ts("/") as ts:
        for path in paths:
            owner = _owner_via_ts(ts, rpm, path)
            if owner is not None:
                owners[path] = owner
    return owners


def _owner_via_ts(ts, rpm, path: str) -> Optional[str]:
    """``NAME`` of the rpm owning *path*, ``None`` when unowned.

    Wraps the ``dbMatch('basenames', …)`` iterator so callers don't
    repeat the take-first-or-none idiom, and decodes the bytes rpm
    hands back on some builds.
    """
    try:
        mi = ts.dbMatch('basenames', path)
    except (KeyError, TypeError):
        return None
    for hdr in mi:
        name = hdr[rpm.RPMTAG_NAME]
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        return name
    return None


register(BrokenLinksCheck())
