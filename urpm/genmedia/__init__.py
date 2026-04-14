"""
urpm.genmedia — Media metadata generation for Mageia repositories.

Generates hdlist.cz, synthesis.hdlist.cz, XML info files (files.xml,
info.xml, changelog.xml), AppStream catalogs, and MD5SUM from a directory
of RPM packages.

This module is packaged separately (urpm-ng-genmedia) and imported on demand.
It depends on urpm.core for format read/write and python3-rpm for headers.

Typical usage::

    from urpm.genmedia import MediaGenerator

    gen = MediaGenerator(rpms_dir=Path("/path/to/RPMS"))
    result = gen.generate(hdlist=True, synthesis=True)
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RpmMetadata:
    """All metadata extracted from a single RPM, consumed by all writers.

    Produced by :class:`RpmScanner`, consumed by write functions in
    :mod:`urpm.core.hdlist`, :mod:`urpm.core.synthesis`,
    :mod:`urpm.core.files_xml`, and :mod:`urpm.core.appstream`.
    """

    filename: str
    """RPM filename on disk, e.g. ``foo-1.0-1.mga10.x86_64.rpm``."""

    name: str
    epoch: int
    version: str
    release: str
    arch: str

    summary: str
    description: str
    group: str
    license: str
    url: str
    sourcerpm: str
    packager: str

    size: int
    """Installed size in bytes."""

    filesize: int
    """Size of the .rpm file on disk."""

    buildtime: int
    """Build timestamp (epoch seconds)."""

    requires: list[str] = field(default_factory=list)
    """Dependency list with constraints, e.g. ``["foo[>= 1.0]", "bar"]``."""

    provides: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    obsoletes: list[str] = field(default_factory=list)
    suggests: list[str] = field(default_factory=list)

    files: list[str] = field(default_factory=list)
    """Full paths of files owned by this RPM."""

    changelog: list[tuple] = field(default_factory=list)
    """List of ``(timestamp: int, author: str, text: str)`` tuples."""

    header_bytes: bytes = field(default=b'', repr=False)
    """Raw ``hdr.unload()`` bytes for hdlist generation."""

    header_sha256: str = ''
    """SHA-256 hex digest of header_bytes, for incremental mode."""

    @property
    def nevra(self) -> str:
        """Full NEVRA string, e.g. ``foo-1.0-1.mga10.x86_64``."""
        return f"{self.name}-{self.version}-{self.release}.{self.arch}"

    @property
    def nvra(self) -> str:
        """NVRA without epoch, used in synthesis @info lines."""
        return self.nevra


@dataclass
class GenerateResult:
    """Result of a :meth:`MediaGenerator.generate` run."""

    success: bool
    packages_count: int = 0
    hdlist_written: bool = False
    synthesis_written: bool = False
    xml_info_written: bool = False
    appstream_written: bool = False
    md5sum_written: bool = False
    errors: list[str] = field(default_factory=list)


# Public API — lazy imports to avoid pulling dependencies at import time.

def MediaGenerator(*args, **kwargs):
    """Create a :class:`~urpm.genmedia.generator.MediaGenerator` instance.

    This is a lazy wrapper to avoid importing heavy dependencies
    (python3-rpm) until actually needed.
    """
    from .generator import MediaGenerator as _cls
    return _cls(*args, **kwargs)


__all__ = [
    'RpmMetadata',
    'GenerateResult',
    'MediaGenerator',
]
