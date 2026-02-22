"""Package installation and download commands.

TODO: Add --config-policy=merge for interactive diff/merge of config files
"""

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.package import (
    extract_pkg_name as _extract_pkg_name,
)
from ..helpers.debug import (
    DEBUG_LAST_INSTALLED_DEPS,
    DEBUG_PREV_INSTALLED_DEPS,
    write_debug_file as _write_debug_file,
    clear_debug_file as _clear_debug_file,
    copy_installed_deps_list as _copy_installed_deps_list,
    notify_urpmd_cache_invalidate as _notify_urpmd_cache_invalidate,
)
from ..helpers.resolver import (
    create_resolver as _create_resolver,
)
from ..helpers.alternatives import (
    PreferencesMatcher,
    _resolve_with_alternatives,
)
from ..helpers.package import (
    resolve_virtual_package as _resolve_virtual_package,
)

# Debug flag for install operations
DEBUG_INSTALL = False


def _apply_config_policy(rpmnew_files: List[str], policy: str) -> int:
    """Apply config policy to .rpmnew files created during this transaction.

    Args:
        rpmnew_files: List of .rpmnew file paths created during install
        policy: 'keep' (do nothing), 'replace' (use new configs), 'ask' (prompt)

    Returns:
        Number of config files processed
    """
    from .. import colors

    if policy == 'keep' or not rpmnew_files:
        return 0

    processed = 0

    for rpmnew in rpmnew_files:
        rpmnew_path = Path(rpmnew)
        if not rpmnew_path.exists():
            continue

        original = rpmnew_path.with_suffix('')  # Remove .rpmnew suffix
        rpmold = Path(str(original) + '.rpmold')

        if policy == 'replace':
            try:
                if original.exists():
                    original.rename(rpmold)
                rpmnew_path.rename(original)
                processed += 1
            except OSError as e:
                print(colors.warning(f"  Config: failed to replace {original}: {e}"))

        elif policy == 'ask':
            print(f"\n  Config conflict: {original}")
            print(f"    [k] Keep existing (new saved as .rpmnew)")
            print(f"    [r] Replace with new (old saved as .rpmold)")
            print(f"    [d] Show diff")

            while True:
                choice = input("  Choice [k/r/d]: ").strip().lower()
                if choice == 'k':
                    break
                elif choice == 'r':
                    try:
                        if original.exists():
                            original.rename(rpmold)
                        rpmnew_path.rename(original)
                        processed += 1
                    except OSError as e:
                        print(colors.warning(f"  Failed to replace: {e}"))
                    break
                elif choice == 'd':
                    subprocess.run(['diff', '-u', str(original), str(rpmnew_path)])
                else:
                    print("  Invalid choice")

    if processed > 0 and policy == 'replace':
        print(f"  {processed} config file(s) updated (old saved as .rpmold)")

    return processed


