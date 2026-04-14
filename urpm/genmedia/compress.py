"""
Compression write utilities for media generation.

Complements :mod:`urpm.core.compression` which handles decompression.
Parses filter strings in genhdlist3 format (e.g. ``".cz:gzip -9"``)
and provides a unified interface for compressing output files.
"""

import gzip
import lzma
import logging
from pathlib import Path
from typing import BinaryIO, Tuple

logger = logging.getLogger(__name__)


def parse_filter(filter_str: str) -> Tuple[str, str, int]:
    """Parse a genhdlist3-style compression filter string.

    Args:
        filter_str: Format ``".ext:command -level"``,
            e.g. ``".cz:gzip -9"`` or ``".lzma:xz -7"``.

    Returns:
        Tuple of (extension, compressor_name, level).
        Example: ``(".cz", "gzip", 9)``.

    Raises:
        ValueError: If the filter string is malformed.
    """
    if ':' not in filter_str:
        raise ValueError(
            f"Invalid filter {filter_str!r} — expected '.ext:command -level'"
        )

    ext, cmd = filter_str.split(':', 1)
    if not ext.startswith('.'):
        raise ValueError(
            f"Invalid filter extension {ext!r} — must start with '.'"
        )

    parts = cmd.strip().split()
    compressor = parts[0]
    level = 9  # default
    for part in parts[1:]:
        if part.startswith('-') and part[1:].isdigit():
            level = int(part[1:])

    return ext, compressor, level


def open_compressed(path: Path, compressor: str, level: int) -> BinaryIO:
    """Open a file for compressed binary writing.

    Args:
        path: Output file path.
        compressor: One of ``"gzip"``, ``"xz"``, ``"lzma"``.
        level: Compression level (0-9).

    Returns:
        A writable binary file-like object. Caller must close it.

    Raises:
        ValueError: If the compressor is not supported.
    """
    if compressor == 'gzip':
        return gzip.open(path, 'wb', compresslevel=level)
    elif compressor in ('xz', 'lzma'):
        preset = level
        return lzma.open(path, 'wb', preset=preset)
    else:
        raise ValueError(f"Unsupported compressor: {compressor!r}")


def compress_file(src: Path, dst: Path, compressor: str, level: int) -> int:
    """Compress a file from *src* to *dst*.

    Args:
        src: Uncompressed input file.
        dst: Compressed output file.
        compressor: One of ``"gzip"``, ``"xz"``, ``"lzma"``.
        level: Compression level (0-9).

    Returns:
        Size of the compressed file in bytes.
    """
    with open(src, 'rb') as fin, open_compressed(dst, compressor, level) as fout:
        while True:
            chunk = fin.read(65536)
            if not chunk:
                break
            fout.write(chunk)
    return dst.stat().st_size
