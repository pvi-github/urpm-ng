"""Keep urpmi's own media configuration on the installed release.

urpmi and urpm-ng coexist on a Mageia system, and the relationship has
been one-way: we read ``/etc/urpmi/urpmi.cfg`` when importing media,
and never write it.  After a distupgrade that leaves urpmi pointing at
the release the machine just left, which is a defect an operator meets
the first time they type ``urpmi``.

A release reaches a URL in more than one shape, and we do not know how
many exist.  Three are handled, all measured on real configurations:

* the release as a **path segment**, ``…/distrib/9/x86_64/media/…`` —
  the form URL-direct entries use;
* the release inside the **mirror API filename**,
  ``mirrorlist: http://mirrors.mageia.org/api/mageia.9.x86_64.list`` —
  the form ``urpmi.addmedia --distrib`` leaves behind, and by far the
  most common on an ordinary install;
* the same path segment inside a **bare local path**,
  ``DVD_Core /media/9/x86_64/media/core`` — what urpmi writes for a
  medium it reaches through the filesystem rather than the network.
  It carries no scheme, so the obvious « a URL is a token containing
  ``://`` » test walked straight past it: a disc medium came out of a
  release upgrade neither moved nor reported, which is the worst of
  the outcomes available.  A bare path is read as a URL on a block
  header, where urpmi puts the medium's own address, and nowhere else:
  ``removable: /dev/sr0`` names a device, not a medium.

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

from .release_segment import is_release_position, release_tokens

logger = logging.getLogger(__name__)

#: urpmi's configuration.  Hard-coded on purpose: the hook verb that
#: reaches this takes no argument, so a rule dropped in
#: ``/etc/urpm/hooks.d`` can only ask for the action we implemented,
#: on the target we chose.
URPMI_CFG = Path("/etc/urpmi/urpmi.cfg")

#: Written beside the original before the first rewrite.
BACKUP_SUFFIX = ".urpm-ng.bak"


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
    #: Media we moved and whose new target answered 404.  Marked
    #: ``ignore`` so urpmi stops failing on them, and named because
    #: putting them back is a manual job.
    disabled: List[str] = field(default_factory=list)
    #: Media we moved whose new path is not on the filesystem right
    #: now.  Named, never switched off: a disc that is not in the
    #: drive is absent without being gone, and marking it ``ignore``
    #: would turn « insert the disc » into « edit the configuration ».
    absent: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.rewritten > 0


def _is_media_url(token: str, *, bare_paths: bool) -> bool:
    """Is this token the address of a medium?

    A scheme is the usual marker.  ``bare_paths`` additionally accepts
    an absolute path, which is how urpmi declares a medium it reads
    from the filesystem; the caller turns it on for a block header and
    leaves it off everywhere else, so a device path in the body of a
    block stays what it is.
    """
    if "://" in token:
        return True
    return bare_paths and token.startswith("/")


def _local_target(url: str) -> Optional[Path]:
    """The filesystem path a medium address points at, or ``None``.

    Both shapes urpmi accepts for a local medium answer here: the bare
    path it writes itself, and the ``file://`` form an administrator
    may have typed.  Everything else travels by network and is checked
    by asking the server.
    """
    if url.startswith("/"):
        return Path(url)
    parts = urlsplit(url)
    if parts.scheme == "file" and not parts.netloc:
        return Path(parts.path)
    return None


def _move_url(url: str, source_release: str, target_release: str) -> tuple:
    """Return ``(url, moved)`` with the release segment on the target.

    The URL is taken apart rather than pattern-matched: the release is
    a path segment, and only the path is ours to touch.  A query string
    or a host that happens to contain the release number stays as it
    is, because it never reaches the segment list.

    Which segment is the release, and how the mirror spells it, are
    :mod:`urpm.core.release_segment`'s business: a community layout
    writes ``mageia9`` where the official tree writes ``9``, and the
    rewrite has to give back the first spelling to the first mirror.
    """
    parts = urlsplit(url)
    if not parts.path:
        return url, 0

    segments = parts.path.split("/")
    moved = 0
    for index, token in release_tokens(segments):
        if token.identity != source_release:
            continue
        if not is_release_position(segments, index):
            continue
        segments[index] = token.respelled(target_release)
        moved += 1

    if not moved:
        return url, 0
    return urlunsplit(parts._replace(path="/".join(segments))), moved


def _move_line(line: str, source_release: str, target_release: str,
               *, bare_paths: bool = False) -> tuple:
    """Return ``(line, moved)``, touching only the URLs it carries.

    A medium's header line is ``<escaped name> <url> {``, so the name
    is on the same line as the URL and must come out untouched: an
    administrator who called a medium ``Core 9`` keeps that name.
    Tokens are told apart by :func:`_is_media_url`, which a name never
    satisfies and ``$MIRRORLIST`` never satisfies either.
    """
    moved = 0
    tokens = line.split(" ")
    for index, token in enumerate(tokens):
        if not _is_media_url(token, bare_paths=bare_paths):
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


def _mentions_release(line: str, source_release: str,
                      *, bare_paths: bool = False) -> bool:
    """Does a URL on this line still name the release we left?

    Deliberately loose, and used only to decide whether to *report* an
    entry.  ``$MIRRORLIST`` is excluded because it is not stale, it is
    resolved elsewhere.

    A release glued to a prefix, ``mageia9``, is surrounded by letters
    and the neighbour test below cannot see it.  That is on purpose
    and no longer costs anything: :func:`_move_url` moves every
    prefixed spelling it finds, so such an entry never reaches the
    reporting stage.  Before it did, and the operator was told nothing
    about a medium nobody had moved, which is the worst of the two
    outcomes.
    """
    for token in line.split(" "):
        # ``$MIRRORLIST`` is not an address, so it never reaches the
        # test below and is never reported as stale.
        if not _is_media_url(token, bare_paths=bare_paths):
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
    one, is the token holding the medium's address; what remains is the
    name urpmi shows and the one an operator recognises.  A local
    medium's bare path counts as that address, or the name reported for
    a disc read ``DVD_Core /media/9/x86_64/media/core``.
    """
    body = header.strip()
    if body.endswith("{"):
        body = body[:-1].rstrip()
    tokens = [t for t in body.split(" ")
              if not _is_media_url(t, bare_paths=True)]
    return " ".join(t for t in tokens if t).replace("\\ ", " ").replace(
        "\\", "")


