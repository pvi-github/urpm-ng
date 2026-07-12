"""Iterative --with-suggests resolution shared by install and upgrade.

The CLI ``install`` and ``upgrade`` commands both need to iterate
``resolver.find_available_suggests`` until the graph closes (a suggest
might itself declare Requires that recommend/suggest other packages),
handle the interactive alternatives dialog, and gather the resulting
``PackageAction`` list.

Historically this block lived only in ``install.py``; ``upgrade.py``
parsed ``--with-suggests`` for symmetry but never consumed it, silently
dropping the feature on the upgrade path.  Extracting the logic here
fixes that asymmetry and removes ~180 lines of internal duplication in
the process.

Public entry point: :func:`resolve_iterative_suggests`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import solv

from ...core.resolution.pool import lookup_all_requires
from ...core.resolver import (
    Alternative,
    InstallReason,
    PackageAction,
    TransactionType,
)
from ...i18n import _


class SuggestsAborted(Exception):
    """Raised when the user aborts the interactive alternatives dialog.

    The caller (install/upgrade CLI command) should catch this and
    return the appropriate exit code (1) rather than let the exception
    propagate to the top-level handler.
    """


def _action_from_solvable(resolver, s) -> PackageAction:
    """Build a SUGGESTED-reason PackageAction from a libsolv solvable.

    Factored out because the historical code repeated this dataclass
    construction four times (single-provider auto-select, user "All",
    user index choice, auto-mode) with identical fields — a copy-paste
    festival that made adding a new field (like ``solvable_id``) a
    four-site edit.
    """
    pkg_info = resolver._solvable_to_pkg.get(s.id, {})
    return PackageAction(
        action=TransactionType.INSTALL,
        name=s.name,
        evr=s.evr,
        arch=s.arch,
        nevra=f"{s.name}-{s.evr}.{s.arch}",
        size=s.size,
        media_name=pkg_info.get('media_name', ''),
        reason=InstallReason.SUGGESTED,
        solvable_id=s.id,
    )


def _pick_first_non_system_solvable(resolver, pkg_name: str):
    """Return the first non-@System solvable providing ``pkg_name``, or None."""
    sel = resolver.pool.select(pkg_name, solv.Selection.SELECTION_NAME)
    for s in sel.solvables():
        if s.repo and s.repo.name != '@System':
            return s
    return None


def _walk_requires_for_next_check(resolver, suggest_action: PackageAction,
                                  checked_packages: set) -> List[str]:
    """Discover packages required by ``suggest_action`` whose names should
    be fed into the next iteration.

    A suggest may itself Require another package that has its own
    Suggests/Recommends — e.g. ``konq-plugins`` requires ``konqueror``,
    which suggests ``konqueror-handbook``.  Without this walk the second
    hop never surfaces.
    """
    discovered: List[str] = []
    s = _pick_first_non_system_solvable(resolver, suggest_action.name)
    if s is None:
        return discovered

    for dep in lookup_all_requires(s):
        dep_str = str(dep).split()[0]
        if dep_str.startswith(('rpmlib(', '/', 'config(')):
            continue
        dep_obj = resolver.pool.Dep(dep_str)
        for provider in resolver.pool.whatprovides(dep_obj):
            if provider.repo and provider.repo.name != '@System':
                if provider.name.lower() not in checked_packages:
                    checked_packages.add(provider.name.lower())
                    discovered.append(provider.name)
                break
    return discovered


def _resolve_alternatives_interactive(resolver, new_alternatives: List[Alternative],
                                      choices: Dict[str, str],
                                      preferences,
                                      new_suggests: List[PackageAction],
                                      checked_packages: set,
                                      new_packages_from_alternatives: List[str]) -> None:
    """Interactive alternatives dialog for one iteration.

    Mutates ``choices``, ``new_suggests`` and
    ``new_packages_from_alternatives`` in place.  Raises
    :class:`SuggestsAborted` if the user cancels the dialog.
    """
    for alt in new_alternatives:
        if alt.capability in choices:
            continue

        filtered = preferences.filter_providers(alt.providers)
        if not filtered:
            continue

        # Single candidate after preference filter: no dialog needed.
        if len(filtered) == 1:
            _adopt_provider(resolver, alt.capability, filtered[0],
                            choices, new_suggests, checked_packages,
                            new_packages_from_alternatives)
            continue

        # Multiple candidates: ask the user.
        print(f"\n{alt.capability} ({alt.required_by}):")
        for i, provider in enumerate(filtered, 1):
            print(f"  {i}) {provider}")
        print(f"  {len(filtered) + 1}) " + _("All"))

        try:
            choice = input("\n" + _("Choice [1]: ")).strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print(_("\nAborted"))
            raise SuggestsAborted from None

        try:
            if choice == str(len(filtered) + 1):
                # "All": adopt every provider.
                for prov_name in filtered:
                    _adopt_provider(resolver, alt.capability, prov_name,
                                    choices, new_suggests, checked_packages,
                                    new_packages_from_alternatives)
            else:
                idx = int(choice) - 1
                if 0 <= idx < len(filtered):
                    _adopt_provider(resolver, alt.capability, filtered[idx],
                                    choices, new_suggests, checked_packages,
                                    new_packages_from_alternatives)
        except ValueError:
            print(_("\nAborted"))
            raise SuggestsAborted from None


def _resolve_alternatives_auto(resolver, new_alternatives: List[Alternative],
                               choices: Dict[str, str],
                               preferences,
                               new_suggests: List[PackageAction],
                               checked_packages: set,
                               new_packages_from_alternatives: List[str]) -> None:
    """Non-interactive alternatives: pick the first candidate after
    preference filtering (list is already sorted by missing-deps count).
    """
    for alt in new_alternatives:
        if alt.capability in choices:
            continue

        filtered = preferences.filter_providers(alt.providers)
        if not filtered:
            continue

        _adopt_provider(resolver, alt.capability, filtered[0],
                        choices, new_suggests, checked_packages,
                        new_packages_from_alternatives)


def _adopt_provider(resolver, capability: str, pkg_name: str,
                    choices: Dict[str, str],
                    new_suggests: List[PackageAction],
                    checked_packages: set,
                    new_packages_from_alternatives: List[str]) -> None:
    """Register ``pkg_name`` as the chosen provider of ``capability`` and
    add the corresponding PackageAction to ``new_suggests`` if it is not
    already tracked.
    """
    choices[capability] = pkg_name
    s = _pick_first_non_system_solvable(resolver, pkg_name)
    if s is None:
        return
    if s.name.lower() in checked_packages:
        return
    new_suggests.append(_action_from_solvable(resolver, s))
    new_packages_from_alternatives.append(s.name)


def resolve_iterative_suggests(
    resolver: Any,
    initial_actions: List[PackageAction],
    choices: Dict[str, str],
    preferences: Any,
    auto: bool,
    max_iterations: int = 10,
) -> Tuple[List[PackageAction], List[Alternative]]:
    """Resolve the transitive closure of ``--with-suggests``.

    Iterates :meth:`Resolver.find_available_suggests` up to
    ``max_iterations`` times, extending the checked-packages set each
    round with newly-adopted suggests and their non-``@System``
    Requires providers (so second-hop Suggests surface too).

    Args:
        resolver: The active :class:`urpm.core.resolver.Resolver`
            instance (its pool must already be created).
        initial_actions: The transaction actions produced by
            ``resolver.resolve_install`` / ``resolve_upgrade`` — used
            both as the starting set of "already checked" names and as
            NEVRA seeds for the first ``find_available_suggests`` call.
        choices: Dict of already-recorded alternative choices
            (``capability`` → provider name).  Mutated in place as new
            choices are made during the iteration.
        preferences: A :class:`PreferencesMatcher` used to filter the
            provider list for each unresolved alternative.
        auto: When True, use the non-interactive alternative-picking
            policy (first candidate after filter).  When False, prompt
            the user for each ambiguous capability.
        max_iterations: Safety bound on the loop.  Ten is deep enough
            for every case observed in production and cheap to keep.

    Returns:
        Tuple ``(suggests, alternatives_left)`` where ``suggests`` is
        the list of PackageAction to append to the transaction, and
        ``alternatives_left`` is the residue of Alternatives that
        neither the user nor auto-mode resolved (surfaced back to the
        CLI so it can print a diagnostic).

    Raises:
        SuggestsAborted: The user cancelled the interactive dialog.
    """
    all_to_install = [a.name for a in initial_actions]
    suggests: List[PackageAction] = []
    alternatives_left: List[Alternative] = []
    # NEVRAs let find_available_suggests pin the exact resolved version
    # for each seed; fall back to bare names for actions that somehow
    # lack a NEVRA (should not happen after the resolver refactor).
    packages_to_check = ([a.nevra for a in initial_actions if a.nevra]
                         or list(all_to_install))
    checked_packages = {p.lower() for p in all_to_install}

    for _iteration in range(max_iterations):
        new_suggests, new_alternatives = resolver.find_available_suggests(
            packages_to_check,
            choices=choices,
            resolved_packages=list(checked_packages),
        )
        if not new_suggests and not new_alternatives:
            break

        new_packages_from_alternatives: List[str] = []
        if new_alternatives:
            if auto:
                _resolve_alternatives_auto(
                    resolver, new_alternatives, choices, preferences,
                    new_suggests, checked_packages,
                    new_packages_from_alternatives,
                )
            else:
                _resolve_alternatives_interactive(
                    resolver, new_alternatives, choices, preferences,
                    new_suggests, checked_packages,
                    new_packages_from_alternatives,
                )
            # Anything still unresolved this round travels back to the
            # caller so it can display the residue.
            alternatives_left.extend(
                alt for alt in new_alternatives if alt.capability not in choices
            )

        next_packages: List[str] = []
        for suggest_action in new_suggests:
            if suggest_action.name.lower() in checked_packages:
                continue
            suggests.append(suggest_action)
            checked_packages.add(suggest_action.name.lower())
            next_packages.append(suggest_action.name)
            # Walk one level of Requires to expose second-hop Suggests.
            next_packages.extend(
                _walk_requires_for_next_check(
                    resolver, suggest_action, checked_packages,
                )
            )

        for pkg_name in new_packages_from_alternatives:
            if pkg_name.lower() not in checked_packages:
                checked_packages.add(pkg_name.lower())
                next_packages.append(pkg_name)

        if not next_packages:
            break
        packages_to_check = next_packages

    return suggests, alternatives_left
