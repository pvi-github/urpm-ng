"""Package name extraction and virtual package resolution helpers."""

import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def extract_pkg_name(package: str) -> str:
    """Extract package name from a NEVRA string.

    Args:
        package: Either a simple name like 'firefox' or NEVRA like 'firefox-120.0-1.mga10.x86_64'

    Returns:
        The package name
    """
    # Don't try to extract NEVRA from virtual packages (pkgconfig, etc.)
    # These have parentheses and the version inside is part of the name
    if '(' in package:
        return package
    # If it looks like a NEVRA (has version pattern), extract name
    # Pattern: name-version-release.arch where version starts with digit
    match = re.match(r'^(.+?)-\d+[.:]', package)
    if match:
        return match.group(1)
    return package


def extract_family(pkg_name: str) -> str:
    """Extract the family prefix from a versioned package name.

    Examples:
        php8.4-opcache → php8.4
        php8.5-fpm → php8.5
        perl5.38-DBI → perl5.38
        python3.11-requests → python3.11
        firefox → firefox (no family)

    Args:
        pkg_name: Package name

    Returns:
        Family prefix, or the full name if no version pattern detected
    """
    # Pattern: name + version number + dash + rest
    # e.g., php8.4-something, perl5.38-something, python3.11-something
    match = re.match(r'^([a-zA-Z]+\d+\.?\d*)-', pkg_name)
    if match:
        return match.group(1)
    return pkg_name


def get_installed_families(prefix: str) -> set:
    """Get installed package families matching a prefix.

    Args:
        prefix: Base name like 'php', 'perl', 'python'

    Returns:
        Set of family names like {'php8.4', 'php8.5'}
    """
    families = set()
    try:
        result = subprocess.run(
            ['rpm', '-qa', '--qf', '%{NAME}\\n'],
            capture_output=True, text=True, timeout=30
        )
        for line in result.stdout.splitlines():
            # Match packages like php8.4-*, php8.5-*, etc.
            match = re.match(rf'^({re.escape(prefix)}\d+\.?\d*)-', line)
            if match:
                families.add(match.group(1))
    except Exception:
        pass
    return families


def resolve_virtual_package(db: 'PackageDatabase', pkg_name: str, auto: bool, install_all: bool) -> list:
    """Resolve a virtual package to concrete package(s).

    When multiple providers exist from different families (php8.4-opcache, php8.5-opcache),
    this function decides which one(s) to install based on:
    - What's already installed
    - User preference (interactive) or flags (--auto, --all)

    Args:
        db: Database instance
        pkg_name: Virtual package name (e.g., 'php-opcache')
        auto: If True, don't ask user
        install_all: If True, install for all installed families

    Returns:
        List of concrete package names to install, or empty list to abort
    """
    # Check if pkg_name is a real package (not just a capability)
    real_pkg = db.get_package(pkg_name)

    # Find all providers of this capability
    providers = db.whatprovides(pkg_name)

    # If real package exists, add it to providers if not already there
    if real_pkg:
        provider_names = {p['name'] for p in providers}
        if pkg_name not in provider_names:
            # Add the real package as a provider option
            providers.append({'name': pkg_name, 'id': real_pkg['id'],
                            'version': real_pkg['version'], 'release': real_pkg['release'],
                            'arch': real_pkg['arch'], 'nevra': real_pkg['nevra']})

    if not providers:
        # Not a virtual package and no providers, return as-is (will fail later)
        return [pkg_name]

    # If only ONE provider and it matches the requested name, use it directly
    if len(providers) == 1 and providers[0]['name'] == pkg_name:
        return [pkg_name]

    # Group providers by family
    families = {}
    for prov in providers:
        family = extract_family(prov['name'])
        if family not in families:
            families[family] = []
        families[family].append(prov)

    # Extract base prefix (php from php8.4, perl from perl5.38)
    first_family = list(families.keys())[0]
    match = re.match(r'^([a-zA-Z]+)', first_family)
    base_prefix = match.group(1) if match else first_family

    # Check which families are installed
    installed_families = get_installed_families(base_prefix)

    # Filter providers to only families that are installed
    matching_families = {f: p for f, p in families.items() if f in installed_families}

    # Case 1: Only one family provides this
    if len(families) == 1:
        family_name = list(families.keys())[0]
        provider_name = families[family_name][0]['name']

        # Check if this family conflicts with installed families
        if installed_families and family_name not in installed_families:
            installed_str = ', '.join(sorted(installed_families))
            print(f"\nWarning: '{pkg_name}' is only provided by {provider_name}")
            print(f"         but you have {installed_str} installed.")
            print("         This will likely cause conflicts!")
            if auto:
                print("Aborting (use explicit package name to force)")
                return []
            try:
                answer = input("\nInstall anyway? [y/N] ").strip()
                if answer.lower() not in ('y', 'yes'):
                    return []
            except (EOFError, KeyboardInterrupt):
                return []

        return [provider_name]

    # Case 2: Multiple families but none installed
    if not matching_families:
        sorted_families = sorted(families.keys(), reverse=True)
        if auto:
            # Use newest version
            return [families[sorted_families[0]][0]['name']]
        # Interactive: ask user
        print(f"\nMultiple providers for '{pkg_name}':")
        for i, fam in enumerate(sorted_families, 1):
            print(f"  {i}) {families[fam][0]['name']}")
        print(f"  {len(sorted_families) + 1}) All")

        try:
            choice = input("\nChoice [1]: ").strip() or "1"
            if choice == str(len(sorted_families) + 1):
                return [families[f][0]['name'] for f in sorted_families]
            idx = int(choice) - 1
            if 0 <= idx < len(sorted_families):
                return [families[sorted_families[idx]][0]['name']]
        except (ValueError, EOFError):
            pass
        return [families[sorted_families[0]][0]['name']]

    # Case 3: One installed family matches
    if len(matching_families) == 1:
        family_name = list(matching_families.keys())[0]
        return [matching_families[family_name][0]['name']]

    # Case 4: Multiple installed families match
    if install_all:
        return [matching_families[f][0]['name'] for f in matching_families]

    if auto:
        # Strict mode: use newest installed family
        sorted_installed = sorted(matching_families.keys(), reverse=True)
        return [matching_families[sorted_installed[0]][0]['name']]

    # Interactive: ask user
    sorted_families = sorted(matching_families.keys(), reverse=True)
    print(f"\nMultiple installed families provide '{pkg_name}':")
    for i, fam in enumerate(sorted_families, 1):
        print(f"  {i}) {matching_families[fam][0]['name']}")
    print(f"  {len(sorted_families) + 1}) All")

    try:
        choice = input("\nChoice [1]: ").strip() or "1"
        if choice == str(len(sorted_families) + 1):
            return [matching_families[f][0]['name'] for f in sorted_families]
        idx = int(choice) - 1
        if 0 <= idx < len(sorted_families):
            return [matching_families[sorted_families[idx]][0]['name']]
    except (ValueError, EOFError):
        pass
    return [matching_families[sorted_families[0]][0]['name']]


# Backwards compatibility aliases (with underscore prefix)
_extract_pkg_name = extract_pkg_name
_extract_family = extract_family
_get_installed_families = get_installed_families
_resolve_virtual_package = resolve_virtual_package
