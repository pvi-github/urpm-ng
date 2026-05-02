"""
Media metadata generator — orchestrates the full generation pipeline.

Coordinates :class:`RpmScanner` with the write functions from ``urpm.core``
to produce a complete ``media_info/`` directory.
"""

import hashlib
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import GenerateResult, RpmMetadata
from .compress import parse_filter
from .scanner import RpmScanner
from traceback import format_exc

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
        force = False,
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
            force: don't use cached metainfo for appstream

        Returns:
            A :class:`~urpm.genmedia.GenerateResult` with outcome details.
        """
        result = GenerateResult(success=False)
        lock_ctx = None

        # ── 0. Ensure output dirs exist ──────────────────────────
        self.media_info_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.media_info_dir / 'tmp'
        tmp_dir.mkdir(exist_ok=True)

        try:
            # ── 1. Acquire lock ──────────────────────────────────
            if self.lock:
                lock_ctx = self._acquire_lock()

            # ── 2. Scan RPMs ─────────────────────────────────────
            # Force C locale so RPM descriptions are untranslated.
            old_lc = os.environ.get('LC_ALL')
            os.environ['LC_ALL'] = 'C'
            try:
                scanner = RpmScanner(
                    no_bad_rpm=self.no_bad_rpm,
                    verbose=self.verbose,
                )
                packages = list(scanner.scan(self.rpms_dir))
            finally:
                if old_lc is None:
                    os.environ.pop('LC_ALL', None)
                else:
                    os.environ['LC_ALL'] = old_lc

            if not packages and not allow_empty:
                result.errors.append(
                    f"No *.rpm files found in {self.rpms_dir}. "
                    f"Use --allow-empty-media to proceed."
                )
                return result

            result.packages_count = len(packages)
            logger.info(f"Scanned {len(packages)} RPMs from {self.rpms_dir}")

            # Parse filter strings once.
            hd_ext, hd_comp, hd_level = parse_filter(hdlist_filter)
            syn_ext, syn_comp, syn_level = parse_filter(synthesis_filter)
            xml_ext, xml_comp, xml_level = parse_filter(xml_info_filter)

            # Build filenames from filter extensions.
            hdlist_filename = f'hdlist{hd_ext}'
            synthesis_filename = f'synthesis.hdlist{syn_ext}'
            generated_files = []

            # ── 3. Write hdlist ──────────────────────────────────
            if hdlist:
                from urpm.core.hdlist import write_hdlist
                hdlist_path = tmp_dir / hdlist_filename
                old_hdlist = self.media_info_dir / hdlist_filename
                write_hdlist(
                    hdlist_path, packages,
                    compression_filter=hd_comp,
                    compression_level=hd_level,
                    incremental=incremental,
                    old_hdlist_path=old_hdlist if incremental and old_hdlist.exists() else None,
                )
                result.hdlist_written = True
                generated_files.append(hdlist_filename)

            # ── 4. Write synthesis ───────────────────────────────
            if synthesis:
                from urpm.core.synthesis import write_synthesis
                syn_path = tmp_dir / synthesis_filename
                write_synthesis(
                    syn_path, packages,
                    compression_filter=f'{syn_comp} -{syn_level}',
                )
                result.synthesis_written = True
                generated_files.append(synthesis_filename)

            # ── 5. Write XML info ────────────────────────────────
            if xml_info:
                from urpm.core.files_xml import (
                    write_files_xml, write_info_xml, write_changelog_xml,
                )
                for writer, prefix in [
                    (write_files_xml, 'files'),
                    (write_info_xml, 'info'),
                    (write_changelog_xml, 'changelog'),
                ]:
                    xml_filename = f'{prefix}.xml{xml_ext}'
                    xml_path = tmp_dir / xml_filename
                    writer(
                        xml_path, packages,
                        compression_filter=f'{xml_comp} -{xml_level}',
                    )
                    generated_files.append(xml_filename)
                result.xml_info_written = True

            # ── 6. Write AppStream ───────────────────────────────
            if appstream:
                # AppStream generation requires the AppStreamManager
                # and a cache directory for per-RPM metainfo.
                cache_dir = self.rpms_dir / '.genhdlist'
                cache_dir.mkdir(exist_ok=True)
                from urpm.core.appstream import AppStreamManager
                if force:
                    print("⚡ Force mode: all packages will be re-extracted.\n")

                results   = {}   # packages processed in this execution
                skipped   = []   # packages skipped (already up to date)
                generated = []   # packages for which XML was generated (no embedded)
                errors    = []   # packages with errors

                # AppStreamManager needs a db instance, but for generation
                # from RPM dir we use extract_from_rpm + build_catalog
                # which don't need the database.
                appstream_mgr = AppStreamManager.__new__(AppStreamManager)
                # Loading persistent state
                state = appstream_mgr._load_state()
                for pkg in packages:
                    pkg_result = appstream_mgr.extract_from_rpm(pkg, cache_dir, force=force)
                    # ── Mise à jour de l'état ────────────────────────────────
                    rpm_name = os.path.basename(pkg.filename)
                    entry = state.get(rpm_name, {})
                    entry.update({
                        "sha256":       pkg_result["sha256"],
                        "extracted":    pkg_result["extracted"],
                        "generated":    pkg_result["generated"],
                        "processed_at": appstream_mgr._now_iso(),
                    })
                    state[rpm_name] = entry
                    appstream_mgr._save_state(state)
                as_filename = f'appstream.xml{xml_ext}'
                as_path = tmp_dir / as_filename
                appstream_mgr.build_catalog(
                    cache_dir, as_path,
                    compression_filter=f'{xml_comp} -{xml_level}',
                )
                result.appstream_written = True
                generated_files.append(as_filename)

            # ── 7. Move tmp → final (atomic per file) ────────────
            version_prefix = ''
            if versioned:
                version_prefix = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-'

            for filename in generated_files:
                src = tmp_dir / filename
                if not src.exists():
                    continue
                dst_name = f'{version_prefix}{filename}' if versioned else filename
                dst = self.media_info_dir / dst_name
                src.replace(dst)
                if self.verbose:
                    logger.info(f"  {dst_name}")

            # Clean up tmp dir.
            try:
                tmp_dir.rmdir()
            except OSError:
                pass

            # ── 8. Generate MD5SUM ───────────────────────────────
            if md5sum and generated_files:
                final_files = [
                    self.media_info_dir / (
                        f'{version_prefix}{f}' if versioned else f
                    )
                    for f in generated_files
                ]
                self._generate_md5sum(final_files)
                result.md5sum_written = True

            result.success = True

        except NotImplementedError:
            # Re-raise stubs so tests can xfail properly.
            raise
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            print(format_exc())
            result.errors.append(str(e))
        finally:
            if lock_ctx is not None:
                self._release_lock(lock_ctx)
            # Clean up tmp on failure.
            if tmp_dir.exists():
                try:
                    shutil.rmtree(tmp_dir)
                except OSError:
                    pass

        return result

    def _acquire_lock(self):
        """Acquire a file lock on media_info/UPDATING.

        Returns:
            The open file object (must be passed to _release_lock).
        """
        import fcntl
        lock_path = self.media_info_dir / 'UPDATING'
        logger.debug(f"Acquiring lock on {lock_path}")
        f = open(lock_path, 'w')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f

    def _release_lock(self, lock_file):
        """Release the file lock."""
        import fcntl
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
            lock_path = self.media_info_dir / 'UPDATING'
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

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
