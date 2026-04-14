"""
Compression write utilities for media generation.

Re-exports from :mod:`urpm.core.compression` and adds the genhdlist3-style
filter string parser with extension extraction.
"""

from urpm.core.compression import (  # noqa: F401
    compress_open,
    parse_compress_filter,
)


def parse_filter(filter_str: str):
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

    compressor, level = parse_compress_filter(cmd)
    return ext, compressor, level
