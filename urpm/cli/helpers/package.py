"""Package name extraction and virtual package resolution helpers."""

import platform
import re
import subprocess
from typing import TYPE_CHECKING

from ...core.synthesis import parse_nevra
from ...i18n import _, confirm_yes

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


_KNOWN_ARCHES = {"x86_64", "i586", "i686", "noarch", "aarch64", "armv7hl"}


def system_arch() -> str:
    """Return the host architecture (e.g. 'x86_64')."""
    return platform.machine()


def resolve_target_arch(args) -> str:
    """Return args.arch if set, else the host arch."""
    return getattr(args, 'arch', None) or system_arch()


def extract_pkg_name(package: str) -> str:
    """Extract package name from a NEVRA string.

    A genuine NEVRA always ends with an explicit ``.arch`` suffix
    (``.x86_64``, ``.noarch``, ``.i586``, ``.aarch64``, ``.armv7hl``,
    ``.i686``).  Without that suffix, the input is treated as a literal
    package name — even if it contains hyphens followed by digits.

    This is critical for Mageia conventions where the package Name
    itself can embed numeric suffixes:

    * SONAME-versioned libraries: ``lib64polkit1-devel-127``
      (Name=``lib64polkit1-devel-127``, not ``lib64polkit1-devel``).
    * Date-versioned packages: ``xmltex-20020625``.
    * Singleton-versioned packages: ``lua-rpm-macros-1``.
    * Kernel packages: ``kernel-desktop-devel-6.18.22-1.mga10``
      (when passed without ``.arch``, it is the kernel Name itself).

    Mirrors :func:`urpm.core.resolver._is_nevra` semantics so the CLI
    helper and the resolver agree on what constitutes a NEVRA.

    Args:
        package: Either a simple name like ``firefox`` or a NEVRA like
            ``firefox-120.0-1.mga10.x86_64``.

    Returns:
        The package name.  For a literal name (no ``.arch`` suffix),
        the input is returned unchanged.
    """
    # Don't try to extract NEVRA from virtual packages (pkgconfig, etc.)
    # These have parentheses and the version inside is part of the name
    if '(' in package:
        return package
    # Globs and version constraints are never NEVRAs
    if any(c in package for c in ('*', '?', '[', ' ')):
        return package
    # A real NEVRA must end with ".arch".  Without an explicit known
    # arch suffix the input is a literal package name and must be
    # returned as-is, even if it contains hyphen-digit patterns.
    dot_pos = package.rfind('.')
    if dot_pos < 0 or package[dot_pos + 1:] not in _KNOWN_ARCHES:
        return package
    # Strip ".arch", then split off "release" (last "-...") and
    # "version" (next-to-last "-...") to get the name.
    head = package[:dot_pos]
    if head.count('-') < 2:
        return package
    name, _ver, _rel = head.rsplit('-', 2)
    return name


def pick_arch_for_lookup(pkg: str, target_arch: str) -> str:
    """Pick the architecture hint for a DB lookup of ``pkg``.

    On a multi-arch system (e.g. ``x86_64`` host with 32-bit media
    enabled), looking up a plain name like ``lib64fuse2`` without an
    arch filter risks returning a foreign-arch row from SQLite (the
    SQL ``ORDER BY`` does not pin the arch). This helper centralises
    the rule used by ``cmd_install``:

    * If ``pkg`` is an explicit NEVRA whose name strips down to
      something different (i.e. it really has a ``.arch`` suffix),
      its own arch wins — the user typed it explicitly.
    * Otherwise the caller-supplied ``target_arch`` (typically
      :func:`resolve_target_arch` of ``args``) is used.

    Args:
        pkg: Raw user input — either a plain name like ``firefox`` or
            a NEVRA like ``firefox-120.0-1.mga10.x86_64``.
        target_arch: Default arch to use for plain names. Should never
            be empty; callers compute it via :func:`resolve_target_arch`.

    Returns:
        The architecture string to pass as ``arch=`` to
        :meth:`PackageDatabase.get_package` (and downstream helpers).
    """
    pkg_name = extract_pkg_name(pkg)
    if pkg_name == pkg:
        # Plain name, no NEVRA suffix detected.
        return target_arch
    # `pkg` is a NEVRA — trust the arch baked into it.
    nevra_arch = parse_nevra(pkg)[3]
    return nevra_arch or target_arch


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


def resolve_virtual_package(db: 'PackageDatabase', pkg_name: str, auto: bool, install_all: bool, arch: 'str | None' = None) -> list:
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
        arch: Optional architecture hint. When provided, the SQLite lookup
            for the (possibly real) ``pkg_name`` row is restricted to this
            arch (and ``noarch``). On multi-arch systems (e.g. host
            ``x86_64`` with 32-bit media enabled), this prevents
            ``get_package(pkg_name)`` from returning the ``i686`` row when
            the user asked for ``x86_64`` — which would otherwise leak
            ``arch='i686'`` into the providers list and let the resolver
            install a 32-bit package in place of the expected 64-bit one
            under ``--allow-arch i686``. ``None`` keeps legacy behaviour.

    Returns:
        List of concrete package names to install, or empty list to abort
    """
    # Check if pkg_name is a real package (not just a capability).
    # The arch hint guards against multi-arch row leakage (see docstring).
    real_pkg = db.get_package(pkg_name, arch=arch)

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
            print("\n" + _("Warning: '{pkg}' is only provided by {provider}").format(pkg=pkg_name, provider=provider_name))
            print("         " + _("but you have {installed} installed.").format(installed=installed_str))
            print("         " + _("This will likely cause conflicts!"))
            if auto:
                print(_("Aborting (use explicit package name to force)"))
                return []
            try:
                answer = input("\n" + _("Install anyway? [y/N] ")).strip()
                if not confirm_yes(answer):
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
        print("\n" + _("Multiple providers for '{pkg}':").format(pkg=pkg_name))
        for i, fam in enumerate(sorted_families, 1):
            print(f"  {i}) {families[fam][0]['name']}")
        print(f"  {len(sorted_families) + 1}) {_('All')}")

        try:
            choice = input("\n" + _("Choice [1]: ")).strip() or "1"
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
    print("\n" + _("Multiple installed families provide '{pkg}':").format(pkg=pkg_name))
    for i, fam in enumerate(sorted_families, 1):
        print(f"  {i}) {matching_families[fam][0]['name']}")
    print(f"  {len(sorted_families) + 1}) {_('All')}")

    try:
        choice = input("\n" + _("Choice [1]: ")).strip() or "1"
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
