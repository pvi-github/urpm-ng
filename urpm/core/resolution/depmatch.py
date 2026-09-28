"""Deciding whether a provide satisfies a require.

One module answers that question for the whole code base, because
answering it by comparing capability names is wrong in a way that is
invisible until it bites.  Two packages installable side by side
routinely provide the same capability at two versions:
``lib64gnome-desktop-gir3.0`` provides ``typelib(GnomeDesktop) = 3.0``
and ``lib64gnome-desktop-gir4.0`` provides the same name at ``4.0``.
A reverse-dependency walk that matches on the name alone concludes
that everything needing the 4.0 typelib also needs the 3.0 one, which
makes a dead package look alive and keeps it on the system forever.

The rules implemented here are rpm's own (``rpmdsCompare``), so a
verdict taken from this module agrees with what rpm will do at
transaction time, and with what libsolv computes in the solver.

Two vocabularies reach us, and both are handled:

* the rpmdb and the RPM headers, where a dependency is a triple of
  parallel arrays, ``NAME`` / ``VERSION`` / ``FLAGS``;
* Mageia's synthesis, where the same dependency is one string,
  ``libpng[>= 1.6.0]``.

Both are normalised to ``(name, sense, evr)``, a *sense* being the
RPMSENSE comparison bitmask.
"""

from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    import rpm
    HAS_RPM = True
except ImportError:  # pragma: no cover - rpm is always available in production
    HAS_RPM = False


#: Isolates the three comparison bits from the other RPMSENSE flags
#: (PREREQ, SCRIPT_PRE, …), which say *when* a dependency matters, not
#: *what* satisfies it.
if HAS_RPM:
    SENSE_MASK = (
        rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL | rpm.RPMSENSE_GREATER
    )
    #: The operators Mageia's synthesis writes, mapped to RPMSENSE bits.
    SYNTHESIS_SENSE_MAP = {
        '<':  rpm.RPMSENSE_LESS,
        '<=': rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL,
        '=':  rpm.RPMSENSE_EQUAL,
        '==': rpm.RPMSENSE_EQUAL,
        '>=': rpm.RPMSENSE_GREATER | rpm.RPMSENSE_EQUAL,
        '>':  rpm.RPMSENSE_GREATER,
    }
else:  # pragma: no cover - rpm is always available in production
    SENSE_MASK = 0
    SYNTHESIS_SENSE_MAP = {}


#: ``(name tag, version tag, flags tag)`` per dependency kind.  Built
#: once so callers name a kind rather than three constants.
if HAS_RPM:
    _DEP_TAGS = {
        'provides': (rpm.RPMTAG_PROVIDENAME, rpm.RPMTAG_PROVIDEVERSION,
                     rpm.RPMTAG_PROVIDEFLAGS),
        'requires': (rpm.RPMTAG_REQUIRENAME, rpm.RPMTAG_REQUIREVERSION,
                     rpm.RPMTAG_REQUIREFLAGS),
        'recommends': (rpm.RPMTAG_RECOMMENDNAME, rpm.RPMTAG_RECOMMENDVERSION,
                       rpm.RPMTAG_RECOMMENDFLAGS),
        'suggests': (rpm.RPMTAG_SUGGESTNAME, rpm.RPMTAG_SUGGESTVERSION,
                     rpm.RPMTAG_SUGGESTFLAGS),
        'supplements': (rpm.RPMTAG_SUPPLEMENTNAME,
                        rpm.RPMTAG_SUPPLEMENTVERSION,
                        rpm.RPMTAG_SUPPLEMENTFLAGS),
    }
else:  # pragma: no cover - rpm is always available in production
    _DEP_TAGS = {}


