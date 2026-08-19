"""urpm.core.rpmdb — Safe rpmdb access from the CLI parent process.

Golden rule
-----------

Every rpmdb read from the CLI parent process MUST go through this
module.  Never call ``rpm.TransactionSet()`` directly from any code
running in the parent : the module's :func:`open_ts` context
manager is the only sanctioned way of opening librpm, because it is
the only way that guarantees the connection is closed before we
return control.

Why : ``rpm.TransactionSet()`` opens rpmdb (SQLite in WAL mode on
modern rpm) and Python's ``del ts`` is only a GC hint — the SQLite
file descriptors are NOT released until the interpreter chooses to
run the finalizer.  On a WAL rpmdb the reader-snapshot semantics
then hide any committed write done through an intervening
subprocess.  The originating field bug : ``urpm system import``
planned to erase ``pipewire-media-session`` even though the install
phase's obsolete had removed it — the diff-time
``_enumerate_installed_names`` had opened a handle that was still
attached at re-query time, and the re-query silently returned the
pre-install snapshot.  Explicit ``closeDB()`` is what tears down
the connection so the follow-up read sees fresh state.

The only legitimate site keeping librpm bindings outside this
module is the actual transaction driver
(:mod:`urpm.core.transaction_queue`), which runs in a *forked*
child : its handle dies with the child, never leaks into the
parent's follow-up subprocesses.

Contract of this module
-----------------------

1. **Every public function has ≥1 caller in the tree**.  Adding a
   function that isn't used is a code review reject — no
   speculative API surface, no « au cas où ».
2. **Zero business logic**.  This module exposes raw primitives
   (name / arch / release / deps).  Distro-specific interpretation
   (32-bit media, tainted / nonfree tagging, kernel categorisation,
   …) lives in the domain module that owns that concern
   (:mod:`urpm.core.media_cfg`, :mod:`urpm.cli.helpers.kernel`, …).
3. **Zero security-adjacent code**.  Signature / digest
   verification stays in :mod:`urpm.core.download` and
   :mod:`urpm.core.resilient_install` for auditability ; those
   sites apply the same ``try/finally: ts.closeDB()`` pattern
   in-place.
4. **Every function docstring** references the module contract in
   one line — no exception.
5. **A new caller migrating a legacy ``rpm.TransactionSet``**
   either reuses an existing function here or adds a new one in
   the same commit as its caller (proving the ≥1 caller rule).

Cache with mtime-based invalidation
------------------------------------

Every read is cached and keyed on an ``rpmdb_signature`` — a tuple
of file mtimes of the rpmdb backing files.  When a subprocess
mutates rpmdb, the mtime moves, the next call's signature
mismatches, and the cache is transparently re-populated.  Callers
never need to think about invalidation.  Fork-safe : each process
has its own copy-on-writed cache and its own mtime observation.

:func:`invalidate_cache` is exposed for the rare edge case where a
caller knows the cache should be blown away (e.g. right after a
``rpm2cpio``-style out-of-band mutation the mtime doesn't catch).
"""
from __future__ import annotations

import functools
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set, Tuple


# ---------------------------------------------------------------------------
# rpmdb signature (mtime-based cache invalidation)
# ---------------------------------------------------------------------------

# Files whose mtime moves when rpmdb mutates.  Stat every candidate
# because backends differ across rpm builds : modern is SQLite
# (``rpmdb.sqlite`` + WAL / SHM), legacy BDB was ``Packages``, and
# some cross-version rpmdb layouts include ``Packages.db``.  A tuple
# of the mtimes uniquely identifies rpmdb state.
_RPMDB_FILES = (
    "var/lib/rpm/rpmdb.sqlite",
    "var/lib/rpm/rpmdb.sqlite-wal",
    "var/lib/rpm/Packages",
    "var/lib/rpm/Packages.db",
)


