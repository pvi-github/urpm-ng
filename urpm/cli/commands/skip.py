"""``urpm skip`` : masquer des paquets des médias.

Deux mécanismes cohabitent et ne disent pas la même chose. Un **hold**
gèle un paquet installé dans l'état où il est. Un **skip** masque des
candidats, comme s'ils n'étaient pas déclarés dans la synthesis.
Qu'un paquet installé et masqué cesse de bouger est une conséquence,
pas l'intention.

Le stockage est le fichier d'urpmi lui-même, ``/etc/urpmi/skip.list``,
au format d'urpmi. Une seule source, donc rien à importer, rien à
réconcilier, et les deux outils voient la même liste sur la même
machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...i18n import _, ngettext

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_skip(args, db: "PackageDatabase") -> int:
    """Point d'entrée du verbe, aiguillé sur le sous-verbe."""
    action = getattr(args, "skip_command", None)
    if action == "list":
        return _list(args)
    if action == "add":
        return _add(args)
    if action == "remove":
        return _remove(args)
    # argparse impose le sous-verbe ; ce retour couvre un appel direct.
    return 1


def _path_args(args) -> dict:
    """Le fichier visé : paramètre, jamais emplacement en dur."""
    from pathlib import Path
    explicit = getattr(args, "file", None)
    return {
        "root": getattr(args, "urpm_root", None) or getattr(args, "root", None),
        "path": Path(explicit) if explicit else None,
    }


def _list(args) -> int:
    from .. import colors
    from ...core import skip_list as sl

    where = _path_args(args)
    target = sl.skip_list_path(**where)
    entries = sl.read_entries(target)
    if not entries:
        # "entry" and not "package": what the list holds may be an
        # expression, and the non-empty header two lines down counts
        # entries.  One word per notion.
        print(colors.dim(_("No masked entry ({path})").format(path=target)))
        return 0

    print(colors.bold(ngettext(
        "{count} masked entry ({path}):",
        "{count} masked entries ({path}):",
        len(entries)).format(count=len(entries), path=target)))
    skips = sl.SkipList(entries)
    for entry in entries:
        print(f"  {entry}")
    # Une expression que Python refuse de compiler ne masque rien ;
    # le fichier est édité à la main, le dire vaut mieux que l'ignorer.
    for raw, error in skips.invalid:
        print(colors.warning(_(
            "  {entry}: invalid expression, ignored ({error})"
        ).format(entry=raw, error=error)))
    return 0


def _add(args) -> int:
    from .. import colors
    from ...auth.privileges import require_privileges
    from ...core import skip_list as sl

    where = _path_args(args)
    # Les privilèges servent à écrire dans /etc.  Une liste que
    # l'opérateur désigne lui-même avec --file est la sienne, et le
    # système de fichiers dira s'il n'a pas le droit.
    if where["path"] is None:
        require_privileges(action_id="org.mageia.urpm.media-manage")
    added = 0
    for entry in args.entries:
        entry = entry.strip()
        if not entry:
            continue
        if sl.add_entry(entry, **where):
            print(colors.success(_("{entry} masked").format(entry=entry)))
            added += 1
        else:
            print(colors.dim(_("{entry} was already masked").format(
                entry=entry)))
    return 0 if added or not args.entries else 0


def _remove(args) -> int:
    from .. import colors
    from ...auth.privileges import require_privileges
    from ...core import skip_list as sl

    where = _path_args(args)
    if where["path"] is None:
        require_privileges(action_id="org.mageia.urpm.media-manage")
    failed = False
    for entry in args.entries:
        entry = entry.strip()
        if sl.remove_entry(entry, **where):
            print(colors.success(_("{entry} no longer masked").format(
                entry=entry)))
            continue
        # Couvert par une expression : la retirer demanderait de
        # réécrire une règle que l'opérateur a écrite, ce qui n'est
        # pas à nous de décider.
        covering = sl.covering_patterns(entry, **where)
        if covering:
            print(colors.warning(_(
                "{entry} is not an entry: it is masked by {patterns}"
            ).format(entry=entry, patterns=", ".join(covering))))
            print(colors.dim(_(
                "  Remove that expression itself to unmask it.")))
        else:
            print(colors.warning(_("{entry} was not masked").format(
                entry=entry)))
        failed = True
    return 1 if failed else 0
