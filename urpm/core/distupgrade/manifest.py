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
    # rpm-helper : scripts sourced by many packages' %pre/%post
    # (add-user, add-group, useradd wrapper, service unit
    # install helpers).  If it's still on the mga N version at
    # Tx B time, the transient window while it's being upgraded
    # loses %pre calls that fire concurrently — rpm silently
    # skips the whole package (no INST_START, no error).  Papoteur
    # beta case : gdm, pipewire, alsa-utils, screen, openvpn all
    # skipped this way in Tx B, all recover if reinstalled after
    # the transaction settles.  Anchoring in Tx A eliminates the
    # race by making sure rpm-helper is on the target version
    # before any Tx B package's %pre runs.
    "rpm-helper",
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


#: What process attributes cannot separate.
#:
#: Under Wayland the compositor and Xwayland run as the session user,
#: children of the session, exactly like Firefox — same uid, same
#: session, not our ancestor.  Nothing in /proc tells them apart, so
#: the display stack is named here rather than guessed at.
#:
#: Expressed as Provides, and deliberately short: these are roles every
#: distribution has, and they move slowly.  A capability nobody
#: provides simply matches nothing.
SESSION_CRITICAL_LOCKS: List[str] = [
    "/usr/bin/Xwayland",
    "/usr/bin/Xorg",
    "/usr/bin/X",
    # Compositors that are also the display server.
    "kwin_wayland",
    "mutter",
    "sway",
    "weston",
    # The path the operator reaches the machine through.
    "openssh-server",
    "NetworkManager",
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


def which_anchors_available(actions, *, resolver, anchors,
                             erased_names=()) -> dict:
    """Return ``{anchor: True/False}`` — will it be there after Tx A?

    That is the only question worth asking.  ``execvp`` restarts urpm
    under the rpm and the Python that Tx A has just committed; an
    anchor missing at that moment leaves the machine stranded
    mid-migration.  So an anchor is satisfied when either

    * the plan installs it from a target medium, or
    * it is already installed and the plan does not remove it.

    The second case is not a loophole, it is the common one.  A
    package the target release ships at the version already on the
    machine produces no action at all: there is nothing to do with it.
    Requiring it in the plan refused ``--to cauldron`` over
    ``rpm-helper``, a noarch bag of shell macros identical in mga10 and
    cauldron, along with three python modules in the same situation.

    What this deliberately does *not* try to re-derive is whether the
    new Python can import those modules.  That is a dependency
    question, and the solver has already answered it: had the new
    python3 needed a rebuilt ``python3-zstandard``, the plan would
    carry one or would not have resolved.

    Two earlier cuts asked the wrong question.  The first required the
    solvable's release tag to match ``f"mga{identity}"`` — but
    ``mgacauldron`` does not exist and never will, so ``--to
    cauldron`` and the documented ``--to cauldron:11`` both refused
    all ten anchors on a plan that contained every one of them.  Nor
    would keying on the numeric have been right: a cauldron repository
    ships ``.mga10`` and ``.mga11`` side by side, whatever has not
    been rebuilt keeping its previous tag, so an anchor legitimately
    served as ``.mga10`` would have been rejected too.  A disttag names
    the release a package was *built for*, never the medium it came
    from.

    Anchors are Provides, not names, so everything here goes through
    ``whatprovides`` — ``python3-pyyaml`` is provided by the package
    Mageia calls ``python3-yaml``.
    """
    plan_ids = {a.solvable_id for a in actions
                if getattr(a, "solvable_id", None) is not None}
    if resolver is None or not plan_ids:
        # Best-effort: treat every anchor as present (older tests /
        # dry-runs that don't have a pool).
        return {name: True for name in anchors}
    pool = resolver.pool
    installed = pool.installed
    erased = set(erased_names or ())
    result: dict = {}
    for name in anchors:
        try:
            dep = pool.Dep(name)
        except Exception:  # noqa: BLE001
            result[name] = False
            continue
        found = False
        for s in pool.whatprovides(dep):
            from_installed = installed is not None and s.repo == installed
            if s.id in plan_ids and not from_installed:
                found = True          # Tx A installs it
                break
            if from_installed and s.name not in erased:
                found = True          # already there, and staying
                break
        result[name] = found
    return result
