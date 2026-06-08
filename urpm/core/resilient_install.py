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
from collections import namedtuple
from pathlib import Path
from typing import List, Set, Dict, Tuple, Optional, TYPE_CHECKING


# Categorised result of :func:`pre_verify_signatures` failure.  The
# category steers the caller toward the right remediation path (bug
# #3 iteration B):
#
#   * ``'preflight'``  — empty file, missing magic, unreadable: a
#                        cache corruption that costs reputation but
#                        does not signal active tampering.
#   * ``'signature'``  — GPG signature verification failed.  Treated
#                        as a compromise event: the source server is
#                        blacklisted unconditionally and the user
#                        gets a visible alert.
#   * ``'structural'`` — header read failed with sig checks disabled
#                        (rpm.error after preflight succeeded —
#                        usually a digest mismatch or malformed
#                        header).  Counts toward reputation, not
#                        blacklist.
FailedRpm = namedtuple("FailedRpm", ["path", "category", "reason"])

# The resilient pipeline returns :class:`urpm.core.install.InstallResult`
# (the same dataclass as the low-level :class:`Installer`).  The extra
# fields ``excluded_packages``, ``reduced_transaction`` and
# ``queue_result`` are populated here; the bottom layer leaves them at
# their neutral defaults.  Callers import ``InstallResult`` from
# :mod:`urpm.core.install` directly.

if TYPE_CHECKING:
    from .operations import PackageOperations, InstallOptions
    from .database import PackageDatabase
    from .download import DownloadItem, DownloadResult

logger = logging.getLogger(__name__)


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
    from .download import verify_rpm_signature

    valid: List[Path] = []
    failed: List[FailedRpm] = []

    # ── Pass 0: cheap structural preflight ──
    # Empty files, files truncated below the magic, or files that
    # simply do not start with ``\xed\xab\xee\xdb`` never reach
    # rpmlib — they exit straight into the retry path with a
    # transparent ``preflight`` category.
    sig_pending: List[Path] = []
    for path in rpm_paths:
        preflight_reason = preflight_check(path)
        if preflight_reason is not None:
            logger.warning(
                "Preflight rejected %s: %s", path.name, preflight_reason,
            )
            failed.append(FailedRpm(path, "preflight", preflight_reason))
            continue
        sig_pending.append(path)

    # ── Pass 1: signature verification ──
    # Run as a dedicated call (``verify_rpm_signature`` enables all
    # VSFlags) so the failure mode is unambiguous — any error from
    # this pass is a sig/digest-from-key failure and we label it as
    # such.  This is the categorisation that drives the
    # compromise-vs-corruption decision in
    # ``operations.resilient_install``: signature failures get the
    # source server blacklisted; everything else only nudges the
    # reputation score.
    header_pending: List[Path] = []
    for path in sig_pending:
        ok, sig_err = verify_rpm_signature(path)
        if not ok:
            logger.warning(
                "Signature check failed for %s: %s", path.name, sig_err,
            )
            failed.append(FailedRpm(path, "signature", str(sig_err)))
            continue
        header_pending.append(path)

    # ── Pass 2: header read with sigs disabled ──
    # Sigs already settled (verified above), so any rpm.error here
    # is genuinely a structural problem (digest mismatch, truncated
    # body past the lead, malformed header…), never a sig issue we
    # would have to disambiguate by string-match.
    if header_pending:
        ts = rpm.TransactionSet(root)
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
        for path in header_pending:
            try:
                with open(path, "rb") as fd:
                    ts.hdrFromFdno(fd.fileno())
                valid.append(path)
            except rpm.error as exc:
                err_msg = str(exc)
                logger.warning(
                    "Header check failed for %s: %s", path.name, err_msg,
                )
                failed.append(FailedRpm(path, "structural", err_msg))
            except (IOError, OSError) as exc:
                err_msg = str(exc)
                logger.warning(
                    "Cannot read RPM %s: %s", path.name, err_msg,
                )
                failed.append(FailedRpm(path, "structural", err_msg))

    if failed:
        logger.info(
            "Signature pre-verification: %d valid, %d failed",
            len(valid),
            len(failed),
        )

    return valid, failed