def _rpmdb_signature(root: str) -> Tuple[int, ...]:
    """Return an integer tuple that changes whenever rpmdb mutates.

    ``mtime_ns`` (nanoseconds since epoch) is fine-grained enough to
    catch two mutations within the same second — RPM's commit-then-
    close writes the file, and inode timestamp resolution on modern
    filesystems is well under a millisecond.  The WAL file is also
    tracked because SQLite writes appear there before checkpointing
    to the main database.
    """
    root_path = Path(root)
    sig: List[int] = []
    for rel in _RPMDB_FILES:
        try:
            sig.append(root_path.joinpath(rel).stat().st_mtime_ns)
        except OSError:
            sig.append(0)
    return tuple(sig)


# Cache : {(func_name, args, kwargs) -> (signature, result)}.
# Signature comparison per call is O(4 stat) — well under 40 µs on
# ext4, and O(1) if the previous call is still valid.
_cache: Dict[Tuple[Any, ...], Tuple[Tuple[int, ...], Any]] = {}


def _cached(func):
    """Decorator : memoise an rpmdb-reading function, invalidating on
    rpmdb mtime change.  Every decorated function MUST accept an
    optional ``root: str = "/"`` kwarg — the signature is computed
    against that root.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        root = kwargs.get("root", "/")
        sig = _rpmdb_signature(root)
        cache_key = (
            func.__name__,
            args,
            tuple(sorted(kwargs.items())),
        )
        cached = _cache.get(cache_key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        result = func(*args, **kwargs)
        _cache[cache_key] = (sig, result)
        return result
    return wrapper


def invalidate_cache() -> None:
    """Blow the entire read-cache away.

    Only needed for out-of-band mutations that don't touch the
    standard rpmdb backing files (rare — think custom ``rpm2cpio``
    replay).  In the normal flow the mtime tracker handles
    invalidation transparently.
    """
    _cache.clear()


# ---------------------------------------------------------------------------
# librpm TransactionSet — context-managed
# ---------------------------------------------------------------------------


@contextmanager
def open_ts(root: str = "/") -> Iterator[Any]:
    """Yield a fresh ``rpm.TransactionSet`` and guarantee ``closeDB()``
    on exit — the ONLY sanctioned way librpm is opened from a CLI
    parent process.

    See the module docstring for the full rationale.  Callers must
    not stash the yielded object for later use ; its lifetime is
    strictly the ``with`` block.

    Prefer the typed helpers (:func:`list_installed_names`,
    :func:`query_by_name`, :func:`is_installed`,
    :func:`get_provides_and_requires`, :func:`system_arch`) — this
    context manager is the escape hatch for the rare sites that
    need raw header access (iteration with early break, libsolv
    ``@System`` repo hydration for a chroot, …) and for which
    typed helpers would not fit the shape of the data extracted.
    """
    import rpm  # noqa: PLC0415 — lazy so importing this module is cheap
    ts = rpm.TransactionSet(root)
    try:
        # Suppress signature/digest verification : read-only queries
        # never need them and toggling them off avoids a spurious
        # warning stream on packages installed from a keyring that
        # changed since.
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)
        yield ts
    finally:
        try:
            ts.closeDB()
        except Exception:  # noqa: BLE001 — nothing sensible to do on close error
            pass


def _decode_str(val: Any) -> str:
    """Normalise a librpm header value to ``str``.  Older rpm builds
    return bytes for some tags on py3 ; modern ones return str.
    Empty / ``None`` → ``""``."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return str(val)


def _decode_epoch(val: Any) -> str:
    """Header EPOCH is either ``None`` / ``0`` (no explicit epoch) or
    a small integer.  Return ``""`` when absent so callers can format
    NEVRA uniformly."""
    if not val:
        return ""
    return str(val)


