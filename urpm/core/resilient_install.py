"""Resilient RPM install pipeline.

Handles signature pre-verification, retry from alternate mirrors,
exclusion of bad packages and their dependents, and execution of
reduced transactions when some packages cannot be installed.

The pipeline flow is:
  1. Pre-verify GPG signatures on all downloaded RPMs
  2. For failures: purge corrupt files, retry from alternate mirrors
  3. For persistent failures: find all transitive dependents
  4. Execute a reduced transaction excluding bad packages + dependents

This module is core logic — no i18n, no UI.  Translation happens
in the CLI / GUI layers that call these functions.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set, Dict, Tuple, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .operations import PackageOperations, InstallOptions
    from .database import PackageDatabase
    from .download import DownloadItem, DownloadResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResilientInstallResult:
    """Result of a resilient install pipeline.

    Attributes:
        success: True if at least some packages were installed successfully.
        installed_count: Number of packages actually installed.
        excluded_packages: List of (package_name, reason) tuples for
            packages that were dropped from the transaction.
        errors: Free-form error messages for logging / display.
        reduced_transaction: True if some packages were excluded and the
            transaction was smaller than originally requested.
    """

    success: bool
    installed_count: int = 0
    excluded_packages: List[Tuple[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    reduced_transaction: bool = False
    queue_result: Optional[object] = None


# ---------------------------------------------------------------------------
# Signature pre-verification
# ---------------------------------------------------------------------------

def pre_verify_signatures(
    rpm_paths: List[Path],
    root: str = "/",
) -> Tuple[List[Path], List[Tuple[Path, str]]]:
    """Pre-verify GPG signatures on all RPM files before transaction.

    This catches corrupt or tampered RPMs early, before the install
    transaction starts, allowing retry from alternate mirrors and
    graceful exclusion of unrecoverable packages.

    Args:
        rpm_paths: List of RPM file paths to verify.
        root: RPM root directory (default ``/``).

    Returns:
        A ``(valid_paths, failed_list)`` tuple where *failed_list*
        contains ``(path, error_message)`` pairs for every RPM that
        did not pass verification.
    """
    if not rpm_paths:
        return [], []

    import rpm

    valid: List[Path] = []
    failed: List[Tuple[Path, str]] = []

    ts = rpm.TransactionSet(root)
    ts.setVSFlags(0)  # Enable all signature / digest checks

    for path in rpm_paths:
        try:
            with open(path, "rb") as fd:
                ts.hdrFromFdno(fd.fileno())
            valid.append(path)
        except rpm.error as exc:
            err_msg = str(exc)
            logger.warning("Signature check failed for %s: %s", path.name, err_msg)
            failed.append((path, err_msg))
        except (IOError, OSError) as exc:
            err_msg = str(exc)
            logger.warning("Cannot read RPM %s: %s", path.name, err_msg)
            failed.append((path, err_msg))

    if failed:
        logger.info(
            "Signature pre-verification: %d valid, %d failed",
            len(valid),
            len(failed),
        )

    return valid, failed


# ---------------------------------------------------------------------------
# Cache purge
# ---------------------------------------------------------------------------

def purge_failed_from_cache(
    failed_paths: List[Path],
    db: "PackageDatabase",
) -> None:
    """Delete corrupt RPM files from the local cache.

    Removes the files from disk so they are not reused by subsequent
    operations.  Database cache-record cleanup is attempted but not
    fatal if the record does not exist (the cache table is advisory).

    Args:
        failed_paths: RPM file paths to purge.
        db: Database instance for optional record removal.
    """
    for path in failed_paths:
        # ── Delete the file from disk ──
        try:
            if path.exists():
                path.unlink()
                logger.info("Purged corrupt RPM from cache: %s", path.name)
        except OSError as exc:
            logger.warning("Failed to delete %s: %s", path, exc)

        # ── Best-effort DB cleanup ──
        # The cache_files table stores (filename, media_id, file_path).
        # We attempt a direct DELETE; if the method is not available yet
        # or the record is missing, we silently continue.
        try:
            conn = db._get_connection()
            conn.execute(
                "DELETE FROM cache_files WHERE file_path = ?",
                (str(path),),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "No cache record to remove for %s: %s", path.name, exc
            )


# ---------------------------------------------------------------------------
# Transitive dependent discovery
# ---------------------------------------------------------------------------

def find_dependents(
    failed_names: Set[str],
    rpm_paths: List[Path],
    root: str = "/",
) -> Set[str]:
    """Find all packages that transitively depend on failed packages.

    Builds a capability-based dependency graph from the RPM headers in
    *rpm_paths*, then walks reverse dependencies (BFS) from
    *failed_names* to collect every package that cannot be installed
    without one of the failed ones.

    Args:
        failed_names: Package names that failed verification.
        rpm_paths: All RPM file paths in the transaction (including
            the good ones — needed to build the graph).
        root: RPM root directory.

    Returns:
        Set of package names to exclude.  Always includes *failed_names*
        themselves (intersected with what is actually present in
        *rpm_paths*).
    """
    if not failed_names or not rpm_paths:
        return set(failed_names)

    import rpm

    # ── Read headers and build maps ──
    # pkg_requires:  name → set of required capability names
    # cap_providers: capability → set of package names that provide it
    pkg_requires: Dict[str, Set[str]] = {}
    cap_providers: Dict[str, Set[str]] = {}

    ts = rpm.TransactionSet(root)
    # We only need headers; skip expensive signature checks.
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)

    for path in rpm_paths:
        try:
            with open(path, "rb") as fd:
                hdr = ts.hdrFromFdno(fd.fileno())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cannot read header from %s: %s", path.name, exc)
            continue

        name = hdr[rpm.RPMTAG_NAME]

        # Collect meaningful requires (skip rpmlib/config internals)
        requires: Set[str] = set()
        for req in hdr[rpm.RPMTAG_REQUIRENAME] or []:
            if not req.startswith(("rpmlib(", "config(")):
                requires.add(req)
        pkg_requires[name] = requires

        # Collect provides (the package name is an implicit provide)
        provides: Set[str] = {name}
        for prov in hdr[rpm.RPMTAG_PROVIDENAME] or []:
            provides.add(prov)

        for cap in provides:
            cap_providers.setdefault(cap, set()).add(name)

    # ── Build reverse dependency graph ──
    # reverse_deps: provider_name → set of names that need it
    reverse_deps: Dict[str, Set[str]] = {}
    for name, requires in pkg_requires.items():
        for req_cap in requires:
            for provider in cap_providers.get(req_cap, set()):
                if provider != name:
                    reverse_deps.setdefault(provider, set()).add(name)

    # ── BFS from failed packages through reverse edges ──
    excluded: Set[str] = set()
    queue = list(failed_names & set(pkg_requires.keys()))

    while queue:
        pkg = queue.pop(0)
        if pkg in excluded:
            continue
        excluded.add(pkg)
        for dependent in reverse_deps.get(pkg, set()):
            if dependent not in excluded:
                queue.append(dependent)

    return excluded


# ---------------------------------------------------------------------------
# Retry from alternate mirrors
# ---------------------------------------------------------------------------

def retry_failed_downloads(
    failed_paths: List[Path],
    download_items: List["DownloadItem"],
    ops: "PackageOperations",
    options: "InstallOptions",
    source_servers: Optional[Dict[str, int]] = None,
    urpm_root: Optional[str] = None,
) -> Tuple[List[Path], List[Tuple[str, str]]]:
    """Re-download failed RPMs from alternate mirrors.

    For each corrupt file, tries to find an alternate server and
    re-download.  The re-downloaded file is signature-verified before
    being accepted.

    Args:
        failed_paths: Corrupt RPM file paths.
        download_items: Original :class:`DownloadItem` list (used to
            find the matching items for retry).
        ops: :class:`PackageOperations` instance with download ability.
        options: Install options controlling download behaviour.
        source_servers: Optional map of ``package_name → server_id``
            identifying the server that served each bad file, so it
            can be deprioritised on retry.
        urpm_root: urpm state directory path.

    Returns:
        A ``(recovered_paths, still_failed)`` tuple.  *still_failed*
        contains ``(package_name, error_message)`` pairs.
    """
    if not failed_paths:
        return [], []

    # Map failed filenames → download items for retry
    failed_filenames = {p.name for p in failed_paths}
    failed_pkg_names = {_extract_name_from_path(p) for p in failed_paths}

    retry_items: List["DownloadItem"] = []
    for item in download_items:
        if item.filename in failed_filenames or item.name in failed_pkg_names:
            retry_items.append(item)

    if not retry_items:
        return [], [
            (_extract_name_from_path(p), "no download item found for retry")
            for p in failed_paths
        ]

    logger.info(
        "Retrying %d failed download(s) from alternate mirrors",
        len(retry_items),
    )

    # If we know which server served the bad file, filter it out of
    # each item's pre-loaded server list so the downloader picks another.
    if source_servers:
        for item in retry_items:
            bad_id = source_servers.get(item.name)
            if bad_id is not None and item.servers:
                item.servers = [
                    s for s in item.servers if s.get("id") != bad_id
                ]

    try:
        dl_results, _downloaded, _cached, _peer_stats = ops.download_packages(
            retry_items,
            options=options,
            urpm_root=urpm_root,
        )
    except Exception as exc:
        logger.error("Retry download failed: %s", exc)
        return [], [(item.name, str(exc)) for item in retry_items]

    # ── Verify retried downloads ──
    recovered: List[Path] = []
    still_failed: List[Tuple[str, str]] = []

    for result in dl_results:
        if result.success and result.path:
            valid, bad = pre_verify_signatures([result.path])
            if valid:
                recovered.append(result.path)
            else:
                reason = bad[0][1] if bad else "signature check failed"
                still_failed.append((result.item.name, reason))
                # Purge the re-downloaded bad file
                try:
                    result.path.unlink()
                except OSError:
                    pass
        else:
            still_failed.append((
                result.item.name,
                result.error or "download failed on retry",
            ))

    logger.info(
        "Retry results: %d recovered, %d still failed",
        len(recovered),
        len(still_failed),
    )

    return recovered, still_failed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_name_from_path(path: Path) -> str:
    """Extract the package name from an RPM filename.

    Handles the standard RPM naming convention::

        name-version-release.arch.rpm

    Examples:
        >>> _extract_name_from_path(Path("foo-1.2-3.mga10.x86_64.rpm"))
        'foo'
        >>> _extract_name_from_path(Path("python3-solv-0.7.30-1.mga10.x86_64.rpm"))
        'python3-solv'

    Args:
        path: Path to an RPM file.

    Returns:
        The package name portion of the filename.
    """
    stem = path.stem  # strip .rpm
    # Remove arch suffix (e.g. ".x86_64", ".noarch")
    dot_pos = stem.rfind(".")
    if dot_pos > 0:
        stem = stem[:dot_pos]
    # Split name-version-release on the last two hyphens
    parts = stem.rsplit("-", 2)
    if len(parts) >= 3:
        return parts[0]
    if len(parts) == 2:
        return parts[0]
    return stem
