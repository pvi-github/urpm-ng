"""Detect whether installed packages require a system or session restart.

Implements the Mageia/Mandriva ``should-restart`` virtual provide mechanism:
packages declare ``Provides: should-restart:<component>`` to signal that
installing or upgrading them requires restarting a component.

Components:
    - ``system``:  Full reboot required (e.g. kernel, glibc, systemd).
    - ``session``: User session restart required (e.g. dbus-user, polkit).
    - *service*:   A specific service must be restarted (e.g. ``sshd``).

Usage::

    from urpm.core.needs_restart import check_needs_restart, format_restart_messages

    # Before transaction — check if any package needs full sync
    restart = check_needs_restart(package_names, root="/")
    if 'system' in restart:
        # Force full sync + show reboot message after install

    # After transaction — show messages
    for msg in format_restart_messages(restart):
        print(msg)
"""

import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..i18n import _, ngettext


def _system_boot_time() -> float:
    """Return the system boot timestamp (seconds since epoch).

    Uses ``/proc/uptime`` to compute when the system was last started.
    """
    try:
        uptime_str = Path('/proc/uptime').read_text().split()[0]
        return time.time() - float(uptime_str)
    except (OSError, ValueError, IndexError):
        return 0.0


def _get_should_restart_providers(root: str = "/") -> Dict[str, List[dict]]:
    """Query RPM database for packages that provide ``should-restart:*``.

    Returns a dict mapping component names to lists of package info dicts::

        {
            'system': [{'name': 'kernel-desktop', 'installtime': 1712345678}],
            'session': [{'name': 'polkit', 'installtime': 1712345700}],
        }
    """
    qf = '%{NAME}\\t%{INSTALLTIME}\\t[%{PROVIDES}:%{PROVIDEVERSION} ]\\n'
    cmd = ['rpm', '--whatprovides', 'should-restart', '-q', '--qf', qf]
    if root and root != '/':
        cmd.insert(1, f'--root={root}')

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return {}
    except (OSError, subprocess.TimeoutExpired):
        return {}

    providers: Dict[str, List[dict]] = {}
    seen_lines: Set[str] = set()

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line in seen_lines:
            continue
        seen_lines.add(line)

        parts = line.split('\t', 2)
        if len(parts) < 3:
            continue

        name, installtime_str, provides_str = parts
        try:
            installtime = int(installtime_str)
        except ValueError:
            continue

        # Extract should-restart:component from provides
        for token in provides_str.split():
            if token.startswith('should-restart:'):
                component = token.split(':', 1)[1].strip()
                if component:
                    providers.setdefault(component, []).append({
                        'name': name,
                        'installtime': installtime,
                    })

    return providers


def check_needs_restart(
    package_names: List[str],
    root: str = "/",
) -> Dict[str, List[str]]:
    """Check whether any of the given packages require a restart.

    Queries the RPM database for ``should-restart`` providers among
    *package_names* and returns only those whose install time is newer
    than the component's launch time.

    Args:
        package_names: Names of packages that were just installed/upgraded.
        root: RPM root path.

    Returns:
        Dict mapping component names to lists of package names that
        trigger a restart.  Empty dict means no restart needed.

    Example::

        >>> check_needs_restart(['kernel-desktop', 'firefox'])
        {'system': ['kernel-desktop']}
    """
    if not package_names:
        return {}

    name_set = {n.lower() for n in package_names}
    providers = _get_should_restart_providers(root)

    if not providers:
        return {}

    boot_time = _system_boot_time()
    result: Dict[str, List[str]] = {}

    for component, pkgs in providers.items():
        for pkg in pkgs:
            if pkg['name'].lower() not in name_set:
                continue

            # For 'system': compare against boot time
            # For other components: always flag (we can't easily check
            # session or service start times portably)
            if component == 'system':
                if pkg['installtime'] > boot_time:
                    result.setdefault(component, []).append(pkg['name'])
            else:
                result.setdefault(component, []).append(pkg['name'])

    return result


