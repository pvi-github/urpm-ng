"""
BuildRequires parser for spec files and source RPMs.

Extracts build dependencies from:
- .spec files (direct parsing)
- .src.rpm files (via rpm -qp --requires)
- Auto-detect in RPM build tree (SPECS/ directory)
"""

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def find_spec_in_workdir(workdir: Path = None) -> Optional[Path]:
    """Auto-detect .spec file in RPM build tree.

    Searches for .spec files in:
    1. SPECS/ subdirectory (standard rpmbuild layout)
    2. Current directory (for simple layouts)

    If multiple .spec files are found, returns None (caller should ask user).

    Args:
        workdir: Working directory to search. Defaults to current directory.

    Returns:
        Path to .spec file if exactly one found, None otherwise.
    """
    if workdir is None:
        workdir = Path.cwd()
    else:
        workdir = Path(workdir)

    # First check SPECS/ subdirectory
    specs_dir = workdir / "SPECS"
    if specs_dir.is_dir():
        specs = list(specs_dir.glob("*.spec"))
        if len(specs) == 1:
            return specs[0]
        elif len(specs) > 1:
            return None  # Multiple specs, need user choice

    # Check current directory
    specs = list(workdir.glob("*.spec"))
    if len(specs) == 1:
        return specs[0]

    return None


def list_specs_in_workdir(workdir: Path = None) -> List[Path]:
    """List all .spec files in RPM build tree.

    Args:
        workdir: Working directory to search. Defaults to current directory.

    Returns:
        List of paths to .spec files found.
    """
    if workdir is None:
        workdir = Path.cwd()
    else:
        workdir = Path(workdir)

    specs = []

    # Check SPECS/ subdirectory
    specs_dir = workdir / "SPECS"
    if specs_dir.is_dir():
        specs.extend(specs_dir.glob("*.spec"))

    # Check current directory (avoid duplicates)
    for spec in workdir.glob("*.spec"):
        if spec not in specs:
            specs.append(spec)

    return sorted(specs)


def parse_buildrequires_from_spec(spec_path: Path) -> List[str]:
    """Parse BuildRequires from a .spec file.

    Parses the spec file directly to extract BuildRequires lines.

    Args:
        spec_path: Path to the .spec file.

    Returns:
        List of build requirement names (package names only, no versions).

    Raises:
        FileNotFoundError: If spec file doesn't exist.
    """
    import re

    spec_path = Path(spec_path)
    if not spec_path.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")

    requirements = []

    with open(spec_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            # Match BuildRequires: (case insensitive)
            if line.lower().startswith('buildrequires:'):
                # Extract the part after BuildRequires:
                deps_part = line.split(':', 1)[1].strip()
                # Split by comma or whitespace for multiple deps on one line
                # Handle version constraints like "pkg >= 1.0" - extract just pkg name
                skip_token = False
                for dep in re.split(r'[,\s]+', deps_part):
                    if skip_token:
                        skip_token = False
                        continue
                    dep = dep.strip()
                    if not dep or dep.startswith('#'):
                        continue
                    # Skip version operators and numbers
                    if dep in ('>=', '<=', '>', '<', '=', '=='):
                        skip_token = True
                        continue
                    if re.match(r'^[\d.]+$', dep):
                        continue
                    # Skip macros we can't resolve
                    if dep.startswith('%'):
                        continue
                    requirements.append(dep)

    return list(dict.fromkeys(requirements))  # Remove duplicates, preserve order


def parse_buildrequires_from_srpm(srpm_path: Path) -> List[str]:
    """Extract BuildRequires from a source RPM.

    Uses `rpm -qp --requires` to get build dependencies.
    Note: For src.rpm, --requires gives BuildRequires, not runtime Requires.

    Args:
        srpm_path: Path to the .src.rpm file.

    Returns:
        List of build requirement names.

    Raises:
        FileNotFoundError: If src.rpm doesn't exist.
        subprocess.CalledProcessError: If rpm command fails.
    """
    srpm_path = Path(srpm_path)
    if not srpm_path.exists():
        raise FileNotFoundError(f"Source RPM not found: {srpm_path}")

    result = subprocess.run(
        ["rpm", "-qp", "--requires", str(srpm_path)],
        capture_output=True,
        text=True,
        check=True
    )

    # Filter out rpmlib(...) dependencies
    requirements = []
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        if line and not line.startswith("rpmlib("):
            requirements.append(line)

    return requirements


def get_buildrequires(target: str = None, workdir: Path = None) -> Tuple[List[str], str]:
    """Get build requirements from target or auto-detect.

    Entry point that handles:
    - Explicit .spec file path
    - Explicit .src.rpm file path
    - Auto-detection in RPM build tree

    Args:
        target: Path to .spec or .src.rpm, or None for auto-detect.
        workdir: Working directory for auto-detect. Defaults to cwd.

    Returns:
        Tuple of (requirements list, source description).
        Source description is the file path or "auto-detected".

    Raises:
        FileNotFoundError: If target file not found.
        ValueError: If auto-detect fails (no spec or multiple specs).
        subprocess.CalledProcessError: If rpm command fails.
    """
    if target:
        target_path = Path(target)

        if target_path.suffix == '.spec':
            reqs = parse_buildrequires_from_spec(target_path)
            return reqs, str(target_path)

        elif target_path.name.endswith('.src.rpm'):
            reqs = parse_buildrequires_from_srpm(target_path)
            return reqs, str(target_path)

        elif target_path.is_file():
            # Try to detect type from content/extension
            if target_path.suffix == '.rpm':
                reqs = parse_buildrequires_from_srpm(target_path)
                return reqs, str(target_path)
            else:
                # Assume it's a spec file
                reqs = parse_buildrequires_from_spec(target_path)
                return reqs, str(target_path)

        else:
            raise FileNotFoundError(f"Target not found: {target}")

    # Auto-detect mode
    spec_path = find_spec_in_workdir(workdir)
    if spec_path:
        reqs = parse_buildrequires_from_spec(spec_path)
        return reqs, str(spec_path)

    # Check for multiple specs
    specs = list_specs_in_workdir(workdir)
    if len(specs) > 1:
        spec_names = [s.name for s in specs]
        raise ValueError(
            f"Multiple .spec files found: {', '.join(spec_names)}. "
            "Please specify which one to use."
        )

    raise ValueError(
        "No .spec file found. Run from an RPM build tree or specify a .spec/.src.rpm file."
    )
