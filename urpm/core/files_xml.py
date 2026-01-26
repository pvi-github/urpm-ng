"""
Parser for files.xml.lzma media metadata

This module provides streaming parsing of files.xml.lzma files, which
contain file lists for all packages in a repository. These files can be
very large (~9 million lines for a full repository), so we use iterparse
to avoid loading the entire file into memory.

Format example:
    <?xml version="1.0" encoding="utf-8"?>
    <media_info><files fn="package-1.0-1.mga9.x86_64">
    /usr/bin/foo
    /usr/lib64/libfoo.so
    </files><files fn="other-pkg-2.0-1.mga9.noarch">
    /etc/other.conf
    </files></media_info>
"""

import logging
import lzma
import re
from pathlib import Path
from typing import Iterator, Tuple, List, Optional, Callable, Set
from xml.etree.ElementTree import iterparse

logger = logging.getLogger(__name__)


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
