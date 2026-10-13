"""Asking before a hold is bypassed.

A hold is a decision the user took earlier, and naming the package on
the command line is how they take it back: the resolver exempts an
explicitly named package from its own lock, the way urpmi's
``select.pm`` clears the skip flag for an explicit selection.

Doing that silently would make the one mechanism meant to keep a
package still defeatable by a command that looks entirely ordinary,
weeks after the hold was placed and its reason forgotten.  So the
override is confirmed rather than assumed, and the reason recorded
with the hold is printed back, which is what it was recorded for.
"""

import sys
from typing import List, Tuple

from ...i18n import _, confirm_yes, ngettext
from .. import colors
from ...core.resolver import _extract_name


def requested_holds(db, package_names) -> List[Tuple[str, str]]:
    """The held packages among those the user named.

    Args:
        db: The package database.
        package_names: What the user typed, names or NEVRAs.

    Returns:
        ``[(name, reason)]`` in the order the user typed them, so the
        question lists their own request rather than the database's
        ordering.  Empty when nothing named is held, which is the
        normal case and costs one query.
    """
    try:
        held = db.get_held_packages_set()
    except Exception:  # noqa: BLE001 - a hold must not break a command
        return []
    if not held:
        return []
    reasons = {}
    try:
        reasons = {row["package_name"]: (row.get("reason") or "")
                   for row in db.list_holds()}
    except Exception:  # noqa: BLE001 - the reason is a comfort, not a gate
        pass

    by_lower = {name.lower(): name for name in held}
    found: List[Tuple[str, str]] = []
    seen = set()
    for spec in package_names or []:
        name = by_lower.get(_extract_name(str(spec)).lower())
        if name and name not in seen:
            seen.add(name)
            found.append((name, reasons.get(name, "")))
    return found


def confirm_hold_override(db, package_names, *, auto: bool = False,
                          operation: str = "upgrade") -> bool:
    """Ask before the holds on *package_names* are bypassed.

    Args:
        db: The package database.
        package_names: What the user typed.
        auto: ``--auto`` / ``-y``, the user saying yes in advance.
        operation: ``"upgrade"`` or ``"install"``, for the wording.

    Returns:
        ``True`` to carry on, ``False`` to abort.  Without a terminal
        and without ``-y`` the answer is no: a script that names a held
        package has most likely not been told about the hold, and
        bypassing it unattended is the one case where nobody is there
        to notice.
    """
    holds = requested_holds(db, package_names)
    if not holds:
        return True

    print(colors.warning(ngettext(
        "This package is held:",
        "These packages are held:",
        len(holds))))
    for name, reason in holds:
        if reason:
            # Parentheses need no translation, and a msgid made of
            # nothing but punctuation gives a translator nothing to go
            # on; the reason itself is the user's own words.
            print("  " + colors.warning(name) + " " + colors.dim(f"({reason})"))
        else:
            print("  " + colors.warning(name))

    if auto:
        print(colors.dim(ngettext(
            "Naming it overrides the hold (-y given).",
            "Naming them overrides the holds (-y given).",
            len(holds))))
        return True

    if not sys.stdin.isatty():
        print(colors.error(ngettext(
            "Refusing to bypass the hold without a confirmation.",
            "Refusing to bypass the holds without a confirmation.",
            len(holds))))
        print(colors.dim(_("Release it with '{cmd} <package>', or pass -y.")
                         .format(cmd="urpm hold remove")))
        return False

    if operation == "install":
        prompt = ngettext(
            "\nInstall it anyway, overriding the hold? [y/N] ",
            "\nInstall them anyway, overriding the holds? [y/N] ",
            len(holds))
    else:
        prompt = ngettext(
            "\nUpgrade it anyway, overriding the hold? [y/N] ",
            "\nUpgrade them anyway, overriding the holds? [y/N] ",
            len(holds))
    try:
        if confirm_yes(input(prompt)):
            return True
    except (KeyboardInterrupt, EOFError):
        print()
    print(_("Aborted."))
    return False
