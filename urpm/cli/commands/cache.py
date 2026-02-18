"""Cache management commands."""

from collections import defaultdict
from pathlib import Path
import time
import urllib.request
import urllib.error
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_cache_info(args, db: 'PackageDatabase') -> int:
    """Handle cache info command."""
    stats = db.get_stats()

    print(f"\nCache: {stats['db_path']}")
    print(f"Size:  {stats['db_size_mb']:.1f} MB")
    print(f"Packages: {stats['packages']:,}")
    print(f"Provides: {stats['provides']:,}")
    print(f"Requires: {stats['requires']:,}")
    print(f"Media:    {stats['media']}")
    print()

    return 0


def cmd_cache_clean(args, db: 'PackageDatabase') -> int:
    """Handle cache clean command - remove orphan RPMs from cache."""
    from .. import display

    cache_dir = Path.home() / ".cache" / "urpm"
    medias_dir = cache_dir / "medias"

    if not medias_dir.exists():
        print("No RPM cache found")
        return 0

    # Get all NEVRAs from database, organized by media
    db_nevras = set()
    cursor = db.conn.execute("""
        SELECT p.nevra, m.name, m.url
        FROM packages p
        JOIN media m ON p.media_id = m.id
    """)
    for row in cursor:
        db_nevras.add(row[0])

    # Scan cache for RPM files
    orphans = []
    total_size = 0

    for rpm_file in medias_dir.rglob("*.rpm"):
        # Extract NEVRA from filename (e.g., firefox-120.0-1.mga9.x86_64.rpm)
        filename = rpm_file.stem  # Remove .rpm
        if filename not in db_nevras:
            orphans.append(rpm_file)
            total_size += rpm_file.stat().st_size

    if not orphans:
        print("No orphan RPMs found in cache")
        return 0

    # Format size
    if total_size > 1024 * 1024 * 1024:
        size_str = f"{total_size / 1024 / 1024 / 1024:.1f} GB"
    elif total_size > 1024 * 1024:
        size_str = f"{total_size / 1024 / 1024:.1f} MB"
    else:
        size_str = f"{total_size / 1024:.1f} KB"

    print(f"\nFound {len(orphans)} orphan RPMs ({size_str}):")

    rpm_names = [rpm_file.name for rpm_file in orphans]
    display.print_package_list(rpm_names, max_lines=10)

    if args.dry_run:
        print(f"\nDry run: would remove {len(orphans)} files ({size_str})")
        return 0

    if not args.auto:
        try:
            answer = input(f"\nRemove {len(orphans)} files ({size_str})? [y/N] ")
            if answer.lower() not in ('y', 'yes'):
                print("Aborted")
                return 1
        except EOFError:
            print("\nAborted")
            return 1

    # Remove the files
    removed = 0
    freed = 0
    for rpm_file in orphans:
        try:
            size = rpm_file.stat().st_size
            rpm_file.unlink()
            removed += 1
            freed += size
        except OSError as e:
            print(f"  Warning: could not remove {rpm_file.name}: {e}")

    if freed > 1024 * 1024 * 1024:
        freed_str = f"{freed / 1024 / 1024 / 1024:.1f} GB"
    elif freed > 1024 * 1024:
        freed_str = f"{freed / 1024 / 1024:.1f} MB"
    else:
        freed_str = f"{freed / 1024:.1f} KB"

    print(f"Removed {removed} files, freed {freed_str}")
    return 0


def cmd_cache_rebuild(args, db: 'PackageDatabase') -> int:
    """Handle cache rebuild command - rebuild database from synthesis files."""
    from ...core.sync import sync_media

    print("Rebuilding cache database...")

    # Get list of media
    media_list = db.list_media()

    if not media_list:
        print("No media configured")
        return 1

    # Clear all packages first
    print(f"Clearing {db.get_stats()['packages']:,} packages...")
    db.conn.execute("DELETE FROM packages")
    db.conn.execute("DELETE FROM provides")
    db.conn.execute("DELETE FROM requires")
    db.conn.execute("DELETE FROM conflicts")
    db.conn.execute("DELETE FROM obsoletes")
    db.conn.commit()

    # Re-sync each enabled media
    enabled_media = [m for m in media_list if m['enabled']]
    print(f"Re-importing {len(enabled_media)} enabled media...")

    urpm_root = getattr(args, 'urpm_root', None)
    for media in enabled_media:
        print(f"\n  {media['name']}...", end='', flush=True)
        try:
            result = sync_media(db, media['name'], force=True, urpm_root=urpm_root)
            if result.success:
                print(f" {result.packages_count:,} packages")
            else:
                print(f" ERROR: {result.error}")
        except Exception as e:
            print(f" ERROR: {e}")

    stats = db.get_stats()
    print(f"\nDone: {stats['packages']:,} packages, {stats['provides']:,} provides")
    return 0