def parse_capability(cap: str) -> Tuple[str, int, str]:
    """Parse a synthesis capability string into ``(name, sense, evr)``.

    Mageia's synthesis encodes a capability as a bare name followed by
    zero or more **trailing** bracket groups::

        NAME               perl(Foo::Bar), libfoo.so.1()(64bit)
        NAME[*]            /bin/sh[*]
        NAME[op evr]       libpng[>= 1.6.0]
        NAME[*][op evr]    apache[*][>= 2.0.54]

    ``[*]`` is a qualifier carrying no version information; ``[op evr]``
    is the version constraint.

    Groups are peeled **from the right**, and only while they are
    recognised, because brackets also occur *inside* capability names:
    a Python extras capability such as ``python3.13dist(coverage[toml])``
    is an ordinary name that happens to contain a bracket pair.  A
    leading ``find('[')`` would truncate it to
    ``python3.13dist(coverage`` and silently break every dependency
    edge that goes through it.  Anything not recognised as a trailing
    group is therefore part of the name.

    Shapes measured on the mga10 ``core/release`` synthesis (716 763
    capabilities): 253 493 ``[op evr]``, 4 685 ``[*]``, 752
    ``[*][op evr]``, 7 names carrying inner brackets.  The only
    operators present are those of :data:`SYNTHESIS_SENSE_MAP` plus
    the ``*`` marker.

    Args:
        cap: Raw capability string from ``synthesis.hdlist.cz``.

    Returns:
        A tuple ``(name, sense, evr)`` where ``sense`` is an RPMSENSE
        bitmask (``0`` for an unversioned capability) and ``evr`` is the
        version string (empty for an unversioned capability).
    """
    name = cap
    sense = 0
    evr = ''
    while name.endswith(']'):
        start = name.rfind('[')
        if start < 0:
            break
        inside = name[start + 1:-1]
        if inside == '*':
            name = name[:start]
            continue
        parts = inside.split(None, 1)
        if len(parts) == 2 and parts[0] in SYNTHESIS_SENSE_MAP:
            # Peeling right-to-left, so the first constraint met is the
            # right-most one; keep it and ignore any further constraint
            # group (genhdlist2 and genmedia never emit two).
            if not evr:
                sense = SYNTHESIS_SENSE_MAP[parts[0]]
                evr = parts[1]
            name = name[:start]
            continue
        break
    return name, sense, evr


def evr_tuple(evr: str) -> Tuple[str, str, str]:
    """Split an ``epoch:version-release`` string for :func:`rpm.labelCompare`.

    Missing epoch defaults to ``'0'``; missing release to ``''``.
    """
    if ':' in evr:
        epoch, rest = evr.split(':', 1)
    else:
        epoch, rest = '0', evr
    if '-' in rest:
        version, release = rest.split('-', 1)
    else:
        version, release = rest, ''
    return (epoch, version, release)


def provider_satisfies(prov_evr: str, req_sense: int, req_evr: str) -> bool:
    """Return ``True`` iff a provider's EVR satisfies a versioned require.

    The check mirrors rpm's own ``rpmdsCompare`` semantics:

    * An unversioned require (no comparison bit set) is satisfied by
      any provider — the same answer :data:`SENSE_MASK`-masking would
      give.
    * A versioned require against an unversioned provider fails.
      RPM's auto-generated ``Provides: NAME = EVR`` covers almost every
      real package, so an unversioned provider here typically means an
      explicit ``Provides: foo`` without a version, which cannot be
      compared against a version constraint.
    * Otherwise the two EVRs are compared with :func:`rpm.labelCompare`
      and the result cross-referenced against the require's sense
      bits.  Granularity matches rpm: if the require omits the release
      component (``Requires: foo = 1`` instead of ``= 1-1``), the
      provider's release is ignored so the comparison degenerates to
      version-only equality.  This is how rpm accepts ``foo-1-5`` as a
      valid provider for ``Requires: foo = 1``.
    """
    if not HAS_RPM:  # pragma: no cover - rpm is always available in production
        return True
    if not (req_sense & SENSE_MASK):
        return True
    if not prov_evr:
        return False
    p_epoch, p_ver, p_rel = evr_tuple(prov_evr)
    r_epoch, r_ver, r_rel = evr_tuple(req_evr)
    if not r_rel:
        p_rel = ''
    result = rpm.labelCompare((p_epoch, p_ver, p_rel), (r_epoch, r_ver, r_rel))
    if result == 0:
        return bool(req_sense & rpm.RPMSENSE_EQUAL)
    if result < 0:
        return bool(req_sense & rpm.RPMSENSE_LESS)
    return bool(req_sense & rpm.RPMSENSE_GREATER)