#: Appended to a medium's URL to ask whether it exists at all.
_SYNTHESIS = "media_info/synthesis.hdlist.cz"

#: What urpmi writes to switch a medium off.  Indented like the rest of
#: a block's body, which is what ``urpmi.addmedia`` produces.
_IGNORE_LINE = "  ignore"


def _http_status(url: str, timeout: float = 5.0) -> Optional[int]:
    """Ask whether a URL exists, and return the status code.

    A ``HEAD`` is enough: the question is existence, not content.

    Returns:
        The HTTP status, or ``None`` when the server could not be
        reached at all.  The distinction is the whole point — a 404
        means the medium was never published for this release, an
        unreachable host means nothing about it.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    try:
        request = Request(url, method="HEAD")
        request.add_header("User-Agent", "urpm-ng")
        with urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)
    except (URLError, OSError, ValueError) as exc:
        logger.debug("urpmi.cfg: %s unreachable: %s", url, exc)
        return None


def _direct_urls(line: str, *, bare_paths: bool = False) -> List[str]:
    """The URL tokens a line carries, mirror lists excluded.

    Only a medium pinned to one server can go missing on its own: the
    official media travel by mirror list and are cloned from the same
    reference, so an absent one is absent everywhere and there is
    nothing to discover by asking.
    """
    if line.strip().startswith(_MIRRORLIST_KEY):
        return []
    return [token for token in line.split(" ")
            if _is_media_url(token, bare_paths=bare_paths)]


def sync_urpmi_config(source_release: str, target_release: str,
                      path: Path = URPMI_CFG,
                      probe=None) -> SyncReport:
    """Move urpmi's URL-direct media from one release to the next.

    Args:
        source_release: The release the URLs currently name, e.g. ``"9"``.
            An identity, never the ``identity:numeric`` form the
            distupgrade state may hold; see
            :func:`urpm.core.distupgrade.version.identity_of`.
        target_release: The release now installed, e.g. ``"10"``.
        path: Override, for tests.
        probe: ``url -> status | None``, used to check that what we
            just wrote exists.  Injected so tests never touch the
            network; defaults to a ``HEAD``.

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
    name, moved_here, stale_here, ignored_here = "", 0, False, False
    header_at, moved_urls = 0, []
    # ``(header line index, medium name, URLs we rewrote)`` for the
    # blocks worth checking afterwards.
    to_check = []
    for line in original.splitlines(keepends=True):
        body = line.rstrip("\n")
        newline = line[len(body):]
        stripped = body.strip()

        # A bare path is a medium's address on a header line, and a
        # device or a directory option anywhere else.  The distinction
        # is positional, and this loop is the only place that knows it.
        is_header = stripped.endswith("{")

        if is_header:
            name, moved_here, stale_here = _medium_name(body), 0, False
            ignored_here, header_at, moved_urls = False, len(lines), []
        elif stripped == "ignore":
            # Already switched off by the operator: nothing to move,
            # nothing to check, nothing to say.
            ignored_here = True

        new_body, moved = _move_line(body, source_release, target_release,
                                     bare_paths=is_header)
        new_body, moved_list = _move_mirrorlist_line(
            new_body, source_release, target_release)
        moved += moved_list
        if not moved and _mentions_release(body, source_release,
                                           bare_paths=is_header):
            stale_here = True
        if moved:
            moved_urls += _direct_urls(new_body, bare_paths=is_header)
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
            if name and moved_urls and not ignored_here:
                to_check.append((header_at, name, moved_urls))
            name, moved_here, stale_here, ignored_here = "", 0, False, False
            moved_urls = []

    if not rewritten:
        report.left_alone = len(original.splitlines())
        report.skipped_reason = "nothing named the previous release"
        return report

    # What we just wrote, checked.  Only the media pinned to one
    # server: a third-party repository can lag behind a release, or
    # change its URL scheme between two of them, and the operator then
    # meets a 404 on every urpmi run.  The official media travel by
    # mirror list, cloned from the same reference, so there is nothing
    # to learn by asking about them.
    check = probe if probe is not None else _http_status
    for header_at, medium, urls in reversed(to_check):
        # A local medium is asked of the filesystem, and the answer is
        # read differently: an absent path is reported, never switched
        # off.  The medium may simply be a disc nobody has inserted,
        # and ``ignore`` would make that an edit instead of a gesture.
        local = [path for path in (_local_target(url) for url in urls)
                 if path is not None]
        if any(not (path / _SYNTHESIS).exists() for path in local):
            report.absent.append(medium)

        missing = any(
            check(url.rstrip("/") + "/" + _SYNTHESIS) == 404
            for url in urls if _local_target(url) is None)
        if not missing:
            continue
        # Switched off rather than left to fail, and named because
        # putting it back means finding the new URL by hand.
        lines.insert(header_at + 1, _IGNORE_LINE + "\n")
        report.disabled.append(medium)
    report.disabled.reverse()
    report.absent.reverse()

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
