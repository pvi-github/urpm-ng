"""Distupgrade manifest defaults (SPEC_DISTUPGRADE §6.1).

Two lists drive the Tx A / Tx B split :

- ``TRANSACTION_A_PROVIDES`` : every Provides that MUST land in
  Tx A because either urpm-ng itself or the smoke-test subprocess
  depends on it.  Full ABI-critical closure : rpm, python3, python3
  modules urpm-ng loads at import time, glibc, urpm-ng-core.
- ``BOOT_CRITICAL_LOCKS_BASE`` : Provides that MUST NEVER be
  sacrificed (SOLVER_LOCK across every solver pass) — the boot
  hard-floor.  UEFI additions live in ``BOOT_CRITICAL_LOCKS_UEFI``
  and are conditionally merged when ``/sys/firmware/efi`` exists.

Every identifier is a **Provides** symbol per
[[feedback-provides-over-names]] — resolved via
``pool.what_provides()`` so a Mageia rename post-v1 doesn't break
this list.
"""

from __future__ import annotations

import os
from typing import List


TRANSACTION_A_PROVIDES: List[str] = [
    "rpm",
    "python3",
    "python3-rpm",
    "python3-solv",
    "python3-pyyaml",
    "python3-curl",
    "python3-zstandard",
    "glibc",
    "urpm-ng-core",
]


BOOT_CRITICAL_LOCKS_BASE: List[str] = [
    "kernel-server-latest",
    "kernel-desktop-latest",
    "kernel-linus-latest",
    "grub2",
    "grub2-common",
    "dracut",
    "systemd",
    "sysvinit",
    "openrc",
    "glibc",
    "bash",
    "coreutils",
    "util-linux",
    "filesystem",
    "setup",
]


BOOT_CRITICAL_LOCKS_UEFI: List[str] = [
    "grub2-efi",
    "efibootmgr",
    "efi-filesystem",
    "shim",
]


def boot_critical_locks(*, uefi_present: bool = None) -> List[str]:
    """Return the effective SOLVER_LOCK Provides list.

    UEFI additions are merged only when ``/sys/firmware/efi``
    exists (or ``uefi_present`` is set explicitly for tests).
    """
    if uefi_present is None:
        uefi_present = os.path.isdir("/sys/firmware/efi")
    if uefi_present:
        return BOOT_CRITICAL_LOCKS_BASE + BOOT_CRITICAL_LOCKS_UEFI
    return BOOT_CRITICAL_LOCKS_BASE


def split_plan_for_tx_a_and_b(
    actions,
    *,
    resolver=None,
    tx_a_provides: List[str] = TRANSACTION_A_PROVIDES,
) -> "tuple[list[str], list[str]]":
    """Return ``(tx_a_plan_nevras, tx_b_plan_nevras)`` from the full solve.

    ``actions`` is the ordered list of :class:`PackageAction` from
    :meth:`Resolver.resolve_distupgrade` — each carries ``nevra``
    and ``solvable_id`` populated by the resolver.  The output split
    is by ``solvable_id`` : Tx A gets the **transitive Requires
    closure** of the packages providing ``tx_a_provides``, Tx B gets
    the rest.

    The closure matters because rpm gets ``nodeps=True`` in Tx A —
    installing ``python3`` without its ``lib64python3.13`` Requires
    would put the process's own Python in a half-loaded state before
    the smoke test / execvp handoff.  We resolve Requires against
    the plan (never pull in something Stage 2 didn't download).

    Without ``resolver`` (legacy path), falls back to a name-match
    against ``tx_a_provides``.  This branch WILL under-include and
    should only be used by unit tests that construct plans without a
    real Pool ; production callers pass ``resolver=stage2["resolver"]``.

    Ordering is preserved within each output list — libsolv already
    topological-sorted the plan, we mustn't reshuffle.
    """
    tx_a_nevras: List[str] = []
    tx_b_nevras: List[str] = []

    # Uniform input handling : accept either a list of PackageAction
    # (production) or a list of NEVRA strings (legacy tests).
    def _is_nevra_string(x) -> bool:
        return isinstance(x, str)

    legacy_strings = actions and _is_nevra_string(actions[0])

    if legacy_strings or resolver is None:
        # Name-only fallback — correct on Mageia where the Provides
        # symbols happen to be package Names for every anchor.
        allowed_names = set(tx_a_provides)
        for entry in actions:
            nevra = entry if _is_nevra_string(entry) else entry.nevra
            name = (nevra.rsplit("-", 2)[0]
                    if nevra.count("-") >= 2 else nevra)
            if name in allowed_names:
                tx_a_nevras.append(nevra)
            else:
                tx_b_nevras.append(nevra)
        return tx_a_nevras, tx_b_nevras

    # ── Full closure via libsolv (production path). ─────────────────
    import solv

    pool = resolver.pool
    # Index by solvable_id → action for order preservation.
    plan_id_to_action = {}
    for a in actions:
        if getattr(a, "solvable_id", None) is not None:
            plan_id_to_action[a.solvable_id] = a
    plan_id_set = set(plan_id_to_action)

    # Seed with every plan solvable that provides one of the anchors.
    anchor_ids: set = set()
    for sym in tx_a_provides:
        try:
            dep = pool.Dep(sym)
        except Exception:  # noqa: BLE001
            continue
        for s in pool.whatprovides(dep):
            if s.id in plan_id_set:
                anchor_ids.add(s.id)

    # Transitive Requires closure, intersected with the plan.
    closure: set = set(anchor_ids)
    to_visit = list(anchor_ids)
    while to_visit:
        current_id = to_visit.pop()
        s = pool.solvables[current_id]
        try:
            requires = s.lookup_deparray(solv.SOLVABLE_REQUIRES)
        except Exception:  # noqa: BLE001
            continue
        for dep in requires or []:
            for provider in pool.whatprovides(dep):
                if provider.id in plan_id_set and provider.id not in closure:
                    closure.add(provider.id)
                    to_visit.append(provider.id)

    # Split preserving the resolver's topological order.
    for a in actions:
        sid = getattr(a, "solvable_id", None)
        if sid is not None and sid in closure:
            tx_a_nevras.append(a.nevra)
        else:
            tx_b_nevras.append(a.nevra)
    return tx_a_nevras, tx_b_nevras