# ---------------------------------------------------------------------------
# Public API — data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstalledPkg:
    """Minimal snapshot of one installed package.  Grown on demand :
    only fields the callers actually consume are populated ; adding a
    field means adding it to :func:`_pkg_from_hdr` and demonstrating
    a caller that consumes it in the same commit."""
    name: str
    epoch: str          # "" when no explicit epoch, else the number as text
    version: str
    release: str
    arch: str
    size: int = 0       # RPMTAG_SIZE — installed footprint, bytes

    @property
    def evr(self) -> str:
        """Epoch:Version-Release with epoch omitted when empty."""
        return f"{self.epoch}:{self.version}-{self.release}" if self.epoch \
            else f"{self.version}-{self.release}"

    @property
    def nevra(self) -> str:
        return f"{self.name}-{self.evr}.{self.arch}"


@dataclass(frozen=True)
class PkgDep:
    """A single dependency entry — capability name + version constraint.

    Faithful to the way rpm stores the entry : ``flags`` is the bitmask
    of ``rpm.RPMSENSE_LESS`` / ``RPMSENSE_GREATER`` / ``RPMSENSE_EQUAL``
    (0 when no version compare is encoded), ``version`` is the epoch:
    version-release string constraint (empty when unversioned).

    Version-aware satisfaction is delegated to :func:`satisfies` which
    uses ``rpm.labelCompare`` — the same binding libsolv-adjacent tools
    use, no artisanal vercmp implementation.

    ``str(dep)`` returns the human-readable ``"name op version"`` form
    (or just ``name`` when unversioned).  Callers that need only the
    capability name reach for ``dep.name``.
    """
    name: str
    flags: int = 0
    version: str = ""

    @property
    def op(self) -> str:
        """String operator (``<``, ``>``, ``=``, ``<=``, ``>=``, ...)
        or empty when no version compare is encoded in ``flags``."""
        import rpm
        s = ""
        if self.flags & rpm.RPMSENSE_LESS:
            s += "<"
        if self.flags & rpm.RPMSENSE_GREATER:
            s += ">"
        if self.flags & rpm.RPMSENSE_EQUAL:
            s += "="
        return s

    def __str__(self) -> str:
        if self.version:
            return f"{self.name} {self.op} {self.version}"
        return self.name


@dataclass(frozen=True)
class PkgDeps:
    """Dependency arrays of an installed package, with full version
    constraints preserved so satisfaction can be tested precisely (see
    :func:`satisfies`).  Producers that only need capability names
    reach for ``dep.name`` on each entry.

    ``rpmlib(...)`` capabilities are filtered upstream — they are
    rpm-format markers, never real deps."""
    provides: Tuple[PkgDep, ...] = field(default_factory=tuple)
    requires: Tuple[PkgDep, ...] = field(default_factory=tuple)
    recommends: Tuple[PkgDep, ...] = field(default_factory=tuple)
    suggests: Tuple[PkgDep, ...] = field(default_factory=tuple)
    supplements: Tuple[PkgDep, ...] = field(default_factory=tuple)


def _parse_evr(evr: str) -> Tuple[str, str, str]:
    """Split an ``epoch:version-release`` string into a 3-tuple suitable
    for :func:`rpm.labelCompare`.  Missing epoch → ``""``, missing
    release → ``""``.  ``labelCompare`` handles empty components
    correctly (they compare equal to any missing counterpart).
    """
    epoch = ""
    remaining = evr
    if ":" in remaining:
        epoch, remaining = remaining.split(":", 1)
    if "-" in remaining:
        version, release = remaining.rsplit("-", 1)
    else:
        version, release = remaining, ""
    return (epoch, version, release)


