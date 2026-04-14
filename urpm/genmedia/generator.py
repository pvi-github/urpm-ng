"""
Media metadata generator — orchestrates the full generation pipeline.

Coordinates :class:`RpmScanner` with the write functions from ``urpm.core``
to produce a complete ``media_info/`` directory.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from . import GenerateResult, RpmMetadata
from .compress import parse_filter
from .scanner import RpmScanner

logger = logging.getLogger(__name__)


class MediaGenerator:
    """Orchestrate generation of media metadata files.

    Usage::

        gen = MediaGenerator(rpms_dir=Path("/repo/RPMS"))
        result = gen.generate(hdlist=True, synthesis=True, xml_info=True)
        if result.success:
            print(f"{result.packages_count} packages indexed")

    The generation flow is:

    1. Acquire lock on ``media_info/`` (unless *lock=False*).
    2. Scan RPMs via :class:`~urpm.genmedia.scanner.RpmScanner`.
    3. Write ``hdlist.cz`` via :func:`urpm.core.hdlist.write_hdlist`.
    4. Write ``synthesis.hdlist.cz`` via :func:`urpm.core.synthesis.write_synthesis`.
    5. Write XML info files via ``urpm.core.files_xml.write_*``.
    6. Write AppStream catalog (if requested).
    7. Generate ``MD5SUM``.
    8. Atomically move temp files to final location.
    9. Release lock.
    """

    def __init__(
        self,
        rpms_dir: Path,
        media_info_dir: Optional[Path] = None,
        *,
        lock: bool = True,
        verbose: bool = False,
        no_bad_rpm: bool = False,
    ):
        """
        Args:
            rpms_dir: Directory containing ``.rpm`` files to index.
            media_info_dir: Output directory for metadata files.
                Defaults to ``rpms_dir/media_info``.
            lock: Acquire a file lock on media_info_dir during generation.
            verbose: Enable verbose logging.
            no_bad_rpm: Skip unreadable RPMs instead of aborting.
        """
        self.rpms_dir = Path(rpms_dir)
        self.media_info_dir = Path(media_info_dir) if media_info_dir else self.rpms_dir / 'media_info'
        self.lock = lock
        self.verbose = verbose
        self.no_bad_rpm = no_bad_rpm

    def generate(
        self,
        *,
        hdlist: bool = True,
        synthesis: bool = True,
        xml_info: bool = False,
        appstream: bool = False,
        md5sum: bool = True,
        incremental: bool = False,
        hdlist_filter: str = '.cz:gzip -9',
        synthesis_filter: str = '.cz:xz -7',
        xml_info_filter: str = '.lzma:xz -7',
        versioned: bool = False,
        allow_empty: bool = False,
    ) -> GenerateResult:
        """Generate media metadata files.

        All output is first written to a temporary directory inside
        ``media_info/``, then atomically moved into place.

        Args:
            hdlist: Generate ``hdlist.cz``.
            synthesis: Generate ``synthesis.hdlist.cz``.
            xml_info: Generate ``files.xml``, ``info.xml``, ``changelog.xml``.
            appstream: Generate ``appstream.xml.lzma``.
            md5sum: Generate ``MD5SUM`` checksums.
            incremental: Reuse unchanged blocks from existing hdlist
                (compares by SHA-256 of RPM headers).
            hdlist_filter: Compression filter for hdlist
                (genhdlist3 format: ``".ext:command -level"``).
            synthesis_filter: Compression filter for synthesis.
            xml_info_filter: Compression filter for XML info files.
            versioned: Prefix output filenames with a timestamp.
            allow_empty: Allow generation with zero RPMs.

        Returns:
            A :class:`~urpm.genmedia.GenerateResult` with outcome details.
        """
        raise NotImplementedError(
            "MediaGenerator.generate() is a stub — implementation needed. "
            "See docstring for the expected flow."
        )

    def _generate_md5sum(self, files: list[Path]) -> Path:
        """Compute MD5 checksums for the given files.

        Writes ``MD5SUM`` in the standard ``md5sum(1)`` format::

            d41d8cd9...  filename

        Args:
            files: List of paths (must be inside media_info_dir).

        Returns:
            Path to the written MD5SUM file.
        """
        md5sum_path = self.media_info_dir / 'MD5SUM'
        with open(md5sum_path, 'w') as f:
            for filepath in files:
                if not filepath.exists():
                    continue
                md5 = hashlib.md5()
                with open(filepath, 'rb') as h:
                    while chunk := h.read(8192):
                        md5.update(chunk)
                name = filepath.name
                f.write(f"{md5.hexdigest()}  {name}\n")
        return md5sum_path