def needs_system_restart(package_names: List[str], root: str = "/") -> bool:
    """Quick check: does this set of packages require a full reboot?

    Use this before starting a transaction to decide between smart sync
    and forced full sync.
    """
    restart = check_needs_restart(package_names, root)
    return 'system' in restart


def check_needs_restart_from_actions(
    actions: List,
    resolver,
) -> Dict[str, List[str]]:
    """Check should-restart from a resolution's actions.

    Single entry point used by ``cmd_install``, ``cmd_upgrade`` and
    the rpmdrake helper to decide whether to force ``full_sync``
    transactions.  Pulls ``SOLVABLE_PROVIDES`` from the resolver pool
    that produced the actions, so every caller sees the same data the
    resolver itself worked from — the pre-R9 setup had two callers
    reading the pool and a third going through ``db.get_package``,
    which guaranteed divergence on virtual packages whose pool view
    and rpmdb view drifted.

    Args:
        actions: ``PackageAction`` list from a ``Resolution``.
        resolver: Live :class:`urpm.core.resolver.Resolver` whose pool
            was used to build the actions.

    Returns:
        Same format as :func:`check_needs_restart_from_provides`:
        ``component → list of package names that triggered it``.
        Empty dict when nothing needs a restart.
    """
    import solv
    # Lazy import to dodge the resolver→needs_restart cycle that
    # would otherwise appear at module load time.
    from .resolver import TransactionType

    pkg_provides: Dict[str, List[str]] = {}
    for action in actions:
        if action.action not in (TransactionType.INSTALL,
                                 TransactionType.UPGRADE):
            continue
        sel = resolver.pool.select(
            action.name, solv.Selection.SELECTION_NAME,
        )
        for s in sel.solvables():
            provides = [
                str(d) for d in s.lookup_deparray(solv.SOLVABLE_PROVIDES)
            ]
            if provides:
                pkg_provides[action.name] = provides
                break

    return check_needs_restart_from_provides(pkg_provides)


def check_needs_restart_from_provides(
    package_provides: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Check should-restart from pre-resolved provides (no RPM query).

    This is faster than :func:`check_needs_restart` when you already
    have the provides list from the resolver (avoids an extra RPM query
    after install).

    Supports both the Fedora format (``should-restart:system``) and the
    Mageia/RPM versioned provide format (``should-restart = system``) as
    returned by libsolv's ``lookup_deparray(SOLVABLE_PROVIDES)``.

    Args:
        package_provides: Dict mapping package names to their provides
            list.  Example: ``{'glibc': ['should-restart = system', ...]}``.

    Returns:
        Same format as :func:`check_needs_restart`.
    """
    result: Dict[str, List[str]] = {}

    for name, provides in package_provides.items():
        for prov in provides:
            # Fedora format: "should-restart:system"
            if prov.startswith('should-restart:'):
                component = prov.split(':', 1)[1].strip()
                if component:
                    result.setdefault(component, []).append(name)
            # Mageia/RPM versioned format: "should-restart = system"
            elif prov.startswith('should-restart '):
                component = prov.split('=', 1)[1].strip() if '=' in prov else ''
                if component:
                    result.setdefault(component, []).append(name)

    return result


def format_restart_messages(
    restart: Dict[str, List[str]],
) -> List[str]:
    """Format restart requirements as human-readable messages.

    Args:
        restart: Dict from :func:`check_needs_restart`.

    Returns:
        List of translated messages, one per component.

    Example::

        >>> format_restart_messages({'system': ['kernel-desktop']})
        ['You should restart your computer for kernel-desktop']
    """
    messages = []

    for component, pkg_names in sorted(restart.items()):
        packages = ', '.join(sorted(pkg_names))

        if component == 'system':
            messages.append(
                _("You should restart your computer for {packages}").format(
                    packages=packages
                )
            )
        elif component == 'session':
            messages.append(
                _("You should restart your session for {packages}").format(
                    packages=packages
                )
            )
        else:
            messages.append(
                _("You should restart {service} for {packages}").format(
                    service=component, packages=packages
                )
            )

    return messages