# ── RPM magic bytes (file header) ─────────────────────────────────────
# https://refspecs.linuxbase.org/LSB_3.0.0/LSB-Core-generic/LSB-Core-generic/swinstall.html#FILEFORMAT
_RPM_MAGIC = b"\xed\xab\xee\xdb"


def preflight_check(path: Path) -> Optional[str]:
    """Cheap structural verification of a cache-resident RPM.

    Catches the failure modes that are obvious without invoking
    rpmlib: an empty / truncated file, a stat error, or a file whose
    first four bytes are not the RPM magic.  Per the user's spec for
    the iteration-A retry loop (bug #3), these are treated exactly
    like a signature failure — the cache file is unlinked and a
    re-download is attempted from a different mirror.

    Returns:
        ``None`` when the file passes the preflight, otherwise a
        short human-readable reason string suitable for the
        ``failed`` list of :func:`pre_verify_signatures`.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"cannot stat: {exc}"

    if size == 0:
        return "empty file"

    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        return f"cannot read header: {exc}"

    if magic != _RPM_MAGIC:
        return f"wrong magic bytes ({magic!r})"

    return None


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

        # ── DB cleanup via the public API ──
        # cache_files is advisory: a missing row is normal when the file
        # was placed in cache outside of urpm-ng's own download path
        # (e.g. a peer-served RPM that failed sig verification before
        # being registered).  ``unregister_cache_file`` returns False
        # silently in that case.
        try:
            db.unregister_cache_file(str(path))
        except Exception as exc:
            logger.warning(
                "Cache record cleanup failed for %s: %s",
                path.name, exc, exc_info=True,
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
    max_retries: Optional[int] = None,
) -> Tuple[List[Path], List[Tuple[str, str]]]:
    """Re-download failed RPMs from alternate mirrors, up to ``max_retries``.

    Each iteration tries every still-failing item once; the server
    that served the bad blob is recorded in the item's
    ``exclude_server_ids`` so the next iteration lands on a different
    mirror.  When a download returns "All servers excluded" the pool
    has been fully swept for that item and it is given up early.

    Args:
        failed_paths: Corrupt RPM file paths from the initial pass.
        download_items: Original :class:`DownloadItem` list (matched
            by filename / package name to recover the retry items).
        ops: :class:`PackageOperations` instance with download ability.
        options: Install options controlling download behaviour.
        source_servers: Optional map of ``package_name → server_id``
            identifying the server that served each bad file BEFORE
            we got involved (e.g. when the failing file was already
            in cache from a previous in-session download).  Falls
            into ``exclude_server_ids`` on the first retry attempt.
            Predownloaded files have no provenance and pass ``None``
            here (iteration A of bug #3 — see
            ``urpm.core.settings.DownloadSettings.max_retries``).
        urpm_root: urpm state directory path.
        max_retries: Maximum number of distinct mirrors tried per
            file.  ``None`` reads :attr:`DownloadSettings.max_retries`
            from the active configuration (default 3).

    Returns:
        A ``(recovered_paths, still_failed)`` tuple.  *still_failed*
        contains ``(package_name, error_message)`` pairs reporting
        the **last** failure observed for each unrecoverable item.
    """
    if not failed_paths:
        return [], []

    if max_retries is None:
        from .settings import get_settings
        max_retries = get_settings().download.max_retries

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

    # Caller-supplied "we know who served the original bad blob"
    # exclusions land in the items before the first retry pass.  Each
    # iteration after that derives its own exclusions from the
    # ``DownloadResult.source_server_id`` of the file that just
    # failed, so the same mirror is never re-tried for the same file.
    if source_servers:
        for item in retry_items:
            bad_id = source_servers.get(item.name)
            if bad_id is not None and bad_id not in item.exclude_server_ids:
                item.exclude_server_ids.append(bad_id)

    recovered: List[Path] = []
    items_still_failing = list(retry_items)
    last_reasons: Dict[str, str] = {}

    attempt = 0
    for attempt in range(1, max_retries + 1):
        if not items_still_failing:
            break

        logger.info(
            "Retry attempt %d/%d for %d package(s) from alternate mirrors",
            attempt, max_retries, len(items_still_failing),
        )

        try:
            dl_results, _downloaded, _cached, _peer_stats = ops.download_packages(
                items_still_failing,
                options=options,
                urpm_root=urpm_root,
            )
        except Exception as exc:
            logger.error(
                "Retry download batch failed at attempt %d: %s",
                attempt, exc, exc_info=True,
            )
            return recovered, [
                (item.name, str(exc)) for item in items_still_failing
            ]

        next_round: List["DownloadItem"] = []
        for result in dl_results:
            if result.success and result.path:
                valid, bad = pre_verify_signatures([result.path])
                if valid:
                    recovered.append(result.path)
                    continue
                failure = bad[0] if bad else None
                reason = failure.reason if failure else "signature check failed"
                last_reasons[result.item.name] = reason
                try:
                    result.path.unlink()
                except OSError:
                    pass
                # Remember which server served this bad blob so the
                # next attempt does not re-pick it.  ``source_server_id``
                # is set by the downloader on every successful HTTP
                # response, regardless of whether the body turns out
                # to be a valid RPM.
                bad_id = getattr(result, 'source_server_id', None)

                # Route by category (bug #3 iteration B):
                #   * a fresh ``signature`` failure on a retry attempt
                #     is an active tampering signal — blacklist the
                #     server we just got it from.
                #   * other failure categories ding the reputation
                #     score so the same misbehaving mirror sinks in
                #     future selections without being banned outright.
                if failure is not None and bad_id is not None and getattr(ops, 'db', None) is not None:
                    if failure.category == "signature":
                        ops.db.blacklist_server(
                            bad_id,
                            reason=(
                                f"served '{result.path.name}' with "
                                f"failing signature ({failure.reason})"
                            ),
                        )
                        server_row = ops.db.get_server_by_id(bad_id) or {}
                        server_name = server_row.get('name') or f"#{bad_id}"
                        logger.error(
                            "SECURITY ALERT: server '%s' potentially "
                            "compromised after signature failure on retry "
                            "of '%s' (%s) — blacklisted.  Detail: "
                            "urpm server status %s",
                            server_name, result.path.name,
                            failure.reason, server_name,
                        )
                    else:
                        ops.db.record_server_failure(
                            bad_id, category="corrupt",
                            detail=f"{result.path.name}: {failure.reason}",
                        )

                if bad_id and bad_id not in result.item.exclude_server_ids:
                    result.item.exclude_server_ids.append(bad_id)
                next_round.append(result.item)
                continue

            # Download itself failed (network unreachable, "All
            # servers excluded by exclude_server_ids", HTTP 4xx/5xx
            # on every candidate).  No point retrying if the pool is
            # empty for this item.
            err = result.error or "download failed on retry"
            last_reasons[result.item.name] = err
            if "all servers excluded" in err.lower():
                continue
            next_round.append(result.item)

        items_still_failing = next_round

    recovered_names = {_extract_name_from_path(p) for p in recovered}
    still_failed: List[Tuple[str, str]] = []
    for item in retry_items:
        if item.name in recovered_names:
            continue
        reason = last_reasons.get(item.name, "max retries exhausted")
        still_failed.append((item.name, reason))

    logger.info(
        "Retry final: %d recovered, %d still failed after %d attempt(s)",
        len(recovered), len(still_failed), attempt,
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
