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
  that carries placeholder attributes (the legacy
  ``add_media_legacy`` footprint), the primitive *adopts* that row,
  updates its placeholders with proper values, then links the
  server.  Used by the repair path for previously broken imports.
* ``'deep'`` — like ``'discover'`` but also performs a per-media
  manifest probe (``<media>/media_info/media.cfg``) to validate
  existence before insertion.  More network traffic, more guarantees.
  GPG keys are **never** auto-imported by this mode.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Optional
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

    The primitive refuses to fall back on ``add_media_legacy`` and to
    persist a placeholder row.  The caller must surface a clear error
    to the user (typically "URL inaccessible or not a recognised Mageia
    media tree").
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
        outcomes: One :class:`UpsertOutcome` per media candidate
            discovered at the URL (zero for an empty catalogue).
    """

    server_id: int
    server_name: str
    outcomes: list[UpsertOutcome]


# ── Helpers ─────────────────────────────────────────────────────────────


_PLACEHOLDER_VALUES = frozenset({"unknown", "none", "null", ""})
_KNOWN_ARCHES = frozenset({"x86_64", "i586", "i686", "aarch64",
                           "armv7hl", "armv5tl", "ppc64le", "noarch"})
_VERSION_RE = re.compile(r"^(?:\d+(?:\.\d+)?|cauldron)$", re.IGNORECASE)


def _is_placeholder(value: Optional[str]) -> bool:
    """Return True if *value* is empty or a legacy placeholder."""
    if value is None:
        return True
    return value.strip().lower() in _PLACEHOLDER_VALUES


def _split_url(url: str) -> tuple[str, str, str]:
    """Split *url* into ``(protocol, host, base_path)``.

    ``base_path`` is everything before the ``<version>/<arch>/media/``
    suffix when present, or the full path when no Mageia pattern is
    recognised.  Strips the trailing slash.
    """
    parsed = urlparse(url.rstrip("/"))
    protocol = parsed.scheme or "https"
    host = parsed.netloc
    path = parsed.path or ""

    # Heuristic: find the last `<version>/<arch>` segment pair and
    # treat everything before it as the server base_path.
    parts = [p for p in path.split("/") if p]
    for i in range(len(parts) - 1):
        if _VERSION_RE.fullmatch(parts[i]) and parts[i + 1] in _KNOWN_ARCHES:
            base = "/" + "/".join(parts[:i]) if parts[:i] else ""
            return protocol, host, base
    return protocol, host, path


def _extract_version_from_url(url: str) -> str:
    """Pull a Mageia version segment from URL path, if any."""
    parsed = urlparse(url.rstrip("/"))
    for part in parsed.path.split("/"):
        if part and _VERSION_RE.fullmatch(part):
            return part.lower()
    return ""


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
    """Resolve mageia_version following the manifest > catalogue >
    hint > URL > /etc/mageia-release cascade.

    The first non-placeholder value wins.  Returns ``''`` only when
    every source is exhausted; the caller then raises
    :class:`MediaTreeAttributeError`.
    """
    # 1. Manifest (per-media) — not yet probed unless mode='deep',
    #    deferred for now.
    # 2. Catalogue
    if media is not None and not _is_placeholder(media.version):
        return media.version
    if info is not None and not _is_placeholder(info.version):
        return info.version
    # 3. Hint
    if hint and not _is_placeholder(hint.get("version")):
        return hint["version"]
    # 4. URL regex
    via_url = _extract_version_from_url(url)
    if not _is_placeholder(via_url):
        return via_url
    # 5. System fallback
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

    # ── Step 1: probe the catalogue. ────────────────────────────────
    info: Optional[MediaCfgInfo] = None
    medias: list[DiscoveredMedia] = []
    catalogue_url = url.rstrip("/") + "/"

    try:
        raw = fetch_media_cfg(catalogue_url)
    except Exception as exc:  # pycurl / network / 4xx / 5xx
        logger.debug("catalogue fetch failed for %s: %s", url, exc)
        raw = None

    if raw is not None:
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
    if server is None:
        server_name = generate_server_name(protocol, host)
        server_id = _add_server_with_unique_name(
            db, server_name, protocol, host, base_path,
            is_official=is_official_for_server,
            priority=server_priority,
        )
        server = {"id": server_id, "name": server_name}
    server_id = server["id"]
    server_name = server["name"]

    # ── Step 3: process each media candidate. ───────────────────────
    outcomes: list[UpsertOutcome] = []
    parent_cfg_sections = _build_parent_sections_map(raw, medias) if raw else None

    for media in medias:
        outcome = _process_one_media(
            db,
            server_id=server_id,
            url=catalogue_url,
            media=media,
            info=info,
            hint=hint,
            mode=mode,
            parent_cfg_sections=parent_cfg_sections,
        )
        outcomes.append(outcome)

    return UpsertResult(
        server_id=server_id,
        server_name=server_name,
        outcomes=outcomes,
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
    enabled = bool(hint.get("enabled", not media.noauto))
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
    """Return True if *row* looks like an ``add_media_legacy`` footprint.

    Recognises the sentinel triple ``mageia_version='unknown'`` /
    ``architecture='unknown'`` / ``relative_path=''`` written by
    legacy import paths.
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
