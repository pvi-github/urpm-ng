"""How a release spells itself inside a URL, in one place.

A Mageia release reaches a URL as a single path segment.  The official
tree spells it bare, ``/distrib/10/x86_64/media/core/release``.  A
community mirror that carries several distributions side by side
prefixes it instead, ``/mageia10/free/x86_64`` or ``/mga10/…``,
because a bare ``/10/`` under its own root would not say which
distribution it holds.

Five places in the tree needed that knowledge and five places had it,
two of them carrying a « keep in sync with » comment.  The drift those
comments were meant to prevent happened anyway: the distupgrade
learned the prefixed form, the urpmi.cfg sync did not, and a community
medium stayed pointed at the release the machine had just left without
even being named in the report.  So the spellings, the canonical
identity behind them, the segments a media URL is built from and the
act of respelling a release while keeping the prefix the mirror chose
all live here.

What belongs in this module: how a release, an architecture and a
source tree are written as path segments, and how to tell a release
from a number that merely looks like one.  What does not: which
releases exist, which of them a given command is willing to accept,
and whether any of it is reachable.  Those are policy, and policy
stays with the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

#: The rolling release, which is a name and not a number.
CAULDRON = "cauldron"

#: What a mirror may glue in front of the release number.  Neither
#: spelling contains the other, so the order is free; it is only fixed
#: so the pattern built below, and any message quoting it, is stable.
RELEASE_PREFIXES: Tuple[str, ...] = ("mageia", "mga")

#: The architectures Mageia builds for, as they appear in a URL.
#: Single list on purpose: three copies of it used to disagree, and an
#: architecture missing from one of them is a URL that silently fails
#: to be recognised as a media tree.
BUILD_ARCHES = frozenset({
    "x86_64", "i586", "i686", "aarch64", "armv7hl", "armv5tl", "ppc64le",
})

#: The 32-bit architectures Mageia has published a media tree under.
#: Two of them, and that is the point: release 9 publishes ``i586``,
#: release 10 publishes ``i686`` (and keeps ``i586`` as an alias of
#: it).  Anything comparing a medium across a release change has to
#: treat them as one slot, or the 32-bit media of the new release look
#: like media the machine never had.
BIT32_ARCHES = frozenset({"i586", "i686"})

#: What the 32-bit tree is called when the question is « which medium
#: is this », not « which directory ».  The catalogue and the CLI
#: already use this word (``urpm media discover --with 32bit``).
BIT32_SLOT = "32bit"

#: ``noarch`` is not something Mageia builds a tree for, but community
#: layouts do publish noarch-only channels at ``…/<class>/noarch/``.
#: Accepted as a *segment*, which is a different question from being a
#: target architecture.
URL_ARCH_SEGMENTS = BUILD_ARCHES | {"noarch"}

#: Where a mirror puts the sources.  It sits exactly where an
#: architecture sits in the binary layout, which is why the two are
#: interchangeable as layout anchors below and nowhere else: ``SRPMS``
#: is a tree, never a value for ``media.architecture``.
SOURCES_SEGMENT = "SRPMS"

#: The segment that follows the architecture in a binary media URL.
MEDIA_SEGMENT = "media"

_TOKEN_RE = re.compile(
    r"^(?P<prefix>" + "|".join(RELEASE_PREFIXES) + r")?"
    r"(?P<identity>\d+(?:\.\d+)?|" + CAULDRON + r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReleaseToken:
    """A path segment read as a release.

    Attributes:
        prefix: The mirror's spelling, verbatim and possibly empty.
            Kept as found rather than normalised: it is going back
            into a URL that has to keep working.
        identity: The release itself, lowercased, as the rest of the
            codebase names it: a numeric (``'10'``, ``'11.5'``) or
            :data:`CAULDRON`.
    """

    prefix: str
    identity: str

    @property
    def text(self) -> str:
        """The segment as it reads in the URL."""
        return f"{self.prefix}{self.identity}"

    def respelled(self, identity: str) -> str:
        """The same segment moved to *identity*, prefix untouched.

        ``mageia9`` becomes ``mageia10`` and ``9`` becomes ``10``: a
        mirror's own spelling is part of its layout, and rewriting
        ``mageia9`` as ``10`` would point the URL nowhere.
        """
        return f"{self.prefix}{identity}"


def parse_release_token(segment: str) -> Optional[ReleaseToken]:
    """Read *segment* as a release, or return None.

    Recognises ``10``, ``11.5``, ``cauldron`` and each of those behind
    a prefix from :data:`RELEASE_PREFIXES`.  Case-insensitive, since a
    mirror is free to publish ``/Mageia10/``.

    Returning None is the common and expected outcome: most segments
    of most URLs are not releases.
    """
    if not segment:
        return None
    match = _TOKEN_RE.match(segment)
    if match is None:
        return None
    return ReleaseToken(
        prefix=match.group("prefix") or "",
        identity=match.group("identity").lower(),
    )


def release_tokens(
    segments: Sequence[str],
) -> List[Tuple[int, ReleaseToken]]:
    """Every segment that reads as a release, with its index."""
    found = []
    for index, segment in enumerate(segments):
        token = parse_release_token(segment)
        if token is not None:
            found.append((index, token))
    return found


def is_arch_segment(segment: str) -> bool:
    """Is *segment* an architecture as a URL spells one?"""
    return segment in URL_ARCH_SEGMENTS


def is_sources_segment(segment: str) -> bool:
    """Is *segment* the source tree?"""
    return segment == SOURCES_SEGMENT


def is_32bit_arch(architecture: str) -> bool:
    """Is *architecture* one of the 32-bit trees?

    Asked of a medium's architecture, not of a URL segment: a 32-bit
    medium on a 64-bit machine is the cross-arch case the catalogue
    calls ``32bit``.
    """
    return (architecture or "").lower() in BIT32_ARCHES


def arch_slot(architecture: str) -> str:
    """The place an architecture occupies, across a release change.

    Two media are the same medium one release apart when they hold the
    same section of the same slot.  For every architecture the slot is
    the architecture itself; for the 32-bit trees it is the single name
    :data:`BIT32_SLOT`, because Mageia renamed that tree from ``i586``
    to ``i686`` between 9 and 10 and the medium did not change for it.

    Use it to *compare*, never to build a URL: the slot is not an
    architecture and no mirror serves a directory by that name.
    """
    if is_32bit_arch(architecture):
        return BIT32_SLOT
    return (architecture or "").lower()


def layout_anchors(segments: Sequence[str]) -> List[Tuple[int, str]]:
    """Every segment that anchors a media layout, with its index.

    An anchor is an architecture or :data:`SOURCES_SEGMENT`: the two
    occupy the same position in a mirror's tree, one holding the
    binaries and the other the sources.  A caller locating the release
    in a URL needs both, or a source media's URL is read as having no
    recognisable shape at all.
    """
    found = []
    for index, segment in enumerate(segments):
        if is_arch_segment(segment) or is_sources_segment(segment):
            found.append((index, segment))
    return found


def is_release_position(segments: Sequence[str], index: int) -> bool:
    """Is the segment at *index* a release, or a number that looks like one?

    A prefixed segment answers for itself: nothing but a release is
    written ``mageia10`` or ``mga10``, so no context is needed and
    none is asked for.  This is the whole reason a community layout
    like ``/mageia10/free/x86_64`` can be recognised at all, since
    nothing in what follows it has the official shape.

    A bare number says nothing on its own, so position decides: it is
    the release when what follows is ``<arch>/media/…`` or
    :data:`SOURCES_SEGMENT`.  Without that check a mirror publishing
    Mageia under its own numbering, ``/mirrors/10/mageia/distrib/9/…``,
    would have its own directory renamed on a 10 to 11 upgrade and the
    operator would be left with a configuration pointing nowhere.
    """
    if index < 0 or index >= len(segments):
        return False
    token = parse_release_token(segments[index])
    if token is None:
        return False
    if token.prefix:
        return True
    following = segments[index + 1:]
    if not following:
        return False
    if is_sources_segment(following[0]):
        return True
    return len(following) > 1 and following[1] == MEDIA_SEGMENT