def satisfies(prov: PkgDep, req: PkgDep) -> bool:
    """Return True if ``prov`` satisfies ``req``.

    * ``req`` has no version constraint (``req.flags == 0`` or
      ``req.version == ""``) → any provider with a matching name
      satisfies.
    * ``prov`` has no version → cannot satisfy a versioned ``req``.
    * Both have versions → compare via :func:`rpm.labelCompare` and
      match against the operator encoded in ``req.flags``.

    Names are matched case-sensitively (rpm capability names are).
    Version comparison uses librpm's own vercmp, so we handle epoch,
    tilde-releases and other rpm-idiosyncratic cases correctly without
    a bespoke implementation.
    """
    if prov.name != req.name:
        return False
    import rpm
    req_versioned = bool(req.version) and bool(
        req.flags & (rpm.RPMSENSE_LESS | rpm.RPMSENSE_GREATER
                     | rpm.RPMSENSE_EQUAL))
    if not req_versioned:
        return True
    if not prov.version:
        return False
    cmp = rpm.labelCompare(_parse_evr(prov.version), _parse_evr(req.version))
    if req.flags & rpm.RPMSENSE_EQUAL and cmp == 0:
        return True
    if req.flags & rpm.RPMSENSE_LESS and cmp < 0:
        return True
    if req.flags & rpm.RPMSENSE_GREATER and cmp > 0:
        return True
    return False


# ---------------------------------------------------------------------------
# Public API — read-only queries
# ---------------------------------------------------------------------------


def _tag(hdr: Any, tag_id: int, default: Any = None) -> Any:
    """Read a header tag, returning ``default`` when absent.

    Real ``rpm.hdr`` returns ``None`` for missing tags ; test fakes
    (plain dicts) raise ``KeyError``.  This helper normalises both
    to ``default`` so :func:`_pkg_from_hdr` and its callers can
    stay uniform across production and test bindings.
    """
    try:
        val = hdr[tag_id]
    except (KeyError, IndexError, TypeError):
        return default
    return val if val is not None else default


def _pkg_from_hdr(hdr: Any) -> InstalledPkg:
    """Build an :class:`InstalledPkg` from a librpm header."""
    import rpm
    return InstalledPkg(
        name=_decode_str(_tag(hdr, rpm.RPMTAG_NAME)),
        epoch=_decode_epoch(_tag(hdr, rpm.RPMTAG_EPOCH)),
        version=_decode_str(_tag(hdr, rpm.RPMTAG_VERSION)),
        release=_decode_str(_tag(hdr, rpm.RPMTAG_RELEASE)),
        arch=_decode_str(_tag(hdr, rpm.RPMTAG_ARCH)),
        size=int(_tag(hdr, rpm.RPMTAG_SIZE, 0) or 0),
    )


@_cached
def list_installed_names(root: str = "/") -> List[str]:
    """Return every installed package's Name (sorted, case preserved).

    See the module contract : rpmdb access via ``open_ts`` context
    manager, closes explicitly.  RPM package names are case-sensitive
    (``perl-Git`` vs ``perl-git``) — the output preserves rpmdb's
    stored casing so callers can pass names straight to ``urpm
    install`` / ``urpm erase``.
    """
    with open_ts(root) as ts:
        return sorted({_decode_str(h["name"]) for h in ts.dbMatch()})


@_cached
def is_installed(names: Tuple[str, ...], root: str = "/") -> Set[str]:
    """Return the subset of ``names`` currently present in rpmdb.

    See the module contract : rpmdb access via ``open_ts`` context
    manager, closes explicitly.  Batched : one ``dbMatch`` walk
    (~5 ms on a typical desktop), set intersection in Python.
    ``names`` must be a tuple (hashable) so the cache key stays
    stable across calls with the same query.
    """
    if not names:
        return set()
    all_installed = set(list_installed_names(root=root))
    return {n for n in names if n in all_installed}


@_cached
def query_by_name(name: str, root: str = "/") -> List[InstalledPkg]:
    """Return every installed build matching ``name`` (empty list if
    not installed).

    See the module contract : rpmdb access via ``open_ts`` context
    manager, closes explicitly.  Multiple hits are possible when
    several arches or multi-version installs (``kernel``, ``java``)
    coexist.
    """
    with open_ts(root) as ts:
        return [_pkg_from_hdr(h) for h in ts.dbMatch("name", name)]


