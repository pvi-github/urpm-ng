"""
Compression utilities for urpm

Auto-detects and handles multiple compression formats:
- zstd (current Mageia format)
- gzip (legacy)
- xz/lzma (legacy)
- bzip2 (legacy)
"""

from pathlib import Path
from typing import Union

# Magic bytes for format detection
MAGIC_ZSTD = b'\x28\xb5\x2f\xfd'
MAGIC_GZIP = b'\x1f\x8b'
MAGIC_XZ = b'\xfd7zXZ\x00'
MAGIC_BZ2 = b'BZ'


def detect_format(data: bytes) -> str:
    """Detect compression format from magic bytes.
    
    Args:
        data: First 8+ bytes of the file
        
    Returns:
        Format name: 'zstd', 'gzip', 'xz', 'bzip2', or 'plain'
    """
    if data[:4] == MAGIC_ZSTD:
        return 'zstd'
    elif data[:2] == MAGIC_GZIP:
        return 'gzip'
    elif data[:6] == MAGIC_XZ:
        return 'xz'
    elif data[:2] == MAGIC_BZ2:
        return 'bzip2'
    else:
        return 'plain'


def decompress_bytes(data: bytes) -> bytes:
    """Decompress bytes, auto-detecting format.
    
    Args:
        data: Compressed data
        
    Returns:
        Decompressed bytes
        
    Raises:
        ImportError: If zstandard module is not installed (for zstd files)
        ValueError: If decompression fails
    """
    fmt = detect_format(data)
    
    if fmt == 'zstd':
        try:
            import zstandard as zstd
        except ImportError:
            raise ImportError(
                "Module 'zstandard' required for zstd decompression. "
                "Install with: pip install zstandard"
            )
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data, max_output_size=len(data) * 20)
    
    elif fmt == 'gzip':
        import gzip
        return gzip.decompress(data)
    
    elif fmt == 'xz':
        import lzma
        return lzma.decompress(data)
    
    elif fmt == 'bzip2':
        import bz2
        return bz2.decompress(data)
    
    else:
        # Plain/uncompressed
        return data


def decompress(filename: Union[str, Path], encoding: str = 'utf-8') -> str:
    """Decompress a file and return as string.
    
    Args:
        filename: Path to compressed file
        encoding: Text encoding (default: utf-8)
        
    Returns:
        Decompressed content as string
    """
    path = Path(filename)
    
    with open(path, 'rb') as f:
        magic = f.read(8)
        f.seek(0)
        fmt = detect_format(magic)
        
        if fmt == 'zstd':
            try:
                import zstandard as zstd
            except ImportError:
                raise ImportError(
                    "Module 'zstandard' required for zstd decompression. "
                    "Install with: pip install zstandard"
                )
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(f) as reader:
                return reader.read().decode(encoding, errors='replace')
        
        elif fmt == 'gzip':
            import gzip
            with gzip.open(f, 'rt', encoding=encoding, errors='replace') as gz:
                return gz.read()
        
        elif fmt == 'xz':
            import lzma
            with lzma.open(f, 'rt', encoding=encoding, errors='replace') as xz:
                return xz.read()
        
        elif fmt == 'bzip2':
            import bz2
            with bz2.open(f, 'rt', encoding=encoding, errors='replace') as bz:
                return bz.read()
        
        else:
            return f.read().decode(encoding, errors='replace')


def decompress_stream(filename: Union[str, Path]):
    """Open a compressed file and return a binary stream.
    
    Args:
        filename: Path to compressed file
        
    Returns:
        File-like object for reading decompressed data
    """
    path = Path(filename)
    
    with open(path, 'rb') as f:
        magic = f.read(8)
    
    fmt = detect_format(magic)
    
    if fmt == 'zstd':
        try:
            import zstandard as zstd
        except ImportError:
            raise ImportError(
                "Module 'zstandard' required for zstd decompression. "
                "Install with: pip install zstandard"
            )
        f = open(path, 'rb')
        dctx = zstd.ZstdDecompressor()
        return dctx.stream_reader(f)
    
    elif fmt == 'gzip':
        import gzip
        return gzip.open(path, 'rb')
    
    elif fmt == 'xz':
        import lzma
        return lzma.open(path, 'rb')
    
    elif fmt == 'bzip2':
        import bz2
        return bz2.open(path, 'rb')
    
    else:
        return open(path, 'rb')