def cmd_install(args, db: 'PackageDatabase') -> int:
    """Handle install command."""
    import signal
    import solv
    from ...core.resolver import Resolver, Resolution, format_size, set_solver_debug, PackageAction, TransactionType
    from ...core.operations import PackageOperations, InstallOptions
    from ...core.background_install import (
        check_background_error, clear_background_error,
        InstallLock
    )
    from .. import colors

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    watched_pkgs = getattr(args, 'watched', None)
    if watched_pkgs:
        watched_pkgs = [p.strip() for p in watched_pkgs.split(',')]
    if debug_solver or watched_pkgs:
        set_solver_debug(enabled=debug_solver, watched=watched_pkgs)

    # Check for previous background install errors
    prev_error = check_background_error()
    if prev_error:
        print(colors.warning(f"Warning: Previous background operation had an error:"))
        print(colors.warning(f"  {prev_error}"))
        print(colors.dim("  (This message will not appear again)"))
        clear_background_error()

    # Debug: save previous state and clear debug files at start
    _copy_installed_deps_list(dest=DEBUG_PREV_INSTALLED_DEPS)
    _clear_debug_file(DEBUG_LAST_INSTALLED_DEPS)

    # Check --nodeps flag
    nodeps = getattr(args, 'nodeps', False)
    download_only = getattr(args, 'download_only', False)
    if nodeps and not download_only:
        print(colors.error("Error: --nodeps requires --download-only"))
        return 1

    # Check root privileges early (unless allowed to skip for mkimage)
    from ...core.install import check_root
    allow_no_root = getattr(args, 'allow_no_root', False)
    if not download_only and not allow_no_root and not check_root():
        print(colors.error("Error: root privileges required for installation"))
        print("Try: sudo urpm install <packages>")
        return 1

    # Handle --builddeps option (install build dependencies from spec/SRPM)
    builddeps = getattr(args, 'builddeps', None)
    if builddeps:
        from ...core.buildrequires import get_buildrequires, list_specs_in_workdir, rpm_dep_to_solver_format

        try:
            if builddeps == 'AUTO':
                # Auto-detect mode
                specs = list_specs_in_workdir()
                if len(specs) > 1:
                    print(colors.info("Multiple .spec files found:"))
                    for i, spec in enumerate(specs, 1):
                        print(f"  {i}. {spec.name}")
                    if getattr(args, 'auto', False):
                        print(colors.error("Error: Multiple .spec files found. Specify which one to use."))
                        return 1
                    try:
                        choice = input("Select spec file (number): ").strip()
                        idx = int(choice) - 1
                        if 0 <= idx < len(specs):
                            builddeps = str(specs[idx])
                        else:
                            print(colors.error("Invalid choice"))
                            return 1
                    except (ValueError, KeyboardInterrupt):
                        print("\nAborted.")
                        return 1
                else:
                    builddeps = 'AUTO'

            target = None if builddeps == 'AUTO' else builddeps
            reqs, source = get_buildrequires(target)
            print(colors.info(f"Build dependencies from: {source}"))
            print(f"  Found {len(reqs)} BuildRequires")

            # Replace packages list with build requirements (convert to solver format)
            args.packages = [rpm_dep_to_solver_format(req) for req in reqs]

        except FileNotFoundError as e:
            print(colors.error(f"Error: {e}"))
            return 1
        except ValueError as e:
            print(colors.error(f"Error: {e}"))
            return 1

    # Check that we have something to install
    if not args.packages and not builddeps:
        print(colors.error("Error: No packages specified"))
        print("Usage: urpm install <packages> or urpm install --builddeps <spec>")
        return 1

    # Separate local RPM files from package names
    from pathlib import Path
    from ...core.rpm import is_local_rpm, read_rpm_header
    from ...core.download import verify_rpm_signature

    local_rpm_paths = []
    local_rpm_infos = []
    package_names = []
    verify_sigs = not getattr(args, 'nosignature', False)

    for pkg in args.packages:
        if is_local_rpm(pkg):
            path = Path(pkg)
            if not path.exists():
                print(colors.error(f"Error: file not found: {pkg}"))
                return 1
            # Read RPM header
            info = read_rpm_header(path)
            if not info:
                print(colors.error(f"Error: cannot read RPM file: {pkg}"))
                return 1
            # Verify signature
            if verify_sigs:
                valid, error = verify_rpm_signature(path)
                if not valid:
                    print(colors.error(f"Error: signature verification failed for {pkg}"))
                    print(colors.error(f"  {error}"))
                    print(colors.dim("  Use --nosignature to skip verification (not recommended)"))
                    return 1
            local_rpm_paths.append(str(path.resolve()))
            local_rpm_infos.append(info)
        else:
            package_names.append(pkg)

    # Scan directories of local RPMs for sibling packages (potential dependencies)
    # Also scan sibling architecture directories (e.g., ../x86_64/, ../noarch/)
    sibling_rpm_infos = []
    if local_rpm_paths:
        scanned_dirs = set()
        explicit_paths = set(local_rpm_paths)
        for rpm_path in local_rpm_paths:
            parent_dir = Path(rpm_path).parent
            # Collect directories to scan: current + sibling arch dirs
            dirs_to_scan = [parent_dir]
            # Check if parent looks like RPMS/<arch>/ structure
            grandparent = parent_dir.parent
            if grandparent.name in ('RPMS', 'rpms'):
                # Scan all arch subdirectories
                for arch_dir in grandparent.iterdir():
                    if arch_dir.is_dir() and arch_dir not in dirs_to_scan:
                        dirs_to_scan.append(arch_dir)
            # Scan all directories
            for scan_dir in dirs_to_scan:
                if scan_dir in scanned_dirs:
                    continue
                scanned_dirs.add(scan_dir)
                # Find all .rpm files in this directory
                for rpm_file in scan_dir.glob('*.rpm'):
                    # Skip already-explicit RPMs and debuginfo/debugsource
                    if str(rpm_file.resolve()) in explicit_paths:
                        continue
                    if '-debuginfo-' in rpm_file.name or '-debugsource-' in rpm_file.name:
                        continue
                    # Read header
                    info = read_rpm_header(rpm_file)
                    if info:
                        sibling_rpm_infos.append(info)
        if sibling_rpm_infos:
            print(colors.dim(f"Found {len(sibling_rpm_infos)} sibling RPMs (available for dependencies)"))

    # If we have local RPMs, show what we're installing
    if local_rpm_infos:
        print(f"Local RPM files ({len(local_rpm_infos)}):")
        for info in local_rpm_infos:
            print(f"  {info['nevra']}")

    # Resolve virtual packages to concrete packages
    # This handles cases like php-opcache → php8.5-opcache based on what's installed
    auto_mode = getattr(args, 'auto', False)
    install_all = getattr(args, 'all', False)

    resolved_packages = []
    # Initialize choices dict early to track virtual package resolutions
    # This prevents the resolver from asking again about already-resolved providers
    choices = {}
    # Add local RPM names to the list
    for info in local_rpm_infos:
        resolved_packages.append(info['name'])
    # Resolve virtual packages from command line
    for pkg in package_names:
        pkg_name = _extract_pkg_name(pkg)
        concrete = _resolve_virtual_package(db, pkg_name, auto_mode, install_all)
        resolved_packages.extend(concrete)
        # Record the choice so resolver doesn't ask again for this capability
        # Only record if single provider was selected (not "All")
        if len(concrete) == 1 and concrete[0] != pkg_name:
            choices[pkg_name] = concrete[0]

    # Remove duplicates while preserving order
    seen = set()
    unique_packages = []
    for p in resolved_packages:
        if p.lower() not in seen:
            seen.add(p.lower())
            unique_packages.append(p)
    resolved_packages = unique_packages

    if not resolved_packages:
        print("Aborted.")
        return 1

    from ...core.resolver import InstallReason

    # Get CLI options for recommends/suggests
    without_recommends = getattr(args, 'without_recommends', False)
    with_suggests = getattr(args, 'with_suggests', False)

    # Parse --prefer using PreferencesMatcher
    prefer_str = getattr(args, 'prefer', None)
    preferences = PreferencesMatcher(prefer_str)

    # Determine initial recommends behavior:
    # - Auto mode: no recommends (never ask)
    # - Interactive mode: yes unless --without-recommends (will ask user)
    if args.auto:
        initial_recommends = False
    else:
        initial_recommends = not without_recommends

    resolver = _create_resolver(db, args, install_recommends=initial_recommends)
    # choices dict was initialized earlier (line ~4818) with virtual package resolutions

    # Add local RPMs to resolver pool before resolution
    if local_rpm_infos:
        resolver.add_local_rpms(local_rpm_infos)
    # Add sibling RPMs as potential dependency sources
    if sibling_rpm_infos:
        resolver.add_local_rpms(sibling_rpm_infos)

    if nodeps:
        # --nodeps: build actions directly without dependency resolution
        from ...core.resolver import PackageAction, TransactionType, Resolution
        actions = []
        not_found = []
        for pkg_spec in resolved_packages:
            pkg = db.get_package_smart(pkg_spec)
            if not pkg:
                not_found.append(pkg_spec)
                continue
            media = db.get_media_by_id(pkg['media_id'])
            media_name = media.get('name', 'unknown') if media else 'unknown'
            epoch = pkg.get('epoch', 0) or 0
            evr = f"{epoch}:{pkg['version']}-{pkg['release']}" if epoch else f"{pkg['version']}-{pkg['release']}"
            actions.append(PackageAction(
                action=TransactionType.INSTALL,
                name=pkg['name'],
                evr=evr,
                arch=pkg['arch'],
                nevra=pkg['nevra'],
                size=pkg.get('filesize', 0) or 0,
                media_name=media_name,
                reason=InstallReason.EXPLICIT
            ))
        if not_found:
            print(colors.error(f"Packages not found ({len(not_found)}):"))
            for p in not_found[:10]:
                print(f"  {p}")
            if len(not_found) > 10:
                print(f"  ... and {len(not_found) - 10} more")
            return 1
        result = Resolution(success=True, actions=actions, problems=[])
        aborted = False
    else:
        # Normal resolution with user choices for alternatives
        # Build set of local package names for SOLVER_UPDATE
        local_pkg_names = {info['name'] for info in local_rpm_infos}
        result, aborted = _resolve_with_alternatives(
            resolver, resolved_packages, choices, args.auto, preferences,
            local_packages=local_pkg_names
        )
    if aborted:
        return 1

    if not result.success:
        print("Resolution failed:")
        for p in result.problems:
            print(f"  {p}")
        return 1

    # Handle --reinstall for local RPMs that are already installed at same version
    reinstall_mode = getattr(args, 'reinstall', False)
    if reinstall_mode and local_rpm_infos:
        from ...core.resolver import PackageAction, TransactionType
        actions_names = {a.name for a in result.actions}
        for info in local_rpm_infos:
            if info['name'] not in actions_names:
                # Package not in actions = already installed at same version
                # Add as REINSTALL action
                epoch = info.get('epoch', 0) or 0
                evr = f"{epoch}:{info['version']}-{info['release']}" if epoch else f"{info['version']}-{info['release']}"
                reinstall_action = PackageAction(
                    action=TransactionType.REINSTALL,
                    name=info['name'],
                    evr=evr,
                    arch=info['arch'],
                    nevra=info['nevra'],
                    size=info.get('filesize', 0) or 0,
                    media_name='@LocalRPMs',
                    reason=InstallReason.EXPLICIT
                )
                result.actions.append(reinstall_action)

    if not result.actions:
        print("Nothing to do")
        return 0

    # Categorize packages by install reason
    rec_pkgs = [a for a in result.actions if a.reason == InstallReason.RECOMMENDED]

    # Find available suggests only if --with-suggests is specified
    # Iterate to find suggests of suggests (e.g., digikam -> marble -> marble-qt)
    all_to_install = [a.name for a in result.actions]
    if with_suggests:
        suggests = []
        suggest_alternatives = []
        packages_to_check = all_to_install[:]
        checked_packages = set(p.lower() for p in all_to_install)
        max_iterations = 10  # Safety limit against infinite loops

        for _iteration in range(max_iterations):
            new_suggests, new_alternatives = resolver.find_available_suggests(
                packages_to_check, choices=choices, resolved_packages=list(checked_packages)
            )

            if not new_suggests and not new_alternatives:
                break

            # Handle alternatives for this iteration
            new_packages_from_alternatives = []

            if new_alternatives and not args.auto:
                for alt in new_alternatives:
                    if alt.capability in choices:
                        continue

                    # Filter providers based on preferences
                    filtered = preferences.filter_providers(alt.providers)

                    # If only one after filtering, auto-select
                    if len(filtered) == 1:
                        chosen_pkg = filtered[0]
                        choices[alt.capability] = chosen_pkg
                        sel = resolver.pool.select(chosen_pkg, solv.Selection.SELECTION_NAME)
                        for s in sel.solvables():
                            if s.repo and s.repo.name != '@System':
                                from ...core.resolver import InstallReason
                                pkg_action = PackageAction(
                                    action=TransactionType.INSTALL,
                                    name=s.name,
                                    evr=s.evr,
                                    arch=s.arch,
                                    nevra=f"{s.name}-{s.evr}.{s.arch}",
                                    size=s.size,
                                    media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                    reason=InstallReason.SUGGESTED,
                                )
                                if s.name.lower() not in checked_packages:
                                    new_suggests.append(pkg_action)
                                    new_packages_from_alternatives.append(s.name)
                                break
                        continue

                    # Ask user to choose
                    print(f"\n{alt.capability} ({alt.required_by}):")
                    for i, provider in enumerate(filtered, 1):
                        print(f"  {i}) {provider}")
                    print(f"  {len(filtered) + 1}) All")

                    try:
                        choice = input(f"\nChoice [1]: ").strip() or "1"
                        if choice == str(len(filtered) + 1):
                            # "All" selected - add all providers
                            for prov_name in filtered:
                                choices[alt.capability] = prov_name
                                sel = resolver.pool.select(prov_name, solv.Selection.SELECTION_NAME)
                                for s in sel.solvables():
                                    if s.repo and s.repo.name != '@System':
                                        from ...core.resolver import InstallReason
                                        pkg_action = PackageAction(
                                            action=TransactionType.INSTALL,
                                            name=s.name,
                                            evr=s.evr,
                                            arch=s.arch,
                                            nevra=f"{s.name}-{s.evr}.{s.arch}",
                                            size=s.size,
                                            media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                            reason=InstallReason.SUGGESTED,
                                        )
                                        if s.name.lower() not in checked_packages:
                                            new_suggests.append(pkg_action)
                                            new_packages_from_alternatives.append(s.name)
                                        break
                        else:
                            idx = int(choice) - 1
                            if 0 <= idx < len(filtered):
                                chosen_pkg = filtered[idx]
                                choices[alt.capability] = chosen_pkg
                                sel = resolver.pool.select(chosen_pkg, solv.Selection.SELECTION_NAME)
                                for s in sel.solvables():
                                    if s.repo and s.repo.name != '@System':
                                        from ...core.resolver import InstallReason
                                        pkg_action = PackageAction(
                                            action=TransactionType.INSTALL,
                                            name=s.name,
                                            evr=s.evr,
                                            arch=s.arch,
                                            nevra=f"{s.name}-{s.evr}.{s.arch}",
                                            size=s.size,
                                            media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                            reason=InstallReason.SUGGESTED,
                                        )
                                        if s.name.lower() not in checked_packages:
                                            new_suggests.append(pkg_action)
                                            new_packages_from_alternatives.append(s.name)
                                        break
                    except (ValueError, EOFError, KeyboardInterrupt):
                        print("\nAborted")
                        return 1

            elif new_alternatives and args.auto:
                # Auto mode: select first provider (already sorted by missing deps count)
                for alt in new_alternatives:
                    if alt.capability in choices:
                        continue

                    filtered = preferences.filter_providers(alt.providers)
                    if not filtered:
                        continue

                    chosen_pkg = filtered[0]
                    choices[alt.capability] = chosen_pkg

                    sel = resolver.pool.select(chosen_pkg, solv.Selection.SELECTION_NAME)
                    for s in sel.solvables():
                        if s.repo and s.repo.name != '@System':
                            from ...core.resolver import InstallReason
                            pkg_action = PackageAction(
                                action=TransactionType.INSTALL,
                                name=s.name,
                                evr=s.evr,
                                arch=s.arch,
                                nevra=f"{s.name}-{s.evr}.{s.arch}",
                                size=s.size,
                                media_name=resolver._solvable_to_pkg.get(s.id, {}).get('media_name', ''),
                                reason=InstallReason.SUGGESTED,
                            )
                            if s.name.lower() not in checked_packages:
                                new_suggests.append(pkg_action)
                                new_packages_from_alternatives.append(s.name)
                            break

            # Collect new suggests (not already checked)
            next_packages = []
            for s in new_suggests:
                if s.name.lower() not in checked_packages:
                    suggests.append(s)
                    checked_packages.add(s.name.lower())
                    next_packages.append(s.name)

                    # Also resolve dependencies of this suggest to check their suggests
                    # e.g., konq-plugins requires konqueror, konqueror suggests konqueror-handbook
                    sel = resolver.pool.select(s.name, solv.Selection.SELECTION_NAME)
                    for solv_pkg in sel.solvables():
                        if solv_pkg.repo and solv_pkg.repo.name != '@System':
                            for dep in solv_pkg.lookup_deparray(solv.SOLVABLE_REQUIRES):
                                dep_str = str(dep).split()[0]
                                if dep_str.startswith(('rpmlib(', '/', 'config(')):
                                    continue
                                # Find provider of this dependency
                                dep_obj = resolver.pool.Dep(dep_str)
                                for provider in resolver.pool.whatprovides(dep_obj):
                                    if provider.repo and provider.repo.name != '@System':
                                        if provider.name.lower() not in checked_packages:
                                            checked_packages.add(provider.name.lower())
                                            next_packages.append(provider.name)
                                        break
                            break

            # Add packages from alternatives to next check
            for pkg_name in new_packages_from_alternatives:
                if pkg_name.lower() not in checked_packages:
                    checked_packages.add(pkg_name.lower())
                    next_packages.append(pkg_name)

            # Next iteration: check newly found suggests
            packages_to_check = next_packages
            if not packages_to_check:
                break
    else:
        suggests = []
        suggest_alternatives = []

    # Calculate sizes for initial display
    rec_size = sum(a.size for a in rec_pkgs)
    sug_size = sum(a.size for a in suggests)

    # Determine final recommends/suggests behavior
    install_recommends_final = initial_recommends
    install_suggests = with_suggests

    # In interactive mode: ask about recommends (unless --without-recommends)
    if rec_pkgs and not args.auto and not without_recommends:
        print(f"\n{colors.success(f'Recommended packages ({len(rec_pkgs)})')} - {format_size(rec_size)}")
        from .. import display
        rec_names = [f"{a.name}-{a.evr}" for a in rec_pkgs]
        display.print_package_list(rec_names, max_lines=5)
        try:
            answer = input(f"\nInstall recommended packages? [Y/n] ")
            install_recommends_final = answer.lower() not in ('n', 'no')
        except EOFError:
            print("\nAborted")
            return 1

    # In interactive mode with --with-suggests: ask about suggests
    if suggests and not args.auto:
        print(f"\n{colors.warning(f'Suggested packages ({len(suggests)})')} - {format_size(sug_size)}")
        from .. import display
        sug_names = [f"{a.name}-{a.evr}" for a in suggests]
        display.print_package_list(sug_names, max_lines=5)
        try:
            answer = input(f"\nInstall suggested packages? [Y/n] ")
            install_suggests = answer.lower() not in ('n', 'no')
        except EOFError:
            print("\nAborted")
            return 1

    # Re-resolve with final preferences (recommends + suggests)
    need_reresolve = False
    final_packages = list(resolved_packages)

    if not install_recommends_final and rec_pkgs:
        need_reresolve = True

    if install_suggests and suggests:
        suggest_names = [s.name for s in suggests]
        final_packages = resolved_packages + suggest_names
        need_reresolve = True

    if need_reresolve:
        resolver = _create_resolver(db, args, install_recommends=install_recommends_final)
        if local_rpm_infos:
            resolver.add_local_rpms(local_rpm_infos)
        if sibling_rpm_infos:
            resolver.add_local_rpms(sibling_rpm_infos)
        result, aborted = _resolve_with_alternatives(
            resolver, final_packages, choices, args.auto, preferences,
            local_packages=local_pkg_names
        )
        if aborted:
            return 1

        # If resolution failed and we have suggests, try removing problematic suggests
        skipped_suggests = {}  # suggest_name -> reason
        if not result.success and install_suggests and suggests:
            suggest_names_set = set(suggest_names)

            # Find suggests mentioned in problems and store the reason
            for prob in result.problems:
                prob_str = str(prob)
                for sug_name in suggest_names:
                    if sug_name in prob_str:
                        skipped_suggests[sug_name] = prob_str

            # If we found problematic suggests, retry without them
            if skipped_suggests:
                remaining_suggests = [s for s in suggest_names if s not in skipped_suggests]
                retry_packages = resolved_packages + remaining_suggests

                # Retry resolution
                resolver = _create_resolver(db, args, install_recommends=install_recommends_final)
                if local_rpm_infos:
                    resolver.add_local_rpms(local_rpm_infos)
                if sibling_rpm_infos:
                    resolver.add_local_rpms(sibling_rpm_infos)
                result, aborted = _resolve_with_alternatives(
                    resolver, retry_packages, choices, args.auto, preferences,
                    local_packages=local_pkg_names
                )
                if aborted:
                    return 1

                # Update suggest_names for marking below
                suggest_names = remaining_suggests

        if not result.success:
            print("Resolution failed:")
            for p in result.problems:
                print(f"  {p}")
            return 1

        # Show skipped suggests with reasons
        if skipped_suggests:
            from .. import colors
            print(f"\n{colors.warning('Skipped suggests:')}")
            for sug in sorted(skipped_suggests.keys()):
                reason = skipped_suggests[sug]
                print(f"  {colors.dim(sug)}: {reason}")

        # Mark the suggest packages with the right reason
        if install_suggests and suggests:
            for action in result.actions:
                if action.name in suggest_names:
                    action.reason = InstallReason.SUGGESTED

    final_actions = list(result.actions)

    # Separate packages being removed (obsoleted) from packages being installed
    remove_pkgs = [a for a in final_actions if a.action == TransactionType.REMOVE]
    install_actions = [a for a in final_actions if a.action != TransactionType.REMOVE]

    # Categorize install packages by install reason
    explicit_pkgs = [a for a in install_actions if a.reason == InstallReason.EXPLICIT]
    dep_pkgs = [a for a in install_actions if a.reason == InstallReason.DEPENDENCY]
    rec_pkgs = [a for a in install_actions if a.reason == InstallReason.RECOMMENDED]
    sug_pkgs = [a for a in install_actions if a.reason == InstallReason.SUGGESTED]

    # Build set of explicit package names for history recording
    explicit_names = set(a.name.lower() for a in explicit_pkgs)

    # Calculate final sizes
    explicit_size = sum(a.size for a in explicit_pkgs)
    dep_size = sum(a.size for a in dep_pkgs)
    rec_size = sum(a.size for a in rec_pkgs)
    sug_size = sum(a.size for a in sug_pkgs)
    total_size = sum(a.size for a in final_actions if a.action.value in ('install', 'upgrade', 'reinstall'))

    # Show final transaction summary
    print(f"\n{colors.bold('Transaction summary:')}\n")
    from .. import display

    if explicit_pkgs:
        print(f"  {colors.info(f'Requested ({len(explicit_pkgs)})')} - {format_size(explicit_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in explicit_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if dep_pkgs:
        print(f"  {colors.dim(f'Dependencies ({len(dep_pkgs)})')} - {format_size(dep_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in dep_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if rec_pkgs:
        print(f"  {colors.success(f'Recommended ({len(rec_pkgs)})')} - {format_size(rec_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in rec_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if sug_pkgs:
        print(f"  {colors.warning(f'Suggested ({len(sug_pkgs)})')} - {format_size(sug_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in sug_pkgs]
        display.print_package_list(pkg_names, indent=4)

    if remove_pkgs:
        remove_size = sum(a.size for a in remove_pkgs)
        print(f"  {colors.error(f'Obsoleted ({len(remove_pkgs)})')} - {format_size(remove_size)}")
        pkg_names = [f"{a.name}-{a.evr}" for a in remove_pkgs]
        display.print_package_list(pkg_names, indent=4)

    # Final confirmation
    if remove_pkgs:
        print(f"\n{colors.bold(f'Total: {len(install_actions)} to install, {len(remove_pkgs)} to remove')} ({format_size(total_size)})")
    else:
        print(f"\n{colors.bold(f'Total: {len(install_actions)} packages')} ({format_size(total_size)})")

    if not args.auto:
        try:
            answer = input("\nProceed with installation? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Update result.actions with final list
    result = Resolution(
        success=True,
        actions=final_actions,
        problems=[],
        install_size=total_size
    )

    if args.test:
        print("\n(dry run - no changes made)")
        return 0

    # Build download items (skip local RPMs - we already have them)
    ops = PackageOperations(db)
    download_items, local_action_paths = ops.build_download_items(
        result.actions, resolver, local_rpm_infos
    )

    # Download remote packages (if any)
    dl_results = []
    downloaded = 0
    cached = 0
    peer_stats = {}

    if download_items:
        print(colors.info("\nDownloading packages..."))
        dl_opts = InstallOptions(
            use_peers=not getattr(args, 'no_peers', False),
            only_peers=getattr(args, 'only_peers', False),
        )

        # Multi-line progress display using DownloadProgressDisplay
        from .. import display
        progress_display = display.DownloadProgressDisplay(num_workers=4)

        def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                     item_bytes=None, item_total=None, slots_status=None):
            # Calculate global speed from all active downloads
            global_speed = 0.0
            if slots_status:
                for slot, prog in slots_status:
                    if prog is not None:
                        global_speed += prog.get_speed()

            progress_display.update(
                pkg_num, pkg_total, bytes_done, bytes_total,
                slots_status or [], global_speed
            )

        download_start = time.time()
        dl_results, downloaded, cached, peer_stats = ops.download_packages(
            download_items, options=dl_opts, progress_callback=progress,
            urpm_root=getattr(args, 'urpm_root', None)
        )
        download_elapsed = time.time() - download_start
        progress_display.finish()

        # Check for failures
        failed = [r for r in dl_results if not r.success]
        if failed:
            print(colors.error(f"\n{len(failed)} download(s) failed:"))
            for r in failed[:5]:
                print(f"  {colors.error(r.item.name)}: {r.error}")
            return 1

        # Download summary with P2P stats and timing
        cache_str = colors.warning(str(cached)) if cached > 0 else colors.dim(str(cached))
        from_peers = peer_stats.get('from_peers', 0)
        from_upstream = peer_stats.get('from_upstream', 0)
        time_str = display.format_duration(download_elapsed)
        if from_peers > 0:
            print(f"  {colors.success(f'{downloaded} downloaded')} ({from_peers} from peers, {from_upstream} from mirrors), {cache_str} from cache in {time_str}")
        else:
            print(f"  {colors.success(f'{downloaded} downloaded')}, {cache_str} from cache in {time_str}")

        # Notify urpmd to invalidate cache index (so new downloads are visible to peers)
        if downloaded > 0:
            PackageOperations.notify_urpmd_cache_invalidate()

    # Handle --download-only mode
    download_only = getattr(args, 'download_only', False)
    if download_only:
        print(colors.success("\nPackages downloaded to cache. Use 'urpm install' to install them later."))
        return 0

    # Collect RPM paths for installation (downloaded + local)
    rpm_paths = [r.path for r in dl_results if r.success and r.path]
    rpm_paths.extend(local_action_paths)  # Add local RPM files

    # DEBUG: show what packages are in rpm_paths
    if DEBUG_INSTALL:
        print(colors.dim(f"  DEBUG rpm_paths ({len(rpm_paths)}):"))
        for rp in rpm_paths:
            print(colors.dim(f"    {Path(rp).name}"))

    if not rpm_paths:
        print("No packages to install")
        return 0

    # Begin transaction for history
    cmd_line = "urpm install " + " ".join(args.packages)
    transaction_id = ops.begin_transaction('install', cmd_line, result.actions)

    # Setup Ctrl+C handler
    interrupted = [False]
    original_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        if interrupted[0]:
            # Second Ctrl+C - force abort
            print("\n\nForce abort!")
            ops.abort_transaction(transaction_id)
            signal.signal(signal.SIGINT, original_handler)
            raise KeyboardInterrupt
        else:
            interrupted[0] = True
            print("\n\nInterrupt requested - finishing current package...")
            print("Press Ctrl+C again to force abort (may leave system inconsistent)")

    signal.signal(signal.SIGINT, sigint_handler)

    print(colors.info(f"\nInstalling {len(rpm_paths)} packages..."))

    # Check if another install is in progress
    # Use root path for lock file when installing to chroot
    install_root = getattr(args, 'root', None) or getattr(args, 'urpm_root', None)
    lock = InstallLock(root=install_root)
    if not lock.acquire(blocking=False):
        print(colors.warning("  RPM database is locked by another process."))
        print(colors.dim("  Waiting for lock... (Ctrl+C to cancel)"))

        def wait_cb(pid):
            pass  # Just wait silently, message already shown

        lock.acquire(blocking=True, wait_callback=wait_cb)
    lock.release()  # Release - child will acquire its own lock

    last_shown = [None]

    try:
        from ...core.config import get_rpm_root
        rpm_root = get_rpm_root(getattr(args, 'root', None), getattr(args, 'urpm_root', None))
        install_opts = InstallOptions(
            verify_signatures=not getattr(args, 'nosignature', False),
            force=getattr(args, 'force', False),
            test=getattr(args, 'test', False),
            reinstall=getattr(args, 'reinstall', False),
            noscripts=getattr(args, 'noscripts', False),
            root=rpm_root or "/",
            use_userns=bool(getattr(args, 'allow_no_root', False) and rpm_root),
            sync=getattr(args, 'sync', False),
            config_policy=getattr(args, 'config_policy', 'keep'),
        )

        # Progress callback
        def queue_progress(op_id: str, name: str, current: int, total: int):
            if last_shown[0] != name:
                print(f"\r\033[K  [{current}/{total}] {name}", end='', flush=True)
                last_shown[0] = name

        queue_result = ops.execute_install(
            rpm_paths, options=install_opts, progress_callback=queue_progress
        )

        # Print done
        print(f"\r\033[K  [{len(rpm_paths)}/{len(rpm_paths)}] done")

        if not queue_result.success:
            print(colors.error(f"\nInstallation failed:"))
            if queue_result.operations:
                for err in queue_result.operations[0].errors[:3]:
                    print(f"  {colors.error(err)}")
            elif queue_result.overall_error:
                print(f"  {colors.error(queue_result.overall_error)}")
            ops.abort_transaction(transaction_id)
            return 1

        if interrupted[0]:
            print(colors.warning(f"\n  Installation interrupted"))
            ops.abort_transaction(transaction_id)
            return 130

        installed_count = queue_result.operations[0].count if queue_result.operations else len(rpm_paths)
        if remove_pkgs:
            print(colors.success(f"  {installed_count} packages installed, {len(remove_pkgs)} removed"))
        else:
            print(colors.success(f"  {installed_count} packages installed"))

        # Apply config policy for .rpmnew files
        if queue_result.operations:
            rpmnew_files = queue_result.operations[0].rpmnew_files
            if rpmnew_files:
                _apply_config_policy(rpmnew_files, install_opts.config_policy)

        ops.complete_transaction(transaction_id)

        # Update installed-through-deps.list for urpmi compatibility
        ops.mark_dependencies(resolver, result.actions)
        dep_packages = [a.name for a in result.actions
                        if a.reason != InstallReason.EXPLICIT]
        if dep_packages:
            _write_debug_file(DEBUG_LAST_INSTALLED_DEPS, dep_packages)

        # Debug: copy the installed-through-deps.list for inspection
        _copy_installed_deps_list()

        return 0

    except Exception as e:
        ops.abort_transaction(transaction_id)
        raise
    finally:
        signal.signal(signal.SIGINT, original_handler)


def cmd_download(args, db: 'PackageDatabase') -> int:
    """Handle download command - download packages without installing.

    Downloads packages and their dependencies to the local cache.
    Uses ignore_installed=True to resolve all dependencies, even if already installed.
    """
    import time
    import platform
    from pathlib import Path

    from ...core.resolver import Resolver, Resolution, format_size, set_solver_debug, PackageAction
    from ...core.download import Downloader, DownloadItem
    from ...core.config import get_base_dir
    from .. import colors

    # Set up solver debug if requested
    debug_solver = getattr(args, 'debug', None) in ('solver', 'all')
    watched_pkgs = getattr(args, 'watched', None)
    if watched_pkgs:
        watched_pkgs = [p.strip() for p in watched_pkgs.split(',')]
    if debug_solver or watched_pkgs:
        set_solver_debug(enabled=debug_solver, watched=watched_pkgs)

    # Collect packages to download
    packages = list(args.packages) if args.packages else []

    # Handle --builddeps option
    builddeps = getattr(args, 'builddeps', None)
    if builddeps:
        from ...core.buildrequires import get_buildrequires, list_specs_in_workdir, rpm_dep_to_solver_format

        try:
            if builddeps == 'AUTO':
                # Auto-detect mode
                specs = list_specs_in_workdir()
                if len(specs) > 1:
                    # Multiple specs found - ask user
                    print(colors.info("Multiple .spec files found:"))
                    for i, spec in enumerate(specs, 1):
                        print(f"  {i}. {spec.name}")
                    if getattr(args, 'auto', False):
                        print(colors.error("Error: Multiple .spec files found. Specify which one to use."))
                        return 1
                    try:
                        choice = input("Select spec file (number): ").strip()
                        idx = int(choice) - 1
                        if 0 <= idx < len(specs):
                            builddeps = str(specs[idx])
                        else:
                            print(colors.error("Invalid choice"))
                            return 1
                    except (ValueError, KeyboardInterrupt):
                        print("\nAborted.")
                        return 1
                else:
                    builddeps = 'AUTO'  # Let get_buildrequires handle it

            target = None if builddeps == 'AUTO' else builddeps
            reqs, source = get_buildrequires(target)
            print(colors.info(f"Build dependencies from: {source}"))
            print(f"  Found {len(reqs)} BuildRequires")
            # Convert to solver format and add to packages
            packages.extend(rpm_dep_to_solver_format(req) for req in reqs)

        except FileNotFoundError as e:
            print(colors.error(f"Error: {e}"))
            return 1
        except ValueError as e:
            print(colors.error(f"Error: {e}"))
            return 1

    if not packages:
        print(colors.error("Error: No packages specified"))
        print("Usage: urpm download [packages...] [--builddeps [spec]]")
        return 1

    # Get target release/arch
    target_release = getattr(args, 'release', None)
    target_arch = getattr(args, 'arch', None) or platform.machine()

    # Get CLI options
    without_recommends = getattr(args, 'without_recommends', False)
    nodeps = getattr(args, 'nodeps', False)
    auto_mode = getattr(args, 'auto', False)

    # Show what we're downloading
    print(colors.info(f"\nResolving packages for download..."))
    if target_release:
        print(f"  Target release: {target_release}")
    print(f"  Target arch: {target_arch}")
    print(f"  Packages: {', '.join(packages[:5])}" + (f" ... (+{len(packages)-5} more)" if len(packages) > 5 else ""))

    # Create resolver with ignore_installed=True (resolves all deps)
    resolver = _create_resolver(
        db, args,
        arch=target_arch,
        install_recommends=not without_recommends,
        ignore_installed=True
    )

    if nodeps:
        # --nodeps: download only specified packages, no dependency resolution
        from ...core.resolver import PackageAction, TransactionType, Resolution, InstallReason
        actions = []
        not_found = []

        for pkg_spec in packages:
            # Clean package name (remove version constraints for lookup)
            pkg_name = pkg_spec.split()[0] if ' ' in pkg_spec else pkg_spec
            pkg = db.get_package_smart(pkg_name)
            if not pkg:
                not_found.append(pkg_spec)
                continue

            media = db.get_media_by_id(pkg['media_id'])
            media_name = media.get('name', 'unknown') if media else 'unknown'
            epoch = pkg.get('epoch', 0) or 0
            evr = f"{epoch}:{pkg['version']}-{pkg['release']}" if epoch else f"{pkg['version']}-{pkg['release']}"
            actions.append(PackageAction(
                action=TransactionType.INSTALL,
                name=pkg['name'],
                evr=evr,
                arch=pkg['arch'],
                nevra=pkg['nevra'],
                size=pkg.get('filesize', 0) or 0,
                media_name=media_name,
                reason=InstallReason.EXPLICIT
            ))
            print(f"Insert {pkg['name']} {pkg.get('filesize',0)}")

        if not_found:
            print(colors.error(f"Packages not found ({len(not_found)}):"))
            for p in not_found[:10]:
                print(f"  {p}")
            if len(not_found) > 10:
                print(f"  ... and {len(not_found) - 10} more")
            return 1

        result = Resolution(success=True, actions=actions, problems=[])
    else:
        # Normal resolution with alternatives handling
        result, aborted = _resolve_with_alternatives(resolver, packages, {}, auto_mode)
        if aborted:
            return 1

    if not result.success:
        print(colors.error("Resolution failed:"))
        for p in result.problems:
            print(f"  {p}")
        return 1

    # Filter to only install actions
    install_actions = [a for a in result.actions if a.action.name in ('INSTALL', 'UPGRADE', 'DOWNGRADE')]

    if not install_actions:
        print(colors.success("Nothing to download - all packages already available."))
        return 0

    # Calculate total size
    total_size = sum(a.size for a in install_actions if a.size)

    # Show summary
    print(colors.info(f"\nPackages to download ({len(install_actions)}):"))
    for action in install_actions[:20]:
        size_str = format_size(action.size) if action.size else "?"
        print(f"  {action.nevra} ({size_str})")
    if len(install_actions) > 20:
        print(f"  ... and {len(install_actions) - 20} more")
    print(f"\nTotal download size: {format_size(total_size)}")

    # Confirm unless --auto
    if not auto_mode:
        try:
            confirm = input("\nProceed with download? [Y/n] ").strip().lower()
            if confirm and confirm not in ('y', 'yes', 'o', 'oui'):
                print("Aborted.")
                return 0
        except KeyboardInterrupt:
            print("\nAborted.")
            return 0

    # Build download items
    download_items = []
    media_cache = {}
    servers_cache = {}

    for action in install_actions:
        media_name = action.media_name
        if media_name not in media_cache:
            media = db.get_media(media_name)
            media_cache[media_name] = media
            if media and media.get('id'):
                servers_cache[media['id']] = db.get_servers_for_media(
                    media['id'], enabled_only=True
                )

        media = media_cache[media_name]
        if not media:
            print(f"  Warning: media '{media_name}' not found")
            continue

        # Parse EVR
        evr = action.evr
        if ':' in evr:
            evr = evr.split(':', 1)[1]
        version, release = evr.rsplit('-', 1) if '-' in evr else (evr, '1')

        if media.get('relative_path'):
            servers = servers_cache.get(media['id'], [])
            servers = [dict(s) for s in servers]
            download_items.append(DownloadItem(
                name=action.name,
                version=version,
                release=release,
                arch=action.arch,
                media_id=media['id'],
                relative_path=media['relative_path'],
                is_official=bool(media.get('is_official', 1)),
                servers=servers,
                media_name=media_name,
                size=action.size,
            ))
        elif media.get('url'):
            download_items.append(DownloadItem(
                name=action.name,
                version=version,
                release=release,
                arch=action.arch,
                media_url=media['url'],
                media_name=media_name,
                size=action.size,
            ))
        else:
            print(f"  Warning: no URL or servers for media '{media_name}'")

    if not download_items:
        print(colors.error("No packages to download"))
        return 1

    # Download packages
    print(colors.info("\nDownloading packages..."))
    use_peers = not getattr(args, 'no_peers', False)
    only_peers = getattr(args, 'only_peers', False)
    cache_dir = get_base_dir(urpm_root=getattr(args, 'urpm_root', None))
    downloader = Downloader(cache_dir=cache_dir, use_peers=use_peers, only_peers=only_peers, db=db)

    # Progress display
    from .. import display
    progress_display = display.DownloadProgressDisplay(num_workers=4)

    def progress(name, pkg_num, pkg_total, bytes_done, bytes_total,
                 item_bytes=None, item_total=None, slots_status=None):
        global_speed = 0.0
        if slots_status:
            for slot, prog in slots_status:
                if prog is not None:
                    global_speed += prog.get_speed()
        progress_display.update(
            pkg_num, pkg_total, bytes_done, bytes_total,
            slots_status or [], global_speed
        )

    download_start = time.time()
    dl_results, downloaded, cached, peer_stats = downloader.download_all(download_items, progress)
    download_elapsed = time.time() - download_start
    progress_display.finish()

    # Check for failures
    failed = [r for r in dl_results if not r.success]
    if failed:
        print(colors.error(f"\n{len(failed)} download(s) failed:"))
        for r in failed[:5]:
            print(f"  {colors.error(r.item.name)}: {r.error}")
        return 1

    # Summary
    from_peers = peer_stats.get('from_peers', 0)
    from_upstream = peer_stats.get('from_upstream', 0)
    time_str = display.format_duration(download_elapsed)

    print(f"\n{colors.success('Download complete')}:")
    print(f"  {downloaded} downloaded, {cached} from cache in {time_str}")
    if from_peers > 0:
        print(f"  P2P: {from_peers} from peers, {from_upstream} from upstream")

    # Notify urpmd to invalidate cache index (so new downloads are visible to peers)
    if downloaded > 0:
        _notify_urpmd_cache_invalidate()

    print(colors.success(f"\nPackages saved to cache. Use 'urpm install' to install them."))
    return 0


