"""Mirror management commands."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_mirror_status(args, db: 'PackageDatabase') -> int:
    """Handle mirror status command."""
    from .. import colors
    from ...core.cache import CacheManager, format_size

    # Global proxy status
    enabled = db.is_mirror_enabled()
    disabled_versions = db.get_disabled_mirror_versions()
    global_quota = db.get_mirror_config('global_quota_mb')
    rate_limit = db.get_mirror_config('rate_limit_enabled', '1')

    print(colors.bold("\nMirror Status"))
    print("-" * 40)
    print(f"Mirror mode:      {colors.success('enabled') if enabled else colors.dim('disabled')}")
    if disabled_versions:
        print(f"Disabled versions: {', '.join(disabled_versions)}")
    if global_quota:
        print(f"Global quota:     {global_quota} MB")
    print(f"Rate limiting:    {'on' if rate_limit == '1' else colors.warning('off')}")

    # Cache statistics
    cache_mgr = CacheManager(db)
    stats = cache_mgr.get_usage()

    print(colors.bold("\nCache Statistics"))
    print("-" * 40)
    print(f"Total files:      {stats.get('total_files', 0)}")
    print(f"Total size:       {format_size(stats.get('total_size', 0))}")
    print(f"Referenced:       {stats.get('referenced_files', 0)} files ({format_size(stats.get('referenced_size', 0))})")
    print(f"Unreferenced:     {stats.get('unreferenced_files', 0)} files ({format_size(stats.get('unreferenced_size', 0))})")

    if stats.get('quota_bytes'):
        pct = stats.get('quota_used_pct', 0)
        pct_str = f"{pct:.1f}%"
        if pct > 90:
            pct_str = colors.error(pct_str)
        elif pct > 75:
            pct_str = colors.warning(pct_str)
        print(f"Quota used:       {pct_str}")

    # Per-media summary
    print(colors.bold("\nMedia with mirror settings"))
    print("-" * 40)
    media_list = db.list_media()
    has_settings = False
    for m in media_list:
        if m.get('quota_mb') or m.get('replication_policy') != 'on_demand' or not m.get('shared', 1):
            has_settings = True
            shared_str = colors.success('Y') if m.get('shared', 1) else colors.dim('N')
            policy = m.get('replication_policy', 'on_demand')
            quota = f"{m['quota_mb']}M" if m.get('quota_mb') else '-'
            print(f"  {m['name'][:30]:<30} shared={shared_str} repl={policy:<10} quota={quota}")

    if not has_settings:
        print(colors.dim("  (all media using defaults)"))

    return 0


def cmd_mirror_enable(args, db: 'PackageDatabase') -> int:
    """Handle mirror enable command."""
    from .. import colors

    db.set_mirror_config('enabled', '1')
    print(colors.success("Mirror mode enabled"))
    print("This urpmd will now serve packages to peers on the network.")
    return 0


def cmd_mirror_disable(args, db: 'PackageDatabase') -> int:
    """Handle mirror disable command."""
    from .. import colors

    db.set_mirror_config('enabled', '0')
    print(colors.success("Mirror mode disabled"))
    print("This urpmd will no longer serve packages to peers.")
    return 0


def cmd_mirror_quota(args, db: 'PackageDatabase') -> int:
    """Handle mirror quota command."""
    from .. import colors
    from ...core.cache import format_size

    if not args.size:
        # Show current quota
        current = db.get_mirror_config('global_quota_mb')
        if current:
            print(f"Global quota: {current} MB ({format_size(int(current) * 1024 * 1024)})")
        else:
            print("No global quota set")
        return 0

    # Parse and set quota
    size_str = args.size.upper()
    try:
        if size_str.endswith('G'):
            quota_mb = int(float(size_str[:-1]) * 1024)
        elif size_str.endswith('M'):
            quota_mb = int(float(size_str[:-1]))
        elif size_str.endswith('K'):
            quota_mb = max(1, int(float(size_str[:-1]) / 1024))
        else:
            quota_mb = int(size_str)
    except ValueError:
        print(colors.error(f"Invalid size format: {args.size}"))
        return 1

    db.set_mirror_config('global_quota_mb', str(quota_mb))
    print(colors.success(f"Global quota set to {quota_mb} MB ({format_size(quota_mb * 1024 * 1024)})"))
    return 0


def cmd_mirror_disable_version(args, db: 'PackageDatabase') -> int:
    """Handle mirror disable-version command."""
    from .. import colors

    current = db.get_disabled_mirror_versions()
    new_versions = [v.strip() for v in args.versions.split(',') if v.strip()]

    # Merge with existing
    all_disabled = set(current) | set(new_versions)
    db.set_mirror_config('disabled_versions', ','.join(sorted(all_disabled)))

    print(colors.success(f"Disabled mirroring for Mageia version(s): {', '.join(new_versions)}"))
    if current:
        print(f"Previously disabled: {', '.join(current)}")
    print(f"Now disabled: {', '.join(sorted(all_disabled))}")
    return 0


def cmd_mirror_enable_version(args, db: 'PackageDatabase') -> int:
    """Handle mirror enable-version command."""
    from .. import colors

    current = db.get_disabled_mirror_versions()
    to_enable = [v.strip() for v in args.versions.split(',') if v.strip()]

    # Remove from disabled list
    still_disabled = [v for v in current if v not in to_enable]
    db.set_mirror_config('disabled_versions', ','.join(sorted(still_disabled)))

    enabled = [v for v in to_enable if v in current]
    if enabled:
        print(colors.success(f"Re-enabled mirroring for Mageia version(s): {', '.join(enabled)}"))
    else:
        print(colors.warning(f"Version(s) {', '.join(to_enable)} were not disabled"))

    if still_disabled:
        print(f"Still disabled: {', '.join(still_disabled)}")
    return 0


def cmd_mirror_clean(args, db: 'PackageDatabase') -> int:
    """Handle mirror clean command - enforce quotas and retention."""
    from .. import colors
    from ...core.cache import CacheManager, format_size

    cache_mgr = CacheManager(db)
    dry_run = getattr(args, 'dry_run', False)

    if dry_run:
        print(colors.info("Dry run mode - no files will be deleted\n"))

    result = cache_mgr.enforce_quotas(dry_run=dry_run)

    # Report results
    print(colors.bold("Cleanup results:"))
    print(f"  Unreferenced files: {result['unreferenced_deleted']} ({format_size(result['unreferenced_bytes'])})")
    print(f"  Retention policy:   {result['retention_deleted']} ({format_size(result['retention_bytes'])})")
    print(f"  Quota enforcement:  {result['quota_deleted']} ({format_size(result['quota_bytes'])})")
    print(f"  {colors.bold('Total:')}            {result['total_deleted']} ({format_size(result['total_bytes'])})")

    if result['errors']:
        print(colors.warning(f"\n{len(result['errors'])} errors occurred"))

    if dry_run and result['total_deleted'] > 0:
        print(colors.info("\nRun without --dry-run to actually delete files"))

    return 0


def cmd_mirror_sync(args, db: 'PackageDatabase') -> int:
    """Handle mirror sync command - force sync according to replication policies.

    Unlike the background daemon which waits for idle, this downloads immediately.
    """
    from .. import colors
    from ..helpers.debug import notify_urpmd_cache_invalidate as _notify_urpmd_cache_invalidate
    from ...core.rpmsrate import RpmsrateParser, DEFAULT_RPMSRATE_PATH
    from ...core.download import Downloader, DownloadItem
    from ...core.config import get_media_local_path, build_media_url
    import json

    # Default sections (same as DVD content)
    DEFAULT_SEED_SECTIONS = [
        'INSTALL',
        # Desktop environments
        'CAT_PLASMA5', 'CAT_GNOME', 'CAT_XFCE', 'CAT_MATE', 'CAT_LXDE', 'CAT_LXQT',
        'CAT_X', 'CAT_GRAPHICAL_DESKTOP',
        # Core system
        'CAT_SYSTEM', 'CAT_ARCHIVING', 'CAT_FILE_TOOLS', 'CAT_TERMINALS',
        'CAT_EDITORS', 'CAT_MINIMAL_DOCS', 'CAT_CONFIG',
        # Multimedia
        'CAT_AUDIO', 'CAT_VIDEO', 'SOUND', 'BURNER', 'SCANNER', 'PHOTO',
        # Applications
        'CAT_OFFICE', 'CAT_GRAPHICS', 'CAT_GAMES',
        # Network
        'CAT_NETWORKING_WWW', 'CAT_NETWORKING_WWW_SERVER',
        'CAT_NETWORKING_FILE', 'CAT_NETWORKING_REMOTE_ACCESS',
        'CAT_NETWORKING_MAIL', 'CAT_NETWORKING_IRC',
        # Development
        'CAT_DEVELOPMENT',
        # Other
        'CAT_PRINTER', 'CAT_ACCESSIBILITY', 'CAT_SPELLCHECK', 'CAT_MONITORING',
    ]

    # Find media with replication_policy='seed'
    media_to_replicate = []
    for media in db.list_media():
        if media.get('replication_policy') == 'seed' and media.get('enabled'):
            if args.media and media['name'] != args.media:
                continue
            media_to_replicate.append(media)

    if not media_to_replicate:
        if args.media:
            print(colors.error(f"Media '{args.media}' not found or doesn't have replication_policy='seed'"))
        else:
            print(colors.warning("No media with replication_policy='seed'"))
            print("Use: urpm media set <name> --replication=seed")
        return 1

    print(f"Media to sync: {len(media_to_replicate)}")
    for m in media_to_replicate:
        print(f"  - {m['name']}")

    # Compute seed set
    print(colors.dim("\nComputing seed set..."))

    # Collect all sections from all media
    all_sections = set()
    for media in media_to_replicate:
        seeds_json = media.get('replication_seeds')
        if seeds_json:
            try:
                sections = json.loads(seeds_json)
                all_sections.update(sections)
            except json.JSONDecodeError:
                print(colors.warning(f"Invalid replication_seeds JSON for {media['name']}"))
        else:
            all_sections.update(DEFAULT_SEED_SECTIONS)

    # Parse rpmsrate
    if not DEFAULT_RPMSRATE_PATH.exists():
        print(colors.error(f"rpmsrate-raw not found at {DEFAULT_RPMSRATE_PATH}"))
        print("Install the meta-task package to enable seed-based replication")
        return 1

    try:
        parser = RpmsrateParser(DEFAULT_RPMSRATE_PATH)
        parser.parse()
    except Exception as e:
        print(colors.error(f"Error parsing rpmsrate-raw: {e}"))
        return 1

    active_categories = [s for s in all_sections if s.startswith('CAT_')]
    seed_packages, locale_patterns = parser.get_packages_and_patterns(
        sections=list(all_sections),
        active_categories=active_categories,
        ignore_conditions=['DRIVER', 'HW', 'HW_CAT'],
        min_priority=4
    )

    print(f"Seed packages from rpmsrate: {len(seed_packages)}")

    # Expand locale patterns using database
    if locale_patterns:
        print(f"Locale patterns to expand: {len(locale_patterns)}")
        expanded = 0
        for pattern in locale_patterns:
            # Find all packages in DB matching this prefix
            cursor = db.conn.execute(
                "SELECT DISTINCT name FROM packages WHERE name LIKE ?",
                (pattern + '%',)
            )
            for (name,) in cursor:
                if name not in seed_packages:
                    seed_packages.add(name)
                    expanded += 1
        print(f"Expanded locale packages: +{expanded}")

    # Expand with dependencies
    result = db.collect_dependencies(seed_packages)
    seed_names = result['packages']
    print(f"With dependencies: {colors.count(len(seed_names))} packages")

    # Import RPM version comparison utilities
    from ...core.rpm import evr_key

    # Collect packages to mirror
    # For each media, keep only the latest version of each package name
    all_missing = []
    by_media = {}  # media_name -> (total, cached, missing)

    # First pass: collect latest version per package name per media
    packages_per_media = {}  # media_id -> {pkg_name -> pkg}
    for media in media_to_replicate:
        all_packages = db.get_packages_for_media(media['id'])
        if not all_packages:
            continue

        latest_by_name = {}
        for pkg in all_packages:
            if pkg['name'] in seed_names:
                name = pkg['name']
                if name not in latest_by_name or evr_key(pkg) > evr_key(latest_by_name[name]):
                    latest_by_name[name] = pkg

        packages_per_media[media['id']] = (media, latest_by_name)

    if getattr(args, 'latest_only', False):
        # --latest-only: deduplicate across media too, prefer Updates
        packages_by_name = {}  # name -> (media, pkg)
        for media_id, (media, latest_by_name) in packages_per_media.items():
            for pkg_name, pkg in latest_by_name.items():
                packages_by_name[pkg_name] = (media, pkg)  # Later media wins

        print(f"Unique packages to mirror: {len(packages_by_name)} (--latest-only)")

        for pkg_name, (media, pkg) in packages_by_name.items():
            media_name = media['name']
            if media_name not in by_media:
                by_media[media_name] = [0, 0, 0]
            by_media[media_name][0] += 1

            filename = pkg.get('filename')
            if not filename:
                continue

            cache_dir = get_media_local_path(media)
            pkg_path = cache_dir / filename
            if pkg_path.exists():
                by_media[media_name][1] += 1
            else:
                all_missing.append((media, pkg))
                by_media[media_name][2] += 1
    else:
        # Default: include latest version from each media (release + updates)
        total_packages = 0
        for media_id, (media, latest_by_name) in packages_per_media.items():
            media_name = media['name']
            by_media[media_name] = [0, 0, 0]

            for pkg_name, pkg in latest_by_name.items():
                by_media[media_name][0] += 1
                total_packages += 1

                filename = pkg.get('filename')
                if not filename:
                    continue

                cache_dir = get_media_local_path(media)
                pkg_path = cache_dir / filename
                if pkg_path.exists():
                    by_media[media_name][1] += 1
                else:
                    all_missing.append((media, pkg))
                    by_media[media_name][2] += 1

        print(f"Total packages to mirror: {total_packages} (release + updates, latest versions)")

    # Show per-media breakdown
    for media_name in sorted(by_media.keys()):
        total, cached, missing = by_media[media_name]
        print(f"  {media_name}: {total} packages ({cached} cached, {missing} to download)")

    if not all_missing:
        print(colors.success("\nAll seed packages are already cached!"))
        return 0

    # Note: 'size' in database is installed size, not RPM file size
    # RPM files are typically ~3x smaller than installed size
    installed_size = sum(p.get('size', 0) or 0 for _, p in all_missing)
    estimated_download = installed_size / 3  # Rough estimate
    print(f"\n{colors.bold('To download')}: {len(all_missing)} packages")
    print(f"  Estimated download: ~{estimated_download / 1024 / 1024 / 1024:.1f} GB (installed: {installed_size / 1024 / 1024 / 1024:.1f} GB)")

    # Build download items
    print(colors.dim("\nPreparing downloads..."))

    # Pre-compute servers per media
    media_info = {}  # media_id -> (servers, relative_path, is_official)
    for media in media_to_replicate:
        servers = db.get_servers_for_media(media['id'])
        if servers:
            media_info[media['id']] = (servers, media['relative_path'], media.get('is_official', 1))

    download_items = []
    skipped = 0
    for media, pkg in all_missing:
        info = media_info.get(media['id'])
        if not info:
            skipped += 1
            continue

        servers, relative_path, is_official = info
        item = DownloadItem(
            name=pkg['name'],
            version=pkg['version'],
            release=pkg['release'],
            arch=pkg['arch'],
            media_id=media['id'],
            relative_path=relative_path,
            is_official=bool(is_official),
            servers=servers,
            size=pkg.get('filesize', 0) or 0
        )
        download_items.append(item)
        print(f"Insert 1 {pkg['name']} {pkg.get('filesize',0)}")

    if not download_items:
        print(colors.warning("No items to download (no servers configured?)"))
        return 1

    print(f"Downloading {len(download_items)} packages...")

    # Use parallel downloader (same as urpm i/u)
    from ...core.config import get_base_dir
    cache_dir = get_base_dir(urpm_root=getattr(args, 'urpm_root', None))
    downloader = Downloader(cache_dir=cache_dir, use_peers=False, db=db)

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

    # Suppress logging during download to avoid polluting progress display
    import logging
    logging.getLogger('urpm.core.download').setLevel(logging.ERROR)

    dl_results, downloaded, cached, peer_stats = downloader.download_all(download_items, progress)

    # Restore logging
    logging.getLogger('urpm.core.download').setLevel(logging.WARNING)

    progress_display.finish()

    # Summary
    failed = [r for r in dl_results if not r.success]
    print(f"\n{colors.bold('Done')}: {downloaded} downloaded, {cached} cached, {len(failed)} failed")

    # Notify urpmd to invalidate cache index (so new downloads are visible to peers)
    if downloaded > 0:
        _notify_urpmd_cache_invalidate()

    if failed:
        print(colors.warning(f"\nFailed downloads:"))
        for r in failed[:10]:
            print(f"  {r.item.name}: {r.error}")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more")

    return 0 if not failed else 1


def cmd_mirror_ratelimit(args, db: 'PackageDatabase') -> int:
    """Handle mirror rate-limit command."""
    from .. import colors

    if not args.setting:
        # Show current setting
        enabled = db.get_mirror_config('rate_limit_enabled', '1')
        rate = db.get_mirror_config('rate_limit_requests_per_min', '60')
        if enabled == '0':
            print(f"Rate limiting: {colors.warning('OFF')} (install party mode)")
        else:
            print(f"Rate limiting: {colors.success('ON')} ({rate} requests/min)")
        return 0

    setting = args.setting.lower()
    if setting == 'off':
        db.set_mirror_config('rate_limit_enabled', '0')
        print(colors.warning("Rate limiting disabled (install party mode)"))
    elif setting == 'on':
        db.set_mirror_config('rate_limit_enabled', '1')
        rate = db.get_mirror_config('rate_limit_requests_per_min', '60')
        print(colors.success(f"Rate limiting enabled ({rate} requests/min)"))
    elif '/min' in setting:
        # Parse N/min
        try:
            rate = int(setting.replace('/min', ''))
            db.set_mirror_config('rate_limit_enabled', '1')
            db.set_mirror_config('rate_limit_requests_per_min', str(rate))
            print(colors.success(f"Rate limiting set to {rate} requests/min"))
        except ValueError:
            print(colors.error(f"Invalid rate format: {args.setting}"))
            print("Use: on, off, or N/min (e.g., 60/min)")
            return 1
    else:
        print(colors.error(f"Invalid setting: {args.setting}"))
        print("Use: on, off, or N/min (e.g., 60/min)")
        return 1

    return 0
