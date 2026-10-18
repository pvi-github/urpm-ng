"""Reading a mounted disc's catalogue and joining it to the database.

:mod:`urpm.core.removable` answers « where is the disc ».  This module
answers « what does it carry, and which media we already know does
each tree belong to ».

The second question is the hard one, and the answer is not the names.
A Mageia 10 pressing calls its trees ``Core Release (Installer)`` and
``Nonfree Release (Installer)`` while the mirror calls the same
packages ``Core Release`` and ``Nonfree Release``.  Upstream added
that suffix precisely because urpmi has no way to express « another
source for a media I already have » and must create a separate media
instead ; urpm-ng does have one, so the suffix is a name collision
avoided for a problem we do not have.  Matching on display names
would therefore never join anything, and stripping the suffix would
be string surgery on a label nobody promised to keep.

What joins them is the canonical key the database already enforces,
``(mageia_version, architecture, short_name)``.  The disc states its
version and arch in ``[media_info]`` ; its section names are the tier
names, ``core`` and ``nonfree``, with the release tree implied — a
pressing carries release trees and nothing else, which is why a real
disc declares no ``updates`` media at all.  So a section becomes
canonical by naming the tree it left implicit : ``core`` reads as
``core/release``, from which the project's existing derivations give
``core_release`` and ``Core Release`` with no new naming rule.

A disc whose sections are already written out (``core/release``)
needs no normalisation and gets none.

Two paths come out of this, and keeping them apart is the whole
point.  The media row keeps the mirror layout,
``10/x86_64/media/core/release``, because every other server of that
media serves it there.  The ``server_media`` link records the disc
layout, ``x86_64/media/core``.  A medium added from a disc with no
mirror in sight is therefore already shaped to accept mirrors later :
nothing has to be rewritten when the network comes back.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .media_cfg import (DiscoveredMedia, MediaCfgInfo, make_display_name,
                        parse_media_cfg)
from .media_pipeline import UpsertResult, upsert_media_tree
from .removable import PROTOCOL, MountPoint, mounted_removable

logger = logging.getLogger(__name__)


#: Where a pressing publishes its catalogue, relative to the mount
#: point.  ``<arch>/media/media_info/media.cfg`` on every Mageia disc
#: observed; the arch directory is the disc's top level, so the glob
#: below is what finds it without hardcoding an architecture.
CATALOGUE_GLOB = "*/media/media_info/media.cfg"

#: The tree a pressing carries when its section names leave it
#: implicit.  Verified on a Mageia 10 x86_64 disc: sections ``core``
#: and ``nonfree``, no ``updates`` section anywhere.
IMPLIED_TREE = "release"

#: Section names that already say which tree they are, so adding
#: :data:`IMPLIED_TREE` would be wrong.  Mirrors use these.
EXPLICIT_TREES = frozenset({
    "release", "updates", "testing", "updates_testing",
    "backports", "backports_testing",
})


class DiscCatalogueError(Exception):
    """The mounted disc carries no catalogue we can read."""


@dataclass(frozen=True)
class DiscCatalogue:
    """What a mounted pressing declares.

    Attributes:
        mount: Where the disc is mounted.
        root: The catalogue's own directory, relative to *mount* —
            ``x86_64/media`` on a Mageia DVD.  Every tree path below
            is relative to the mount point, not to this.
        info: The ``[media_info]`` header: version, arch, branch.
        media: One entry per tree, already canonicalised.  Their
            ``relative_path`` is left empty on purpose so the media
            pipeline derives the **mirror** layout from the canonical
            section; the disc's own layout is in *disc_paths*.
        disc_paths: ``{section: path}`` giving each tree's layout on
            the disc itself, relative to the mount point.
        raw: The catalogue text, kept for display-name resolution.
    """

    mount: MountPoint
    root: str
    info: MediaCfgInfo
    media: List[DiscoveredMedia]
    disc_paths: Dict[str, str]
    raw: str


def canonical_section(section: str) -> str:
    """The section name a mirror would use for the same tree.

    A pressing writes ``core`` for what a mirror publishes under
    ``core/release``.  Spelling the implied tree out is what lets the
    canonical key and the display name fall out of the derivations the
    project already has, instead of a second naming convention living
    here.

    Args:
        section: Section name as written in the disc's ``media.cfg``.

    Returns:
        The canonicalised name, unchanged when the section already
        says which tree it is.
    """
    section = section.strip("/")
    if not section:
        return section
    if section.rsplit("/", 1)[-1] in EXPLICIT_TREES:
        return section
    return f"{section}/{IMPLIED_TREE}"


def find_catalogue(mount: MountPoint) -> Optional[Path]:
    """The ``media.cfg`` on a mounted disc, if it has one.

    Searched rather than constructed: the top level of a pressing is
    an architecture directory, and hardcoding ``x86_64`` would make
    this work on exactly one kind of disc.  The glob is shallow, so
    this is a handful of ``stat`` calls and not a walk of the disc.

    Returns:
        The catalogue path, or ``None`` on a disc that has none —
        a music CD, a data backup, the installer-only ISO.
    """
    for candidate in sorted(mount.path.glob(CATALOGUE_GLOB)):
        if candidate.is_file():
            return candidate
    return None


def read_catalogue(mount: MountPoint) -> DiscCatalogue:
    """Read and canonicalise the catalogue of a mounted pressing.

    Args:
        mount: A mounted disc, as returned by
            :func:`urpm.core.removable.mounted_removable`.

    Returns:
        A :class:`DiscCatalogue` whose media carry mirror layouts and
        whose :attr:`DiscCatalogue.disc_paths` carry the disc's own.

    Raises:
        DiscCatalogueError: No catalogue, an unreadable one, or one
            that declares neither a version nor an architecture —
            without those two the canonical key cannot be formed and
            guessing them would attach the disc to the wrong release.
    """
    catalogue = find_catalogue(mount)
    if catalogue is None:
        raise DiscCatalogueError(
            f"{mount.path} carries no media_info/media.cfg")

    try:
        raw = catalogue.read_text()
    except OSError as exc:
        raise DiscCatalogueError(
            f"{catalogue} cannot be read: {exc}") from exc

    # The catalogue sits in ``<root>/media_info/``, so its media root
    # is two levels up, expressed relative to the mount point.
    root = catalogue.parent.parent.relative_to(mount.path).as_posix()

    try:
        info, discovered = parse_media_cfg(raw, root)
    except configparser.Error as exc:
        raise DiscCatalogueError(
            f"{catalogue} is not a usable media.cfg: {exc}") from exc

    if not info.version or not info.arch:
        raise DiscCatalogueError(
            f"{catalogue} declares no version and architecture pair "
            f"(version={info.version!r}, arch={info.arch!r}), so the "
            f"media it carries cannot be identified")
    if not discovered:
        raise DiscCatalogueError(f"{catalogue} declares no media")

    canonical, disc_paths = _canonicalise(discovered)
    return DiscCatalogue(
        mount=mount,
        root=root,
        info=info,
        media=canonical,
        disc_paths=disc_paths,
        raw=raw,
    )


def _canonicalise(
    discovered: List[DiscoveredMedia],
) -> Tuple[List[DiscoveredMedia], Dict[str, str]]:
    """Rewrite disc sections as mirror sections, keeping both layouts.

    What :func:`parse_media_cfg` hands back describes the disc: the
    section as written and the path on the pressing.  What the media
    row needs is the mirror's view.  So each entry is re-keyed on the
    canonical section, which makes the downstream derivations produce
    ``core_release`` / ``Core Release``, while the disc's own path is
    set aside to go on the link.

    The disc's own ``name`` is dropped deliberately and replaced by
    the one the canonical section yields.  Keeping
    ``Core Release (Installer)`` would name the media after whichever
    source happened to be added first, and a mirror attaching later
    would serve a media whose name says « Installer ».  Setting the
    canonical name here rather than leaving it empty also keeps
    display-name resolution on its first step: left empty it would
    fall through to its network-recovery step and spend two doomed
    fetches per media against a ``cdrom://`` URL, in the one situation
    where there may well be no network at all.
    """
    canonical: List[DiscoveredMedia] = []
    disc_paths: Dict[str, str] = {}

    for media in discovered:
        section = canonical_section(media.section)
        disc_paths[section] = media.relative_path
        canonical.append(DiscoveredMedia(
            section=section,
            # The name a mirror would carry for this tree.
            name=make_display_name(section),
            # Left for the pipeline to derive from the canonical
            # section: it owns that derivation, and the disc's own
            # path is not what belongs on the media row.
            relative_path="",
            version=media.version,
            architecture=media.architecture,
            # Recomputed from the canonical section: ``core`` on its
            # own would collide with nothing but would also match no
            # mirror media.
            short_name=section.replace("/", "_"),
            is_update=media.is_update,
            is_srpms=media.is_srpms,
            is_debug=media.is_debug,
            is_testing=media.is_testing,
            is_nonfree=media.is_nonfree,
            is_tainted=media.is_tainted,
            is_32bit=media.is_32bit,
            is_backports=media.is_backports,
            noauto=media.noauto,
            media_type=media.media_type,
            # A pressing of the distribution is official.  The
            # catalogue on a disc carries no ``media_type=`` to infer
            # it from, and inferring « not official » from a missing
            # field would let unsigned packages through on the one
            # source whose provenance is least in doubt: the disc's
            # own pubkey is the Mageia packages key.
            is_official=True,
        ))

    return canonical, disc_paths


def attach_disc(db, catalogue: DiscCatalogue, *,
                server_name: Optional[str] = None,
                priority: int = 90,
                mode: str = "discover") -> UpsertResult:
    """Record a disc as a source for the media it carries.

    The disc becomes one ``server`` row of protocol
    :data:`urpm.core.removable.PROTOCOL`, identified by its label, and
    one ``server_media`` link per tree, each carrying the disc's own
    layout.  Media that already exist are linked ; media that do not
    are created with the mirror's canonical attributes, so that
    adding a mirror afterwards joins the same rows instead of
    duplicating them.

    Nothing about where the disc is mounted is stored, by design: the
    label is the identity, and :func:`urpm.core.removable.resolve`
    finds the mount again each time it is needed.

    Args:
        db: Database instance.
        catalogue: What :func:`read_catalogue` read off the disc.
        server_name: Display name for the source.  Defaults to the
            disc label, which is what the operator sees in the mount
            path and in ``urpm media list``.
        priority: Server priority.  Above the mirror default of 50 on
            purpose: when the disc is in the drive, reading it beats
            downloading the identical package.
        mode: Passed through to :func:`upsert_media_tree`.

    Returns:
        The :class:`UpsertResult` describing what was created, linked
        or left alone.
    """
    label = catalogue.mount.path.name or catalogue.info.version
    return upsert_media_tree(
        db,
        f"{PROTOCOL}://{server_name or label}",
        catalogue=(catalogue.info, catalogue.media),
        raw_catalogue=None,
        mode=mode,
        server_priority=priority,
        server_is_official=True,
        link_paths=catalogue.disc_paths,
    )


def read_mounted_catalogues() -> List[DiscCatalogue]:
    """Every mounted disc that carries a readable Mageia catalogue.

    Used by the command that adds a disc: the operator inserts one and
    asks us to use it, rather than naming a device or a mount point
    they should not have to know.

    Discs without a catalogue are skipped silently — a mounted data
    CD is not an error, it is simply not a source.
    """
    catalogues = []
    for mount in mounted_removable():
        try:
            catalogues.append(read_catalogue(mount))
        except DiscCatalogueError as exc:
            logger.debug("%s is not a media disc: %s", mount.path, exc)
    return catalogues
