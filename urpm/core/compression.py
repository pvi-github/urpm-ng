"""
Compression utilities for urpm

Auto-detects and handles multiple compression formats:
- zstd (current Mageia format)
- gzip (legacy)
- xz/lzma (legacy)
- bzip2 (legacy)
"""

import shutil
import subprocess
from pathlib import Path
from typing import Union

# Magic bytes for format detection
MAGIC_ZSTD = b'\x28\xb5\x2f\xfd'
MAGIC_GZIP = b'\x1f\x8b'
MAGIC_XZ = b'\xfd7zXZ\x00'
MAGIC_BZ2 = b'BZ'


def _decompress_zstd_subprocess(data: bytes) -> bytes:
    """Decompress zstd data using zstdcat subprocess (fallback)."""
    result = subprocess.run(
        ['zstdcat'],
        input=data,
        capture_output=True
    )
    if result.returncode != 0:
        raise ValueError(f"zstdcat failed: {result.stderr.decode()}")
    return result.stdout


def _decompress_zstd_file_subprocess(filepath) -> bytes:
    """Decompress zstd file using zstdcat subprocess (fallback)."""
    result = subprocess.run(
        ['zstdcat', str(filepath)],
        capture_output=True
    )
    if result.returncode != 0:
        raise ValueError(f"zstdcat failed: {result.stderr.decode()}")
    return result.stdout


class _ZstdWrapper:
    """Wrapper to provide unified API for different zstd modules.

    Handles API differences between:
    - zstandard (pip): ZstdDecompressor().decompress(data), stream_reader()
    - zstd (Mageia): simpler API, may not support streaming well
    - subprocess: fallback to zstdcat command
    """

    def __init__(self):
        self._module = None
        self._api_type = None  # 'zstandard', 'zstd_simple', or 'subprocess'

        # Try 'zstandard' first (pip package, full API)
        try:
            import zstandard
            self._module = zstandard
            self._api_type = 'zstandard'
            return
        except ImportError:
            pass

        # Check if zstdcat is available (most reliable on Mageia)
        if shutil.which('zstdcat'):
            self._api_type = 'subprocess'
            return

        # Try 'zstd' (Mageia python3-zstd, but may have issues)
        try:
            import zstd
            self._module = zstd
            self._api_type = 'zstd_simple'
            return
        except ImportError:
            pass

        raise ImportError(
            "No zstd decompression available. "
            "Install zstd tools (zstdcat) or pip install zstandard"
        )

    def decompress(self, data: bytes, max_output_size: int = None) -> bytes:
        """Decompress data."""
        if self._api_type == 'zstandard':
            dctx = self._module.ZstdDecompressor()
            if max_output_size:
                return dctx.decompress(data, max_output_size=max_output_size)
            return dctx.decompress(data)
        elif self._api_type == 'subprocess':
            return _decompress_zstd_subprocess(data)
        else:
            # zstd simple API
            return self._module.decompress(data)

    def stream_decompress(self, filepath) -> bytes:
        """Decompress a file and return bytes."""
        if self._api_type == 'zstandard':
            with open(filepath, 'rb') as f:
                dctx = self._module.ZstdDecompressor()
                with dctx.stream_reader(f) as reader:
                    return reader.read()
        elif self._api_type == 'subprocess':
            return _decompress_zstd_file_subprocess(filepath)
        else:
            # zstd simple API - read file and decompress
            with open(filepath, 'rb') as f:
                return self._module.decompress(f.read())


# Lazy-loaded singleton
_zstd_wrapper = None

def _get_zstd():
    """Get the zstd wrapper (lazy singleton)."""
    global _zstd_wrapper
    if _zstd_wrapper is None:
        _zstd_wrapper = _ZstdWrapper()
    return _zstd_wrapper


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
        return _get_zstd().decompress(data, max_output_size=len(data) * 20)
    
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
            # Close file, use wrapper's stream decompress
            f.close()
            data = _get_zstd().stream_decompress(path)
            return data.decode(encoding, errors='replace')
        
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
        # Return BytesIO with decompressed data
        import io
        data = _get_zstd().stream_decompress(path)
        return io.BytesIO(data)
    
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
