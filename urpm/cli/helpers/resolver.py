"""Resolver helper functions for CLI commands."""

import platform
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def extract_version(pkg_name: str) -> str:
    """Extract version from package name (e.g., php8.4-fpm -> 8.4)."""
    match = re.search(r'(\d+\.\d+)', pkg_name)
    return match.group(1) if match else None


def group_by_version(packages: set) -> dict:
    """Group packages by their version.

    Returns dict: {version: set of packages}
    Packages without version go under None key.
    """
    groups = {}
    for pkg in packages:
        ver = extract_version(pkg)
        if ver not in groups:
            groups[ver] = set()
        groups[ver].add(pkg)
    return groups


def create_resolver(db: 'PackageDatabase', args, **kwargs) -> 'Resolver':
    """Create a Resolver with root options from args.

    Args:
        db: Package database
        args: Parsed arguments (may contain root, urpm_root, allow_arch)
        **kwargs: Additional arguments to pass to Resolver

    Returns:
        Configured Resolver instance
    """
    from ...core.resolver import Resolver

    # Get root options from args
    root = getattr(args, 'root', None)
    urpm_root = getattr(args, 'urpm_root', None)

    # Default arch if not provided
    if 'arch' not in kwargs:
        kwargs['arch'] = platform.machine()

    # Handle --allow-arch: build allowed_arches list
    # Default: [system_arch, 'noarch']
    # With --allow-arch: add specified architectures
    if 'allowed_arches' not in kwargs:
        allow_arch = getattr(args, 'allow_arch', None)
        if allow_arch:
            # User specified additional architectures
            arch = kwargs.get('arch', platform.machine())
            allowed_arches = [arch, 'noarch'] + list(allow_arch)
            # Remove duplicates while preserving order
            seen = set()
            kwargs['allowed_arches'] = [x for x in allowed_arches if not (x in seen or seen.add(x))]

    return Resolver(db, root=root, urpm_root=urpm_root, **kwargs)


# Backwards compatibility aliases (with underscore prefix)
_extract_version = extract_version
_group_by_version = group_by_version
_create_resolver = create_resolver
