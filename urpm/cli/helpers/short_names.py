"""Auto-generation of media display and short names from a URL.

Feeds ``urpm media add --custom`` and ``urpm media discover`` : when
the user does not spell out ``--name`` / ``--shortname``, both
commands derive them mechanically from the mirror URL so that a
community media added with a single ``urpm media add --custom <url>``
lands with sensible, distinct identifiers.

Design goals
------------

* **Deterministic** — same URL, same names, no time-of-day surprise.
* **Discriminant** — short names must survive a database
  ``UNIQUE`` constraint even when two mirrors publish under similar
  hostnames.  Version and architecture blocks are added only when
  they carry information (release differs from the machine's
  current one, arch differs from the primary), so the common case
  stays short.
* **Extensible** — the ``AMBIGUOUS_HOSTS`` table maps a hostname
  first segment to a per-host mnemonic + canonical TLD when the
  bare compression is ambiguous (``mageia.org`` vs ``mageia.biz``,
  both compress to ``mg`` otherwise).

Compression rule (the ``blgrk`` function)
-----------------------------------------

For each dash-separated segment of the source string, lowercased :

* Drop every vowel (``a``, ``e``, ``i``, ``o``, ``u``, ``y``).
* Keep the first consonant of the segment.
* Keep any other consonant iff at least one of its immediate
  neighbours in the original string is a vowel.

Segments are re-joined with ``-``.  Examples : ``blogdrake`` →
``blgrk``, ``mageialinux-online`` → ``mglnx-nln``,
``distrib-coffee`` → ``dsrb-cff``.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from .media import KNOWN_ARCHES, _match_version_token


# Vowels considered for the blgrk rule.  ``y`` is included because
# French treats it as a vowel — the rule is applied to hostnames
# and channel names, most of which are Latin-script identifiers
# where treating ``y`` as a vowel gives readable results.
_VOWELS = frozenset("aeiouy")

# Hostname first segments that need TLD disambiguation.  Each entry
# carries the canonical TLD (dropped from the short form when
# present) and a human mnemonic that overrides the algorithmic
# compression (``mageia`` blgrks to ``mg`` which loses the identity
# users associate with the disttag ``mga``).
AMBIGUOUS_HOSTS: dict[str, dict[str, str]] = {
    "mageia": {"canonical_tld": "org", "short_mnemonic": "mga"},
}

# Hostname prefixes that carry no information about the mirror
# operator — the discriminant part comes right after.
_HOSTNAME_STRIP_PREFIXES = frozenset({"www", "ftp", "repo", "pub"})


def blgrk(text: str) -> str:
    """Compress ``text`` following the vowel-drop / consonant-touch rule.

    Case-insensitive on input, always lowercase on output.  Empty
    input returns empty output.  Dash-separated segments are
    processed individually and re-joined with ``-`` so a
    ``foo-bar`` name keeps its structure in the compressed form.
    """
    if not text:
        return ""
    parts = text.lower().split("-")
    return "-".join(_blgrk_segment(p) for p in parts if p)


def _blgrk_segment(segment: str) -> str:
    """Blgrk-compress a single segment (no dashes)."""
    result: list[str] = []
    first_consonant_seen = False
    n = len(segment)
    for i, char in enumerate(segment):
        if char in _VOWELS:
            continue
        if not first_consonant_seen:
            result.append(char)
            first_consonant_seen = True
            continue
        left = segment[i - 1] if i > 0 else ""
        right = segment[i + 1] if i + 1 < n else ""
        if left in _VOWELS or right in _VOWELS:
            result.append(char)
    return "".join(result)


def extract_server_names(hostname: str) -> tuple[str, str]:
    """Return ``(long_name, short_name)`` derived from a bare hostname.

    Strips the leading ``www`` / ``ftp`` / ``repo`` / ``pub`` prefix
    when present, then splits the remainder :

    * When the resulting first segment is registered in
      :data:`AMBIGUOUS_HOSTS`, the TLD is folded into the identity
      (``mageia.org`` → ``Mageia.Org`` long, ``mga`` short ;
      ``mageia.biz`` → ``Mageia.Biz`` long, ``mgabiz`` short).
    * Otherwise, only the first segment is kept — long in Title
      Case with the internal dashes preserved
      (``mageialinux-online`` → ``Mageialinux-Online``), short as
      the blgrk compression (``mglnx-nln``).

    Empty hostname returns ``("", "")``.  Never raises.
    """
    if not hostname:
        return "", ""
    segments = [s for s in hostname.split(".") if s]
    if not segments:
        return "", ""
    if segments[0] in _HOSTNAME_STRIP_PREFIXES and len(segments) > 1:
        segments = segments[1:]

    first = segments[0].lower()
    entry = AMBIGUOUS_HOSTS.get(first)
    if entry is not None and len(segments) >= 2:
        tld = segments[-1].lower()
        long_name = f"{_title_dashed(first)}.{tld.capitalize()}"
        if tld == entry["canonical_tld"]:
            short_name = entry["short_mnemonic"]
        else:
            short_name = entry["short_mnemonic"] + tld
        return long_name, short_name

    long_name = _title_dashed(first)
    short_name = blgrk(first)
    return long_name, short_name


def _title_dashed(text: str) -> str:
    """Title-case every dash-separated segment of *text*."""
    return "-".join(part.capitalize() for part in text.split("-") if part)


def extract_channel(
    relative_path: str, version: Optional[str], arch: Optional[str],
) -> str:
    """Extract the channel portion from a media's ``relative_path``.

    The channel is everything between the version and arch
    segments (blogdrake plate model, ``mageia10/free/x86_64`` →
    ``free``) or, when the path uses the officiel ``.../media/...``
    container, everything after the ``media`` marker
    (``10/x86_64/media/core/release`` → ``core-release``).

    Segments are joined with ``-`` and returned as-is (no blgrk —
    channel names like ``free`` / ``core`` / ``nonfree`` are short
    enough already, and compressing them costs readability without
    saving space).

    Returns an empty string when no meaningful channel emerges.
    """
    if not relative_path:
        return ""
    parts = [p for p in relative_path.split("/") if p]
    if not parts:
        return ""

    # Prefer the officiel container split — everything after ``media``
    # is the channel path.
    if "media" in parts:
        idx = parts.index("media")
        channel_parts = parts[idx + 1:]
    else:
        channel_parts = [
            p for p in parts
            if _match_version_token(p) is None and p not in KNOWN_ARCHES
        ]
    return "-".join(channel_parts)


def generate_media_names(
    url: str,
    *,
    current_release: Optional[str] = None,
    primary_arch: Optional[str] = None,
    override_name: Optional[str] = None,
    override_shortname: Optional[str] = None,
) -> dict:
    """Generate ``(name, short_name)`` for a media URL.

    Structure of the resulting identifiers, from most compact to
    most explicit :

    * ``<server>_<channel>`` when the URL matches the current
      machine's release and primary architecture.
    * ``<server>_<arch>_<channel>`` when the URL targets a
      secondary architecture on the current release.
    * ``<server>_<release>_<arch>_<channel>`` when the URL targets
      a different release (arch is spelled out even if it happens
      to be the primary one — a release switch is a big enough
      event to warrant full disambiguation).

    Any explicit ``override_name`` / ``override_shortname``
    replaces the corresponding auto-generated block, so callers can
    let the user override just one identifier from the CLI while
    letting the other be derived.

    Returns a dict ``{"name": ..., "short_name": ...,
    "version": ..., "arch": ..., "channel": ...}`` so callers get
    both the identifiers and the pieces they were built from
    without re-parsing the URL.
    """
    parsed = urlparse(url.rstrip("/"))
    hostname = parsed.hostname or ""
    server_long, server_short = extract_server_names(hostname)

    # Version and arch detection : reuse the CLI helper's rules so
    # a URL that fails to expose either bit yields empty release /
    # arch blocks and lets the caller error out (or prompt).
    parts = [p for p in (parsed.path or "").split("/") if p]
    version: Optional[str] = None
    arch: Optional[str] = None
    version_idxs = [i for i, p in enumerate(parts)
                    if _match_version_token(p) is not None]
    arch_idxs = [i for i, p in enumerate(parts) if p in KNOWN_ARCHES]
    if len(version_idxs) == 1 and len(arch_idxs) == 1:
        version = _match_version_token(parts[version_idxs[0]])
        arch = parts[arch_idxs[0]]

    # Reconstruct the relative_path the caller will store — pivot
    # is the first of the two detected segments (mirrors
    # :func:`urpm.cli.helpers.media.parse_custom_media_url`).
    if version_idxs and arch_idxs:
        pivot = min(version_idxs[0], arch_idxs[0])
        relative_path = "/".join(parts[pivot:])
    else:
        relative_path = "/".join(parts)
    channel = extract_channel(relative_path, version, arch)

    # Assemble the identifiers.  release_block is added only when
    # the release differs from the current one ; arch_block when
    # the arch differs from the primary — or, in the release-diff
    # case, systematically (as decided during the spec talk).
    release_diff = (
        version is not None and current_release is not None
        and version != current_release
    )
    arch_diff = (
        arch is not None and primary_arch is not None
        and arch != primary_arch
    )

    short_blocks = [server_short] if server_short else []
    long_blocks = [server_long] if server_long else []
    if release_diff and version is not None and arch is not None:
        short_blocks.extend([version, arch])
        long_blocks.extend([version.capitalize(), arch])
    elif arch_diff and arch is not None:
        short_blocks.append(arch)
        long_blocks.append(arch)
    if channel:
        short_blocks.append(channel)
        long_blocks.append(_title_dashed(channel))

    short_name = "_".join(b for b in short_blocks if b)
    long_name = "_".join(b for b in long_blocks if b)

    if override_name:
        long_name = override_name
    if override_shortname:
        short_name = override_shortname

    return {
        "name": long_name,
        "short_name": short_name,
        "version": version,
        "arch": arch,
        "channel": channel,
    }
