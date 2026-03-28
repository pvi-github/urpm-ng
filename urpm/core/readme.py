"""README.urpmi support — detect and read post-install/upgrade messages.

RPM packages may ship documentation files that should be displayed to the
user after installation or upgrade.  Three naming conventions are supported:

- ``README.urpmi``              — shown on both install and upgrade
- ``README.install.urpmi``      — shown only on fresh install
- ``README.upgrade.urpmi``      — shown on every upgrade
- ``README.<N>.upgrade.urpmi``  — shown once when upgrading *past* version N
  (i.e. old version < N ≤ new version)

Files are located under ``/usr/share/doc/<name>-<version>/`` inside the
target root.

This module is intentionally decoupled from the CLI so that rpmdrake (or
any other frontend) can reuse it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

from ..core.resolver import TransactionType

# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class ReadmeMessage:
    """A single README.urpmi message for a package."""
    package: str
    content: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VERSION_UPGRADE_RE = re.compile(r"^README\.(\d+)\.upgrade\.urpmi$")
"""Matches ``README.<N>.upgrade.urpmi`` and captures N."""


def _rpm_query_docfiles(name: str, root: str | None) -> List[str]:
    """Return the list of doc files owned by *name* in the RPM database.

    Uses ``rpm -ql --docfiles`` so we only get documentation entries.
    """
    cmd = ["rpm"]
    if root:
        cmd += ["--root", str(Path(root).resolve())]
    cmd += ["-ql", "--docfiles", name]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _compare_versions(ver_a: str, ver_b: str) -> int:
    """Compare two RPM version strings.

    Returns negative if *ver_a* < *ver_b*, 0 if equal, positive otherwise.
    Uses ``rpmdev-vercmp`` if available, otherwise falls back to a simple
    numeric/string comparison.
    """
    # Try rpmdev-vercmp first (most accurate)
    try:
        result = subprocess.run(
            ["rpmdev-vercmp", ver_a, ver_b],
            capture_output=True, text=True, timeout=5,
        )
        # Exit code: 11 = a < b, 0 = equal, 12 = a > b
        if result.returncode == 11:
            return -1
        if result.returncode == 12:
            return 1
        return 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: simple segment-by-segment comparison
    def _segments(v: str) -> list:
        return [int(s) if s.isdigit() else s for s in re.split(r'[.\-]', v)]
    a_seg, b_seg = _segments(ver_a), _segments(ver_b)
    for a, b in zip(a_seg, b_seg):
        if type(a) is type(b):
            if a < b:
                return -1
            if a > b:
                return 1
        else:
            # int vs str: int sorts before str
            return -1 if isinstance(a, int) else 1
    return (len(a_seg) > len(b_seg)) - (len(a_seg) < len(b_seg))


def _extract_version(evr: str) -> str:
    """Extract the version part from an EVR string (``[epoch:]version[-release]``)."""
    v = evr
    if ":" in v:
        v = v.split(":", 1)[1]
    if "-" in v:
        v = v.rsplit("-", 1)[0]
    return v


def _should_show(filename: str, action: TransactionType,
                 from_evr: str, to_evr: str) -> bool:
    """Decide whether a README.urpmi file should be displayed.

    Args:
        filename: The basename of the README file.
        action: The transaction type (INSTALL, UPGRADE, etc.).
        from_evr: The previous EVR for upgrades (empty string for installs).
        to_evr: The new EVR being installed.

    Returns:
        True if the file should be shown to the user.
    """
    basename = Path(filename).name

    # README.urpmi — always shown (install or upgrade)
    if basename == "README.urpmi":
        return True

    is_upgrade = action in (TransactionType.UPGRADE, TransactionType.DOWNGRADE)
    is_install = action == TransactionType.INSTALL

    # README.install.urpmi — fresh install only
    if basename == "README.install.urpmi":
        return is_install

    # README.upgrade.urpmi — upgrade only
    if basename == "README.upgrade.urpmi":
        return is_upgrade

    # README.<N>.upgrade.urpmi — upgrade past version N
    m = _VERSION_UPGRADE_RE.match(basename)
    if m and is_upgrade and from_evr:
        threshold = m.group(1)
        old_ver = _extract_version(from_evr)
        return _compare_versions(old_ver, threshold) < 0

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_readme_messages(
    actions: Sequence,
    root: str | None = None,
) -> List[ReadmeMessage]:
    """Collect README.urpmi messages for the given transaction actions.

    Args:
        actions: Sequence of :class:`PackageAction` from the resolver.
            Each action must have ``name``, ``action``, ``evr`` and
            ``from_evr`` attributes.
        root: RPM root path (``--root``).  ``None`` for the system root.

    Returns:
        List of :class:`ReadmeMessage`, one per displayed README file,
        in the order the actions were processed.
    """
    messages: List[ReadmeMessage] = []

    for act in actions:
        if act.action == TransactionType.REMOVE:
            continue

        doc_files = _rpm_query_docfiles(act.name, root)

        for filepath in doc_files:
            basename = Path(filepath).name
            if not basename.endswith(".urpmi"):
                continue
            if not _should_show(basename, act.action, act.from_evr, act.evr):
                continue

            # Read the file content from the target root
            full_path = Path(root or "/") / filepath.lstrip("/")
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if content:
                messages.append(ReadmeMessage(package=act.name, content=content))

    return messages


def format_readme_output(messages: List[ReadmeMessage]) -> str:
    """Format README messages for terminal display.

    Each message is rendered as::

        More information on package <name>:
        <content>

        ----------------------------------------------------------------------

    Args:
        messages: List of :class:`ReadmeMessage` to format.

    Returns:
        Formatted string ready for printing.  Empty string if no messages.
    """
    if not messages:
        return ""

    separator = "-" * 70
    parts: list[str] = []
    for msg in messages:
        parts.append(
            f"\nMore information on package {msg.package}:\n"
            f"{msg.content}\n\n"
            f"{separator}"
        )
    return "".join(parts)
