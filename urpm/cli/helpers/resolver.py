"""Resolver helper functions for CLI commands."""

import re
from typing import TYPE_CHECKING

from .package import resolve_target_arch, system_arch

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def extract_version(pkg_name: str) -> str:
    """Extract version from package name (e.g., php8.4-fpm -> 8.4)."""
    match = re.search(r'(\d+\.\d+)', pkg_name)
    return match.group(1) if match else None


def group_by_version(packages: set) -> dict:
    """Group packages by their version.

    Returns dict: {version: set of packages}
    Packages without version go under None key.
    """
    groups = {}
    for pkg in packages:
        ver = extract_version(pkg)
        if ver not in groups:
            groups[ver] = set()
        groups[ver].add(pkg)
    return groups


def create_resolver(db: 'PackageDatabase', args, **kwargs) -> 'Resolver':
    """Create a Resolver with root options from args.

    Args:
        db: Package database
        args: Parsed arguments (may contain root, urpm_root, allow_arch)
        **kwargs: Additional arguments to pass to Resolver

    Returns:
        Configured Resolver instance
    """
    from ...core.resolver import Resolver

    # Get root options from args
    root = getattr(args, 'root', None)
    urpm_root = getattr(args, 'urpm_root', None)

    # Honour args.arch when no explicit kwarg was provided.
    # Priority: explicit kwarg > args.arch > system_arch().
    if 'arch' not in kwargs:
        kwargs['arch'] = resolve_target_arch(args)

    # Handle --allow-arch: build allowed_arches list
    # Default: [system_arch, 'noarch']
    # With --allow-arch: add specified architectures
    if 'allowed_arches' not in kwargs:
        allow_arch = getattr(args, 'allow_arch', None)
        if allow_arch:
            # User specified additional architectures
            arch = kwargs.get('arch', system_arch())
            allowed_arches = [arch, 'noarch'] + list(allow_arch)
            # Remove duplicates while preserving order
            seen = set()
            kwargs['allowed_arches'] = [x for x in allowed_arches if not (x in seen or seen.add(x))]

    # Pass media filtering options from CLI args
    if 'media' not in kwargs:
        kwargs['media'] = getattr(args, 'media', None)
    if 'excludemedia' not in kwargs:
        kwargs['excludemedia'] = getattr(args, 'excludemedia', None)
    if 'sortmedia' not in kwargs:
        kwargs['sortmedia'] = getattr(args, 'sortmedia', None)

    # ``--enablemedia`` / ``--disablemedia`` : per-transaction media
    # scoping.  Resolved to canonical display names here — the Resolver
    # matches on those and knows nothing about short names.
    enable = resolve_media_identifiers(db, getattr(args, 'enablemedia', None))
    disable = resolve_media_identifiers(db, getattr(args, 'disablemedia', None))

    if enable and 'enablemedia' not in kwargs:
        assert_media_synced(db, enable)
        kwargs['enablemedia'] = enable
    if disable:
        # ``--disablemedia`` is exactly the existing excludemedia
        # semantics (skip an otherwise-enabled media), so it feeds the
        # same set rather than duplicating the mechanism.  Merge instead
        # of overwrite : both may legitimately be in play.
        existing = kwargs.get('excludemedia')
        merged = set(disable)
        if existing:
            merged |= set(existing.split(','))
        kwargs['excludemedia'] = ','.join(sorted(merged))

    return Resolver(db, root=root, urpm_root=urpm_root, **kwargs)


def resolve_media_identifiers(db, identifiers) -> list:
    """Turn what the operator typed into canonical media display names.

    Accepts a list of identifiers (argparse ``append``) or a single
    comma-separated string, so ``--enablemedia a --enablemedia b`` and
    ``--enablemedia a,b`` both work.

    Args:
        db: Package database, for :meth:`resolve_media`.
        identifiers: Raw CLI values, or ``None``.

    Returns:
        Canonical display names, deduplicated, order-stable.  Empty list
        when *identifiers* is falsy.

    Raises:
        MediaNotFoundError: on the first identifier that matches no
            media, carrying close short names to suggest.  Failing here
            rather than silently ignoring matters : a typo'd
            ``--enablemedia`` would otherwise resolve without the media
            the operator asked for, and they would read the resulting
            « package not found » as a repository problem.
    """
    if not identifiers:
        return []
    if isinstance(identifiers, str):
        identifiers = [identifiers]

    wanted = []
    for raw in identifiers:
        for part in str(raw).split(','):
            part = part.strip()
            if part:
                wanted.append(part)

    resolved, seen = [], set()
    for identifier in wanted:
        media = db.resolve_media(identifier)
        if media is None:
            raise MediaNotFoundError(
                identifier, db.suggest_media_names(identifier),
            )
        if media['name'] not in seen:
            seen.add(media['name'])
            resolved.append(media['name'])
    return resolved


def assert_media_synced(db, names: list) -> None:
    """Refuse to force-enable a media whose metadata was never fetched.

    A disabled media is usually one that has never been synced, so
    ``--enablemedia`` on it would load an empty repository and the
    resolution would fail with « package not found » — which reads as
    « that package is not in backports » rather than « you never synced
    backports ».  Same symptom, opposite cause, and nothing on screen
    to tell them apart.

    Syncing automatically is deliberately not the answer : that is an
    unrequested metadata fetch against shared Mageia infrastructure,
    triggered by what the operator thinks is a local convenience flag.
    We say what is missing and let them decide.

    Raises:
        MediaNotSyncedError: naming every unsynced media and the command
            that fixes it.
    """
    if not names:
        return
    unsynced = [
        m['short_name'] or m['name']
        for m in db.list_media()
        if m['name'] in set(names) and not m.get('last_sync')
    ]
    if unsynced:
        raise MediaNotSyncedError(unsynced)


class MediaNotSyncedError(Exception):
    """``--enablemedia`` named a media with no local metadata."""

    def __init__(self, names: list):
        from ...i18n import _, ngettext
        self.names = names
        message = ngettext(
            "Media '{names}' has never been synced, so it holds no "
            "package metadata locally.",
            "Media '{names}' have never been synced, so they hold no "
            "package metadata locally.",
            len(names),
        ).format(names="', '".join(names))
        message += " " + _("Run: urpm media update {names}").format(
            names=" ".join(names))
        super().__init__(message)


class MediaNotFoundError(Exception):
    """A ``--enablemedia`` / ``--disablemedia`` value matched no media.

    Carries the suggestions so the CLI layer can render them; the
    message itself is already user-facing and translated.
    """

    def __init__(self, identifier: str, suggestions: list):
        from ...i18n import _
        self.identifier = identifier
        self.suggestions = suggestions
        message = _("No media matches '{identifier}'.").format(
            identifier=identifier)
        if suggestions:
            message += " " + _("Did you mean: {names}?").format(
                names=", ".join(suggestions))
        super().__init__(message)


# Backwards compatibility aliases (with underscore prefix)
_extract_version = extract_version
_group_by_version = group_by_version
_create_resolver = create_resolver
