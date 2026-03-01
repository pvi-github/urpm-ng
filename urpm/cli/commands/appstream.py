"""AppStream metadata command."""

import gzip
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import _, ngettext
if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_appstream(args, db: 'PackageDatabase') -> int:
    """Handle appstream command - manage AppStream metadata."""
    from ...core.config import get_system_version, get_base_dir
    from ...core.appstream import AppStreamManager
    from .. import colors

    appstream_mgr = AppStreamManager(db, get_base_dir())

    if args.appstream_command in ('generate', 'gen', None):
        media_name = getattr(args, 'media', None)

        if media_name:
            # Generate for specific media
            media = db.get_media(media_name)
            if not media:
                print(colors.error(_("Media '{media_name}' not found").format(media_name=media_name)))
                return 1

            print(_("Generating AppStream for {media_name}...").format(media_name=media_name))
            xml_str, count = appstream_mgr.generate_for_media(
                media['id'], media_name
            )

            output_path = appstream_mgr.get_media_appstream_path(media_name)
            appstream_mgr._ensure_dirs()
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(xml_str)

            print(colors.ok(_("Generated {count} components -> {path}").format(count=count, path=output_path)))
            return 0

        else:
            # Generate for all enabled media and merge
            print(_("Generating AppStream for all enabled media..."))

            media_list = db.list_media()
            enabled_media = [m for m in media_list if m['enabled']]

            total = 0
            for media in enabled_media:
                xml_str, count = appstream_mgr.generate_for_media(
                    media['id'], media['name']
                )

                output_path = appstream_mgr.get_media_appstream_path(media['name'])
                appstream_mgr._ensure_dirs()
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(xml_str)

                print("  " + _("{name}: {count} components").format(name=media['name'], count=count))
                total += count

            # Merge all catalogs
            print(_("\nMerging catalogs..."))
            total_merged, media_count = appstream_mgr.merge_all_catalogs()
            print(colors.ok(_("Merged {total} components from {count} media").format(total=total_merged, count=media_count)))
            print(_("Output: {path}").format(path=appstream_mgr.catalog_path))

            print(_("\nTo refresh the AppStream cache, run:"))
            print(_("  sudo appstreamcli refresh-cache --force"))
            return 0

    elif args.appstream_command == 'status':
        # Show AppStream status for all media
        status_list = appstream_mgr.get_status()

        if not status_list:
            print(_("No media configured"))
            return 0

        # Header
        print(f"{_('Media'):<30} {_('Source'):<12} {_('Components'):>10} {_('Last Updated'):<20}")
        print("-" * 75)

        for item in status_list:
            name = item['media_name'][:29]
            source = item['source']
            count = item['component_count']
            mtime = item['last_updated']

            if mtime > 0:
                updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            else:
                updated = '-'

            # Color source
            if source == 'upstream':
                source_str = colors.ok(source)
            elif source == 'generated':
                source_str = colors.warning(source)
            elif source == 'missing':
                source_str = colors.error(source)
            else:
                source_str = source

            print(f"{name:<30} {source_str:<21} {count:>10} {updated:<20}")

        # Summary
        print("-" * 75)
        total = sum(s['component_count'] for s in status_list)
        upstream = sum(1 for s in status_list if s['source'] == 'upstream')
        generated = sum(1 for s in status_list if s['source'] == 'generated')
        missing = sum(1 for s in status_list if s['source'] == 'missing')

        print(_("Total: {total} components | upstream: {upstream}, generated: {generated}, missing: {missing}").format(
            total=total, upstream=upstream, generated=generated, missing=missing))

        # Check merged catalog
        if appstream_mgr.catalog_path.exists():
            mtime = appstream_mgr.catalog_path.stat().st_mtime
            updated = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            print("\n" + _("Merged catalog: {path} (updated: {updated})").format(path=appstream_mgr.catalog_path, updated=updated))
        else:
            print("\n" + _("Merged catalog: {status} (run 'urpm appstream merge')").format(status=colors.warning(_("not found"))))

        return 0

    elif args.appstream_command == 'merge':
        # Merge per-media files into unified catalog
        print(_("Merging AppStream catalogs..."))

        total, media_count = appstream_mgr.merge_all_catalogs(
            progress_callback=lambda msg: print(f"  {msg}")
        )

        if total == 0:
            print(colors.warning(_("No components found. Run 'urpm media update' first.")))
            return 1

        print(colors.ok(_("Merged {total} components from {count} media").format(total=total, count=media_count)))
        print(_("Output: {path}").format(path=appstream_mgr.catalog_path))

        # Refresh system cache if requested
        if getattr(args, 'refresh', False):
            print(_("\nRefreshing system AppStream cache..."))
            if appstream_mgr.refresh_system_cache():
                print(colors.ok(_("Cache refreshed")))
            else:
                print(colors.warning(_("Cache refresh failed (appstreamcli may not be installed)")))

        return 0

    elif args.appstream_command == 'init-distro':
        # Create OS metainfo file for AppStream
        metainfo_dir = Path('/usr/share/metainfo')
        metainfo_file = metainfo_dir / 'org.mageia.mageia.metainfo.xml'

        if metainfo_file.exists() and not getattr(args, 'force', False):
            print(_("OS metainfo file already exists: {path}").format(path=metainfo_file))
            print(_("Use --force to overwrite"))
            return 1

        # Get system version
        version = get_system_version() or 'unknown'

        metainfo_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<component type="operating-system">
  <id>org.mageia.mageia</id>
  <name>Mageia</name>
  <summary>Mageia Linux Distribution</summary>
  <description>
    <p>Mageia is a GNU/Linux-based, Free Software operating system.
    It is a community project, supported by a nonprofit organization
    of elected contributors.</p>
  </description>
  <url type="homepage">https://www.mageia.org</url>
  <metadata_license>CC0-1.0</metadata_license>
  <releases>
    <release version="{version}" />
  </releases>
</component>
'''
        try:
            metainfo_dir.mkdir(parents=True, exist_ok=True)
            with open(metainfo_file, 'w', encoding='utf-8') as f:
                f.write(metainfo_content)
            print(colors.ok(_("OS metainfo file created: {path}").format(path=metainfo_file)))
            return 0
        except PermissionError:
            print(colors.error(_("Permission denied. Run with sudo.")))
            return 1
        except Exception as e:
            print(colors.error(_("Failed to create metainfo: {error}").format(error=e)))
            return 1

    else:
        print(_("Unknown appstream command: {command}").format(command=args.appstream_command))
        return 1
