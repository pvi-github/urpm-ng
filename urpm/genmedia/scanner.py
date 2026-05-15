"""
RPM directory scanner — extracts metadata from all .rpm files.

Produces :class:`~urpm.genmedia.RpmMetadata` objects consumed by the
write functions in ``urpm.core``.

Reference implementation for header reading: :func:`urpm.core.rpm.read_rpm_header`.
"""

import logging
from pathlib import Path
import hashlib
from typing import Iterator, IO

from . import RpmMetadata
import os
import rpm

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
        # TODO use absolute() ?
        rpms = [os.path.join(rpms_dir, f) for f in Path(rpms_dir).glob("*.rpm")]
        rpms.sort()

        rpms_todo = {os.path.basename(f): None for f in rpms}

        for pkg in rpms_todo:
            rpm_path = rpms_dir / pkg
            if rpm_path.exists():
                with rpm_path.open("rb") as rpm_file:
                    rpm_header = self._get_rpm_info(rpm_file)

                    # create hdlist entry
                    yield self.rpm_data(rpm_header, rpm_file)

    def _get_rpm_info(self, rpm_file):
        # Initialize the RPM transaction set
        ts = rpm.TransactionSet()

        # Open the RPM file
        # Extract the RPM package header
        hdr = ts.hdrFromFdno(rpm_file)
        return hdr

    def rpm_data(self, hdr: bytes, rpm_file: IO):
        # Get basic package information

        changelogname = hdr[rpm.RPMTAG_CHANGELOGNAME]
        changelogtext = hdr[rpm.RPMTAG_CHANGELOGTEXT]
        changelogtime = hdr[rpm.RPMTAG_CHANGELOGTIME]
        changelog = []
        for i in range(0, len(changelogname)):
            changelog.append([
                changelogtime[i],
                self._encode_xml(changelogname[i]),
                self._encode_xml(changelogtext[i])
                ])
        """List of ``(timestamp: int, author: str, text: str)`` tuples."""

        # Package dependencies (if any)
        requires = []
        if hdr.requires != []:
            requires: list[str] = self._print_list_entry(
                hdr[rpm.RPMTAG_REQUIRES],
                iter(hdr[rpm.RPMTAG_REQUIREVERSION]),
                iter(hdr[rpm.RPMTAG_REQUIREFLAGS]),
            )
        # this could be claryfied. Are recommends and suggests equivalent or used simultaneously
        suggests = []
        if hdr.recommends != []:
            suggests: list[str] = self._print_list_entry(
                hdr[rpm.RPMTAG_RECOMMENDS],
                iter(hdr[rpm.RPMTAG_RECOMMENDVERSION]),
                iter(hdr[rpm.RPMTAG_RECOMMENDFLAGS]),
            )
        conflicts = []
        if hdr.conflicts != []:
            conflicts: list[str] = self._print_list_entry(
                hdr[rpm.RPMTAG_CONFLICTS],
                iter(hdr[rpm.RPMTAG_CONFLICTVERSION]),
                iter(hdr[rpm.RPMTAG_CONFLICTFLAGS]),
                )
        obsoletes = []
        if hdr.obsoletes != []:
            obsoletes: list[str] = self._print_list_entry(
                hdr[rpm.RPMTAG_OBSOLETES],
                iter(hdr[rpm.RPMTAG_OBSOLETEVERSION]),
                iter(hdr[rpm.RPMTAG_OBSOLETEFLAGS]),
                )
        provides = []
        if hdr.provides != []:
            provides: list[str] = self._print_list_entry(
                hdr[rpm.RPMTAG_PROVIDES],
                iter(hdr[rpm.RPMTAG_PROVIDEVERSION]),
                iter(hdr[rpm.RPMTAG_PROVIDEFLAGS])
                )
        header_bytes = hdr.unload()
        package_info = RpmMetadata(
            str(rpm_file.name),
            hdr[rpm.RPMTAG_NAME],
            0 if hdr[rpm.RPMTAG_EPOCH] is None else hdr[rpm.RPMTAG_EPOCH],
            hdr[rpm.RPMTAG_VERSION],
            hdr[rpm.RPMTAG_RELEASE],
            hdr[rpm.RPMTAG_ARCH],
            hdr[rpm.RPMTAG_SUMMARY],
            hdr[rpm.RPMTAG_DESCRIPTION],
            hdr[rpm.RPMTAG_GROUP],
            self._encode_xml(hdr[rpm.RPMTAG_LICENSE]),
            self._encode_xml(hdr[rpm.RPMTAG_URL]),
            self._encode_xml(hdr[rpm.RPMTAG_SOURCERPM]),
            hdr[rpm.RPMTAG_PACKAGER],
            hdr[rpm.RPMTAG_SIZE],  # Installed size in bytes.
            hdr[rpm.RPMTAG_LONGSIGSIZE] + 440,  # Size of the .rpm file on disk.  440 is the rpm toc size
            hdr[rpm.RPMTAG_BUILDTIME],  # Build timestamp (epoch seconds)
            requires,
            provides,
            conflicts,
            obsoletes,
            suggests,
            [pkg.name for pkg in rpm.files(hdr)],  # Full paths of files owned by this RPM.  Get files in the RPM package
            changelog,
            header_bytes,
            hashlib.sha256(header_bytes).hexdigest(),  # SHA-256 hex digest of header_bytes, for incremental mode
            )

        return package_info

    def _print_list_entry(self, names: list[str], versions: list[str], flags: list[int]) -> list[str]:
        reqs = []
        for name in names:
            version = next(versions)
            flag = next(flags)
            if not name.startswith('rpmlib('):
                if version != "":
                    constraint = ""
                    if (flag & rpm.RPMSENSE_LESS):
                        constraint = '<'
                    if (flag & rpm.RPMSENSE_GREATER):
                        constraint = '>'
                    if (flag & rpm.RPMSENSE_EQUAL):
                        constraint += '='
                    if ((flag & (rpm.RPMSENSE_LESS | rpm.RPMSENSE_EQUAL | rpm.RPMSENSE_GREATER)) == rpm.RPMSENSE_EQUAL):
                        constraint = '=='
                    reqs.append(f"{name}[{constraint} {version}]")
                else:
                    reqs.append(name)
        return reqs

    def _encode_xml(self, entry: str) -> str:
        if entry is not None:
            output = entry.replace('&', '&amp;')
            output = output.replace('>', '&gt;')
            output = output.replace('<', '&lt;')
            return output
        return None
