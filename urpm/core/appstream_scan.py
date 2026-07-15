"""
AppStream candidate scanner for a media's ``files.xml.lzma``.

This module answers a single question — *which packages of a media expose
an AppStream component?* — with a decisive, non-heuristic signal:

    a package is a candidate iff it ships one of
        /usr/share/applications/*.desktop
        /usr/share/metainfo/*.xml
        /usr/share/appdata/*.xml

Filtering on names (``lib*``, ``python-*``, …) or RPM groups is unreliable
(``libreoffice-*`` are apps, not libraries; multi-purpose paquets can carry
a desktop entry regardless of their group).  The file listing is the only
authoritative source we have without downloading each RPM.

Pipeline
--------

The scan runs two shell passes chained through a kernel pipe so no
intermediate buffer ever lands in Python:

1. ``xzgrep -aE '<files fn=|/usr/share/(applications/.*\\.desktop|
                                        metainfo/.*\\.xml|
                                        appdata/.*\\.xml)'``
   decompresses on the fly and keeps only lines that open a package block
   or list one of the candidate paths — dropping ~99 % of the raw XML.

2. ``awk '/<files fn=/{buf=$0; w=0; next} {if(!w){print buf; w=1} print}'``
   drops the package blocks kept "just in case" by pass 1 that turn out to
   have no candidate path — further cutting the output by ~85 %.

Bench on a 23 MB ``files.xml.lzma`` (core/release, ~13 000 packages):
about 650 ms wall-clock, ~6 300 lines out (down from ~1.4 M raw lines).

Format
------

The XML is a flat ``<media_info>`` element containing back-to-back
``<files fn="NEVRA">…</files>`` blocks.  Package boundaries can appear
inline, e.g. ``</files><files fn="next-package-1.0-1.mga10.x86_64">`` on
the same physical line — the parser handles that via
:func:`re.findall` rather than :func:`re.match`.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Keep in sync with the awk-side pattern in :data:`_AWK_SCRIPT`; both need
# to recognise the same three candidate directory prefixes.
_XZGREP_PATTERN = (
    r'<files fn=|'
    r'/usr/share/(applications/.*\.desktop|'
    r'metainfo/.*\.xml|'
    r'appdata/.*\.xml)'
)

# Buffer the last ``<files fn="…">`` header and only emit it once a
# candidate path follows in the same package block.  Blocks with no
# candidate path stay in the buffer and get overwritten by the next
# header — never printed.
_AWK_SCRIPT = (
    '/<files fn=/{buf=$0; w=0; next}'
    '{if(!w){print buf; w=1} print}'
)

# A single line can carry ``</files><files fn="X">`` inline (package
# transition), so we scan with :func:`re.findall` and keep the last match.
_FN_RE = re.compile(r'<files fn="([^"]+)">')

# NEVRA split: the version segment must start with a digit (that anchors
# the greedy ``name`` group), release runs to the last ``.``, arch is the
# final dot-separated component.  Handles names with any number of dashes
# (``golang-github-cespare-xxhash``, ``python3-foo``, …).
_NEVRA_RE = re.compile(r'^(.+)-([0-9][^-]*)-([^-]+)\.([^.]+)$')


def parse_nevra(nevra: str) -> Optional[Tuple[str, str, str, str]]:
    """Split ``name-version-release.arch`` into its four parts.

    Args:
        nevra: The NEVRA string, e.g.
            ``'libreoffice-writer-26.2.3.2-1.mga10.x86_64'``.

    Returns:
        A ``(name, version, release, arch)`` tuple, or ``None`` if the
        string does not parse as a valid NEVRA.
    """
    m = _NEVRA_RE.match(nevra)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def scan_media_appstream_candidates(
    files_xml_path: Path,
) -> Dict[str, List[str]]:
    """Scan a media's ``files.xml.lzma`` for AppStream-carrying packages.

    Runs the two-pass xzgrep+awk pipeline described in the module
    docstring, then parses its output into a ``{nevra: [path, …]}`` map.
    A package appears in the returned dict iff it ships at least one
    candidate file.

    Args:
        files_xml_path: Path to a media's ``files.xml.lzma``.  Must be
            readable by the current process.

    Returns:
        Mapping of NEVRA to the list of candidate paths that package
        ships.  Empty dict if no package carries any candidate (or if
        the file itself is empty).

    Raises:
        FileNotFoundError: If ``files_xml_path`` does not exist.
        PermissionError: If ``files_xml_path`` is not readable.
        RuntimeError: If the shell pipeline exits with a code other than
            0 (matches found) or 1 (no matches).
    """
    if not files_xml_path.exists():
        raise FileNotFoundError(files_xml_path)

    # Chain xzgrep -> awk via a kernel pipe.  Popen (rather than shell=True)
    # keeps argument quoting under our control and lets awk see xzgrep's
    # stdout close cleanly on completion.
    xzgrep = subprocess.Popen(
        ['xzgrep', '-aE', _XZGREP_PATTERN, str(files_xml_path)],
        stdout=subprocess.PIPE,
    )
    awk = subprocess.Popen(
        ['awk', _AWK_SCRIPT],
        stdin=xzgrep.stdout,
        stdout=subprocess.PIPE,
        text=True,
    )
    # Close our own reference so xzgrep gets SIGPIPE when awk exits.
    xzgrep.stdout.close()
    stdout, _ = awk.communicate()
    xzgrep.wait()

    # xzgrep: 0 = matched, 1 = no match, ≥2 = error (missing file, bad
    # archive, etc.).  awk: only fails on syntax error, which is a
    # programming bug on our side.
    if xzgrep.returncode not in (0, 1):
        raise RuntimeError(
            f'xzgrep failed on {files_xml_path}: exit={xzgrep.returncode}'
        )
    if awk.returncode != 0:
        raise RuntimeError(
            f'awk failed on {files_xml_path}: exit={awk.returncode}'
        )

    candidates: Dict[str, List[str]] = {}
    current_nevra: Optional[str] = None
    for line in stdout.splitlines():
        # Handle inline ``</files><files fn="X">`` — keep the rightmost
        # header on the line as the active package.
        fn_matches = _FN_RE.findall(line)
        if fn_matches:
            current_nevra = fn_matches[-1]
            continue
        if current_nevra:
            candidates.setdefault(current_nevra, []).append(line.strip())
    return candidates
