"""Keep urpmi's own media configuration on the installed release.

urpmi and urpm-ng coexist on a Mageia system, and the relationship has
been one-way: we read ``/etc/urpmi/urpmi.cfg`` when importing media,
and never write it.  After a distupgrade that leaves urpmi pointing at
the release the machine just left, which is a defect an operator meets
the first time they type ``urpmi``.

A release reaches a URL in more than one shape, and we do not know how
many exist.  Two are handled, both measured on real configurations:

* the release as a **path segment**, ``…/distrib/9/x86_64/media/…`` —
  the form URL-direct entries use;
* the release inside the **mirror API filename**,
  ``mirrorlist: http://mirrors.mageia.org/api/mageia.9.x86_64.list`` —
  the form ``urpmi.addmedia --distrib`` leaves behind, and by far the
  most common on an ordinary install.

Only ``$MIRRORLIST`` genuinely repairs itself: urpmi resolves that one
through ``/etc/product.id``, which comes from ``mageia-release``, which
the distupgrade replaces.  A mirrorlist holding a literal versioned URL
does not, and would keep asking the API for the mirrors of a release
the machine has left.  ``urpmi.update`` does not fix any of it either:
it re-fetches from the URL it is given, it never rewrites it.

Anything else is **named, not guessed at**.  An entry whose URL still
mentions the release we left, and which none of the rules above could
move, is reported to the operator by name.  The rewriting rules are
deliberately narrow and the detection deliberately wide: a false
positive in a report costs one line someone reads and dismisses, a
false positive in a rewrite breaks their configuration.

What we do is the narrowest thing that works: move the release inside
each media URL.  Not a regeneration from our own media table, which
would discard the ``ignore`` markers, the key ids, the third-party
entries and the ordering an administrator chose.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

#: urpmi's configuration.  Hard-coded on purpose: the hook verb that
#: reaches this takes no argument, so a rule dropped in
#: ``/etc/urpm/hooks.d`` can only ask for the action we implemented,
#: on the target we chose.
URPMI_CFG = Path("/etc/urpmi/urpmi.cfg")

#: Written beside the original before the first rewrite.
BACKUP_SUFFIX = ".urpm-ng.bak"

#: The segment that follows the architecture in a binary media URL.
_MEDIA_SEGMENT = "media"

#: The architecture's stand-in in a source media URL.
_SRPMS_SEGMENT = "SRPMS"


@dataclass
class SyncReport:
    """What the rewrite did, for the caller to phrase and log."""

    path: Path
    source_release: str = ""
    target_release: str = ""
    rewritten: int = 0
    left_alone: int = 0
    backup: Optional[Path] = None
    skipped_reason: str = ""
    errors: List[str] = field(default_factory=list)
    #: Media whose URL still names the release we left and that none of
    #: the rules could move.  Named rather than silently skipped: the
    #: shapes a release can take in a URL are not enumerable, so the
    #: honest answer to an unknown one is to say so.
    unhandled: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.rewritten > 0


def _release_forms(release: str) -> tuple:
    """The spellings a release takes as a path segment.

    The canonical tree uses the bare number (``/10/``).  Community
    mirrors that carry several distributions side by side prefix it
    (``/mageia10/``, ``/mga10/``), and urpmi is happy with either since
    it only ever concatenates.
    """
    return (release, f"mageia{release}", f"mga{release}")


def _is_release_position(segments: List[str], index: int) -> bool:
    """Is the segment at ``index`` the release, or a number that looks like one?

    Position decides, not the value.  A Mageia media URL always spells
    out ``<release>/<arch>/media/...`` for binaries and
    ``<release>/SRPMS/...`` for sources, so a segment is the release
    when what follows it has that shape.  Without the check, a mirror
    published under ``/mirrors/10/mageia/distrib/9/...`` would have its
    own directory renamed on a 10 to 11 upgrade, and the operator would
    be left with a configuration pointing nowhere.
    """
    following = segments[index + 1:]
    if not following:
        return False
    if following[0] == _SRPMS_SEGMENT:
        return True
    return len(following) > 1 and following[1] == _MEDIA_SEGMENT


def _move_url(url: str, source_release: str, target_release: str) -> tuple:
    """Return ``(url, moved)`` with the release segment on the target.

    The URL is taken apart rather than pattern-matched: the release is
    a path segment, and only the path is ours to touch.  A query string
    or a host that happens to contain the release number stays as it
    is, because it never reaches the segment list.
    """
    parts = urlsplit(url)
    if not parts.path:
        return url, 0

    forms = _release_forms(source_release)
    segments = parts.path.split("/")
    moved = 0
    for index, segment in enumerate(segments):
        if segment not in forms or not _is_release_position(segments, index):
            continue
        segments[index] = segment.replace(source_release, target_release)
        moved += 1

    if not moved:
        return url, 0
    return urlunsplit(parts._replace(path="/".join(segments))), moved


def _move_line(line: str, source_release: str, target_release: str) -> tuple:
    """Return ``(line, moved)``, touching only the URLs it carries.

    A medium's header line is ``<escaped name> <url> {``, so the name
    is on the same line as the URL and must come out untouched: an
    administrator who called a medium ``Core 9`` keeps that name.
    Tokens are told apart by carrying a scheme, which the name never
    does and ``$MIRRORLIST`` never does either.
    """
    moved = 0
    tokens = line.split(" ")
    for index, token in enumerate(tokens):
        if "://" not in token:
            continue
        new_token, n = _move_url(token, source_release, target_release)
        if n:
            tokens[index] = new_token
            moved += n
    if not moved:
        return line, 0
    return " ".join(tokens), moved


#: The key whose value carries a mirror list.
_MIRRORLIST_KEY = "mirrorlist:"


def _move_mirror_api_url(url: str, source_release: str,
                         target_release: str) -> tuple:
    """Return ``(url, moved)`` with the release moved in the API filename.

    ``http://mirrors.mageia.org/api/mageia.9.x86_64.list`` names the
    release inside the file name, where the path-segment rule cannot
    see it.  The name is taken apart on its dots rather than matched:
    only the component right after ``mageia`` is a release, and only
    when the name ends in ``list``, so nothing else in the URL can be
    mistaken for one.
    """
    parts = urlsplit(url)
    segments = parts.path.split("/")
    if not segments:
        return url, 0
    fields = segments[-1].split(".")
    if len(fields) < 3 or fields[0] != "mageia" or fields[-1] != "list":
        return url, 0
    if fields[1] != source_release:
        return url, 0
    fields[1] = target_release
    segments[-1] = ".".join(fields)
    return urlunsplit(parts._replace(path="/".join(segments))), 1


def _move_mirrorlist_line(line: str, source_release: str,
                          target_release: str) -> tuple:
    """Return ``(line, moved)`` for a ``mirrorlist:`` line.

    Restricted to that key because the mirror API URL has no meaning
    anywhere else, and a rule that fires only where the shape occurs
    cannot fire where it should not.
    """
    stripped = line.strip()
    if not stripped.startswith(_MIRRORLIST_KEY):
        return line, 0
    value = stripped[len(_MIRRORLIST_KEY):].strip()
    if not value:
        return line, 0
    # ``$MIRRORLIST`` needs no special case: it has no file name to
    # take apart, so the rule below finds nothing to move.  Which is
    # the right answer, since that value resolves itself.
    moved_value, moved = _move_mirror_api_url(
        value, source_release, target_release)
    if not moved:
        return line, 0
    return line.replace(value, moved_value, 1), moved


def _mentions_release(line: str, source_release: str) -> bool:
    """Does a URL on this line still name the release we left?

    Deliberately loose, and used only to decide whether to *report* an
    entry.  ``$MIRRORLIST`` is excluded because it is not stale, it is
    resolved elsewhere.
    """
    for token in line.split(" "):
        # ``$MIRRORLIST`` carries no scheme, so it never reaches the
        # test below and is never reported as stale.
        if "://" not in token:
            continue
        for index in _positions_of(token, source_release):
            before = token[index - 1] if index else "/"
            after_at = index + len(source_release)
            after = token[after_at] if after_at < len(token) else "/"
            if not before.isalnum() and not after.isalnum():
                return True
    return False


def _positions_of(haystack: str, needle: str) -> List[int]:
    """Every index at which ``needle`` occurs in ``haystack``."""
    found, start = [], haystack.find(needle)
    while start != -1:
        found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def _medium_name(header: str) -> str:
    """The display name from a block header, spaces unescaped.

    A header is ``<escaped name> [url] {``.  The URL, when there is
    one, is the last token carrying a scheme; what remains is the name
    urpmi shows and the one an operator recognises.
    """
    body = header.strip()
    if body.endswith("{"):
        body = body[:-1].rstrip()
    tokens = [t for t in body.split(" ") if "://" not in t]
    return " ".join(t for t in tokens if t).replace("\\ ", " ").replace(
        "\\", "")


def sync_urpmi_config(source_release: str, target_release: str,
                      path: Path = URPMI_CFG) -> SyncReport:
    """Move urpmi's URL-direct media from one release to the next.

    Args:
        source_release: The release the URLs currently name, e.g. ``"9"``.
            An identity, never the ``identity:numeric`` form the
            distupgrade state may hold; see
            :func:`urpm.core.distupgrade.version.identity_of`.
        target_release: The release now installed, e.g. ``"10"``.
        path: Override, for tests.

    Returns:
        A :class:`SyncReport`.  Nothing here raises: this runs after a
        committed operation and must never turn a success into a
        failure.
    """
    report = SyncReport(path=path,
                        source_release=source_release,
                        target_release=target_release)

    if not source_release or not target_release:
        report.skipped_reason = "no release pair to move between"
        return report
    if source_release == target_release:
        report.skipped_reason = "already on the installed release"
        return report
    if not path.exists():
        # urpmi is not installed, or was never configured.  Not our
        # business either way.
        report.skipped_reason = "no urpmi configuration"
        return report

    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"unreadable: {exc}")
        return report

    # Walked block by block — ``<name> [url] {`` down to ``}`` — rather
    # than line by line, so an entry no rule could move can be reported
    # under the name its operator knows it by.
    lines, rewritten = [], 0
    name, moved_here, stale_here = "", 0, False
    for line in original.splitlines(keepends=True):
        body = line.rstrip("\n")
        newline = line[len(body):]
        stripped = body.strip()

        if stripped.endswith("{"):
            name, moved_here, stale_here = _medium_name(body), 0, False

        new_body, moved = _move_line(body, source_release, target_release)
        new_body, moved_list = _move_mirrorlist_line(
            new_body, source_release, target_release)
        moved += moved_list
        if not moved and _mentions_release(body, source_release):
            stale_here = True
        rewritten += moved
        moved_here += moved
        lines.append(new_body + newline)

        if stripped == "}":
            # Only a block where nothing at all was moved counts as
            # unhandled.  A mirror publishing under its own numbered
            # directory keeps that number legitimately, and reporting
            # an entry we did move would train the operator to ignore
            # the message.
            if name and stale_here and not moved_here:
                report.unhandled.append(name)
            name, moved_here, stale_here = "", 0, False

    if not rewritten:
        report.left_alone = len(original.splitlines())
        report.skipped_reason = "nothing named the previous release"
        return report

    try:
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
        report.backup = backup
        path.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"could not write: {exc}")
        return report

    report.rewritten = rewritten
    logger.info("urpmi.cfg: moved %d URL(s) from %s to %s",
                rewritten, source_release, target_release)
    return report
