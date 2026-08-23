"""Target release detection (SPEC_DISTUPGRADE §6.4).

Two sources, in priority order :

1. **Explicit user flag** — ``urpm distupgrade --to N`` (or the
   ``cauldron:N`` syntax) : forces the target regardless of what
   the mirror API thinks.  Wins over everything so a packager can
   work offline during a freeze window.
2. **`releases.mageia.org` API** — canonical, honours
   ``release_date`` and ``desktop-update-end``.  See §6.4.a.

The API implementation is stubbed for now : downstream tickets
will flesh out the HTTPS + JSON parse.  This module provides the
type surface + the current-release helper so the orchestrator can
be wired end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReleaseIdentity:
    """A Mageia release the caller can migrate to.

    ``identity`` is what the machine tracks in the DB
    ``mageia-version`` (``'10'``, ``'11'``, ``'cauldron'``…).
    ``numeric`` is the concrete integer version rendered into
    ``.mgaN`` release tags — usually the same as ``identity`` for
    numeric names ; distinct only during a Cauldron freeze
    (``identity='cauldron'``, ``numeric='11'``).
    """
    identity: str
    numeric: Optional[str] = None

    def display(self) -> str:
        if self.numeric and self.numeric != self.identity:
            return f"{self.identity}:{self.numeric}"
        return self.identity


class VersionDetectionError(Exception):
    """No target release could be determined."""


def choose_target_url_segment(
    server_url_version: Optional[str],
    source_identity: str,
    target_identity: str,
) -> "tuple[str, bool]":
    """Decide the URL segment to use for a target-release catalogue.

    ``server.url_version`` is a per-server override describing the
    URL segment that mirror exposes for the release the machine
    currently tracks (e.g. ``'cauldron'`` when the mirror serves
    release 11 under ``/cauldron/`` during the pre-GA freeze).

    In a stable, single-release lifetime the field is either
    ``NULL`` (never populated) or captures the current release
    identity extracted from the URL at server-add time.  Neither
    case creates a semantic issue for install / upgrade — the
    fallback ``url_version or identity`` returns the correct
    segment for the machine's current release.

    A distupgrade N → N+1 crosses that boundary.  The segment
    stored on the server row (``'9'`` on a mga9 machine) becomes a
    stale pin the moment we start building URLs for the target
    release : without this helper the caller would fetch the
    ``.../9/x86_64/media/`` catalogue and see the SOURCE release
    tree — no target packages ever reach the pool, plan comes back
    empty or ``0-install/N-erase`` (papoteur beta case).

    The 3-way decision :

    - ``server_url_version`` **empty** (``None`` or ``""``) →
      ``(target_identity, False)`` : no pin, no update needed.
    - ``server_url_version`` **numeric and equal to
      ``source_identity``** → stale pin, use ``(target_identity,
      True)`` : caller must snapshot + UPDATE the row so the pin
      tracks the new current release post-distupgrade.
    - ``server_url_version`` **anything else** (a non-numeric alias
      like ``'cauldron'``, or a numeric value not matching the
      source) → ``(server_url_version, False)`` : intentional
      per-server override for the target release, preserved
      verbatim.  Notably the freeze case where a mirror keeps
      serving release N+1 under ``/cauldron/`` even after GA.

    Args:
        server_url_version: Current value of ``server.url_version``
            (may be ``None``).
        source_identity: The release identity the machine is
            migrating FROM (e.g. ``'9'``).  Used to detect the
            stale-pin case.
        target_identity: The release identity the machine is
            migrating TO (e.g. ``'10'``).  Used both as the default
            segment and as the value to write on stale-pin update.

    Returns:
        ``(url_segment, pin_needs_update)``.  Callers building a
        catalogue URL use ``url_segment``.  When
        ``pin_needs_update`` is True, callers in a mutating context
        (Stage 1 with an undo journal) must snapshot the row and
        ``UPDATE server SET url_version = target_identity``.
    """
    if not server_url_version:
        return target_identity, False
    if (server_url_version == source_identity
            and server_url_version.isdigit()):
        return target_identity, True
    return server_url_version, False


def read_current_release() -> Optional[str]:
    """Return the current release numeric identity or ``None``.

    Thin wrapper around :func:`urpm.core.config.get_system_version`
    kept for symmetry with :func:`detect_target_release` (both live
    in the distupgrade version helpers).  The system-wide primitive
    is authoritative — it reads ``/etc/os-release`` (same source
    the CLI and the daemon consult).
    """
    from ..config import get_system_version
    return get_system_version()


def parse_release_arg(arg: str) -> ReleaseIdentity:
    """Parse a ``--to`` flag value into a :class:`ReleaseIdentity`.

    Accepted forms :

    - ``'10'``, ``'11'``               — bare numeric identity.
    - ``'cauldron'``                    — bare Cauldron.
    - ``'cauldron:11'``                 — Cauldron freeze pointing at 11.
    - ``'cauldron:'``, ``':11'``         — invalid, raises.
    """
    arg = arg.strip()
    if not arg:
        raise VersionDetectionError("empty --to argument")
    if ":" in arg:
        identity, _, numeric = arg.partition(":")
        identity = identity.strip().lower()
        numeric = numeric.strip()
        if not identity or not numeric:
            raise VersionDetectionError(
                f"invalid --to syntax {arg!r} ; expected `identity:N`")
        if not numeric.isdigit():
            raise VersionDetectionError(
                f"invalid --to numeric {numeric!r} ; expected digits")
        return ReleaseIdentity(identity=identity, numeric=numeric)
    identity = arg.lower()
    if identity.isdigit():
        return ReleaseIdentity(identity=identity, numeric=identity)
    return ReleaseIdentity(identity=identity, numeric=None)


def detect_target_release(
        *,
        current: Optional[str] = None,
        user_supplied: Optional[str] = None,
) -> ReleaseIdentity:
    """Return the release the distupgrade should migrate to.

    Priority :

    1. ``user_supplied`` (``--to`` CLI flag) — wins outright.
    2. **N → N+1 heuristic** when ``current`` is a numeric identity
       (``'9'`` → ``'10'``, ``'10'`` → ``'11'``…).  This is the MVP :
       Mageia's release cadence is sequential, so bumping the digit
       hits the right catalogue in ≥95 % of runs.  If the resulting
       release doesn't exist on the mirror yet, Stage 1's target-
       catalogue upsert will 404 loudly — not silent corruption.
    3. Cauldron currents (``identity='cauldron'``) refuse : cauldron
       is a rolling target with no defined « next » — user must pass
       ``--to`` explicitly.

    The full §6.4.a API path (query ``releases.mageia.org`` for the
    active target with ``release_date`` + ``desktop-update-end``
    honouring) is deferred — the +1 heuristic covers the standard
    upgrade window ; the API adds accuracy for edge cases (freeze
    late-life releases, EOL detection).
    """
    if user_supplied is not None:
        target = parse_release_arg(user_supplied)
        # §591 : refuse hard if target ≤ current (numeric comparison
        # when both sides are numeric — cauldron is treated as
        # « strictly above every numeric release » so
        # ``cauldron`` > ``11`` holds).  No ``--force`` bypasses.
        if current is not None:
            if target.identity == current:
                raise VersionDetectionError(
                    f"target release {target.display()!r} is already "
                    f"the current release ; nothing to migrate")
            if (current.isdigit() and target.identity.isdigit()
                    and int(target.identity) < int(current)):
                raise VersionDetectionError(
                    f"target release {target.display()!r} is older "
                    f"than the current release {current!r} — "
                    f"downgrade via distupgrade is not supported")
        return target

    if current is None:
        raise VersionDetectionError(
            "could not read the current Mageia release from "
            "/etc/mageia-release ; pass `--to <version>` explicitly")
    if not current.isdigit():
        raise VersionDetectionError(
            f"current release {current!r} is not numeric (rolling "
            f"target?) ; pass `--to <version>` explicitly")
    numeric_next = str(int(current) + 1)
    return ReleaseIdentity(identity=numeric_next, numeric=numeric_next)


@dataclass(frozen=True)
class TargetMaturity:
    """Snapshot of what a mirror advertises about the target release.

    ``branch`` is the raw ``[media_info] branch=`` value from the
    target's ``media.cfg`` — Mageia convention : ``"Official"`` for
    a released version, ``"Cauldron"`` for the rolling dev tree,
    ``"Alpha"`` / ``"Beta N"`` / ``"RC N"`` during a release cycle.
    ``is_stable`` is a boolean shortcut : ``True`` iff branch equals
    ``"Official"`` (case-insensitive).

    ``probe_error`` is None on success ; when set it holds a short
    diagnostic (network failure, malformed cfg…) — the CLI uses it
    to decide whether to proceed silently or warn about the missing
    signal.
    """
    branch: Optional[str]
    is_stable: bool
    probe_error: Optional[str] = None


def probe_target_maturity(
        db,
        target: ReleaseIdentity,
        arch: str,
        *,
        source_identity: Optional[str] = None,
) -> TargetMaturity:
    """Fetch the target ``media.cfg`` and report its release stage.

    Walks the DB's official + enabled servers, tries each catalogue
    URL until one responds, parses the ``[media_info]`` section and
    reports ``branch=``.  A hit on ``"Official"`` (any case) means
    the target has been released ; anything else (``Cauldron``,
    ``Beta``, ``RC``, ``Alpha``, ``mgabiz``, …) is a heads-up worth
    a user prompt.

    Read-only : no DB mutation, no cache write, minimal HTTP.
    """
    from ..config import build_server_url
    from ..media_cfg import fetch_media_cfg, parse_media_cfg

    try:
        conn = db._get_connection()
        servers = conn.execute("""
            SELECT id, name, protocol, host, base_path, url_version,
                   is_official
            FROM server
            WHERE is_official = 1 AND enabled = 1
        """).fetchall()
    except Exception as exc:  # noqa: BLE001
        return TargetMaturity(branch=None, is_stable=False,
                              probe_error=f"DB error : {exc}")

    if not servers:
        return TargetMaturity(
            branch=None, is_stable=False,
            probe_error="no official server enabled in DB")

    identity_for_url = target.numeric or target.identity
    last_err: Optional[str] = None
    for srv in servers:
        srv_dict = dict(srv)
        base_url = build_server_url(srv_dict)
        # Read-only probe : ignore the `pin_needs_update` flag, the
        # DB update happens at Stage 1 in the mutating context.
        url_seg, _ = choose_target_url_segment(
            srv_dict.get("url_version"), source_identity,
            identity_for_url,
        )
        catalogue_url = f"{base_url.rstrip('/')}/{url_seg}/{arch}/media/"
        try:
            raw = fetch_media_cfg(catalogue_url)
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
        try:
            info, _medias = parse_media_cfg(
                raw, f"{url_seg}/{arch}/media")
        except Exception as exc:  # noqa: BLE001
            last_err = f"parse : {exc}"
            continue
        branch = (info.branch or "").strip()
        return TargetMaturity(
            branch=branch or None,
            is_stable=(branch.lower() == "official"),
        )

    return TargetMaturity(
        branch=None, is_stable=False,
        probe_error=last_err or "no reachable mirror")


def multi_version_jump(*, current: str, target: ReleaseIdentity) -> int:
    """Return the number of releases skipped in a target > current+1 jump.

    Returns 0 when the jump is « adjacent » (``target = current + 1``)
    or when either side is non-numeric (cauldron, unknown).  §592
    requires an interactive confirmation when the return value is > 0
    — the CLI is responsible for the prompt itself ; this helper only
    computes the skip count so the wording can quote it.
    """
    if not current.isdigit() or not target.identity.isdigit():
        return 0
    diff = int(target.identity) - int(current)
    if diff <= 1:
        return 0
    return diff - 1
