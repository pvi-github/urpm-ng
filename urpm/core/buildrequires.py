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
        List of build requirements with version constraints (e.g., "pkg >= 1.0").

    Raises:
        FileNotFoundError: If spec file doesn't exist.
    """
    import re

    spec_path = Path(spec_path)
    if not spec_path.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")

    requirements = []

    # Regex to parse a single dependency with optional version constraint
    # Matches: name, name >= version, name(arch) >= version, etc.
    dep_pattern = re.compile(
        r'^([a-zA-Z0-9_+.-]+(?:\([^)]+\))?)'  # Package name (with optional arch like (x86-64))
        r'(?:\s*(>=|<=|>|<|=)\s*([^\s,#]+))?'  # Optional: operator and version
    )

    with open(spec_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            # Match BuildRequires: (case insensitive)
            if line.lower().startswith('buildrequires:'):
                # Extract the part after BuildRequires:
                deps_part = line.split(':', 1)[1].strip()
                # Remove comments
                if '#' in deps_part:
                    deps_part = deps_part.split('#')[0].strip()

                # Split by comma for multiple deps on one line
                for dep_str in re.split(r'\s*,\s*', deps_part):
                    dep_str = dep_str.strip()
                    if not dep_str:
                        continue
                    # Skip macros we can't resolve
                    if dep_str.startswith('%'):
                        continue

                    # Parse the dependency
                    match = dep_pattern.match(dep_str)
                    if match:
                        name, op, version = match.groups()
                        if op and version:
                            # Normalize operator (= -> ==)
                            if op == '=':
                                op = '=='
                            requirements.append(f"{name} {op} {version}")
                        else:
                            requirements.append(name)

    return list(dict.fromkeys(requirements))  # Remove duplicates, preserve order


def rpm_dep_to_solver_format(dep: str) -> str:
    """Convert RPM dependency format to solver format.

    RPM format: "name >= 1.0" or "name"
    Solver format: "name >= 1.0" or "name" (same, libsolv uses this directly)

    Args:
        dep: Dependency string in RPM format.

    Returns:
        Dependency string in solver format.
    """
    import re

    # Match: name (>= | <= | > | < | = | ==) version
    match = re.match(
        r'^([a-zA-Z0-9_+.-]+(?:\([^)]+\))?)'  # Package name
        r'\s+(>=|<=|>|<|==|=)\s+'              # Operator
        r'(.+)$',                              # Version
        dep.strip()
    )

    if match:
        name, op, version = match.groups()
        # libsolv expects "name op version" format directly
        return f"{name} {op} {version}"

    return dep.strip()


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
