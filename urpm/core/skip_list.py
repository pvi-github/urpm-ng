"""urpmi's ``skip.list``, read and written as it is.

Two mechanisms coexist on a Mageia machine and they do not say the
same thing.  A **hold** freezes an installed package in the state it
is in; the object is the installed package, the question is « does it
move ».  A **skip** masks candidates from the media, as if they were
not declared in the synthesis; the object is the candidate, the
question is « do we offer it ».  That an installed and masked package
stops moving is a consequence, not the intention.

urpmi's own source settles the nature of the file.
``_compute_flags_for_skiplist`` flags packages **in the media**, not
the installed ones, and ``select.pm`` clears the flag as soon as the
package is explicitly asked for.  The documentation says « packages
that should not be automatically updated when using ``--auto-select`` »:
a selection rule.

So the file is the storage, not a mirror of some table of ours: there
is one source, it cannot drift from itself, and urpm knows a format
rather than a location.  ``urpm skip`` edits it, the resolution reads
it.

Format, from ``urpm::sys::get_packages_list``: ``#`` to the end of the
line is dropped, the rest is trimmed, empty lines are ignored.  An
entry is a package name, or a regular expression between slashes
compared against the full ``name-version-release.arch``, without the
epoch.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Where urpmi keeps it.  A path, never hard-coded anywhere else: the
#: commands take it as a parameter so a list can be read from, or
#: written to, somewhere else entirely.
SKIP_LIST = Path("/etc/urpmi/skip.list")

#: What urpmi writes at the top of a fresh file, kept so ours reads
#: the same way to whoever opens it next.
_HEADER = (
    "# Here you can specify the packages that won't be upgraded "
    "automatically\n"
    "# for example, to exclude all apache packages :\n"
    "# /^apache/\n"
)


def skip_list_path(root: Optional[str] = None,
                   path: Optional[Path] = None) -> Path:
    """The file to use, honouring a chroot like urpmi does.

    urpmi resolves it through ``prefer_rooted``; an operation on a
    target root must read and write the target's list, not the host's.
    """
    if path is not None:
        return Path(path)
    if root and str(root) != "/":
        return Path(root) / "etc/urpmi/skip.list"
    return SKIP_LIST


def parse_line(line: str) -> str:
    """One raw line to its entry, or ``''`` when it carries none.

    Mirrors ``get_packages_list``: everything from ``#`` is dropped,
    which also covers a trailing comment, then both ends are trimmed.
    """
    return line.split("#", 1)[0].strip()


def read_entries(path: Path) -> List[str]:
    """Every entry of the file, in order, comments and blanks gone.

    A missing file is not an error: urpmi may simply not be installed,
    and then nothing is masked.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return []
    return [entry for entry in (parse_line(line) for line in raw.splitlines())
            if entry]


class SkipList:
    """The entries, ready to be matched against a candidate.

    Built once per resolution.  Plain names are an exact set, the
    expressions are compiled once, and an expression that does not
    compile is reported and dropped rather than raising: the file is
    edited by hand, so a bad one is a matter of time.
    """

    __slots__ = ("names", "patterns", "invalid")

    def __init__(self, entries: Iterable[str] = ()):
        self.names = set()
        self.patterns: List[Tuple[str, "re.Pattern"]] = []
        self.invalid: List[Tuple[str, str]] = []
        for entry in entries:
            if len(entry) > 1 and entry.startswith("/") and entry.endswith("/"):
                source = entry[1:-1]
                try:
                    self.patterns.append((entry, re.compile(source)))
                except re.error as exc:
                    self.invalid.append((entry, str(exc)))
            else:
                self.names.add(entry)

    @classmethod
    def from_file(cls, root: Optional[str] = None,
                  path: Optional[Path] = None) -> "SkipList":
        return cls(read_entries(skip_list_path(root, path)))

    def __bool__(self) -> bool:
        return bool(self.names or self.patterns)

    def masks(self, name: str, evr: str = "", arch: str = "") -> bool:
        """Is this candidate masked?

        The expression is compared against ``name-version-release.arch``,
        which is what urpmi calls the full name and what its own
        documentation names.  The epoch is not part of it.
        """
        if name in self.names:
            return True
        if not self.patterns:
            return False
        fullname = name
        if evr:
            fullname = f"{name}-{evr}"
        if arch:
            fullname = f"{fullname}.{arch}"
        return any(pattern.search(fullname) for _raw, pattern in self.patterns)


def _write(path: Path, lines: List[str]) -> None:
    """Replace the file atomically, keeping its mode.

    This file belongs to urpmi: a half-written one would leave the
    other package manager with a truncated policy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skip.list.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_entry(entry: str, root: Optional[str] = None,
              path: Optional[Path] = None) -> bool:
    """Append an entry unless the file already carries it.

    Only entry lines are touched: comments and anything else stay
    where they are, to the character.  Returns False when it was
    already there.
    """
    target = skip_list_path(root, path)
    if entry in read_entries(target):
        return False
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    except FileNotFoundError:
        lines = [_HEADER]
    lines.append(entry + "\n")
    _write(target, lines)
    return True


def remove_entry(entry: str, root: Optional[str] = None,
                 path: Optional[Path] = None) -> bool:
    """Drop the lines carrying exactly this entry.

    Returns False when the file does not carry it.  A name covered
    only by an expression is not removed here: rewriting somebody
    else's rule to drop one name is not ours to do, and the caller
    says so.
    """
    target = skip_list_path(root, path)
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    kept, found = [], False
    for line in raw.splitlines(keepends=True):
        if parse_line(line) == entry:
            found = True
            continue
        kept.append(line)
    if not found:
        return False
    _write(target, kept)
    return True


def covering_patterns(name: str, root: Optional[str] = None,
                      path: Optional[Path] = None) -> List[str]:
    """The expressions that mask *name*, for a refusal that explains.

    Used when an entry cannot be removed because it is not an entry:
    the operator asked for a name, and what masks it is a rule they
    wrote.
    """
    skips = SkipList.from_file(root, path)
    return [raw for raw, pattern in skips.patterns if pattern.search(name)]
