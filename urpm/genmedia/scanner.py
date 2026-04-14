"""
RPM directory scanner — extracts metadata from all .rpm files.

Produces :class:`~urpm.genmedia.RpmMetadata` objects consumed by the
write functions in ``urpm.core``.

Reference implementation for header reading: :func:`urpm.core.rpm.read_rpm_header`.
"""

import logging
from pathlib import Path
from typing import Iterator

from . import RpmMetadata

logger = logging.getLogger(__name__)


class RpmScanner:
    """Scan a directory of RPMs and extract metadata.

    Usage::

        scanner = RpmScanner()
        for meta in scanner.scan(Path("/repo/RPMS")):
            print(meta.nevra, meta.filesize)

    Implementation notes for the contributor:

    - Use ``rpm.TransactionSet`` to open each ``.rpm`` and read its header.
    - Call ``hdr.unload()`` to get the raw header bytes (for hdlist).
    - Compute ``hashlib.sha256(header_bytes).hexdigest()`` (for incremental).
    - Use ``rpm.files(hdr)`` to get the file list (cast ``fi.name`` to str).
    - Format dependencies with version constraints as ``name[op version]``
      using ``rpm.RPMSENSE_LESS / GREATER / EQUAL`` flags.
    - ``filesize`` = ``hdr[rpm.RPMTAG_LONGSIGSIZE] + 440`` (empirical).
    - See :func:`urpm.core.rpm.read_rpm_header` for a working example.
    """

    def __init__(self, *, no_bad_rpm: bool = False, verbose: bool = False):
        """
        Args:
            no_bad_rpm: If True, skip unreadable RPMs instead of raising.
            verbose: If True, log each RPM as it is scanned.
        """
        self.no_bad_rpm = no_bad_rpm
        self.verbose = verbose

    def scan(self, rpms_dir: Path) -> Iterator[RpmMetadata]:
        """Yield :class:`RpmMetadata` for each ``.rpm`` in *rpms_dir*.

        RPMs are yielded in sorted filename order for reproducibility.

        Raises:
            FileNotFoundError: If *rpms_dir* does not exist.
            RuntimeError: If a bad RPM is encountered and *no_bad_rpm* is False.
        """
        raise NotImplementedError(
            "RpmScanner.scan() is a stub — implementation needed. "
            "See docstring and urpm.core.rpm.read_rpm_header() for reference."
        )