def cmd_cache_stats(args, db: 'PackageDatabase') -> int:
    """Handle cache stats command - detailed cache statistics."""
    cache_dir = Path.home() / ".cache" / "urpm"

    # Database stats
    stats = db.get_stats()
    print(f"\n{'='*60}")
    print("DATABASE")
    print(f"{'='*60}")
    print(f"  Path:      {stats['db_path']}")
    print(f"  Size:      {stats['db_size_mb']:.1f} MB")
    print(f"  Packages:  {stats['packages']:,}")
    print(f"  Provides:  {stats['provides']:,}")
    print(f"  Requires:  {stats['requires']:,}")

    # Media stats
    media_list = db.list_media()
    print(f"\n{'='*60}")
    print("MEDIA")
    print(f"{'='*60}")

    for media in media_list:
        cursor = db.conn.execute(
            "SELECT COUNT(*) FROM packages WHERE media_id = ?",
            (media['id'],)
        )
        pkg_count = cursor.fetchone()[0]
        status = "enabled" if media['enabled'] else "disabled"
        print(f"  {media['name']}: {pkg_count:,} packages ({status})")

    # RPM cache stats
    medias_dir = cache_dir / "medias"
    print(f"\n{'='*60}")
    print("RPM CACHE")
    print(f"{'='*60}")

    if not medias_dir.exists():
        print("  No RPM cache found")
    else:
        total_rpms = 0
        total_size = 0

        # Find all RPMs recursively, group by parent directory
        dir_stats = defaultdict(lambda: {'count': 0, 'size': 0})

        for rpm_path in medias_dir.rglob("*.rpm"):
            if rpm_path.is_file():
                try:
                    size = rpm_path.stat().st_size
                    # Get relative path from medias_dir
                    rel_path = rpm_path.relative_to(medias_dir)
                    # Use parent dir as key (e.g., official/10/x86_64/media/core/release)
                    parent_key = str(rel_path.parent)
                    dir_stats[parent_key]['count'] += 1
                    dir_stats[parent_key]['size'] += size
                    total_rpms += 1
                    total_size += size
                except OSError:
                    continue

        # Display sorted by path
        for path_key in sorted(dir_stats.keys()):
            dir_stat = dir_stats[path_key]
            rpm_size = dir_stat['size']
            rpm_count = dir_stat['count']

            if rpm_size > 1024 * 1024 * 1024:
                size_str = f"{rpm_size / 1024 / 1024 / 1024:.1f} GB"
            elif rpm_size > 1024 * 1024:
                size_str = f"{rpm_size / 1024 / 1024:.1f} MB"
            else:
                size_str = f"{rpm_size / 1024:.1f} KB"

            print(f"  {path_key}: {rpm_count} RPMs ({size_str})")

        if total_size > 1024 * 1024 * 1024:
            total_str = f"{total_size / 1024 / 1024 / 1024:.1f} GB"
        elif total_size > 1024 * 1024:
            total_str = f"{total_size / 1024 / 1024:.1f} MB"
        else:
            total_str = f"{total_size / 1024:.1f} KB"

        print(f"\n  Total: {total_rpms} RPMs ({total_str})")

    # History stats
    cursor = db.conn.execute("SELECT COUNT(*) FROM history")
    history_count = cursor.fetchone()[0]
    cursor = db.conn.execute("SELECT COUNT(*) FROM history_packages")
    history_pkgs = cursor.fetchone()[0]

    print(f"\n{'='*60}")
    print("HISTORY")
    print(f"{'='*60}")
    print(f"  Transactions: {history_count}")
    print(f"  Package records: {history_pkgs}")

    print()
    return 0


def cmd_cache_rebuild_fts(args, db: 'PackageDatabase') -> int:
    """Handle cache rebuild-fts command - rebuild FTS index for file search."""
    from ...core.config import DEV_PORT, PROD_PORT, is_dev_mode

    # Check current FTS state
    stats = db.get_fts_stats()

    print(f"\nFTS Index Status:")
    print(f"  Available: {'yes' if stats['available'] else 'no'}")
    print(f"  Current:   {'yes' if stats['current'] else 'no'}")
    print(f"  Files in package_files: {stats['pf_count']:,}")
    print(f"  Files in FTS index:     {stats['fts_count']:,}")

    if stats['last_rebuild']:
        from datetime import datetime
        rebuild_time = datetime.fromtimestamp(stats['last_rebuild'])
        print(f"  Last rebuild: {rebuild_time.strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"\nRebuilding FTS index...", flush=True)

    # Try to use urpmd API if running (avoids database lock issues)
    port = DEV_PORT if is_dev_mode() else PROD_PORT

    try:
        req = urllib.request.Request(
            f'http://localhost:{port}/api/rebuild-fts',
            data=b'{}',
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode())

        if result.get('success'):
            print(f"\nDone: {result.get('indexed', 0):,} files indexed in {result.get('elapsed', 0)}s")
            print("  (rebuilt via urpmd)")
            return 0
        elif result.get('error'):
            print(f"Error from urpmd: {result['error']}")
            return 1
    except urllib.error.URLError:
        # urpmd not running, do it directly
        pass
    except Exception as e:
        # urpmd error, try direct rebuild
        print(f"  urpmd unavailable ({e}), rebuilding directly...")

    # Direct rebuild (urpmd not running)
    start_time = time.time()
    last_progress = [0]

    def progress_callback(current: int, total: int):
        pct = int(current * 100 / total) if total > 0 else 0
        # Show progress every 10%
        if pct >= last_progress[0] + 10 or current == total:
            print(f"  {pct}% ({current:,} / {total:,} files)", flush=True)
            last_progress[0] = (pct // 10) * 10

    indexed = db.rebuild_fts_index(progress_callback=progress_callback)

    elapsed = time.time() - start_time
    print(f"\nDone: {indexed:,} files indexed in {elapsed:.1f}s")

    return 0
