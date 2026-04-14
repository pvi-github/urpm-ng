"""
Parser for files.xml.lzma media metadata.

This module provides streaming parsing of files.xml.lzma files, which
contain file lists for all packages in a repository. These files can
be very large (~9 million lines for a full repository), so we use
iterparse to avoid loading the entire file into memory.

Format example:
    <?xml version="1.0" encoding="utf-8"?>
    <media_info><files fn="package-1.0-1.mga9.x86_64">
    /usr/bin/foo
    /usr/lib64/libfoo.so
    </files><files fn="other-pkg-2.0-1.mga9.noarch">
    /etc/other.conf
    </files></media_info>

In addition to the low-level parser :func:`parse_files_xml`, this
module provides :func:`iter_file_matches`, a high-level scanner used
by ``urpm f`` to answer "which package contains <file>?" queries
without ever populating a SQLite cache.
"""

import fnmatch
import logging
import lzma
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Callable, Set, Tuple
from xml.etree.ElementTree import iterparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileMatch:
    """A single (package, file_path) hit produced by the scanner."""
    nevra: str
    path: str
    media_name: str


def parse_files_xml(
    path: Path,
    progress_callback: Optional[Callable[[int], None]] = None,
    progress_interval: int = 100000
) -> Iterator[Tuple[str, List[str]]]:
    """Parse files.xml.lzma and yield (nevra, file_list) tuples.

    Uses streaming XML parsing to handle large files efficiently.
    Memory usage stays constant regardless of file size.

    Args:
        path: Path to files.xml.lzma file
        progress_callback: Optional callback called with line count every progress_interval lines
        progress_interval: How often to call progress_callback (default: every 100k lines)

    Yields:
        Tuples of (package_nevra, list_of_files)

    Example:
        for nevra, files in parse_files_xml(Path('/var/cache/urpm/media_info/files.xml.lzma')):
            print(f"{nevra}: {len(files)} files")
    """
    if not path.exists():
        logger.warning(f"files.xml not found: {path}")
        return

    # Determine if compressed
    if path.suffix == '.lzma' or str(path).endswith('.xml.lzma'):
        opener = lambda p: lzma.open(p, 'rb')
    else:
        opener = lambda p: open(p, 'rb')

    pkg_count = 0
    file_count = 0

    try:
        with opener(path) as f:
            # Use iterparse for streaming - only care about 'end' events for <files>
            context = iterparse(f, events=('end',))

            for event, elem in context:
                if elem.tag == 'files':
                    # Get package NEVRA from 'fn' attribute
                    nevra = elem.get('fn', '')
                    if not nevra:
                        # Skip entries without fn attribute
                        elem.clear()
                        continue

                    # Parse file list from text content
                    # Files are newline-separated in the element text
                    text = elem.text or ''
                    files = [line.strip() for line in text.strip().split('\n') if line.strip()]

                    pkg_count += 1
                    file_count += len(files)

                    # Progress callback
                    if progress_callback and file_count % progress_interval < len(files):
                        progress_callback(file_count)

                    yield nevra, files

                    # Clear element to free memory (critical for large files!)
                    elem.clear()

    except lzma.LZMAError as e:
        logger.error(f"LZMA decompression error: {e}")
        raise
    except Exception as e:
        logger.error(f"Error parsing files.xml: {e}")
        raise

    logger.debug(f"Parsed {pkg_count} packages, {file_count} files from {path}")


def count_files_xml(path: Path) -> Tuple[int, int]:
    """Count packages and files in files.xml without loading all data.

    Args:
        path: Path to files.xml.lzma file

    Returns:
        Tuple of (package_count, file_count)
    """
    pkg_count = 0
    file_count = 0

    for nevra, files in parse_files_xml(path):
        pkg_count += 1
        file_count += len(files)

    return pkg_count, file_count


