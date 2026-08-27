"""Stage 2 — distupgrade solve + pre-load download (SPEC_DISTUPGRADE §4.2).

Two steps :

- **Solve** — delegated to :meth:`Resolver.resolve_distupgrade`, which
  uses the ``SOLVER_DISTUPGRADE | SOLVER_SOLVABLE_ALL`` job with
  ``SOLVER_FLAG_DUP_ALLOW_NAMECHANGE``.  Returns a standard
  :class:`Resolution` — same shape ``resolve_upgrade`` returns.
- **Download** — delegated to :meth:`PackageOperations.build_download_items`
  + :meth:`download_packages`.  Same pipeline ``cmd_install`` /
  ``cmd_upgrade`` use — HTTPS + pinned IP + parallel workers + GPG
  verify.

Contract with Stage 1 : after Stage 1's media swap, ``Resolver(db, arch)``
builds a pool that reflects the target release (source media flipped
to ``enabled=0``, target media inserted + enabled).  Stage 2 doesn't
need any bespoke pool-loading machinery — the DB is the truth.

State bookkeeping (`.state.stage` bump) is Stage 2's own responsibility.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from ..database import PackageDatabase
    from ..resolver import Resolution
    from .version import ReleaseIdentity


logger = logging.getLogger(__name__)


class Stage2Error(Exception):
    """Raised for any Stage 2 failure."""


class Stage2Aborted(Exception):
    """Raised when the user declines the Stage 2 confirmation prompt.

    Not an error — the caller catches this and exits cleanly.
    ``.state`` is not persisted so a re-run starts fresh from solve.
    """


class Stage2EmptyPlanError(Stage2Error):
    """Raised when the resolver returns zero actions.

    A distupgrade that reaches Stage 2 with an empty plan is *never*
    a legitimate outcome — Stage 0 already gated on the source ≠
    target release identity, and Stage 1 has just transposed the
    media rows.  An empty plan therefore means the resolver held
    every candidate (typically a broken require or a Conflicts/
    Obsoletes chain against a critical package like kernel).

    Proceeding would let Stage 4 flag the mga N media for deferred
    deletion while the machine is still entirely on mga N — the
    reboot-time cleanup would then brick the system.  The CLI
    catches this and auto-rolls back Stage 1 so the user is left in
    exactly the pre-distupgrade state.

    Carries the ``Resolution`` for the caller to render the
    ``skipped`` jobs (which package was held, why).
    """

    def __init__(self, result):
        self.result = result
        super().__init__(
            "distupgrade solve returned zero actions — every candidate "
            "was held.  See ``result.skipped`` for the diagnosis."
        )


def solve_distupgrade(
    db: "PackageDatabase",
    target: "ReleaseIdentity",
    *,
    arch: Optional[str] = None,
) -> "Resolution":
    """Thin wrapper around :meth:`Resolver.resolve_distupgrade`.

    Kept as a module-level function so callers don't need to know the
    resolver's constructor signature ; the actual solve strategy lives
    on the resolver.  ``target`` is accepted for logging symmetry
    with Stage 0/1 — the pool state is what actually drives the solve.
    """
    import platform

    from ..resolver import Resolver

    if arch is None:
        arch = platform.machine()

    resolver = Resolver(db, arch=arch)
    result = resolver.resolve_distupgrade(target_version=target.identity)
    if not result.success:
        details = "\n".join(result.problems)
        raise Stage2Error(
            f"libsolv reported {len(result.problems)} problem(s) "
            f"during distupgrade solve for target "
            f"{target.display()}:\n{details}"
        )
    logger.info(
        "distupgrade solve: %d action(s), target=%s",
        len(result.actions), target.display(),
    )
    # Attach the resolver on the result so callers can hand it to
    # ``PackageOperations.build_download_items`` — mirrors what
    # ``cmd_upgrade`` does when it holds onto its resolver post-solve.
    result._resolver = resolver  # type: ignore[attr-defined]
    return result


def download_plan(
    db: "PackageDatabase",
    result: "Resolution",
    *,
    urpm_root: Optional[str] = None,
    progress_callback=None,
    target_version: Optional[str] = None,
    target_arch: Optional[str] = None,
) -> dict:
    """Fetch every RPM the ``result.actions`` requires.

    Delegates entirely to :class:`PackageOperations` — the same
    pipeline used by ``urpm install`` / ``urpm upgrade``.  Returns a
    dict summary with ``nevra_to_path`` mapping the successful
    downloads to their on-disk RPM paths (consumed by Stage 3).

    ``progress_callback`` : forwarded verbatim to
    :meth:`PackageOperations.download_packages`.  See the signature
    documented in :meth:`Downloader.download_all` — same one
    ``urpm install`` wires to :class:`display.DownloadProgressDisplay`.
    """
    from ..operations import InstallOptions, PackageOperations

    summary: Dict[str, object] = {
        "requested": len(result.actions),
        "downloaded": 0,
        "already_present": 0,
        "failed": [],
        "nevra_to_path": {},
    }
    if not result.actions:
        return summary

    resolver = getattr(result, "_resolver", None)
    if resolver is None:
        raise Stage2Error(
            "download_plan received a Resolution without an attached "
            "resolver ; call solve_distupgrade() to obtain one.")

    ops = PackageOperations(db)
    download_items, local_paths = ops.build_download_items(
        result.actions, resolver,
    )
    # Distupgrade never installs local RPMs — the local_paths branch is
    # empty in practice, but if a hook ever adds one we'd surface it.
    for p in local_paths:
        # Path passthrough : the caller resolves NEVRA→path from the
        # action list directly since local RPMs don't go through the
        # download pipeline.
        summary["nevra_to_path"][Path(p).stem] = Path(p)

    if not download_items:
        return summary

    dl_results, downloaded, cached, peer_stats = ops.download_packages(
        download_items,
        options=InstallOptions(),
        progress_callback=progress_callback,
        urpm_root=urpm_root,
        # Threaded through so ``query_peers_have`` can filter each
        # LAN peer's inventory to the release we're actually asking
        # for.  Without this the peer discovery returned an empty
        # availability map on every distupgrade download and all
        # traffic went upstream.
        target_version=target_version,
        target_arch=target_arch,
    )
    summary["downloaded"] = downloaded
    summary["already_present"] = cached
    # P2P split so the CLI can render « X from peers, Y from mirrors »
    # like ``urpm i``/``urpm u`` do.
    summary["from_peers"] = peer_stats.get("from_peers", 0) if peer_stats else 0
    summary["from_upstream"] = peer_stats.get("from_upstream", 0) if peer_stats else 0
    for r in dl_results:
        if r.success and r.path is not None:
            nevra = f"{r.item.name}-{r.item.version}-{r.item.release}.{r.item.arch}"
            summary["nevra_to_path"][nevra] = Path(r.path)
        else:
            summary["failed"].append(
                f"{r.item.name}-{r.item.version}-{r.item.release}"
                f".{r.item.arch}"
            )
    return summary


def run_stage2(
    db: "PackageDatabase",
    *,
    target: "ReleaseIdentity",
    arch: Optional[str] = None,
    confirm_callback=None,
    download_progress_callback=None,
) -> dict:
    """Orchestrate solve + download for Stage 2.

    Bumps ``.state.stage`` to ``stage2_running`` on entry and
    ``downloaded`` on success.  Returns a summary the CLI renders +
    hands to Stage 3.

    ``confirm_callback(result: Resolution) -> bool`` — optional gate
    called after solve, before download.  Returning ``False`` raises
    :class:`Stage2Aborted` ; the caller exits cleanly and ``.state``
    is not persisted.  Returning ``True`` (or leaving the callback
    ``None``) proceeds with download.
    """
    from .state import read_state, write_state

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prior = read_state(db) or {}
    prior.update({
        "started_at": prior.get("started_at", started_at),
        "stage": "stage2_running",
    })
    write_state(prior, db)

    logger.info("Stage 2 : solve + download for target %s",
                target.display())
    try:
        result = solve_distupgrade(db, target, arch=arch)
    except Stage2Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Stage2Error(f"distupgrade solve failed: {exc}") from exc

    # SAFETY GUARD (SPEC_DISTUPGRADE §4.2 / 0.9.1 hotfix) : an empty
    # plan means libsolv held every candidate.  Never proceed — that
    # would carry us to Stage 4 where the mga N media get flagged
    # for deferred deletion, bricking the still-on-mga-N machine at
    # reboot.  Caller catches this and rolls back Stage 1.
    if not result.actions:
        raise Stage2EmptyPlanError(result)

    if confirm_callback is not None and not confirm_callback(result):
        raise Stage2Aborted()

    download_summary = download_plan(
        db, result,
        progress_callback=download_progress_callback,
        target_version=target.identity,
        target_arch=arch,
    )

    prior.update({"stage": "downloaded"})
    write_state(prior, db)

    plan_head = [a.nevra for a in result.actions[:5]]
    return {
        "plan_size": len(result.actions),
        "plan": result.actions,
        "plan_head": plan_head,
        "download": download_summary,
        "nevra_to_path": download_summary["nevra_to_path"],
        "resolver": getattr(result, "_resolver", None),
    }
