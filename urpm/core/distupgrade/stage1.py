"""Stage 1 — media swap (SPEC_DISTUPGRADE §4.1).

Migrates the machine's media set from the source release to the
target release :

- **Enabled source media that have a target equivalent** →
  disabled with ``disabled_by='distupgrade'``.  The migration is a
  soft-disable so the user can inspect ``.disabled_by`` post-mortem
  and, at ``--abort`` time, we can flip them back on trivially.
- **Enabled source media that have no target equivalent** (custom
  third-party repos that don't ship an mga N+1 tree) →
  ``disabled_by='distupgrade_orphan'``.  Reported separately in
  Stage 4 so the user knows what won't come back.
- **Target release media** — inserted via
  :func:`urpm.core.media_pipeline.upsert_media_tree` on each
  existing server, one call per server.  Idempotent : a second run
  finds the rows already there and no-ops.

Contracts :

- The distupgrade lock is already held by the orchestrator ; media
  writes bypass ``sync_lock`` re-acquisition.
- ``.state`` is bumped to ``stage1_running`` at entry and
  ``media_swapped`` on success — a crash mid-swap is caught by
  the read at ``--resume`` time.
- Custom media without an ``upsert_media_tree``-parsable URL are
  left alone with ``disabled_by='distupgrade_orphan'``.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from ..database import PackageDatabase
    from .version import ReleaseIdentity


logger = logging.getLogger(__name__)


class Stage1Error(Exception):
    """Raised for any Stage 1 failure."""


# ── Display-name suffix (frees display names for target-release upsert) ──


# Distinctive marker so ``_stripped_name`` never false-positives on a
# legitimate user name that happens to end in ``[foo]``.
_NAME_SUFFIX_RE = re.compile(r"\s*\[dg:[^\]]+\]\s*$")


def _suffixed_name(current: str, mageia_version: str) -> str:
    """Append ``[dg:<version>]`` to ``current`` unless already present.

    Rationale : ``media.name`` carries a UNIQUE constraint in the
    schema.  A fresh mga N+1 « Core Release » cannot be inserted
    while the disabled mga N « Core Release » still claims the name.
    Suffixing the disabled row (``Core Release [dg:9]``) frees the
    plain name so ``_insert_target_media`` succeeds without renaming
    the incoming target rows — post-distupgrade the user sees the
    mga N rows as clearly disabled + suffixed, mga N+1 rows as the
    canonical media set.

    ``--abort`` uses :func:`_stripped_name` (inverse) to restore
    when reverting.  The ``dg:`` prefix inside the brackets keeps
    the marker distinctive so we never strip a legitimate
    user-authored ``[foo]`` suffix.
    """
    if _NAME_SUFFIX_RE.search(current):
        return current
    return f"{current} [dg:{mageia_version}]"


def _stripped_name(current: str) -> str:
    """Inverse of :func:`_suffixed_name` for ``--abort`` restore."""
    return _NAME_SUFFIX_RE.sub("", current)


# ── Undo journal ─────────────────────────────────────────────────────


# Media columns Stage 1 mutates and ``--abort`` must restore verbatim.
_UNDO_TRACKED_COLS = ("enabled", "disabled_by", "name")


def _snapshot_media_row(row) -> dict:
    """Capture the pre-mutation state of a media row for ``--abort``.

    Returns ``{id, enabled, disabled_by, name}`` — exactly the fields
    Stage 1's UPDATEs touch.  ``--abort`` restores these verbatim, so
    the DB after abort is bit-for-bit the state it was before the
    Stage 1 pass.
    """
    d = dict(row)
    return {"id": d["id"], **{c: d.get(c) for c in _UNDO_TRACKED_COLS}}


def _try_transpose_string(s: str, src: str, tgt: str) -> Optional[str]:
    """Return ``s`` with any release-marker occurrence rewritten.

    Patterns handled, in priority order :

    1. ``mga<src>`` → ``mga<tgt>`` — Mageia disttag anywhere.
    2. Segment ``<src>`` where segments are ``/``-separated — covers
       ``/9/`` in the middle, ``9/`` at start, ``/9`` at end, and a
       bare ``9`` alone.  Matched segment-wise (not by ``str.replace``)
       so a ``9`` inside a coincidental hash or file name is left alone.

    Returns ``None`` when no substitution applied — the caller knows
    it can't transpose the row and must mark it orphan.  Non-numeric
    source identities (``"cauldron"``) don't transpose : cauldron is
    a rolling target, N→N+1 arithmetic doesn't apply.
    """
    if not s or not src.isdigit():
        return None
    tag_src, tag_tgt = f"mga{src}", f"mga{tgt}"
    if tag_src in s:
        return s.replace(tag_src, tag_tgt)
    # Segment-wise bump — treats the string as ``/``-separated tokens
    # and only rewrites tokens that are exactly ``src``.  Handles
    # ``9/x86_64/...`` (leading), ``.../x86_64/9`` (trailing),
    # ``.../9/...`` (middle), and a bare ``9``.
    parts = s.split("/")
    if src in parts:
        return "/".join(tgt if p == src else p for p in parts)
    return None


def _probe_url_reachable(url: str, *, timeout: int = 5) -> bool:
    """Best-effort HEAD/exists probe on the target-transposition URL.

    Not authoritative — probes only the base URL, not ``media.cfg``
    or the synthesis specifically.  A follow-up ``urpm media
    update`` will surface real breakage.  We use it as a cheap
    reachability gate so we don't insert obviously-dead media rows.
    """
    if not url:
        return False
    if url.startswith("file://"):
        return Path(url[7:]).exists()
    if url.startswith(("http://", "https://")):
        try:
            import pycurl
            c = pycurl.Curl()
            c.setopt(pycurl.URL, url)
            c.setopt(pycurl.NOBODY, 1)
            c.setopt(pycurl.CONNECTTIMEOUT, timeout)
            c.setopt(pycurl.TIMEOUT, timeout)
            c.setopt(pycurl.FOLLOWLOCATION, 1)
            c.setopt(pycurl.MAXREDIRS, 5)
            c.perform()
            code = c.getinfo(pycurl.RESPONSE_CODE)
            c.close()
            return 200 <= code < 400
        except Exception:  # noqa: BLE001
            return False
    return False


def _transpose_third_party_media(
    db: "PackageDatabase",
    source_identity: str,
    target_identity: str,
    *,
    probe: bool = True,
    undo_journal: dict = None,
) -> Tuple[List[dict], List[dict]]:
    """Attempt ``mga<src>`` → ``mga<tgt>`` transposition for tier media.

    SPEC_DISTUPGRADE §4.1 mechanism #1 (naming-convention substitution).
    For every enabled non-official (``is_official=0``) media whose
    ``mageia_version`` matches the source, tries to rewrite the URL,
    probes it, and either :

    - inserts a target-version equivalent row (enabled) then disables
      the source with ``disabled_by='distupgrade'`` — same treatment
      as officials, so Stage 4 reports it uniformly ;
    - marks the source ``disabled_by='distupgrade_orphan'`` when no
      transposition rule matches or when the probe fails.

    Returns ``(activated, orphaned)`` — lists of source row dicts.
    The `probe` flag lets tests bypass the network reachability check.
    """
    from ..config import build_server_url

    conn = db._get_connection()
    # Third-party (is_official=0) rows tagged with the source release.
    # Accept both storage formats : the documented ``"9"`` and the
    # legacy ``"mga9"`` produced by older auto-discovery paths.
    rows = conn.execute("""
        SELECT id, name, short_name, mageia_version, architecture,
               relative_path, url, mirrorlist, priority, is_official,
               allow_unsigned, update_media, enabled, disabled_by
        FROM media
        WHERE enabled = 1
          AND is_official = 0
          AND mageia_version IN (?, ?)
    """, (source_identity, f"mga{source_identity}")).fetchall()

    activated: List[dict] = []
    orphaned: List[dict] = []
    for r in rows:
        row = dict(r)

        # The version marker lives in ``relative_path`` (Mageia
        # mirror convention : ``<version>/<arch>/media/...``) —
        # ``media.url`` is a legacy column left NULL on modern rows.
        # ``server.base_path`` stays untouched : a physical mirror
        # hosts mga N and mga N+1 side by side, only the media's
        # per-version segment bumps.
        new_relpath = _try_transpose_string(
            row.get("relative_path") or "",
            source_identity, target_identity)
        new_name = _try_transpose_string(
            row.get("name") or "", source_identity, target_identity)
        new_short = _try_transpose_string(
            row.get("short_name") or "", source_identity, target_identity)

        if not new_relpath:
            # Not a warning — the aggregate count is reported in the
            # translated Stage 1 summary and the row is listed again
            # in the Stage 4 orphan-media section.  Keep the detail
            # at info level for post-mortem debugging only.
            logger.info(
                "stage1 : cannot transpose %s : relative_path %r "
                "has no 'mga%s' marker and no '%s' segment — "
                "marking orphan",
                row["name"], row.get("relative_path"),
                source_identity, source_identity)
            _mark_orphan(conn, db, row, undo_journal)
            orphaned.append(row)
            continue

        # Enumerate every server hosting the source media.  Some
        # may lag on mga N+1 — probe each and keep only the ones
        # that respond, so a media survives when one mirror is
        # behind but the others already ship the target tree.
        try:
            linked_servers = db.get_servers_for_media(
                row["id"], enabled_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stage1 : cannot enumerate servers for %s : %s",
                row["name"], exc)
            linked_servers = []

        reachable_servers: List[dict] = []
        for srv in linked_servers:
            probe_url = f"{build_server_url(srv).rstrip('/')}/{new_relpath}"
            if not probe or _probe_url_reachable(probe_url):
                reachable_servers.append(srv)

        if not reachable_servers:
            # info-level : same rationale as above — the row shows up
            # in the Stage 1 aggregate count and again in the Stage 4
            # orphan-media section, both properly translated.
            logger.info(
                "stage1 : none of %d server(s) for %s serve %r — "
                "marking orphan",
                len(linked_servers), row["name"], new_relpath)
            _mark_orphan(conn, db, row, undo_journal)
            orphaned.append(row)
            continue

        try:
            new_id = db.add_media(
                name=new_name or f"{row['name']}-mga{target_identity}",
                short_name=new_short or f"{row['short_name']}"
                                        f"-mga{target_identity}",
                mageia_version=target_identity,
                architecture=row.get("architecture") or "",
                relative_path=new_relpath,
                is_official=False,
                allow_unsigned=bool(row.get("allow_unsigned")),
                enabled=True,
                update_media=bool(row.get("update_media")),
                priority=row.get("priority", 50),
                url=None,          # modern rows carry no legacy url
                mirrorlist=None,
            )
            if undo_journal is not None and new_id is not None:
                undo_journal["created_media_ids"].append(int(new_id))
            for srv in reachable_servers:
                db.link_server_media(srv["id"], new_id)
            with db._lock:
                if undo_journal is not None:
                    undo_journal["modified_media"].append(
                        _snapshot_media_row(row))
                conn.execute(
                    "UPDATE media SET enabled=0, "
                    "disabled_by='distupgrade', name=? WHERE id=?",
                    (_suffixed_name(row["name"],
                                    row["mageia_version"]),
                     row["id"]))
                conn.commit()
            activated.append(row)
            logger.info(
                "stage1 : transposed %s (relpath %s → %s) on %d "
                "server(s)",
                row["name"], row.get("relative_path"), new_relpath,
                len(reachable_servers))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stage1 : transpose insert failed for %s : %s",
                row["name"], exc)
            _mark_orphan(conn, db, row, undo_journal)
            orphaned.append(row)
    return activated, orphaned


def _mark_orphan(conn, db, row: dict,
                 undo_journal: Optional[dict]) -> None:
    """Disable ``row`` with ``disabled_by='distupgrade_orphan'``.

    Snapshots into ``undo_journal`` so ``--abort`` restores the
    row bit-for-bit.  Extracted so the orphan branches in the
    transposition loop stay compact.
    """
    with db._lock:
        if undo_journal is not None:
            undo_journal["modified_media"].append(
                _snapshot_media_row(row))
        conn.execute(
            "UPDATE media SET enabled=0, "
            "disabled_by='distupgrade_orphan', name=? WHERE id=?",
            (_suffixed_name(row["name"], row["mageia_version"]),
             row["id"]))
        conn.commit()


def _disable_source_media(db: "PackageDatabase",
                          source_identity: str,
                          *,
                          undo_journal: dict = None) -> List[dict]:
    """Mark every enabled source-release media as ``distupgrade``.

    Returns the affected rows so the caller can log / report on
    them.  A media whose ``mageia_version`` doesn't match the
    source identity is left alone — likely a custom repo or a
    parallel release the user maintains on the side.

    ``undo_journal`` (optional) : populated with a
    :func:`_snapshot_media_row` per mutated row so ``--abort`` can
    restore each field verbatim.
    """
    conn = db._get_connection()
    rows = conn.execute("""
        SELECT id, name, mageia_version, is_official,
               enabled, disabled_by
        FROM media
        WHERE enabled = 1
          AND mageia_version = ?
    """, (source_identity,)).fetchall()

    if not rows:
        return []

    with db._lock:
        for row in rows:
            if undo_journal is not None:
                undo_journal["modified_media"].append(
                    _snapshot_media_row(row))
            conn.execute("""
                UPDATE media
                SET enabled = 0,
                    disabled_by = 'distupgrade',
                    name = ?
                WHERE id = ?
            """, (_suffixed_name(row["name"], row["mageia_version"]),
                  row["id"]))
        conn.commit()

    return [dict(row) for row in rows]


def _mark_third_party_orphans(db: "PackageDatabase",
                              source_identity: str,
                              target_identity: str,
                              *,
                              undo_journal: dict = None) -> List[dict]:
    """Mark enabled non-source, non-target media as
    ``distupgrade_orphan``.

    These are custom / third-party media (mgabiz, RPMFusion,
    non-standard repos) that don't ship a target-release tree.
    Downstream Stage 4 will surface them so the user can
    re-enable / find replacements manually.
    """
    conn = db._get_connection()
    rows = conn.execute("""
        SELECT id, name, mageia_version, is_official,
               enabled, disabled_by
        FROM media
        WHERE enabled = 1
          AND mageia_version NOT IN (?, ?)
    """, (source_identity, target_identity)).fetchall()

    if not rows:
        return []

    with db._lock:
        for row in rows:
            if undo_journal is not None:
                undo_journal["modified_media"].append(
                    _snapshot_media_row(row))
            conn.execute("""
                UPDATE media
                SET enabled = 0,
                    disabled_by = 'distupgrade_orphan',
                    name = ?
                WHERE id = ?
            """, (_suffixed_name(row["name"], row["mageia_version"]),
                  row["id"]))
        conn.commit()

    return [dict(row) for row in rows]


def _collect_source_enabled_short_names(db: "PackageDatabase",
                                        source_identity: str) -> set:
    """Snapshot which official mga N media the user actually had enabled.

    Called BEFORE :func:`_disable_source_media` flips the flag, so the
    resulting set represents the user's authoritative choice
    (Nonfree/Tainted opt-in etc.).  Fed into
    :func:`_insert_target_media` so the mga N+1 counterparts inherit
    the same enable state — otherwise :meth:`upsert_media_tree` would
    honour ``media.noauto=1`` and silently disable Nonfree/Tainted
    on the target release, contradicting the user's mga N config.
    """
    conn = db._get_connection()
    rows = conn.execute("""
        SELECT short_name FROM media
        WHERE enabled = 1
          AND is_official = 1
          AND mageia_version = ?
    """, (source_identity,)).fetchall()
    return {r["short_name"] for r in rows if r["short_name"]}


def _insert_target_media(db: "PackageDatabase",
                         target: "ReleaseIdentity",
                         arch: str,
                         *,
                         source_enabled_short_names: set = None,
                         undo_journal: dict = None,
                         ) -> Tuple[List[str], List[str]]:
    """Call :func:`upsert_media_tree` for every official server.

    ``source_enabled_short_names`` — set of short_names that were
    enabled on the source release, as captured by
    :func:`_collect_source_enabled_short_names` before the disable
    pass.  When provided, an ``enabled_policy`` is threaded into
    :func:`upsert_media_tree` so target media inherit the source
    enable state ; when ``None``, the catalogue's ``noauto`` flag
    wins (the pre-v0.9 behaviour).

    Returns ``(created_urls, failed_servers)`` — ``created_urls``
    are the catalogue URLs successfully upserted, ``failed_servers``
    the servers whose upsert raised (network error, 404, unreachable
    mirror).  A partial success is fine : as long as one server
    surfaced the target catalogue, downstream Stage 2 can download
    from any of them.
    """
    from ..config import build_server_url
    from ..media_pipeline import upsert_media_tree, MediaTreeError

    identity_for_url = (
        target.identity if target.numeric is None
        else target.numeric  # cauldron:11 → URL uses 11 unless server carries a url_version override
    )
    # Prefer the server's own url_version if set (SPEC v0.8.7 rules).

    conn = db._get_connection()
    servers = conn.execute("""
        SELECT id, name, protocol, host, base_path, url_version,
               is_official
        FROM server
        WHERE is_official = 1 AND enabled = 1
    """).fetchall()

    if not servers:
        return [], []

    def _inherit_source_enable(discovered):
        """enabled_policy honouring the mga N user choice.

        Returns True iff a source-release counterpart with the same
        ``short_name`` was enabled.  Falls back to the noauto flag
        when no mapping is available (fresh install case).
        """
        if source_enabled_short_names is None:
            return not discovered.noauto
        return discovered.short_name in source_enabled_short_names

    created: List[str] = []
    failed: List[str] = []
    for srv in servers:
        srv_dict = dict(srv)
        base_url = build_server_url(srv_dict)
        # Server-specific url_version wins for the URL segment
        url_seg = srv_dict.get("url_version") or target.identity
        # Target arch/version tree URL
        catalogue_url = (
            f"{base_url.rstrip('/')}/{url_seg}/{arch}/media/"
        )
        try:
            result = upsert_media_tree(
                db, catalogue_url, mode="reconcile",
                enabled_policy=_inherit_source_enable,
            )
            # Capture what upsert created for the undo journal.
            if undo_journal is not None:
                if getattr(result, "server_was_created", False):
                    undo_journal["created_server_ids"].append(
                        int(result.server_id))
                for outcome in result.outcomes:
                    if outcome.action == "created" and \
                            outcome.media_id is not None:
                        undo_journal["created_media_ids"].append(
                            int(outcome.media_id))
            # Reconcile mode doesn't re-flip ``enabled`` on rows that
            # already exist (they've been through a prior Stage 1 pass
            # or a manual ``urpm media add``).  Explicitly re-align
            # their enable state with the source-side user choice ;
            # capture the pre-flip snapshot so ``--abort`` restores it.
            if source_enabled_short_names is not None:
                for outcome in result.outcomes:
                    row = db.get_media(outcome.media_name)
                    if not row:
                        continue
                    want_enabled = int(
                        outcome.short_name in source_enabled_short_names)
                    if row.get("enabled") != want_enabled:
                        if undo_journal is not None:
                            # Only snapshot rows that pre-existed —
                            # freshly-created rows will be deleted
                            # by the undo, not restored.
                            if outcome.action != "created":
                                undo_journal["modified_media"].append(
                                    _snapshot_media_row(row))
                        with db._lock:
                            conn.execute(
                                "UPDATE media SET enabled=? WHERE id=?",
                                (want_enabled, row["id"]))
                            conn.commit()
            created.append(catalogue_url)
            n_out = len(result.outcomes)
            n_created = sum(
                1 for o in result.outcomes if o.action == "created")
            logger.info(
                "stage1 : upsert %s → %d media in catalogue "
                "(%d created)",
                catalogue_url, n_out, n_created,
            )
        except (MediaTreeError, Exception) as exc:  # noqa: BLE001
            logger.warning("target-media upsert failed for %s : %s",
                           srv_dict["name"], exc)
            failed.append(srv_dict["name"])
    return created, failed


def run_stage1(
    db: "PackageDatabase",
    *,
    source_identity: str,
    target: "ReleaseIdentity",
    arch: str = None,
) -> dict:
    """Swap media from the source identity to the target release.

    Returns a summary dict :

    - ``disabled_source`` : rows flagged ``disabled_by='distupgrade'``
    - ``disabled_orphan`` : rows flagged ``disabled_by='distupgrade_orphan'``
    - ``created_urls``    : target catalogue URLs successfully upserted
    - ``failed_servers``  : official servers whose upsert failed

    The state file is bumped to ``stage1_running`` at entry and
    ``media_swapped`` on success (§4.6 state machine).
    """
    import platform
    from .state import write_state

    if arch is None:
        arch = platform.machine()

    logger.info("Stage 1 : media swap %s → %s (arch=%s)",
                source_identity, target.display(), arch)

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_state({
        "version_from": source_identity,
        "version_to": target.display(),
        "started_at": started_at,
        "stage": "stage1_running",
    }, db)

    # Snapshot which official mga N media the user had enabled BEFORE
    # the disable pass touches them — needed so target-release rows
    # inherit the user's Nonfree/Tainted opt-in rather than defaulting
    # to noauto=1.
    source_enabled = _collect_source_enabled_short_names(
        db, source_identity)

    # Undo journal populated by every mutating helper.  Persisted into
    # ``.state.stage1_undo`` at end of Stage 1 ; consumed by
    # ``urpm distupgrade --abort`` to reverse every mutation verbatim.
    undo_journal = {
        "modified_media": [],       # snapshots for restore
        "created_media_ids": [],    # DELETE these
        "created_server_ids": [],   # DELETE these (cascades)
    }

    # SPEC §4.1 : tier media first — transposition attempt via naming
    # convention BEFORE the coarse source-disable pass, otherwise
    # `_disable_source_media` would eat them uniformly and lose the
    # user's target-side equivalents.
    tp_activated, tp_orphaned = _transpose_third_party_media(
        db, source_identity, target.identity,
        undo_journal=undo_journal)

    disabled_source = _disable_source_media(
        db, source_identity, undo_journal=undo_journal)
    disabled_orphan = _mark_third_party_orphans(
        db, source_identity, target.identity,
        undo_journal=undo_journal)
    # Merge tier orphans into the reported orphan list so Stage 4
    # sees them with the same treatment.
    disabled_orphan.extend(tp_orphaned)

    created_urls, failed_servers = _insert_target_media(
        db, target, arch,
        source_enabled_short_names=source_enabled,
        undo_journal=undo_journal,
    )
    if not created_urls:
        raise Stage1Error(
            "no target-release media could be inserted ; "
            "check network connectivity and mirror configuration.")

    write_state({
        "version_from": source_identity,
        "version_to": target.display(),
        "started_at": started_at,
        "stage1_undo": undo_journal,
        "stage": "media_swapped",
    }, db)

    return {
        "disabled_source": disabled_source,
        "disabled_orphan": disabled_orphan,
        "third_party_activated": tp_activated,
        "created_urls": created_urls,
        "failed_servers": failed_servers,
    }