def search_files_xml(
    path: Path,
    pattern: str,
    case_sensitive: bool = False,
    exact_match: bool = False,
    max_results: int = 0
) -> Iterator[Tuple[str, str]]:
    """Search for files matching a pattern in files.xml.

    This is a fallback method for when the SQLite cache is not available.
    For normal use, prefer searching via the database.

    Args:
        path: Path to files.xml.lzma file
        pattern: Search pattern (substring match by default)
        case_sensitive: If True, match case exactly
        exact_match: If True, match full path exactly (not substring)
        max_results: Maximum results to return (0 = unlimited)

    Yields:
        Tuples of (package_nevra, file_path)
    """
    import fnmatch
    import re

    results = 0

    # Prepare pattern for matching
    if not case_sensitive:
        pattern_lower = pattern.lower()

    # Check if pattern contains wildcards
    is_glob = '*' in pattern or '?' in pattern or '[' in pattern

    if is_glob:
        if not case_sensitive:
            regex = re.compile(fnmatch.translate(pattern), re.IGNORECASE)
        else:
            regex = re.compile(fnmatch.translate(pattern))
        match_func = lambda f: regex.match(f)
    elif exact_match:
        if case_sensitive:
            match_func = lambda f: f == pattern
        else:
            match_func = lambda f: f.lower() == pattern_lower
    else:
        # Substring match
        if case_sensitive:
            match_func = lambda f: pattern in f
        else:
            match_func = lambda f: pattern_lower in f.lower()

    for nevra, files in parse_files_xml(path):
        for filepath in files:
            if match_func(filepath):
                yield nevra, filepath
                results += 1
                if max_results > 0 and results >= max_results:
                    return


def extract_nevras_from_files_xml(path: Path) -> Set[str]:
    """Extract only package NEVRAs from files.xml without full parsing.

    This is much faster than parse_files_xml() when you only need the
    package list, not the file contents. Uses regex on raw bytes.

    Args:
        path: Path to files.xml.lzma file

    Returns:
        Set of package NEVRAs (e.g., {'wget-1.21-1.mga9.x86_64', ...})

    Performance: ~2 seconds for a 7M-line files.xml vs ~30s for full parse.
    """
    if not path.exists():
        logger.warning(f"files.xml not found: {path}")
        return set()

    nevras = set()
    pattern = re.compile(rb'fn="([^"]+)"')

    # Determine if compressed
    if path.suffix == '.lzma' or str(path).endswith('.xml.lzma'):
        opener = lambda p: lzma.open(p, 'rb')
    else:
        opener = lambda p: open(p, 'rb')

    try:
        with opener(path) as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    nevras.add(match.group(1).decode('utf-8'))
    except lzma.LZMAError as e:
        logger.error(f"LZMA decompression error: {e}")
        raise
    except Exception as e:
        logger.error(f"Error extracting NEVRAs: {e}")
        raise

    logger.debug(f"Extracted {len(nevras)} NEVRAs from {path}")
    return nevras


# ---------------------------------------------------------------------------
# High-level scanner used by ``urpm f``
# ---------------------------------------------------------------------------
#
# The scanner streams every enabled media's ``files.xml.lzma`` and yields
# one :class:`FileMatch` per file matching the user-supplied pattern.
# It does *not* maintain any persistent cache: each invocation reopens
# the compressed XML.  For ~26 MB of compressed data on SSD this stays
# below one second, which is acceptable for an interactive command.


def _compile_byte_matcher(pattern: str) -> Callable[[bytes], bool]:
    """Build a case-insensitive matcher operating directly on UTF-8 bytes.

    The synthesis format is ASCII-clean for the file-path lines we
    care about (Mageia paths are ASCII), so working on bytes lets us
    skip a full ``str.decode`` for every one of the ~9 M paths in a
    typical Core Release scan.  Three regimes:

    * pattern with no ``/`` and no wildcard → basename match,
      ``line.lower().endswith(b'/' + pattern.lower())``.  This is the
      most common shape (``urpm f bash``) and runs about 5× faster
      than the equivalent regex with ``IGNORECASE``.
    * pattern starting with ``/`` and no wildcard → exact-path match
      via lowercase bytes equality.
    * any pattern containing ``*`` or ``?`` → fall back to a regex
      compiled from :func:`fnmatch.translate`.  The regex still runs
      on decoded text via ``re.match``; we decode each line lazily
      from inside the wrapper.
    """
    has_wildcard = '*' in pattern or '?' in pattern
    pattern_lower = pattern.lower()

    if not has_wildcard:
        if pattern.startswith('/'):
            target = pattern_lower.encode('utf-8')
            return lambda b: b.lower() == target
        needle = ('/' + pattern_lower).encode('utf-8')
        return lambda b: b.lower().endswith(needle)

    rx = re.compile(fnmatch.translate(pattern), re.IGNORECASE)
    def _match(b: bytes) -> bool:
        try:
            return rx.match(b.decode('utf-8')) is not None
        except UnicodeDecodeError:
            return False
    return _match


