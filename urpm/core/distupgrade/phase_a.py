"""Stage 0 Phase A : metadata refresh + delayed daily upgrade.

SPEC_DISTUPGRADE §4.0 « Phase A » — before touching anything
release-specific, catch up whatever a normal daily ``urpm upgrade``
would have done today.  Two steps :

- :func:`run_phase_a_refresh` — pure metadata sync (non-mutant on
  rpmdb) via :func:`urpm.core.sync.sync_all_media`.
- :func:`run_phase_a_upgrade` — actual upgrade on the current
  release, chaining the existing :class:`PackageOperations` primitives
  the CLI uses.

Nothing bespoke : all the pipeline reuses production primitives
(:class:`Resolver`, :class:`PackageOperations`, :class:`TransactionQueue`).
The distupgrade-specific value is just wiring them together with
sane, non-interactive defaults + wrapping failures in
:class:`PhaseAError` so the Stage 0 orchestrator can abort cleanly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

if TYPE_CHECKING:
    from ..database import PackageDatabase


logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str, str, int, int], None]


class PhaseAError(Exception):
    """Raised when Phase A cannot proceed."""


def run_phase_a_refresh(
    db: "PackageDatabase",
    *,
    progress_callback: Optional[ProgressCallback] = None,
    force: bool = False,
    max_workers: int = 4,
) -> List[Tuple[str, object]]:
    """Refresh every enabled media's metadata.

    Delegates to :func:`urpm.core.sync.sync_all_media` — same
    machinery that ``urpm media update`` calls at the CLI top-level.
    """
    from ..sync import sync_all_media

    logger.info("Phase A refresh: sync_all_media(force=%s)", force)
    try:
        results = sync_all_media(
            db,
            progress_callback=progress_callback,
            force=force,
            max_workers=max_workers,
        )
    except Exception as exc:  # noqa: BLE001
        raise PhaseAError(
            f"Phase A metadata refresh failed: {exc}") from exc
    return results


def run_phase_a_upgrade(
    db: "PackageDatabase",
    *,
    progress_callback: Optional[Callable] = None,
    install_progress_callback: Optional[Callable] = None,
    on_plan_computed: Optional[Callable] = None,
) -> int:
    """Run a plain ``urpm upgrade`` on the current release.

    Direct composition of the same primitives ``cmd_upgrade`` uses,
    without the interactive prompts / colour rendering / progress
    displays that make sense only on a terminal :

    1. :meth:`Resolver.resolve_upgrade` — full-system upgrade plan.
    2. :meth:`PackageOperations.build_download_items` — turn actions
       into downloadable items.
    3. :meth:`PackageOperations.download_packages` — pipeline sync
       (parallel + peers + resumable Range + GPG verify).
    4. :meth:`PackageOperations.begin_transaction` — history row.
    5. :meth:`PackageOperations.execute_upgrade` — commit via
       :class:`TransactionQueue`.
    6. :meth:`PackageOperations.complete_transaction` +
       :meth:`mark_dependencies`.

    Zero interactivity — Phase A is fire-and-forget by contract.

    Returns 0 on success ; raises :class:`PhaseAError` with an
    actionable message on any failure so the Stage 0 orchestrator
    can abort cleanly.
    """
    import platform

    from ..operations import InstallOptions, PackageOperations
    from ..resolver import Resolver

    logger.info("Phase A upgrade: upgrade against current release")

    # Distupgrade code paths must be explicit about arch — a
    # mis-detected arch would silently mis-upgrade.
    arch = platform.machine()
    resolver = Resolver(db, arch=arch)

    try:
        result = resolver.resolve_upgrade()
    except Exception as exc:  # noqa: BLE001
        raise PhaseAError(
            f"Phase A upgrade solve failed: {exc}") from exc

    if not result.success:
        problems = "; ".join(result.problems) if result.problems \
            else "resolver reported failure"
        raise PhaseAError(
            f"Phase A upgrade did not converge: {problems}.  "
            f"Fix conflicts with `urpm upgrade` before retrying "
            f"`urpm distupgrade`.")

    if not result.actions:
        logger.info("Phase A upgrade: nothing to do")
        if on_plan_computed is not None:
            on_plan_computed(0, 0, 0)
        return 0

    # Announce the plan upfront so the user knows what's coming — the
    # CLI turns this into « Phase A : N updates pending... » before
    # the download bar shows up.
    action_count = len(result.actions)
    total_dl = sum(
        (getattr(a, "filesize", 0) or getattr(a, "size", 0) or 0)
        for a in result.actions
        if a.action.value in ("install", "upgrade", "reinstall"))
    total_inst = sum(
        getattr(a, "size", 0) or 0
        for a in result.actions
        if a.action.value in ("install", "upgrade", "reinstall"))
    if on_plan_computed is not None:
        on_plan_computed(action_count, total_dl, total_inst)

    ops = PackageOperations(db)
    download_items, local_paths = ops.build_download_items(
        result.actions, resolver,
    )

    rpm_paths: list = list(local_paths or [])
    if download_items:
        dl_results, _dl, _cached, _peer = ops.download_packages(
            download_items,
            options=InstallOptions(),
            progress_callback=progress_callback,
        )
        for r in dl_results:
            if not r.success:
                raise PhaseAError(
                    f"Phase A upgrade download failed for "
                    f"{r.item.filename}: {r.error}")
            if r.path is not None:
                rpm_paths.append(str(r.path))

    remove_names = [a.name for a in result.actions
                    if a.action.value == 'remove']
    if not rpm_paths and not remove_names:
        logger.info("Phase A upgrade: nothing to install")
        return 0

    tx_id = ops.begin_transaction('upgrade', 'urpm distupgrade phase A',
                                  result.actions)
    try:
        ops.execute_upgrade(
            rpm_paths=rpm_paths,
            erase_names=remove_names or None,
            options=InstallOptions(),
            progress_callback=install_progress_callback,
            full_sync=True,   # need scriptlets to complete before Stage 1
        )
    except Exception as exc:  # noqa: BLE001
        ops.abort_transaction(tx_id)
        raise PhaseAError(
            f"Phase A upgrade failed at commit: {exc}.  See "
            f"/var/log/urpm/*.log for detailed error output.") from exc
    ops.mark_dependencies(resolver, result.actions)
    ops.complete_transaction(tx_id)
    return 0
