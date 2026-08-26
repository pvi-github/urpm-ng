"""Orphan classification for the interactive triage workflow.

Consumed by :mod:`urpm.cli.helpers.orphans_triage` (TUI) and by
:mod:`urpm.core.resolution.orphans` (enrichment during
``find_all_orphans``).  Kept as a distinct module so classification is
pure-Python, offline, and unit-testable without touching librpm — the
enrichment step reads the rpmdb once and hands off ``OrphanInfo``
records that any downstream code (filters, TUI, tests) can work with.

The rules are conventions rather than heuristics :

* **``sublib``** — the package exposes at least one soname-versioned
  capability (``lib*.so.N`` style).  This is the RPM contract for a
  Mageia sub-lib package built via ``%mklibname``.  The classifier
  never uses the package name : ``libreoffice``, ``librecad`` and
  ``libinput-tools`` do not match because they do not expose a
  ``.so.N`` provide.  See ``reference_mageia_soname_package_pattern``.

* **``previous_release``** — the package's disttag encodes a Mageia
  major version lower than the running system's major version.  Post
  a mga9 → mga10 distupgrade, all remaining ``.mga9`` packages fall
  in this bucket.

* **``userland``** — the residual category.  User-facing packages the
  operator has to look at one by one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional


# --- Data model -----------------------------------------------------------


@dataclass
class OrphanInfo:
    """Presentation-ready description of an orphan package.

    Built once from an ``rpm.hdr`` (via :func:`orphan_info_from_hdr`)
    and passed through the classifier, filter engine and TUI without
    ever touching librpm again.  Every field is a plain scalar or list
    of scalars so tests can construct instances directly.
    """

    name: str
    evr: str                          # e.g. "0.2-4.mga10"
    arch: str                         # e.g. "x86_64" or "noarch"
    nevra: str                        # e.g. "mgatools-0.2-4.mga10.noarch"
    size: int = 0                     # installed size, bytes
    install_time: int = 0             # RPMTAG_INSTALLTIME (unix ts)
    summary: str = ""
    group: str = ""
    provides: List[str] = field(default_factory=list)


# --- Disttag parsing ------------------------------------------------------

# Mageia release identifier embedded in the RPM Release field.
# Examples : ``2.mga9``, ``4.mga10``, ``1.mga11``, ``0.rc1.mga10``.
# ``cauldron`` is treated as the current development release: we do
# NOT classify it as a previous release even when the running system
# is a numbered release.
_DISTTAG_RE = re.compile(r"\.mga(\d+|cauldron)\b")


def parse_disttag(evr: str) -> Optional[str]:
    """Return the Mageia disttag embedded in an EVR string, or ``None``.

    ``"0.2-4.mga10"`` → ``"mga10"``.
    ``"1:17.0.19.0.10-1.mga9"`` → ``"mga9"``.
    ``"1.0-1"`` (no disttag) → ``None``.
    """
    m = _DISTTAG_RE.search(evr or "")
    return f"mga{m.group(1)}" if m else None


def disttag_major(disttag: Optional[str]) -> Optional[int]:
    """Extract the numeric major from a disttag string.

    ``"mga10"`` → ``10``.  ``"mgacauldron"`` → ``None`` (rolling —
    never comparable to a numbered release).  ``None`` → ``None``.
    """
    if not disttag:
        return None
    tail = disttag[3:]
    return int(tail) if tail.isdigit() else None


# --- Current release detection --------------------------------------------


_MAGEIA_RELEASE_PATH = Path("/etc/mageia-release")


def current_distmajor(root: str = "/") -> Optional[int]:
    """Return the running system's Mageia major, or ``None``.

    Reads ``/etc/mageia-release`` (``Mageia release 10 (Official)``
    style).  Callers can pass a non-``/`` root for chroot / test
    scenarios.  Returns ``None`` when the file is absent, unparseable,
    or the release is ``cauldron`` (rolling — no comparison possible).
    """
    path = Path(root) / _MAGEIA_RELEASE_PATH.relative_to("/")
    try:
        line = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    m = re.search(r"\brelease\s+(\d+)\b", line, re.IGNORECASE)
    return int(m.group(1)) if m else None


# --- Classifiers ----------------------------------------------------------

# A soname-versioned capability : ``libfoo.so.1``, ``lib64gtk-4.so.1``,
# ``libc.so.6``.  The trailing digit is required — plain ``libfoo.so``
# is a devel-symlink capability, not a runtime soname.
_SONAME_RE = re.compile(r"^lib[^\s()]*\.so\.\d")


def is_soname_sublib(info: OrphanInfo) -> bool:
    """True when the package exposes at least one soname-versioned provide.

    Detection is on ``Provides:`` only, never on the package name.
    ``libreoffice`` (no soname provide) is NOT a sublib ; ``lib64gtk3_0``
    (provides ``libgtk-3.so.0()(64bit)``) IS.
    """
    return any(_SONAME_RE.match(prov) for prov in info.provides)


def is_previous_release_relic(
    info: OrphanInfo, current_major: Optional[int]
) -> bool:
    """True when the package's disttag is a Mageia release older than the system.

    Requires an integer ``current_major`` (from :func:`current_distmajor`).
    Returns False if either the current major or the package's disttag
    major cannot be resolved — the caller must not treat unknowns as
    relics.
    """
    if current_major is None:
        return False
    pkg_major = disttag_major(parse_disttag(info.evr))
    if pkg_major is None:
        return False
    return pkg_major < current_major


# Category labels are the keys the TUI uses to drive its welcome
# screen and filter presets ; keep them stable.
CATEGORY_PREVIOUS_RELEASE = "previous_release"
CATEGORY_SUBLIB = "sublib"
CATEGORY_USERLAND = "userland"


def classify_orphans(
    orphans: Iterable[OrphanInfo], current_major: Optional[int] = None,
) -> Dict[str, List[OrphanInfo]]:
    """Sort orphans into the three triage categories.

    The category assignment is disjoint and prioritised :

    1. ``previous_release`` wins over everything else — a mga9 sublib
       still under a mga10 system is a relic first (it will be gone
       once the operator confirms), the fact that it is also a sublib
       is a secondary concern.
    2. ``sublib`` picks up the remaining soname-exposing packages.
    3. ``userland`` catches everything left.

    Passing ``current_major=None`` disables the ``previous_release``
    check ; every candidate is then split between ``sublib`` and
    ``userland`` only.
    """
    buckets: Dict[str, List[OrphanInfo]] = {
        CATEGORY_PREVIOUS_RELEASE: [],
        CATEGORY_SUBLIB: [],
        CATEGORY_USERLAND: [],
    }
    for info in orphans:
        if is_previous_release_relic(info, current_major):
            buckets[CATEGORY_PREVIOUS_RELEASE].append(info)
        elif is_soname_sublib(info):
            buckets[CATEGORY_SUBLIB].append(info)
        else:
            buckets[CATEGORY_USERLAND].append(info)
    return buckets


# --- Enrichment from librpm headers ---------------------------------------


def orphan_info_from_hdr(hdr) -> OrphanInfo:
    """Build an :class:`OrphanInfo` from an ``rpm.hdr`` object.

    Isolated so tests can bypass librpm entirely by constructing
    :class:`OrphanInfo` instances directly.  The lazy import keeps
    this module import-safe on systems without librpm (build hosts,
    CI images, doc renderers).
    """
    import rpm

    name = hdr[rpm.RPMTAG_NAME]
    version = hdr[rpm.RPMTAG_VERSION]
    release = hdr[rpm.RPMTAG_RELEASE]
    epoch = hdr[rpm.RPMTAG_EPOCH]
    arch = hdr[rpm.RPMTAG_ARCH]

    evr = f"{epoch}:{version}-{release}" if epoch else f"{version}-{release}"
    nevra = f"{name}-{version}-{release}.{arch}"

    return OrphanInfo(
        name=name,
        evr=evr,
        arch=arch,
        nevra=nevra,
        size=int(hdr[rpm.RPMTAG_SIZE] or 0),
        install_time=int(hdr[rpm.RPMTAG_INSTALLTIME] or 0),
        summary=(hdr[rpm.RPMTAG_SUMMARY] or "").strip(),
        group=(hdr[rpm.RPMTAG_GROUP] or "").strip(),
        provides=list(hdr[rpm.RPMTAG_PROVIDENAME] or []),
    )