def _grep_pattern_for(pattern: str) -> str:
    """Translate a user pattern into a POSIX ERE suitable for ``xzgrep``.

    The grep regex is intentionally a *superset* of the historical
    ``fnmatch`` semantics: false positives are filtered out a second
    time in Python by :func:`_compile_byte_matcher`.  Erring on the
    side of "too generous" lets us keep the grep expression simple
    and predictable, while never missing a true match.
    """
    has_wildcard = '*' in pattern or '?' in pattern

    if not has_wildcard:
        # Bare basename → ``/<basename>`` anywhere on the line, but
        # not as a substring of a larger basename.  Anchor on end of
        # line: paths in the file are one-per-line.
        if not pattern.startswith('/'):
            return '/' + re.escape(pattern) + '$'
        # Absolute path → exact line match.
        return '^' + re.escape(pattern) + '$'

    # Glob pattern: translate ``*`` → ``.*`` and ``?`` → ``.``,
    # escaping every other regex metacharacter.  This is coarser
    # than ``fnmatch.translate`` (no character-class support) but
    # the Python matcher reapplies the strict fnmatch afterwards.
    out = []
    for ch in pattern:
        if ch == '*':
            out.append('.*')
        elif ch == '?':
            out.append('.')
        else:
            out.append(re.escape(ch))
    glob_re = ''.join(out)
    if pattern.startswith('/'):
        return '^' + glob_re + '$'
    return glob_re + '$'


# Lines emitted by ``xzgrep -n`` look like ``<lineno>:<content>``.
# We only ever need to peek at ``<content>`` so we slice the prefix
# off in bulk rather than running a regex per line.
_FN_LINE_BYTES_RE = re.compile(rb'<files fn="([^"]+)"')


