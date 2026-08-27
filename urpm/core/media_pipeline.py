"""Unified media-creation primitive: ``upsert_media_tree``.

This module hosts the single source of truth for inserting or
reconciling media entries from a URL.  Every command that produces
media records — ``urpm media add``, ``urpm media discover``,
``urpm media autoconfig``, ``urpm media import``, ``urpm init`` —
is meant to call :func:`upsert_media_tree` rather than its own
ad-hoc upsert path.

Contract invariants (a/b/c) honoured by construction:

* (a) Every media inserted is linked to at least one server in the
  same transaction.  Creating an orphan media is impossible through
  this API.
* (b) The display name is resolved via
  :func:`urpm.core.media_cfg.resolve_display_name` — never by
  inline string formatting.  Names like ``mga9-core-release`` cannot
  appear here.
* (c) Required attributes (``mageia_version``, ``architecture``,
  ``short_name``, ``relative_path``) are resolved via a cascade
  (manifest > catalogue > URL regex > ``/etc/mageia-release`` >
  computed fallback).  Placeholders such as ``'unknown'`` or ``''``
  never reach the database.  When the cascade exhausts itself
  without producing a real value for a strictly-required field, the
  primitive refuses with a :class:`MediaTreeAttributeError`.

Invariant (d) — *do not invent a server for a URL we cannot reach* —
is enforced by :class:`MediaTreeFetchError` raised when neither the
catalogue at ``<url>/media_info/media.cfg`` nor the URL-pattern
fallback yield anything usable.

Modes:

* ``'discover'`` — default.  Insert new media, link the server,
  refuse to silently fuse with placeholder entries.
* ``'reconcile'`` — when the canonical key matches an existing media
  that carries placeholder attributes (``mageia_version='unknown'``,
  ``architecture='unknown'``, empty ``relative_path`` — the footprint
  left by pre-0.8 imports), the primitive *adopts* that row, updates
  its placeholders with proper values, then links the server.  Used
  by the repair path for previously broken imports.
* ``'deep'`` — like ``'discover'`` but also performs a per-media
  manifest probe (``<media>/media_info/media.cfg``) to validate
  existence before insertion.  More network traffic, more guarantees.
  GPG keys are **never** auto-imported by this mode.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

from urpm.cli.helpers.media import (
    MediaNameCollision,
    disambiguate_media_name,
    generate_server_name,
    generate_short_name,
    parse_custom_media_url,
    parse_mageia_media_url,
)
from urpm.core.media_cfg import (
    DiscoveredMedia,
    MediaCfgInfo,
    fetch_media_cfg,
    is_ugly_name,
    parse_media_cfg,
    resolve_display_name,
)

logger = logging.getLogger(__name__)


# ── Exceptions ──────────────────────────────────────────────────────────


class MediaTreeError(Exception):
    """Base exception for upsert_media_tree refusal paths."""


class MediaTreeFetchError(MediaTreeError):
    """Neither the catalogue at ``<url>/media_info/media.cfg`` nor a
    fallback URL pattern parser yielded anything usable.

    The primitive refuses to persist a placeholder row on this path.
    The caller must surface a clear error to the user (typically "URL
    inaccessible or not a recognised Mageia media tree").
    """


class MediaTreeAttributeError(MediaTreeError):
    """A strictly-required attribute could not be resolved from any
    source in the cascade.

    Carries the field name and a list of sources that were tried.
    """

    def __init__(self, field: str, sources_tried: list[str]):
        super().__init__(
            f"required field {field!r} could not be resolved from any source; "
            f"tried: {', '.join(sources_tried)}"
        )
        self.field = field
        self.sources_tried = sources_tried


# ── Result objects ──────────────────────────────────────────────────────


@dataclasses.dataclass
class UpsertOutcome:
    """What happened to a single media candidate.

    Attributes:
        action: One of ``'created'`` (new row inserted),
            ``'updated'`` (existing placeholder row reconciled),
            ``'relinked'`` (row was fine, only the server↔media
            link was added), ``'noop'`` (already linked, nothing
            to do), ``'skipped'`` (collision the primitive could
            not resolve and the caller did not ask to repair).
        media_id: ID of the affected media row (None if ``skipped``).
        media_name: Final display name of the media.
        short_name: Canonical short_name.
        reason: Free-form explanation for ``skipped`` outcomes.
    """

    action: str
    media_id: Optional[int]
    media_name: Optional[str]
    short_name: Optional[str]
    reason: str = ""


@dataclasses.dataclass
class UpsertResult:
    """Aggregate result of one ``upsert_media_tree`` call.

    Attributes:
        server_id: ID of the server upserted from the input URL.
        server_name: Display name of the server.
        server_was_created: ``True`` when the server entry was
            inserted during this call, ``False`` when an existing
            row matched ``(protocol, host, base_path)`` and was
            reused.  Useful for the CLI to display ``(new)`` vs
            ``(existing)`` next to the server name.
        outcomes: One :class:`UpsertOutcome` per media candidate
            discovered at the URL (zero for an empty catalogue).
    """

    server_id: int
    server_name: str
    server_was_created: bool
    outcomes: list[UpsertOutcome]


# ── Helpers ─────────────────────────────────────────────────────────────


_PLACEHOLDER_VALUES = frozenset({"unknown", "none", "null", ""})
_KNOWN_ARCHES = frozenset({"x86_64", "i586", "i686", "aarch64",
                           "armv7hl", "armv5tl", "ppc64le", "noarch"})
# Bare version segment as officiels ship it — ``10``, ``11.5``,
# ``cauldron``.
_VERSION_RE = re.compile(r"^(?:\d+(?:\.\d+)?|cauldron)$", re.IGNORECASE)
# Version segment with optional ``mageia`` / ``mga`` prefix — covers
# community layouts like blogdrake (``mageia10``, ``mga10``) in
# addition to the officiels' bare form.  Capturing group ``(1)`` is
# the version number itself, stripped of any prefix.
_VERSION_TOKEN_RE = re.compile(
    r"^(?:mageia|mga)?(\d+(?:\.\d+)?|cauldron)$", re.IGNORECASE,
)


def _match_version_token(segment: str) -> Optional[str]:
    """Return the canonical version number if *segment* carries one.

    Recognises both bare (``10``, ``cauldron``) and prefixed
    (``mageia10``, ``mga10``) forms, and returns the version number
    (or ``'cauldron'``) as a lowercase string.  Returns ``None`` when
    the segment doesn't look like a version at all.
    """
    if not segment:
        return None
    m = _VERSION_TOKEN_RE.match(segment)
    return m.group(1).lower() if m else None


def _is_placeholder(value: Optional[str]) -> bool:
    """Return True if *value* is empty or a legacy placeholder."""
    if value is None:
        return True
    return value.strip().lower() in _PLACEHOLDER_VALUES


def _split_url(url: str) -> tuple[str, str, str]:
    """Split *url* into ``(protocol, host, base_path)``.

    ``base_path`` is everything *before* the first ``<version>/<arch>``
    segment pair, when such a Mageia-style pattern is detected.  When
    no pattern is recognised (custom file:// URLs, community repos
    that don't follow the Mageia layout), ``base_path`` is the empty
    string and the entire path is expected to live in the media's
    ``relative_path``.  This matches the existing
    ``parse_custom_media_url`` convention so the server URL the rest
    of the codebase reconstructs from ``server.base_path`` and
    ``media.relative_path`` never duplicates path segments.
    """
    parsed = urlparse(url.rstrip("/"))
    protocol = parsed.scheme or "https"
    host = parsed.netloc
    path = parsed.path or ""

    # Independent scan for version and arch — we don't require them
    # to be adjacent.  Officiels put them consecutive
    # (``.../10/x86_64/``), blogdrake plate separates them by the
    # channel (``.../mageia10/free/x86_64/``).  Both work here.
    parts = [p for p in path.split("/") if p]
    version_idxs = [i for i, p in enumerate(parts)
                    if _match_version_token(p)]
    arch_idxs = [i for i, p in enumerate(parts) if p in _KNOWN_ARCHES]
    # Guard: exactly one of each — multiple hits on either side would
    # be ambiguous and the safer play is to leave base_path empty and
    # let the caller fall back on --custom explicit values.
    if len(version_idxs) != 1 or len(arch_idxs) != 1:
        return protocol, host, ""
    pivot = min(version_idxs[0], arch_idxs[0])
    base = "/" + "/".join(parts[:pivot]) if parts[:pivot] else ""
    return protocol, host, base


def _extract_version_from_url(url: str) -> str:
    """Pull a Mageia version segment from URL path, if any."""
    parsed = urlparse(url.rstrip("/"))
    for part in parsed.path.split("/"):
        version = _match_version_token(part)
        if version is not None:
            return version
    return ""


def split_release_arch_tail(path: str, arch: str) -> tuple[str, Optional[str]]:
    """Split a URL path into ``(server_root, url_version)``.

    Detects the trailing ``.../<version>/<arch>`` pair — where
    ``<version>`` matches :data:`_VERSION_RE` (a numeric or
    ``cauldron``) — and returns:

    * ``server_root``: the path with that trailing pair removed
      (leading slash preserved, no trailing slash).
    * ``url_version``: the matched version segment as served by
      this mirror.  May differ from the release identity the
      caller is targeting — most notably during a freeze, where
      a mirror asked for release ``11`` serves it under
      ``cauldron``.

    Returns ``(path.rstrip("/"), None)`` when the tail does not
    end with a recognised ``<version>/<arch>`` pair (custom
    layouts, third-party repos).  Callers must fall back to the
    release identity in that case.
    """
    stripped = path.rstrip("/")
    parts = [p for p in stripped.split("/") if p]
    if len(parts) >= 2 and parts[-1] == arch and _VERSION_RE.fullmatch(parts[-2]):
        server_root = "/" + "/".join(parts[:-2]) if parts[:-2] else ""
        return server_root, parts[-2].lower()
    return stripped, None


def _extract_arch_from_url(url: str) -> str:
    """Pull a known architecture segment from URL path, if any."""
    parsed = urlparse(url.rstrip("/"))
    for part in parsed.path.split("/"):
        if part in _KNOWN_ARCHES:
            return part
    return ""


def _system_version_fallback() -> str:
    """Read ``/etc/mageia-release`` and return the version number.

    Returns an empty string when the file is absent or unparseable.
    Used as the last step of the version cascade for catalogues
    that lack a ``[media_info].version=`` entry.
    """
    try:
        with open("/etc/mageia-release", encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return ""
    m = re.search(r"\b(\d+|cauldron)\b", line, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _resolve_version(
    info: Optional[MediaCfgInfo],
    media: Optional[DiscoveredMedia],
    hint: Optional[dict],
    url: str,
) -> str:
    """Resolve the release-level identity of a media row.

    ``mageia_version`` on the ``media`` table is the release-directory
    identity (``'cauldron'`` or a numeric like ``'10'``, ``'11'``, …)
    that anchors filtering in :meth:`_get_accepted_versions`.  It must
    match the ``mageia-version`` config pin so a machine set to
    ``cauldron`` sees its cauldron media and only those.

    URL wins because it carries this identity unambiguously — the
    ``/distrib/<release>/`` segment is what Mageia's infra actually
    uses to partition the archive.  ``media.cfg`` is authored by the
    server side to describe the *target* numeric of cauldron (e.g.
    ``version=11`` while cauldron is baking mga11), which is a
    different concept than tree identity; consuming it here silently
    reprints cauldron media as ``'11'`` and breaks the filter.  We
    consume it separately as ``system-numeric`` at config time (see
    :func:`cmd_init`).

    The catalogue path stays as a fallback for URLs that don't carry
    the release segment (custom media, mirror shims, in-house repos).
    """
    # 1. URL — authoritative for Mageia's standard mirror layout.
    via_url = _extract_version_from_url(url)
    if not _is_placeholder(via_url):
        return via_url
    # 2. Catalogue (media.cfg) — fallback for non-standard URLs.
    if media is not None and not _is_placeholder(media.version):
        return media.version
    if info is not None and not _is_placeholder(info.version):
        return info.version
    # 3. Hint
    if hint and not _is_placeholder(hint.get("version")):
        return hint["version"]
    # 4. System fallback
    return _system_version_fallback()


def _resolve_arch(
    info: Optional[MediaCfgInfo],
    media: Optional[DiscoveredMedia],
    hint: Optional[dict],
    url: str,
) -> str:
    """Resolve architecture following the same cascade as version."""
    if media is not None and not _is_placeholder(media.architecture):
        return media.architecture
    if info is not None and not _is_placeholder(info.arch):
        return info.arch
    if hint and not _is_placeholder(hint.get("arch")):
        return hint["arch"]
    via_url = _extract_arch_from_url(url)
    if not _is_placeholder(via_url):
        return via_url
    return ""  # no /etc-fallback for arch; caller will raise


def _build_relative_path(
    version: str,
    arch: str,
    section: str,
    hint: Optional[dict],
) -> str:
    """Construct the relative path for a media row.

    For a Mageia-style catalogue the path is
    ``{version}/{arch}/media/{section}`` (or
    ``{version}/{arch}/{section}`` when ``section`` already begins
    with ``media/``).  For cross-arch references
    (``../../<arch>/media/<class>/<type>``), we resolve them relative
    to the current ``<version>/<arch>/media`` root.
    """
    # Honour hint when caller carries the path verbatim (e.g. urpmi.cfg
    # import with explicit URL).
    if hint and hint.get("relative_path"):
        return hint["relative_path"]

    if section.startswith("../../"):
        # Cross-arch reference: walk up two levels then re-anchor.
        # Section: ../../i586/media/core/release -> version/i586/media/core/release
        sub = section[len("../../"):]
        return f"{version}/{sub}"

    if section.startswith("media/"):
        return f"{version}/{arch}/{section}"
    return f"{version}/{arch}/media/{section}"


def _short_name_for(section: str) -> str:
    """Compute the canonical short_name for a media section.

    ``core/release`` → ``core_release``.  Cross-arch references
    drop the ``../../<arch>/media/`` prefix to keep the short_name
    arch-agnostic (the arch lives in its own column).
    """
    if section.startswith("../../"):
        # Drop "../../<arch>/media/"
        parts = section.split("/", 4)
        if len(parts) >= 4 and parts[3] == "media":
            inner = parts[4] if len(parts) > 4 else ""
        else:
            inner = "/".join(parts[3:])
        section = inner

    section = section.removeprefix("media/")
    return section.replace("/", "_")


# ── Main primitive ──────────────────────────────────────────────────────


def upsert_media_tree(
    db,
    url: str,
    *,
    hint: Optional[dict] = None,
    mode: str = "discover",
    server_priority: int = 50,
    server_is_official: Optional[bool] = None,
    catalogue: Optional[Tuple[MediaCfgInfo, list[DiscoveredMedia]]] = None,
    raw_catalogue: Optional[str] = None,
    media_filter: Optional[Callable[[DiscoveredMedia], bool]] = None,
    enabled_policy: Optional[Callable[[DiscoveredMedia], bool]] = None,
) -> UpsertResult:
    """Insert or reconcile media entries discovered at *url*.

    See module docstring for the full contract.  This function is the
    single canonical entry point that every media-creating command is
    expected to call.

    Args:
        db: A ``PackageDatabase``-compatible accessor (must expose
            ``get_server_by_location``, ``add_server``, ``add_media``,
            ``get_media``, ``get_media_by_version_arch_shortname``,
            ``link_server_media``, ``server_media_link_exists`` plus
            the raw ``conn`` attribute for ``UPDATE`` in reconcile
            mode).
        url: URL of the media root (e.g.
            ``https://host/path/9/x86_64/media/`` for an arch-level
            catalogue, or a specific media URL for the fallback path).
        hint: Optional dict carrying caller-provided overrides.
            Recognised keys: ``name``, ``arch``, ``version``,
            ``relative_path``, ``short_name``, ``is_official``,
            ``allow_unsigned``, ``enabled``, ``update_media``,
            ``priority``.  Empty / placeholder values are ignored
            (treated as missing).
        mode: ``'discover'`` | ``'reconcile'`` | ``'deep'`` — see
            module docstring.
        server_priority: Priority assigned to a newly-created server.
        server_is_official: When None (default), inferred from the
            catalogue / URL parser.  Pass an explicit value to
            override (e.g. for community repos served from official
            mirror hosts).
        catalogue: Pre-parsed ``(MediaCfgInfo, [DiscoveredMedia])``
            tuple.  When provided, the primitive skips the network
            fetch and uses this catalogue directly.  Used by callers
            that need to preview the catalogue (display plan, dry-run)
            before performing the actual upsert — avoids a second
            HTTP round-trip.
        raw_catalogue: Companion to ``catalogue`` — the raw text of
            the media.cfg used to feed ``resolve_display_name``'s
            ``parent_cfg_sections`` argument.  When ``catalogue`` is
            passed but ``raw_catalogue`` is not, display-name
            resolution falls back to the network or the computed
            Title-Cased default.
        media_filter: Optional ``DiscoveredMedia -> bool`` callable.
            Media for which the filter returns False are silently
            skipped (no outcome produced).  Default: include
            everything from the catalogue.  Used by ``cmd_media_discover``
            to drop SRPMS / debug media before any DB write.
        enabled_policy: Optional ``DiscoveredMedia -> bool`` callable
            that decides the ``enabled`` field for each media row.
            Default: ``not media.noauto`` (the catalogue marker).
            Used by ``cmd_media_discover`` to apply smart-enable
            policies based on detected installed categories
            (nonfree, tainted, 32-bit).

    Returns:
        An :class:`UpsertResult` summarising what happened.

    Raises:
        MediaTreeFetchError: If the URL is reachable neither as a
            catalogue nor as a Mageia-pattern media URL.
        MediaTreeAttributeError: If a required attribute cannot be
            resolved from any source in the cascade.
        ValueError: If *mode* is not recognised.
    """
    if mode not in {"discover", "reconcile", "deep"}:
        raise ValueError(
            f"mode must be 'discover', 'reconcile' or 'deep', got {mode!r}"
        )

    hint = hint or {}

    # ── Step 1: probe the catalogue (unless pre-loaded). ────────────
    info: Optional[MediaCfgInfo] = None
    medias: list[DiscoveredMedia] = []
    catalogue_url = url.rstrip("/") + "/"
    raw = raw_catalogue

    if catalogue is not None:
        info, medias = catalogue
        # raw is whatever the caller passed (may be None).
    else:
        try:
            raw = fetch_media_cfg(catalogue_url)
        except Exception as exc:  # pycurl / network / 4xx / 5xx
            logger.debug("catalogue fetch failed for %s: %s", url, exc)
            raw = None

    if raw is not None and catalogue is None:
        # Only parse if we fetched the raw text ourselves; when the caller
        # passed a pre-parsed catalogue, info/medias are already set.
        media_root = _build_relative_path(
            _extract_version_from_url(url) or hint.get("version") or "",
            _extract_arch_from_url(url) or hint.get("arch") or "",
            "",
            None,
        ).rstrip("/")
        info, medias = parse_media_cfg(raw, media_root)

    if not medias:
        # ── Step 1 fallback: try to derive a single media from the URL. ─
        parsed_url = parse_mageia_media_url(url) or parse_custom_media_url(url)
        if parsed_url is None or _is_unidentifiable_url(parsed_url):
            raise MediaTreeFetchError(
                f"URL {url!r} is not reachable as a media catalogue and "
                f"does not match any known media URL pattern"
            )
        # Build a synthetic DiscoveredMedia from the URL parser output.
        medias = [_synthesise_media_from_url_parse(parsed_url, hint)]
        if info is None:
            info = MediaCfgInfo(
                version=parsed_url.get("version", ""),
                arch=parsed_url.get("arch", ""),
                branch="",
            )

    # ── Step 2: upsert the server. ──────────────────────────────────
    protocol, host, base_path = _split_url(url)
    is_official_for_server = (
        server_is_official
        if server_is_official is not None
        else _infer_is_official(info, medias)
    )

    server = db.get_server_by_location(protocol, host, base_path)
    server_was_created = False
    if server is None:
        server_name = generate_server_name(protocol, host)
        server_id = _add_server_with_unique_name(
            db, server_name, protocol, host, base_path,
            is_official=is_official_for_server,
            priority=server_priority,
        )
        server = {"id": server_id, "name": server_name}
        server_was_created = True
    server_id = server["id"]
    server_name = server["name"]

    # ── Step 3: process each media candidate. ───────────────────────
    outcomes: list[UpsertOutcome] = []
    parent_cfg_sections = _build_parent_sections_map(raw, medias) if raw else None

    for media in medias:
        # Caller-supplied filter (e.g. drop SRPMS/debug for ``discover``).
        if media_filter is not None and not media_filter(media):
            continue
        outcome = _process_one_media(
            db,
            server_id=server_id,
            url=catalogue_url,
            media=media,
            info=info,
            hint=hint,
            mode=mode,
            parent_cfg_sections=parent_cfg_sections,
            enabled_policy=enabled_policy,
        )
        outcomes.append(outcome)

    return UpsertResult(
        server_id=server_id,
        server_name=server_name,
        server_was_created=server_was_created,
        outcomes=outcomes,
    )


def insert_pending_mirrorlist_media(
    db,
    *,
    with_dir: str,
    version: str,
    arch: str,
    name: str = "",
    enabled: bool = True,
    update: bool = False,
    is_official: bool = True,
) -> UpsertOutcome:
    """Create a media record for a ``mirrorlist:`` entry in urpmi.cfg.

    Unlike :func:`upsert_media_tree`, this function creates the media
    **without linking any server**.  Mirrorlist entries by definition
    do not carry a single server URL — the ``$MIRRORLIST`` macro
    expands at sync time to the mirror pool.  Caller is expected to
    invoke ``urpm server autoconfig`` afterwards to attach servers via
    HEAD MD5SUM scan on ``relative_path``.

    This is a **documented exception** to invariant (a) of
    :func:`upsert_media_tree`: the media exists in the DB with no
    server link until autoconfig completes the picture.  The
    invariant holds for the higher-level *pipeline* (import + then
    autoconfig), not for this single call in isolation.

    Args:
        db: Database accessor.
        with_dir: Value of the ``with-dir:`` line in urpmi.cfg
            (e.g. ``"media/core/release"``).
        version: Mageia version this media targets (``"9"``,
            ``"10"``, ``"cauldron"``).  Must be non-empty.
        arch: Target architecture.  Must be non-empty.
        name: Display name from urpmi.cfg (e.g. ``"Core Release"``).
            When empty, a name is computed from ``with_dir`` via the
            standard Title-Cased fallback.
        enabled: Corresponds to the absence of ``ignore`` in the
            urpmi.cfg entry.
        update: Corresponds to the ``update`` marker in urpmi.cfg.
        is_official: Whether the media should be marked as official.
            Mirrorlist entries in urpmi.cfg are typically the
            distribution's own mirrors, so defaults to True.

    Returns:
        An :class:`UpsertOutcome` describing what happened
        (``created`` for new, ``noop`` for already-present).

    Raises:
        MediaTreeAttributeError: If version or arch is empty.
    """
    if not version:
        raise MediaTreeAttributeError(
            "mageia_version",
            ["urpmi.cfg entry has no version (was --release passed?)"],
        )
    if not arch:
        raise MediaTreeAttributeError(
            "architecture",
            ["urpmi.cfg entry has no arch (was --arch passed?)"],
        )

    # Compute the canonical short_name from the with-dir path.
    #   "media/core/release" → "core_release"
    #   "media/nonfree/updates" → "nonfree_updates"
    normalised = with_dir.strip("/").removeprefix("media/")
    short_name = normalised.replace("/", "_") or "media"

    # Build the on-disk relative path: {version}/{arch}/media/{sub}
    if with_dir.startswith("media/"):
        relative_path = f"{version}/{arch}/{with_dir}"
    elif with_dir.startswith("/"):
        relative_path = f"{version}/{arch}{with_dir}"
    else:
        relative_path = f"{version}/{arch}/media/{with_dir}"

    # Idempotency: if the same canonical key already exists, no-op.
    existing = db.get_media_by_version_arch_shortname(
        version, arch, short_name,
    )
    if existing is not None:
        return UpsertOutcome(
            action="noop",
            media_id=existing["id"],
            media_name=existing["name"],
            short_name=short_name,
        )

    # Resolve display name via the canonical cascade (respects the
    # urpmi.cfg name; falls back to Title-Cased when empty / ugly).
    display_name = resolve_display_name(
        media_url="",  # no network probing possible without a server
        section=normalised,
        explicit_name=name or None,
        parent_cfg_sections=None,
        prefer="global",
    )

    # Disambiguate against existing rows on the same or foreign arch.
    try:
        display_name = disambiguate_media_name(db, display_name, arch)
    except MediaNameCollision as collision:
        # Native-arch collision — cannot silently rename.  Skip this
        # entry and let the caller surface the situation.
        return UpsertOutcome(
            action="skipped",
            media_id=None,
            media_name=display_name,
            short_name=short_name,
            reason=(f"display name {display_name!r} already taken by "
                    f"media #{collision.existing.get('id')} "
                    f"(short_name={collision.existing.get('short_name')!r}) "
                    "on native arch — pass a different --name in "
                    "urpmi.cfg or edit the collision away"),
        )

    media_id = db.add_media(
        name=display_name,
        short_name=short_name,
        mageia_version=version,
        architecture=arch,
        relative_path=relative_path,
        is_official=is_official,
        allow_unsigned=False,
        enabled=enabled,
        update_media=update,
        priority=50,
        url=None,
    )
    return UpsertOutcome(
        action="created",
        media_id=media_id,
        media_name=display_name,
        short_name=short_name,
    )


def upsert_single_media(
    db,
    url: str,
    *,
    hint: Optional[dict] = None,
    mode: str = "discover",
) -> UpsertResult:
    """Insert exactly one media identified by its URL, no tree discovery.

    Thin wrapper around :func:`upsert_media_tree` for callers that
    know they want to upsert one specific media (``urpm media add``,
    ``urpm media import`` on URL-direct entries) rather than walk an
    entire catalogue.  Forces the URL-parser fallback path: skips
    the catalogue fetch entirely and builds a synthetic
    :class:`DiscoveredMedia` from the URL pattern + caller hints.

    Why this exists: when the caller passes a per-media URL like
    ``https://mirror/9/x86_64/media/core/release/``, blindly calling
    ``upsert_media_tree`` could try to fetch a catalogue at that URL
    (which would fetch the per-media manifest if it exists), with
    ambiguous semantics.  This wrapper sidesteps the question by
    going straight to URL parsing.

    Args:
        db: Database accessor (same contract as
            :func:`upsert_media_tree`).
        url: URL of one specific media (must match a known Mageia or
            custom pattern that the URL parsers can decompose).
        hint: Same semantics as in :func:`upsert_media_tree`.  Used
            here to carry ``--name`` / ``--custom`` overrides.
        mode: Same values as :func:`upsert_media_tree`.

    Returns:
        An :class:`UpsertResult` with exactly one outcome.

    Raises:
        MediaTreeFetchError: When neither URL parser recognises *url*.
        MediaTreeAttributeError: When the cascade cannot resolve a
            required attribute.
    """
    hint = hint or {}
    parsed = parse_mageia_media_url(url) or parse_custom_media_url(url)
    if parsed is None:
        raise MediaTreeFetchError(
            f"URL {url!r} does not match any known media URL pattern"
        )

    # An "unidentifiable" URL is one where neither the URL parser nor
    # the caller-provided hint supplies a version/arch.  When the
    # caller has explicit values (e.g. ``urpm media add --custom`` +
    # explicit ``--version``, or a ``file://`` URL where the test
    # passes version+arch out-of-band), the URL itself doesn't need
    # to embed them.
    final_version = parsed.get("version") or hint.get("version") or ""
    final_arch = parsed.get("arch") or hint.get("arch") or ""
    if not final_version or not final_arch:
        raise MediaTreeFetchError(
            f"URL {url!r} does not embed a recognised "
            f"version/arch pattern, and the caller did not provide "
            f"these via hint either"
        )

    info = MediaCfgInfo(
        version=final_version,
        arch=final_arch,
        branch="",
    )
    synthetic = _synthesise_media_from_url_parse(parsed, hint)
    return upsert_media_tree(
        db,
        url,
        hint=hint,
        mode=mode,
        catalogue=(info, [synthetic]),
    )


# ── Sub-helpers ─────────────────────────────────────────────────────────


def _is_unidentifiable_url(parsed: dict) -> bool:
    """Reject URLs that the custom parser couldn't pin down.

    ``parse_custom_media_url`` accepts any ``http(s)://`` URL and
    returns a dict, but with ``version=None`` and ``arch=None`` when
    no Mageia-style segments were detected.  That is *not* a media
    tree — emit a FetchError rather than try to invent attributes.
    """
    # Both version and arch must be at least guessable from the URL.
    # If parse_custom_media_url returned None / None for both, we have
    # no Mageia anchor and nothing trustworthy to write to the database.
    return not parsed.get("version") and not parsed.get("arch")


def _synthesise_media_from_url_parse(
    parsed: dict,
    hint: dict,
) -> DiscoveredMedia:
    """Build a synthetic DiscoveredMedia from URL-parser output.

    Used when the catalogue at the URL is unreachable but the URL
    itself follows a known Mageia or custom pattern.  Mirrors the
    field set the parser produces.
    """
    section = parsed.get("section") or parsed.get("name", "").lower().replace(" ", "_")
    return DiscoveredMedia(
        section=section,
        name=hint.get("name") or parsed.get("name", ""),
        relative_path=parsed.get("relative_path", ""),
        version=parsed.get("version", ""),
        architecture=parsed.get("arch", ""),
        short_name=parsed.get("short_name") or _short_name_for(section),
        is_update=parsed.get("is_update", False),
        is_srpms=parsed.get("is_srpms", False),
        is_debug=parsed.get("is_debug", False),
        is_testing=parsed.get("is_testing", False),
        is_nonfree=parsed.get("is_nonfree", False),
        is_tainted=parsed.get("is_tainted", False),
        is_32bit=parsed.get("is_32bit", False),
        is_backports=parsed.get("is_backports", False),
        noauto=False,
        media_type=parsed.get("media_type", ""),
        is_official=parsed.get("is_official", False),
    )


def _infer_is_official(
    info: Optional[MediaCfgInfo],
    medias: list[DiscoveredMedia],
) -> bool:
    """A server is official when its catalogue lists at least one
    official media or the catalogue itself carries the
    ``branch=Official`` marker.
    """
    if info is not None and info.branch.lower() == "official":
        return True
    return any(m.is_official for m in medias)


def _add_server_with_unique_name(
    db,
    base_name: str,
    protocol: str,
    host: str,
    base_path: str,
    *,
    is_official: bool,
    priority: int,
) -> int:
    """Insert a server, suffixing the name on UNIQUE collisions.

    Mirrors the boilerplate present in ``_import_single_media`` —
    centralised here so every caller path uses the same uniqueness
    handling.
    """
    name = base_name
    counter = 1
    while True:
        try:
            return db.add_server(
                name=name,
                protocol=protocol,
                host=host,
                base_path=base_path,
                is_official=is_official,
                enabled=True,
                priority=priority,
            )
        except Exception as exc:
            if "UNIQUE" in str(exc) and "name" in str(exc):
                counter += 1
                name = f"{base_name}-{counter}"
                if counter > 100:
                    raise
            else:
                raise


def _build_parent_sections_map(
    raw: str,
    medias: list[DiscoveredMedia],
) -> dict[str, dict]:
    """Re-parse the catalogue raw text into a section → options dict.

    Mirrors what ``cmd_media_discover`` does to feed
    ``resolve_display_name`` with the parent media.cfg sections.
    """
    import configparser
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(raw)
    except configparser.Error:
        return {}
    return {
        section: dict(cfg.items(section))
        for section in cfg.sections()
        if section != "media_info"
    }


def _process_one_media(
    db,
    *,
    server_id: int,
    url: str,
    media: DiscoveredMedia,
    info: Optional[MediaCfgInfo],
    hint: dict,
    mode: str,
    parent_cfg_sections: Optional[dict],
    enabled_policy: Optional[Callable[[DiscoveredMedia], bool]] = None,
) -> UpsertOutcome:
    """Apply the decision tree to a single media candidate.

    Steps:
      1. Resolve canonical attributes (version, arch, short_name,
         relative_path, display name).
      2. Refuse if any strictly-required attribute is missing.
      3. Look up the canonical key in DB.  Four branches:
         - clé inconnue, nom libre → create + link.
         - clé inconnue, nom pris → disambiguate (foreign arch) OR
           adopt existing placeholder row (reconcile mode).
         - clé connue, nom différent ou ID match → keep row, just
           ensure the link exists.
         - clé connue, déjà liée → noop.
    """
    # ── Attribute cascade. ──────────────────────────────────────────
    version = _resolve_version(info, media, hint, url)
    arch = _resolve_arch(info, media, hint, url)
    section = media.section
    short_name = (
        (hint.get("short_name") or "").strip()
        or media.short_name
        or _short_name_for(section)
    )
    relative_path = (
        media.relative_path
        or _build_relative_path(version, arch, section, hint)
    )

    # (c) hard refusal if any strictly-required field is still empty.
    for field, value, sources in [
        ("mageia_version", version, ["manifest", "catalogue", "hint",
                                     "URL regex", "/etc/mageia-release"]),
        ("architecture", arch, ["manifest", "catalogue", "hint", "URL regex"]),
        ("short_name", short_name, ["hint", "section name"]),
        ("relative_path", relative_path, ["catalogue", "computed from version/arch/section"]),
    ]:
        if _is_placeholder(value):
            raise MediaTreeAttributeError(field, sources)

    # ── Display name via the canonical resolver. ────────────────────
    display_name = resolve_display_name(
        media_url=url + section,
        section=section,
        explicit_name=hint.get("name") or media.name or None,
        parent_cfg_sections=parent_cfg_sections,
        prefer="global",
    )

    is_official = (
        hint["is_official"]
        if "is_official" in hint
        else media.is_official
    )
    allow_unsigned = bool(hint.get("allow_unsigned", False))
    # Enabled value cascade: hint > policy callback > catalogue noauto flag.
    if "enabled" in hint:
        enabled = bool(hint["enabled"])
    elif enabled_policy is not None:
        enabled = bool(enabled_policy(media))
    else:
        enabled = not media.noauto
    update_media = bool(hint.get("update_media", media.is_update))
    priority = int(hint.get("priority", 50))

    # ── Decision tree on canonical key (version, arch, short_name). ─
    existing = db.get_media_by_version_arch_shortname(
        version, arch, short_name,
    )

    if existing is None:
        # Either a brand-new row, or a name collision against a row
        # that was inserted with a different canonical key (legacy
        # placeholder import is the typical case).
        try:
            safe_name = disambiguate_media_name(db, display_name, arch)
        except MediaNameCollision as collision:
            # Collision against a real row on the native arch.
            # In reconcile mode, treat as "adopt the existing row"
            # when it looks like a placeholder.
            existing_dict = collision.existing
            if mode == "reconcile" and _looks_like_placeholder_row(existing_dict):
                return _reconcile_placeholder(
                    db,
                    existing=existing_dict,
                    server_id=server_id,
                    version=version,
                    arch=arch,
                    short_name=short_name,
                    relative_path=relative_path,
                    is_official=is_official,
                    enabled=enabled,
                    update_media=update_media,
                    priority=priority,
                )
            return UpsertOutcome(
                action="skipped",
                media_id=None,  # candidate was not inserted
                media_name=display_name,
                short_name=short_name,  # the candidate's canonical short_name
                reason=(f"display name {display_name!r} already taken by "
                        f"media #{existing_dict.get('id')} "
                        f"(short_name={existing_dict.get('short_name')!r}) "
                        "on native arch — pass mode='reconcile' to adopt"),
            )

        media_id = db.add_media(
            name=safe_name,
            short_name=short_name,
            mageia_version=version,
            architecture=arch,
            relative_path=relative_path,
            is_official=is_official,
            allow_unsigned=allow_unsigned,
            enabled=enabled,
            update_media=update_media,
            priority=priority,
            url=None,
        )
        db.link_server_media(server_id, media_id)
        return UpsertOutcome(
            action="created",
            media_id=media_id,
            media_name=safe_name,
            short_name=short_name,
        )

    # Canonical key already exists. Either re-link or noop.
    media_id = existing["id"]
    if db.server_media_link_exists(server_id, media_id):
        return UpsertOutcome(
            action="noop",
            media_id=media_id,
            media_name=existing["name"],
            short_name=short_name,
        )
    db.link_server_media(server_id, media_id)
    return UpsertOutcome(
        action="relinked",
        media_id=media_id,
        media_name=existing["name"],
        short_name=short_name,
    )


def _looks_like_placeholder_row(row: dict) -> bool:
    """Return True if *row* carries the legacy-placeholder fingerprint.

    Recognises the sentinel triple ``mageia_version='unknown'`` /
    ``architecture='unknown'`` / ``relative_path=''`` written by
    pre-0.8 import paths.  Used by reconcile mode to decide whether
    an existing row can be adopted and repaired.
    """
    return (
        _is_placeholder(row.get("mageia_version"))
        or _is_placeholder(row.get("architecture"))
        or _is_placeholder(row.get("relative_path"))
    )


def _reconcile_placeholder(
    db,
    *,
    existing: dict,
    server_id: int,
    version: str,
    arch: str,
    short_name: str,
    relative_path: str,
    is_official: bool,
    enabled: bool,
    update_media: bool,
    priority: int,
) -> UpsertOutcome:
    """Update a legacy placeholder row with real values and link the server."""
    media_id = existing["id"]
    db.conn.execute(
        """
        UPDATE media SET
            short_name = ?,
            mageia_version = ?,
            architecture = ?,
            relative_path = ?,
            is_official = ?,
            enabled = ?,
            update_media = ?,
            priority = ?
        WHERE id = ?
        """,
        (short_name, version, arch, relative_path,
         int(is_official), int(enabled), int(update_media), priority,
         media_id),
    )
    db.conn.commit()
    if not db.server_media_link_exists(server_id, media_id):
        db.link_server_media(server_id, media_id)
    return UpsertOutcome(
        action="updated",
        media_id=media_id,
        media_name=existing["name"],
        short_name=short_name,
    )
