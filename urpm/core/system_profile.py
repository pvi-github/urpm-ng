"""System-state export / import — align a machine's config on another.

Exports a JSON snapshot of the current system (installed packages
grouped by install reason, media, servers) and can re-apply such a
snapshot on another machine.

Packages are stored by **name only** — no NEVRA pinning : between
export and import the mirrors have moved, so the importer resolves
each requested name to the best available version in its (freshly
imported) media set.

Media and servers use **replace** semantics by default : anything not
in the imported profile gets removed on the target.  Callers can
opt into merge semantics per section.

Install-reason classification uses the same flat-file source of truth
as :mod:`urpm.core.resolution.orphans` :

* ``/var/lib/rpm/installed-through-deps.list``       → dependency
* ``/var/lib/rpm/installed-through-builddeps.list``  → buildrequires
  (file format : ``name<tab>spec-source`` per line)
* everything else installed → explicit
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .database import PackageDatabase


SCHEMA_VERSION = 1
BACKUP_DIR = Path("/var/lib/urpm")

DEPS_LIST_REL = "var/lib/rpm/installed-through-deps.list"
BUILDDEPS_LIST_REL = "var/lib/rpm/installed-through-builddeps.list"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SectionDiff:
    """Difference for one section (servers, media) between current and
    target state."""
    to_add: List[Dict] = field(default_factory=list)
    to_remove: List[Dict] = field(default_factory=list)
    unchanged: List[Dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class PackageDiff:
    """Difference for the packages section between current and target
    state.  All lists carry package names (lowercase).
    """
    # Installs the importer must perform to reach the target state.
    install_explicit: List[str] = field(default_factory=list)
    install_dependency: List[str] = field(default_factory=list)
    install_buildrequires: List[str] = field(default_factory=list)
    # Names currently installed on the target as EXPLICIT that don't
    # appear as explicit in the source profile.  Removed to match.
    remove_explicit: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ProfileDiff:
    servers: SectionDiff = field(default_factory=SectionDiff)
    media: SectionDiff = field(default_factory=SectionDiff)
    packages: PackageDiff = field(default_factory=PackageDiff)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _read_name_list_file(path: Path) -> Dict[str, str]:
    """Parse a ``name<tab>source`` list file.  Returns ``{name: source}``.
    Missing or unreadable file returns ``{}`` — same tolerance as
    :meth:`OrphansMixin._get_builddep_packages`."""
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            name = parts[0].lower()
            source = parts[1] if len(parts) > 1 else ""
            result[name] = source
    except (OSError, IOError):
        pass
    return result


def _read_deps_set(path: Path) -> set:
    """Same as :func:`_read_name_list_file` but returns just the name
    set (dependency list has no interesting second column)."""
    return set(_read_name_list_file(path).keys())


def _enumerate_installed_names(root: str = "/") -> List[str]:
    """Return lowercase names of every package in the rpmdb at ``root``.

    ``rpm.TransactionSet`` with an empty ``dbMatch()`` yields every
    header in the database.  We reuse the same pattern as
    :mod:`urpm.core.recovery` for consistency.
    """
    import rpm
    ts = rpm.TransactionSet(root)
    try:
        return sorted({str(h["name"]).lower() for h in ts.dbMatch()})
    finally:
        del ts


def _classify_packages(root: str = "/") -> Dict[str, Any]:
    """Return the three package buckets ready for JSON serialisation."""
    installed = set(_enumerate_installed_names(root))
    root_path = Path(root)
    deps = _read_deps_set(root_path / DEPS_LIST_REL)
    br = _read_name_list_file(root_path / BUILDDEPS_LIST_REL)

    br_names = set(br.keys())
    explicit_names = sorted(installed - deps - br_names)
    dependency_names = sorted(installed & deps)
    # Keep buildrequires as {name: spec_source} — the spec source is
    # useful post-import to know which pkg pulled it in.
    buildrequires = {n: br[n] for n in sorted(installed & br_names)}

    return {
        "explicit": explicit_names,
        "dependency": dependency_names,
        "buildrequires": buildrequires,
    }


def _serialize_media(db) -> List[Dict]:
    """Return one JSON-serialisable dict per media row.  Only the
    fields the importer needs — internal counters (last_sync, MD5s,
    reputation) are dropped."""
    keep = (
        "name", "short_name", "mageia_version", "architecture",
        "relative_path", "url", "mirrorlist", "is_official",
        "allow_unsigned", "enabled", "update_media", "priority",
        "disabled_by",
    )
    return [
        {k: row.get(k) for k in keep if k in row}
        for row in db.list_media()
    ]


def _serialize_servers(db) -> List[Dict]:
    """One dict per server row.  Priority-derived reputation, sync
    stats and blacklist noise dropped."""
    keep = (
        "name", "protocol", "host", "base_path",
        "is_official", "enabled", "priority", "country", "url_version",
    )
    return [
        {k: row.get(k) for k in keep if k in row}
        for row in db.list_servers()
    ]


def export_profile(db, *, root: str = "/",
                   hostname: Optional[str] = None,
                   release: Optional[str] = None,
                   arch: Optional[str] = None) -> Dict[str, Any]:
    """Build the export dict for the current system state.

    ``hostname`` / ``release`` / ``arch`` are captured from the system
    when not supplied — parameters exist to make the function testable
    with a fake environment.
    """
    import platform
    import socket

    from .config import get_system_version

    if hostname is None:
        hostname = socket.gethostname()
    if release is None:
        release = get_system_version(root if root != "/" else None) or "unknown"
    if arch is None:
        arch = platform.machine()

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "hostname": hostname,
            "release": release,
            "arch": arch,
        },
        "servers": _serialize_servers(db),
        "media": _serialize_media(db),
        "packages": _classify_packages(root),
    }


def save_profile(profile: Dict[str, Any], path: Path) -> None:
    """Write ``profile`` to ``path`` as pretty-printed JSON (readable
    diffs across profile files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(profile, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class ProfileError(Exception):
    """Raised for malformed / unsupported profile files."""


def load_profile(path: Path) -> Dict[str, Any]:
    """Read and validate a profile JSON.  Refuses to load a profile
    from a schema newer than we understand — the caller must upgrade
    urpm-ng first."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ProfileError(
            f"cannot read profile {path}: {exc}") from exc

    try:
        profile = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(
            f"malformed JSON in {path}: {exc}") from exc

    if not isinstance(profile, dict):
        raise ProfileError(
            f"{path} : top-level JSON must be an object")

    version = profile.get("schema_version")
    if not isinstance(version, int):
        raise ProfileError(
            f"{path} : missing or non-integer schema_version")
    if version > SCHEMA_VERSION:
        raise ProfileError(
            f"{path} : schema_version={version} is newer than "
            f"this urpm-ng supports (max {SCHEMA_VERSION}) — "
            f"upgrade urpm-ng first")

    for section in ("servers", "media", "packages"):
        if section not in profile:
            raise ProfileError(
                f"{path} : missing required section '{section}'")

    return profile


def _server_key(srv: Dict) -> tuple:
    """Deduplication key : (host, base_path) — matches the DB unique
    constraint."""
    return (srv.get("host", ""), srv.get("base_path", ""))


def _media_key(m: Dict) -> str:
    """Deduplication key : the display name (``media.name`` is the
    unique constraint in the schema)."""
    return m.get("name", "")


def diff_servers(current: List[Dict], target: List[Dict],
                 *, replace: bool = True) -> SectionDiff:
    """Compute the server section diff.

    In ``replace`` mode (default), any server present on the target
    machine but absent from the profile is scheduled for removal.  In
    merge mode, extras are left alone.
    """
    diff = SectionDiff()
    cur_by_key = {_server_key(s): s for s in current}
    tgt_by_key = {_server_key(s): s for s in target}

    for key, srv in tgt_by_key.items():
        if key not in cur_by_key:
            diff.to_add.append(srv)
        else:
            diff.unchanged.append(srv)

    if replace:
        for key, srv in cur_by_key.items():
            if key not in tgt_by_key:
                diff.to_remove.append(srv)

    return diff


def diff_media(current: List[Dict], target: List[Dict],
               *, replace: bool = True) -> SectionDiff:
    """Compute the media section diff."""
    diff = SectionDiff()
    cur_by_key = {_media_key(m): m for m in current}
    tgt_by_key = {_media_key(m): m for m in target}

    for key, m in tgt_by_key.items():
        if key not in cur_by_key:
            diff.to_add.append(m)
        else:
            diff.unchanged.append(m)

    if replace:
        for key, m in cur_by_key.items():
            if key not in tgt_by_key:
                diff.to_remove.append(m)

    # Flag local media whose path won't be reachable post-import — the
    # user can act (attach the disk, edit the URL) before rebooting the
    # profile apply.  We only check ``file://`` URLs here ; remote
    # HEAD checks belong to ``urpm media test``, out of scope.
    for m in diff.to_add:
        url = m.get("url") or ""
        if url.startswith("file://"):
            fs_path = url[len("file://"):]
            if not Path(fs_path).exists():
                diff.warnings.append(
                    f"local media '{m.get('name')}' points at "
                    f"{fs_path} which is not present on this host")

    return diff


def diff_packages(current: Dict[str, Any],
                  target: Dict[str, Any]) -> PackageDiff:
    """Compute the package section diff.

    * ``install_*`` : names present in the target's explicit /
      dependency / buildrequires but absent locally.
    * ``remove_explicit`` : names present locally as explicit but
      absent from the target (any bucket).

    Dependencies + buildrequires present locally but missing from the
    target are NOT forcibly removed — libsolv autoremove is the right
    tool for that, not us.
    """
    cur_explicit = set(current.get("explicit") or [])
    cur_dep = set(current.get("dependency") or [])
    cur_br = set((current.get("buildrequires") or {}).keys())
    cur_all = cur_explicit | cur_dep | cur_br

    tgt_explicit = set(target.get("explicit") or [])
    tgt_dep = set(target.get("dependency") or [])
    tgt_br = set((target.get("buildrequires") or {}).keys())
    tgt_all = tgt_explicit | tgt_dep | tgt_br

    diff = PackageDiff()
    diff.install_explicit = sorted(tgt_explicit - cur_all)
    diff.install_dependency = sorted(tgt_dep - cur_all)
    diff.install_buildrequires = sorted(tgt_br - cur_all)
    diff.remove_explicit = sorted(cur_explicit - tgt_all)
    return diff


def compute_diff(current: Dict[str, Any], target: Dict[str, Any],
                 *, replace_media: bool = True,
                 replace_servers: bool = True) -> ProfileDiff:
    """Assemble the full diff — one call per section."""
    diff = ProfileDiff()
    diff.servers = diff_servers(
        current.get("servers") or [], target.get("servers") or [],
        replace=replace_servers)
    diff.media = diff_media(
        current.get("media") or [], target.get("media") or [],
        replace=replace_media)
    diff.packages = diff_packages(
        current.get("packages") or {}, target.get("packages") or {})
    return diff


def timestamp_backup_path() -> Path:
    """Where an auto-backup should land — one file per invocation, no
    overwrite of prior backups."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return BACKUP_DIR / f"system-backup-{stamp}.json"
