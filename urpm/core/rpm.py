"""
RPM utilities for urpm.

Provides version comparison and other RPM-related functions.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def is_local_rpm(pkg_spec: str) -> bool:
    """Check if a package spec is a local RPM file path.

    Args:
        pkg_spec: Package specification (name or path)

    Returns:
        True if it looks like a local RPM file path
    """
    return pkg_spec.endswith('.rpm') and ('/' in pkg_spec or pkg_spec.startswith('.'))


def read_rpm_header(rpm_path: Path) -> Optional[Dict[str, Any]]:
    """Read metadata from a local RPM file.

    Args:
        rpm_path: Path to the RPM file

    Returns:
        Dict with package metadata, or None if reading failed.
        Keys: name, version, release, epoch, arch, nevra, size,
              requires, provides, conflicts, obsoletes,
              recommends, suggests, supplements, enhances
    """
    import rpm

    path = Path(rpm_path)
    if not path.exists():
        return None

    try:
        ts = rpm.TransactionSet()
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)

        fd = os.open(str(path), os.O_RDONLY)
        try:
            hdr = ts.hdrFromFdno(fd)
        finally:
            os.close(fd)

        name = hdr[rpm.RPMTAG_NAME]
        version = hdr[rpm.RPMTAG_VERSION]
        release = hdr[rpm.RPMTAG_RELEASE]
        epoch = hdr[rpm.RPMTAG_EPOCH] or 0
        arch = hdr[rpm.RPMTAG_ARCH]
        size = hdr[rpm.RPMTAG_SIZE] or 0

        # Build NEVRA
        if epoch:
            nevra = f"{name}-{epoch}:{version}-{release}.{arch}"
        else:
            nevra = f"{name}-{version}-{release}.{arch}"

        def get_deps(tag) -> List[str]:
            """Extract dependency list from header (names only)."""
            deps = hdr[tag]
            if not deps:
                return []
            # Handle both string and list
            if isinstance(deps, str):
                return [deps] if deps else []
            return list(deps) if deps else []

        def get_versioned_deps(name_tag, version_tag, flags_tag) -> List[str]:
            """Extract versioned dependencies combining name, version and flags."""
            names = hdr[name_tag] or []
            versions = hdr[version_tag] or []
            flags_list = hdr[flags_tag] or []

            result = []
            for i, dep_name in enumerate(names):
                if not dep_name:
                    continue
                ver = versions[i] if i < len(versions) else ''
                flags = flags_list[i] if i < len(flags_list) else 0

                if ver and flags:
                    # Convert RPM flags to operator string
                    op = ''
                    if flags & rpm.RPMSENSE_LESS:
                        op += '<'
                    if flags & rpm.RPMSENSE_GREATER:
                        op += '>'
                    if flags & rpm.RPMSENSE_EQUAL:
                        op += '='
                    if op:
                        result.append(f"{dep_name} {op} {ver}")
                    else:
                        result.append(dep_name)
                else:
                    result.append(dep_name)
            return result

        return {
            'name': name,
            'version': version,
            'release': release,
            'epoch': epoch,
            'arch': arch,
            'nevra': nevra,
            'size': size,
            'path': str(path.resolve()),
            'requires': get_versioned_deps(rpm.RPMTAG_REQUIRENAME, rpm.RPMTAG_REQUIREVERSION, rpm.RPMTAG_REQUIREFLAGS),
            'provides': get_versioned_deps(rpm.RPMTAG_PROVIDENAME, rpm.RPMTAG_PROVIDEVERSION, rpm.RPMTAG_PROVIDEFLAGS),
            'conflicts': get_versioned_deps(rpm.RPMTAG_CONFLICTNAME, rpm.RPMTAG_CONFLICTVERSION, rpm.RPMTAG_CONFLICTFLAGS),
            'obsoletes': get_versioned_deps(rpm.RPMTAG_OBSOLETENAME, rpm.RPMTAG_OBSOLETEVERSION, rpm.RPMTAG_OBSOLETEFLAGS),
            'recommends': get_versioned_deps(rpm.RPMTAG_RECOMMENDNAME, rpm.RPMTAG_RECOMMENDVERSION, rpm.RPMTAG_RECOMMENDFLAGS) if hasattr(rpm, 'RPMTAG_RECOMMENDNAME') else [],
            'suggests': get_versioned_deps(rpm.RPMTAG_SUGGESTNAME, rpm.RPMTAG_SUGGESTVERSION, rpm.RPMTAG_SUGGESTFLAGS) if hasattr(rpm, 'RPMTAG_SUGGESTNAME') else [],
            'supplements': get_versioned_deps(rpm.RPMTAG_SUPPLEMENTNAME, rpm.RPMTAG_SUPPLEMENTVERSION, rpm.RPMTAG_SUPPLEMENTFLAGS) if hasattr(rpm, 'RPMTAG_SUPPLEMENTNAME') else [],
            'enhances': get_versioned_deps(rpm.RPMTAG_ENHANCENAME, rpm.RPMTAG_ENHANCEVERSION, rpm.RPMTAG_ENHANCEFLAGS) if hasattr(rpm, 'RPMTAG_ENHANCENAME') else [],
        }
    except Exception:
        return None


def split_version(v: str) -> List[Tuple[int, Any]]:
    """Split version into comparable parts (numeric vs alpha).

    Returns tuples (type, value) where type=0 for int, 1 for str.
    This ensures consistent ordering: numbers < strings.

    Args:
        v: Version string (e.g., "1.2.3", "1.0rc1")

    Returns:
        List of (type, value) tuples for comparison
    """
    parts = re.findall(r'(\d+|[a-zA-Z]+)', v or '0')
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def evr_key(pkg: Dict) -> Tuple:
    """Return a sortable key for epoch-version-release comparison.

    This implements a simplified rpmvercmp for comparing package versions.
    Can be used as a sort key or for direct comparison.

    Args:
        pkg: Package dict with 'epoch', 'version', 'release' keys

    Returns:
        Tuple suitable for comparison (higher = newer)

    Example:
        packages.sort(key=evr_key, reverse=True)  # newest first
        if evr_key(pkg1) > evr_key(pkg2): ...
    """
    epoch = pkg.get('epoch', 0) or 0
    return (epoch,
            split_version(pkg.get('version', '0')),
            split_version(pkg.get('release', '0')))


def filter_latest_versions(packages: List[Dict]) -> List[Dict]:
    """Filter a list of packages to keep only the latest version of each name.

    Args:
        packages: List of package dicts with 'name', 'epoch', 'version', 'release'

    Returns:
        List with only the latest version of each package name
    """
    latest_by_name = {}
    for pkg in packages:
        name = pkg.get('name')
        if not name:
            continue
        if name not in latest_by_name or evr_key(pkg) > evr_key(latest_by_name[name]):
            latest_by_name[name] = pkg
    return list(latest_by_name.values())
