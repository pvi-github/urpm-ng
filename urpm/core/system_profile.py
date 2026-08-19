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
    # Names currently installed on this machine (any bucket : explicit,
    # dep or BR) that do NOT appear in the target profile at all.
    # Clone semantic — end state must match the profile ; a local dep
    # whose provider is being removed but that isn't in the target
    # would otherwise leave broken deps and block the whole erase
    # transaction.  Every local pkg not in the profile is destined to
    # go so libsolv can plan the removal in one coherent pass.
    remove: List[str] = field(default_factory=list)
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
    """Return names of every package in the rpmdb at ``root``, **case
    preserved** — RPM package names are case-sensitive.

    Case-preservation is critical for the import path : ``urpm install``
    / ``urpm erase`` look up packages by exact name, and Perl module
    packages on Mageia are mixed-case (``perl-Git``, ``perl-Digest-HMAC``,
    ``perl-Crypt-OpenSSL-X509``, …) mirroring the CPAN module identity.
    Lowercasing at export time makes every mixed-case name look like an
    unknown package to the importer.

    Delegates to :func:`urpm.core.rpmdb.list_installed_names` — never
    opens a librpm handle in the parent process (see rpmdb module
    docstring : mga9 BDB env caching would silently freeze the rpmdb
    view for every subprocess ``rpm`` that follows, breaking the
    post-install re-filter in ``urpm system import``).
    """
    from . import rpmdb
    return rpmdb.list_installed_names(root=root)


def _classify_packages(root: str = "/") -> Dict[str, Any]:
    """Return the three package buckets ready for JSON serialisation.

    Classification (dep / BR / explicit) is decided by lowercased set
    membership because the ``installed-through-{deps,builddeps}.list``
    flat files that own the reason data are lowercased by
    :class:`OrphansMixin` on write.  The emitted lists themselves keep
    the **original-case** RPM Name so the importer can pass them
    verbatim to ``urpm install`` / ``urpm erase`` without breaking
    Perl-style mixed-case packages.
    """
    installed = _enumerate_installed_names(root)   # original case, sorted
    root_path = Path(root)
    deps_lower = _read_deps_set(root_path / DEPS_LIST_REL)
    br_lower = _read_name_list_file(root_path / BUILDDEPS_LIST_REL)

    explicit: List[str] = []
    dependency: List[str] = []
    buildrequires: Dict[str, str] = {}
    for name in installed:
        low = name.lower()
        if low in br_lower:
            buildrequires[name] = br_lower[low]
        elif low in deps_lower:
            dependency.append(name)
        else:
            explicit.append(name)

    return {
        "explicit": explicit,
        "dependency": dependency,
        "buildrequires": buildrequires,
    }


def _serialize_media(db) -> List[Dict]:
    """Return one JSON-serialisable dict per media row.

    Fields kept : identity, addressing (url / mirrorlist /
    relative_path), the display / update flags.  Internal counters
    (last_sync, MD5s, reputation) are dropped.

    Also emits ``server_links`` : the list of server names the media
    is currently linked to via ``server_media``.  Without this the
    importer would recreate the media row but leave it orphaned —
    ``urpm media update`` then fails with « No server available ».
    """
    keep = (
        "name", "short_name", "mageia_version", "architecture",
        "relative_path", "url", "mirrorlist", "is_official",
        "allow_unsigned", "enabled", "update_media", "priority",
        "disabled_by",
    )
    out = []
    for row in db.list_media():
        entry = {k: row.get(k) for k in keep if k in row}
        try:
            servers = db.get_servers_for_media(
                row["id"], enabled_only=False,
                include_blacklisted=True)
            entry["server_links"] = [s["name"] for s in servers]
        except Exception:  # noqa: BLE001
            entry["server_links"] = []
        out.append(entry)
    return out


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
    * ``remove`` : names present locally in **any** bucket (explicit,
      dep or BR) but absent from the target profile entirely.

    Clone semantic : the target profile is the desired end state,
    everything on this machine outside it is destined to go.  Not
    limiting the remove list to explicit-only avoids the trap where
    a local dep would keep sitting on the system with broken deps
    (its provider being removed via an explicit erase) and blocking
    the whole libsolv erase transaction.  With the full delta,
    libsolv sees the coherent picture and orders the removal
    correctly.

    **Case handling** : set membership is compared case-insensitively
    so a target profile carrying ``perl-git`` matches a locally
    installed ``perl-Git``.  The emitted install lists keep the
    **target** casing (that's what the source machine had, most likely
    the real RPM Name), and the remove list keeps the **current**
    casing (matches what's actually in rpmdb here).  Passing the wrong
    case to ``urpm install`` / ``urpm erase`` would spuriously fail :
    Perl module packages in Mageia are mixed-case.
    """
    def _to_lowered_map(names) -> Dict[str, str]:
        """Return ``{lowered: original}`` — later duplicates win, so
        the last seen case is preserved (harmless if all are same)."""
        return {n.lower(): n for n in (names or [])}

    cur_expl = _to_lowered_map(current.get("explicit"))
    cur_dep = _to_lowered_map(current.get("dependency"))
    cur_br = _to_lowered_map(
        (current.get("buildrequires") or {}).keys())
    # Merged map for the remove list — explicit wins the casing race
    # when the same name shows up in multiple buckets (order matters :
    # later inserts overwrite).
    cur_all_map: Dict[str, str] = {}
    cur_all_map.update(cur_br)
    cur_all_map.update(cur_dep)
    cur_all_map.update(cur_expl)
    cur_all = set(cur_all_map)

    tgt_expl = _to_lowered_map(target.get("explicit"))
    tgt_dep = _to_lowered_map(target.get("dependency"))
    tgt_br = _to_lowered_map(
        (target.get("buildrequires") or {}).keys())
    tgt_all = set(tgt_expl) | set(tgt_dep) | set(tgt_br)

    diff = PackageDiff()
    diff.install_explicit = sorted(
        tgt_expl[k] for k in set(tgt_expl) - cur_all)
    diff.install_dependency = sorted(
        tgt_dep[k] for k in set(tgt_dep) - cur_all)
    diff.install_buildrequires = sorted(
        tgt_br[k] for k in set(tgt_br) - cur_all)
    # Clone semantic : every local pkg missing from the target profile
    # is destined to go — explicit, dep or BR alike.  A local dep whose
    # provider is being removed but that isn't in the target would
    # otherwise leave broken deps and block the whole erase transaction.
    diff.remove = sorted(
        cur_all_map[k] for k in cur_all - tgt_all)
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
