"""Shared test fixtures and helpers for urpm-ng.

Two entry points:

* :func:`load_media_cfg` — load a ``.cfg`` from ``media_cfg/`` by name,
  returning its raw text (UTF-8).  Combine with
  ``monkeypatch.setattr(urpm.core.media_cfg, 'fetch_media_cfg',
  lambda url, timeout=10: load_media_cfg('name'))`` to bypass the network
  in unit tests.

* :func:`assert_well_formed_media` — invariant checks for media records
  created through the upsert_media_tree pipeline.  See its docstring for
  the four invariants (a/b/c/d) enforced.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

_FIXTURES_DIR = Path(__file__).parent / "media_cfg"


def load_media_cfg(name: str) -> str:
    """Read a media.cfg fixture by name and return its text content.

    Args:
        name: Fixture filename without the ``.cfg`` extension
            (e.g. ``"official_mageia_9_x86_64"``).

    Returns:
        The raw UTF-8 text content of the fixture file.

    Raises:
        FileNotFoundError: If no fixture with this name exists.
    """
    path = _FIXTURES_DIR / f"{name}.cfg"
    if not path.exists():
        available = sorted(p.stem for p in _FIXTURES_DIR.glob("*.cfg"))
        raise FileNotFoundError(
            f"media.cfg fixture {name!r} not found; available: {available}"
        )
    return path.read_text(encoding="utf-8")


# ── Invariants enforced by the upsert_media_tree pipeline ─────────────
# These mirror the four contracts from doc/AUDIT_PIPELINE_MEDIA.md:
#   (a) Every media must be linked to at least one server.
#   (b) Display name must not be ugly (no mga{ver}-{class}-{type} pattern).
#   (c) Required attributes must not be empty/unknown placeholders.
#   (d) Reserved for cross-cutting checks performed by callers
#       (e.g. that the media.cfg manifest was consulted) — verified at
#       the call site rather than on the record itself.

_UGLY_NAME_RE = re.compile(
    r"^(mga\d+|MGA\d+)-",  # e.g. mga9-core-release, MGA10-nonfree-updates
    re.IGNORECASE,
)

_PLACEHOLDER_VALUES = frozenset({"unknown", "", "none", "null"})


def assert_well_formed_media(
    media: Dict[str, Any],
    servers: list | None = None,
) -> None:
    """Assert that *media* satisfies the upsert_media_tree contracts.

    Args:
        media: A media record as returned by ``db.get_media()`` or
            equivalent (must have keys ``name``, ``short_name``,
            ``mageia_version``, ``architecture``, ``relative_path``).
        servers: Optional list of servers linked to this media (as
            returned by ``db.get_servers_for_media(media['id'])``).
            When provided, asserts that at least one is present
            (invariant a).  When ``None``, this check is skipped — for
            callers that intentionally test the unlinked transient
            state.

    Raises:
        AssertionError: If any invariant is violated.  The message
            names the specific invariant for easy diagnosis.
    """
    # (b) Display name must not be ugly.
    name = media.get("name", "")
    assert name, "invariant (b): media.name is empty"
    assert not _UGLY_NAME_RE.match(name), (
        f"invariant (b): media.name {name!r} matches the ugly "
        f"mga{{ver}}-{{class}}-{{type}} pattern that resolve_display_name "
        f"is supposed to prevent"
    )

    # (c) Required attributes must not be placeholders.
    for field in ("short_name", "mageia_version", "architecture"):
        value = (media.get(field) or "").strip().lower()
        assert value not in _PLACEHOLDER_VALUES, (
            f"invariant (c): media.{field} is a placeholder "
            f"({media.get(field)!r}) — upsert_media_tree must resolve "
            f"a real value via the catalogue / manifest / fallback "
            f"cascade or refuse the operation"
        )

    # (c bis) relative_path must point somewhere.
    rel_path = media.get("relative_path", "")
    assert rel_path, (
        "invariant (c): media.relative_path is empty — server-side "
        "HEAD scans need this to locate the media tree"
    )

    # (a) At least one server linked.
    if servers is not None:
        assert servers, (
            f"invariant (a): media {name!r} has no server linked — "
            f"upsert_media_tree must produce the server↔media link "
            f"in the same transaction as the media insert"
        )
