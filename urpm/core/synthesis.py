"""
Synthesis file parser for urpm

Parses synthesis.hdlist.cz files containing lightweight package metadata.
Format: Tags (@provides, @requires, etc.) followed by @info which terminates
each package definition.
"""

import re
from pathlib import Path
from typing import Dict, Iterator, List, Any, Optional, Tuple

from .compression import decompress


def parse_nevra(nevra: str) -> Tuple[str, str, str, str]:
    """Parse a NEVRA string into components.

    Args:
        nevra: String like "firefox-120.0-1.mga9.x86_64"

    Returns:
        Tuple of (name, version, release, arch)
    """
    # Split off architecture
    parts = nevra.rsplit('.', 1)
    if len(parts) == 2:
        arch = parts[1]
        name_ver_rel = parts[0]
    else:
        arch = 'noarch'
        name_ver_rel = nevra

    # Split name-version-release (tricky because name can contain -)
    parts = name_ver_rel.rsplit('-', 2)
    if len(parts) >= 3:
        name = parts[0]
        version = parts[1]
        release = parts[2]
    elif len(parts) == 2:
        name = parts[0]
        version = parts[1]
        release = ''
    else:
        name = name_ver_rel
        version = ''
        release = ''

    return name, version, release, arch


def parse_dependency(dep: str) -> Tuple[str, str, str]:
    """Parse a dependency string with optional version constraint.

    Args:
        dep: String like "libfoo>=1.0" or "bar[>= 2.0]" or just "baz"

    Returns:
        Tuple of (name, operator, version)
    """
    # Handle [operator version] format
    match = re.match(r'^(.+?)\[([<>=!]+)\s*(.+?)\]$', dep)
    if match:
        return match.group(1), match.group(2), match.group(3)

    # Handle name>=version format (no brackets)
    match = re.match(r'^(.+?)([<>=!]+)(.+)$', dep)
    if match:
        return match.group(1), match.group(2), match.group(3)

    # No version constraint
    return dep, '', ''


def _split_synthesis_line(line: str) -> List[str]:
    """Split a synthesis line on @ separators, handling nested parentheses.

    The synthesis format uses @ as separator, but some provides contain @
    inside parentheses like bundled(npm(@xterm/addon-canvas)).
    We need to only split on @ that are NOT inside parentheses.

    Args:
        line: A synthesis line starting with @

    Returns:
        List of parts (first element is empty since line starts with @)
    """
    parts = []
    current = ""
    paren_depth = 0

    for char in line:
        if char == '(':
            paren_depth += 1
            current += char
        elif char == ')':
            paren_depth -= 1
            current += char
        elif char == '@' and paren_depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char

    if current:
        parts.append(current)

    return parts


def parse_synthesis(filename: Path) -> Iterator[Dict[str, Any]]:
    """Parse a synthesis file and yield package dictionaries.

    The synthesis format has tags BEFORE @info. When we encounter @info,
    we create the package with all accumulated tags.

    Args:
        filename: Path to synthesis.hdlist.cz file

    Yields:
        Package dictionaries
    """
    content = decompress(filename)

    current_tags: Dict[str, Any] = {}

    for line in content.split('\n'):
        line = line.strip()
        if not line or not line.startswith('@'):
            continue

        parts = _split_synthesis_line(line)
        if len(parts) < 2:
            continue

        tag = parts[1]

        if tag == 'info':
            # @info terminates the package definition
            nevra = parts[2] if len(parts) > 2 else ''
            epoch_str = parts[3] if len(parts) > 3 else '0'
            size_str = parts[4] if len(parts) > 4 else '0'
            group = parts[5] if len(parts) > 5 else ''

            name, version, release, arch = parse_nevra(nevra)

            try:
                epoch = int(epoch_str)
            except ValueError:
                epoch = 0

            try:
                size = int(size_str)
            except ValueError:
                size = 0

            try:
                filesize = int(current_tags.get('filesize', "0"))
            except ValueError:
                filesize = 0

            pkg = {
                'name': name,
                'version': version,
                'release': release,
                'epoch': epoch,
                'arch': arch,
                'nevra': nevra,
                'size': size,
                'group': group,
                'summary': current_tags.get('summary', ''),
                'provides': current_tags.get('provides', []),
                'requires': current_tags.get('requires', []),
                'conflicts': current_tags.get('conflicts', []),
                'obsoletes': current_tags.get('obsoletes', []),
                'suggests': current_tags.get('suggests', []),
                'recommends': current_tags.get('recommends', []),
                'supplements': current_tags.get('supplements', []),
                'enhances': current_tags.get('enhances', []),
                'filesize': filesize,
            }

            yield pkg
            current_tags = {}

        else:
            # Accumulate tags for the next @info
            if tag == 'summary':
                current_tags['summary'] = parts[2] if len(parts) > 2 else ''
            elif tag == 'provides':
                current_tags['provides'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'requires':
                current_tags['requires'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'conflicts':
                current_tags['conflicts'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'obsoletes':
                current_tags['obsoletes'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'suggests':
                current_tags['suggests'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'recommends':
                current_tags['recommends'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'supplements':
                current_tags['supplements'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'enhances':
                current_tags['enhances'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'filesize':
                current_tags['filesize'] = parts[2] if len(parts) > 2 else "0"


def parse_synthesis_to_list(filename: Path) -> List[Dict[str, Any]]:
    """Parse a synthesis file and return list of packages.

    Args:
        filename: Path to synthesis.hdlist.cz file

    Returns:
        List of package dictionaries
    """
    return list(parse_synthesis(filename))