def header_rows(hdr, kind: str, skip_rpmlib: bool = True,
                skip_files: bool = False) -> List[Tuple[str, int, str]]:
    """Read one dependency kind off an RPM header as ``(name, sense, evr)``.

    The three parallel arrays rpm stores are zipped back together here
    rather than at each call site, which is where reading only
    ``…NAME`` and forgetting ``…VERSION`` used to happen.

    Args:
        hdr: An rpm header, from ``ts.dbMatch()`` or a package file.
        kind: One of ``provides``, ``requires``, ``recommends``,
            ``suggests``, ``supplements``.
        skip_rpmlib: Drop ``rpmlib(...)`` build-time capabilities.
        skip_files: Drop file capabilities (a leading ``/``).  Off by
            default: a file dependency is a real edge, and only the
            callers that cannot resolve file provides drop them.

    Returns:
        One tuple per entry, in header order.  Provides carry a sense
        too, since rpm records one, and it costs nothing to keep.
    """
    if not HAS_RPM:  # pragma: no cover - rpm is always available in production
        return []
    name_tag, version_tag, flags_tag = _DEP_TAGS[kind]
    names = hdr[name_tag] or []
    versions = hdr[version_tag] or []
    flags = hdr[flags_tag] or []
    rows = []
    for index, name in enumerate(names):
        if skip_rpmlib and name.startswith('rpmlib('):
            continue
        if skip_files and name.startswith('/'):
            continue
        sense = (flags[index] if index < len(flags) else 0) & SENSE_MASK
        rows.append((name, sense,
                     (versions[index] if index < len(versions) else '') or ''))
    return rows


class ProvidesIndex:
    """Who provides what, at which version.

    Replaces the ``capability -> {owner names}`` dictionaries that were
    built by hand all over the code base.  Same O(1) lookup by name,
    except the version constraint is then honoured instead of dropped.

    An owner is whatever the caller wants back: a package name, a
    ``(name, arch)`` pair, a solvable id.  The index never interprets
    it.
    """

    __slots__ = ('_by_name',)

    def __init__(self) -> None:
        #: capability name -> list of ``(owner, provider evr)``
        self._by_name: Dict[str, List[Tuple[object, str]]] = {}

    def add(self, name: str, evr: str, owner) -> None:
        """Record that ``owner`` provides ``name`` at ``evr``."""
        self._by_name.setdefault(name, []).append((owner, evr or ''))

    def add_capability(self, cap: str, owner) -> None:
        """Record a provide written as a synthesis string."""
        name, _sense, evr = parse_capability(cap)
        self.add(name, evr, owner)

    def add_header(self, hdr, owner) -> None:
        """Record every provide an RPM header declares."""
        for name, _sense, evr in header_rows(hdr, 'provides'):
            self.add(name, evr, owner)

    def providers(self, name: str, sense: int = 0, evr: str = '') -> Set:
        """The owners whose provide of ``name`` satisfies the constraint.

        An unversioned require takes the fast path and gets every
        provider of the name, which is also what rpm does.
        """
        entries = self._by_name.get(name)
        if not entries:
            return set()
        if not (sense & SENSE_MASK):
            return {owner for owner, _evr in entries}
        return {owner for owner, prov_evr in entries
                if provider_satisfies(prov_evr, sense, evr)}

    def providers_of(self, row: Tuple[str, int, str]) -> Set:
        """The owners that satisfy a ``(name, sense, evr)`` requirement."""
        return self.providers(row[0], row[1], row[2])

    def names(self) -> Iterable[str]:
        """Every capability name in the index."""
        return self._by_name.keys()

    def __len__(self) -> int:
        return len(self._by_name)