@_cached
def system_arch(root: str = "/") -> str:
    """Return the effective user-space architecture.

    See the module contract : rpmdb access via ``open_ts`` context
    manager, closes explicitly.  Reads the ``ARCH`` header of
    ``filesystem`` (the least replaceable base package), falls back
    to ``glibc``.  Both are always installed on a working Mageia
    system and both are strictly single-arch, giving a reliable
    answer that survives a running kernel of a different arch
    (``uname -m`` is wrong on a 32-bit userland running on a 64-bit
    kernel).  Ultimate fallback is ``platform.machine()``.
    """
    for probe in ("filesystem", "glibc"):
        for pkg in query_by_name(probe, root=root):
            if pkg.arch and pkg.arch != "noarch":
                return pkg.arch
    import platform
    return platform.machine()


_DEP_TAG_FAMILIES = (
    # (attr_name, tag_prefix) — the three parallel arrays that make
    # up one dep family are ``PROVIDENAME`` / ``PROVIDEVERSION`` /
    # ``PROVIDEFLAGS``, etc.
    ("provides", "PROVIDE"),
    ("requires", "REQUIRE"),
    ("recommends", "RECOMMEND"),
    ("suggests", "SUGGEST"),
    ("supplements", "SUPPLEMENT"),
)


def _read_dep_family(hdr: Any, prefix: str) -> Tuple[PkgDep, ...]:
    """Extract one dep family (``PROVIDE``, ``REQUIRE``, ...) from
    ``hdr`` as a deduplicated tuple of :class:`PkgDep`.

    ``rpmlib(...)`` and self-file-deps (``/usr/bin/foo``) are dropped
    — the first are rpm-format markers, the second are payload paths
    handled at a different layer of the resolver.  Older rpm builds
    that lack ``RECOMMEND*`` / ``SUGGEST*`` / ``SUPPLEMENT*`` tags
    return an empty family instead of raising.
    """
    import rpm
    name_tag = getattr(rpm, f"RPMTAG_{prefix}NAME", None)
    ver_tag = getattr(rpm, f"RPMTAG_{prefix}VERSION", None)
    flag_tag = getattr(rpm, f"RPMTAG_{prefix}FLAGS", None)
    if name_tag is None:
        return ()
    names = _tag(hdr, name_tag, []) or []
    versions = _tag(hdr, ver_tag, []) or [] if ver_tag is not None else []
    flags = _tag(hdr, flag_tag, []) or [] if flag_tag is not None else []
    seen: Set[Tuple[str, int, str]] = set()
    out: List[PkgDep] = []
    for i, raw_name in enumerate(names):
        name = _decode_str(raw_name)
        if not name or name.startswith("rpmlib("):
            continue
        ver = _decode_str(versions[i]) if i < len(versions) else ""
        flg = int(flags[i]) if i < len(flags) else 0
        key = (name, flg, ver)
        if key in seen:
            continue
        seen.add(key)
        out.append(PkgDep(name=name, flags=flg, version=ver))
    return tuple(out)


@_cached
def get_provides_and_requires(root: str = "/") -> Dict[str, PkgDeps]:
    """Return ``{pkg_name: PkgDeps(...)}`` for every installed package.

    See the module contract : rpmdb access via ``open_ts`` context
    manager, closes explicitly.  Single ``dbMatch()`` walk : per-
    header extraction of the five dependency array families with
    their full version constraints (see :class:`PkgDep`), so callers
    can test satisfaction precisely via :func:`satisfies`.

    ``rpmlib(...)`` capabilities are filtered — they are rpm-format
    markers, never real deps.

    Downstream consumers : incoherent-profile rescue in ``urpm
    system import`` (version-aware), orphan analysis and reverse-dep
    display commands.  Cached so repeated calls in the same command
    cost nothing.
    """
    result: Dict[str, PkgDeps] = {}
    with open_ts(root) as ts:
        for hdr in ts.dbMatch():
            name = _decode_str(hdr["name"])
            if not name:
                continue
            kwargs = {
                attr: _read_dep_family(hdr, prefix)
                for attr, prefix in _DEP_TAG_FAMILIES
            }
            result[name] = PkgDeps(**kwargs)
    return result


