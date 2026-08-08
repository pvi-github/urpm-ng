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
        url_seg = srv_dict.get("url_version") or identity_for_url
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