def _iter_matches_in_lzma(
    path: Path,
    matcher: Callable[[bytes], bool],
    grep_re: str,
    media_name: str,
    matches: List["FileMatch"],
    limit: int,
) -> bool:
    """Stream-scan ``path`` and append matching :class:`FileMatch`.

    Strategy: shell out to ``xzgrep -nE`` with a single combined
    regex matching both the package boundaries (``<files fn="...">``)
    and the user pattern.  ``xzgrep`` runs ``xzcat | grep`` in a
    pipeline so decompression and matching happen in parallel C
    processes; on Mageia Core Release this returns the candidate
    lines in ~0.5 s vs the ~1.5-2 s a pure-Python read of the same
    decompressed content takes.  The combined regex is the trick:
    one xz pass yields both context (which package?) and content
    (which path matched?), so we never have to scan the file twice.

    Returns ``True`` when ``limit`` is reached so the caller can
    stop iterating remaining media early.  Falls back to an
    in-process ``lzma`` scan when ``xzgrep`` is missing.
    """
    import subprocess

    combined = '<files fn=|' + grep_re

    try:
        proc = subprocess.Popen(
            ['xzgrep', '-aE', combined, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=-1,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("xzgrep unavailable (%s); falling back to lzma module", exc)
        return _iter_matches_in_lzma_pure(
            path, matcher, media_name, matches, limit,
        )

    out, err = proc.communicate()
    if proc.returncode not in (0, 1):
        # 0 = matches, 1 = no matches; anything else is an error.
        logger.warning(
            "xzgrep failed on %s (rc=%d): %s",
            path, proc.returncode, err.decode('utf-8', 'replace').strip(),
        )
        return False

    current_nevra: Optional[str] = None
    for line in out.split(b'\n'):
        if not line:
            continue

        # Package boundary?  Update the cursor and move on.
        m = _FN_LINE_BYTES_RE.search(line)
        if m is not None:
            current_nevra = m.group(1).decode('utf-8', 'replace')
            continue

        # Re-validate via the precise Python matcher: the grep regex
        # is intentionally a superset of the historical fnmatch
        # semantics, so a few false positives may slip through.
        if current_nevra is None or not matcher(line):
            continue
        try:
            filepath = line.decode('utf-8')
        except UnicodeDecodeError:
            continue
        matches.append(FileMatch(current_nevra, filepath, media_name))
        if limit and len(matches) >= limit:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            return True
    return False


def _iter_matches_in_lzma_pure(
    path: Path,
    matcher: Callable[[bytes], bool],
    media_name: str,
    matches: List["FileMatch"],
    limit: int,
) -> bool:
    """Pure-Python fallback used when ``xzgrep`` isn't on PATH.

    Slower than the xzgrep pipeline (~1.5 s vs 0.5 s on Core
    Release) because the LZMA decompression and the Python loop
    can't truly run in parallel, but functionally equivalent.
    """
    try:
        with lzma.open(path, 'rb') as fh:
            blob = fh.read()
    except (lzma.LZMAError, OSError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return False

    current_nevra: Optional[str] = None
    for line in blob.split(b'\n'):
        if not line:
            continue
        if line[:1] == b'<':
            m = _FN_LINE_BYTES_RE.search(line)
            if m is not None:
                current_nevra = m.group(1).decode('utf-8', 'replace')
            continue
        if current_nevra is None or not matcher(line):
            continue
        try:
            filepath = line.decode('utf-8')
        except UnicodeDecodeError:
            continue
        matches.append(FileMatch(current_nevra, filepath, media_name))
        if limit and len(matches) >= limit:
            return True
    return False


def _split_nevra(nevra: str) -> Tuple[str, str, str]:
    """Split ``name-version-release.arch`` (with optional ``epoch:``).

    Returns ``(name, evr, arch)``.  ``evr`` is reassembled as
    ``[epoch:]version-release`` so it can be compared with
    :func:`rpm.labelCompare`.
    """
    name_evr, _, arch = nevra.rpartition('.')
    name_v, _, release = name_evr.rpartition('-')
    name, _, version = name_v.rpartition('-')
    return name, f'{version}-{release}', arch


def _evr_compare(a: str, b: str) -> int:
    """Compare two EVR strings using rpm semantics.

    Returns ``-1`` if ``a < b``, ``0`` if equal, ``1`` if ``a > b``.
    Falls back to plain string comparison when the ``rpm`` Python
    bindings are not available (very unusual on a Mageia host).
    """
    try:
        import rpm
    except ImportError:
        return (a > b) - (a < b)

    def _split(evr: str) -> Tuple[str, str, str]:
        if ':' in evr:
            epoch, _, rest = evr.partition(':')
        else:
            epoch, rest = '0', evr
        version, _, release = rest.partition('-')
        return epoch, version, release

    return rpm.labelCompare(_split(a), _split(b))


def iter_file_matches(
    media_files: Iterable[Tuple[Path, str]],
    pattern: str,
    *,
    all_versions: bool = False,
    limit: int = 0,
) -> List[FileMatch]:
    """Scan ``files.xml.lzma`` of given media for paths matching ``pattern``.

    Args:
        media_files: iterable of ``(files_xml_path, media_name)`` tuples,
            iterated in order.  Missing or empty files are silently
            skipped.
        pattern: user pattern; see :func:`_compile_pattern`.
        all_versions: when ``False`` (default), the result is deduped on
            ``(name, arch)`` keeping only matches whose package has the
            highest EVR seen across all media — i.e. what ``urpm
            install`` would actually pick.  When ``True``, every match
            is returned, including older versions and any duplicate
            across media.
        limit: stop after this many matches (``0`` means unlimited).
            Applied **before** dedup to keep the scan bounded; in dedup
            mode the final list may be shorter than ``limit``.

    Returns:
        List of :class:`FileMatch` in scan order.

    Notes:
        Concurrent providers (different package names providing the
        same file, e.g. ``postfix`` vs ``sendmail`` both shipping
        ``/usr/sbin/sendmail``) are kept separate by the
        ``(name, arch)`` dedup key, so they remain visible.
    """
    matcher = _compile_byte_matcher(pattern)
    grep_re = _grep_pattern_for(pattern)
    matches: List[FileMatch] = []

    for path, media_name in media_files:
        if not path.exists():
            logger.debug("Skipping %s: not on disk", path)
            continue
        if _iter_matches_in_lzma(path, matcher, grep_re, media_name, matches, limit):
            break  # ``limit`` reached — short-circuit remaining media.

    if all_versions:
        return matches

    # Dedup by (name, arch) on highest EVR.  Two passes:
    #   1. find the winning EVR for each (name, arch);
    #   2. keep only matches whose nevra has that EVR.
    best_evr: dict[Tuple[str, str], str] = {}
    for m in matches:
        name, evr, arch = _split_nevra(m.nevra)
        key = (name, arch)
        if key not in best_evr or _evr_compare(evr, best_evr[key]) > 0:
            best_evr[key] = evr

    deduped: List[FileMatch] = []
    for m in matches:
        name, evr, arch = _split_nevra(m.nevra)
        if best_evr.get((name, arch)) == evr:
            deduped.append(m)
    return deduped


# ─── Write API (used by urpm.genmedia) ────────────────────────────


def _xml_escape(text: str) -> str:
    """Escape XML special characters in text content and attributes."""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace("'", '&apos;')
            .replace('"', '&quot;'))


def write_files_xml(
    output_path: Path,
    packages,
    *,
    compression_filter: str = 'xz -7',
) -> int:
    """Write a files.xml.lzma file from RPM metadata.

    Output format::

        <media_info>
          <files fn="package-1.0-1.mga10.x86_64.rpm">
            /usr/bin/foo
            /usr/lib/libfoo.so
          </files>
          ...
        </media_info>

    Compressed according to *compression_filter*.

    Args:
        output_path: Destination file
            (e.g. ``media_info/tmp/files.xml.lzma``).
        packages: Iterable of :class:`~urpm.genmedia.RpmMetadata`.
        compression_filter: Compressor and level, e.g. ``"xz -7"``.

    Returns:
        Number of packages written.
    """
    from .compression import compress_open, parse_compress_filter
    compressor, level = parse_compress_filter(compression_filter)
    count = 0
    with compress_open(output_path, compressor, level) as f:
        f.write('<media_info>\n')
        for pkg in packages:
            f.write(f'<files fn="{_xml_escape(pkg.filename)}">')
            for filepath in pkg.files:
                f.write(_xml_escape(filepath) + '\n')
            f.write('</files>\n')
            count += 1
        f.write('</media_info>\n')
    return count


def write_info_xml(
    output_path: Path,
    packages,
    *,
    compression_filter: str = 'xz -7',
) -> int:
    """Write an info.xml.lzma file from RPM metadata.

    Output format::

        <media_info>
          <info fn='package.rpm' sourcerpm='...' url='...' license='...'>
            Description text
          </info>
          ...
        </media_info>

    Compressed according to *compression_filter*.

    Args:
        output_path: Destination file.
        packages: Iterable of :class:`~urpm.genmedia.RpmMetadata`.
        compression_filter: Compressor and level.

    Returns:
        Number of packages written.
    """
    from .compression import compress_open, parse_compress_filter
    compressor, level = parse_compress_filter(compression_filter)
    count = 0
    with compress_open(output_path, compressor, level) as f:
        f.write('<media_info>\n')
        for pkg in packages:
            f.write(
                f"<info fn='{_xml_escape(pkg.filename)}'"
                f" sourcerpm='{_xml_escape(pkg.sourcerpm)}'"
                f" url='{_xml_escape(pkg.url)}'"
                f" license='{_xml_escape(pkg.license)}'>"
            )
            f.write(_xml_escape(pkg.description))
            f.write('</info>\n')
            count += 1
        f.write('</media_info>\n')
    return count


def write_changelog_xml(
    output_path: Path,
    packages,
    *,
    compression_filter: str = 'xz -7',
) -> int:
    """Write a changelog.xml.lzma file from RPM metadata.

    Output format::

        <media_info>
          <changelogs fn='package.rpm'>
            <log time='1234567890'>
              <log_name>Author Name</log_name>
              <log_text>Change description</log_text>
            </log>
          </changelogs>
          ...
        </media_info>

    Compressed according to *compression_filter*.

    Args:
        output_path: Destination file.
        packages: Iterable of :class:`~urpm.genmedia.RpmMetadata`.
        compression_filter: Compressor and level.

    Returns:
        Number of packages written.
    """
    from .compression import compress_open, parse_compress_filter
    compressor, level = parse_compress_filter(compression_filter)
    count = 0
    with compress_open(output_path, compressor, level) as f:
        f.write('<media_info>\n')
        for pkg in packages:
            f.write(f"<changelogs fn='{_xml_escape(pkg.filename)}'>\n")
            for ts, author, text in pkg.changelog:
                f.write(f"<log time='{ts}'>\n")
                f.write(f'<log_name>{_xml_escape(author)}</log_name>\n')
                f.write(f'<log_text>{_xml_escape(text)}</log_text>\n')
                f.write('</log>\n')
            f.write('</changelogs>\n')
            count += 1
        f.write('</media_info>\n')
    return count
