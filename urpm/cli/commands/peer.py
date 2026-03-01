"""Peer discovery and P2P download management commands."""

import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import _, ngettext, confirm_yes
if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def _query_daemon_peers() -> list:
    """Query local urpmd for discovered peers."""
    from ...core.config import PROD_PORT, DEV_PORT

    # Try dev port first, then prod
    for port in [DEV_PORT, PROD_PORT]:
        try:
            url = f"http://127.0.0.1:{port}/api/peers"
            req = urllib.request.Request(url)
            req.add_header('Accept', 'application/json')
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data.get('peers', [])
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
            continue
    return []


def cmd_peer(args, db: 'PackageDatabase') -> int:
    """Handle peer command - manage P2P peers and provenance."""
    from .. import colors, display

    # peer list - show peer stats (default when no subcommand)
    if args.peer_command in ('list', 'ls', None):
        # Query daemon for discovered peers
        discovered_peers = _query_daemon_peers()
        stats = db.get_peer_stats()
        blacklisted = db.list_blacklisted_peers()
        blacklisted_hosts = {(b['peer_host'], b['peer_port']) for b in blacklisted}

        has_content = discovered_peers or stats or blacklisted

        if not has_content:
            print(_("No peers discovered and no download history."))
            print(_("Make sure urpmd is running for peer discovery."))
            return 0

        # Show discovered peers from daemon
        if discovered_peers:
            print(colors.bold(_("Discovered peers on LAN:\n")))
            print(f"{_('Peer'):<30} {_('Media'):>8} {_('Last seen'):<20} {_('Status')}")
            print("-" * 70)
            for p in discovered_peers:
                peer_id = f"{p['host']}:{p['port']}"
                media_count = len(p.get('media', []))
                last_seen = p.get('last_seen', '')[:19].replace('T', ' ')  # ISO format to readable

                # Check status
                if (p['host'], p['port']) in blacklisted_hosts or \
                   (p['host'], None) in blacklisted_hosts:
                    status = colors.error(_("BLACKLISTED"))
                elif p.get('alive', True):
                    status = colors.ok(_("online"))
                else:
                    status = colors.warning(_("offline"))

                print(f"{peer_id:<30} {media_count:>8} {last_seen:<20} {status}")
            print()
        else:
            print(colors.warning(_("No peers discovered on LAN (is urpmd running?)\n")))

        # Show download statistics
        if stats:
            print(colors.bold(_("Download history:\n")))
            print(f"{_('Peer'):<30} {_('Downloads'):>10} {_('Size'):>12} {_('Last download'):<20}")
            print("-" * 75)
            for s in stats:
                peer_id = f"{s['peer_host']}:{s['peer_port']}"
                size_mb = (s['total_bytes'] or 0) / (1024 * 1024)
                last_dl = datetime.fromtimestamp(s['last_download']).strftime('%Y-%m-%d %H:%M')
                print(f"{peer_id:<30} {s['download_count']:>10} {size_mb:>10.1f}MB {last_dl:<20}")
            print()

        if blacklisted:
            print(colors.bold(_("Blacklisted peers:\n")))
            for b in blacklisted:
                port_str = f":{b['peer_port']}" if b['peer_port'] else " " + _("(all ports)")
                bl_time = datetime.fromtimestamp(b['blacklist_time']).strftime('%Y-%m-%d %H:%M')
                reason = f" - {b['reason']}" if b['reason'] else ""
                print(f"  {b['peer_host']}{port_str} (since {bl_time}){reason}")

        return 0

    # peer downloads - list packages downloaded from peers
    elif args.peer_command in ('downloads', 'dl'):
        downloads = db.get_peer_downloads(peer_host=args.host, limit=args.limit)

        if not downloads:
            if args.host:
                print(_("No downloads recorded from peer: {host}").format(host=args.host))
            else:
                print(_("No peer downloads recorded yet."))
            return 0

        print(colors.bold(_("Packages downloaded from peers (last {limit}):").format(limit=args.limit) + "\n"))
        print(f"{_('Filename'):<50} {_('Peer'):<25} {_('Date'):<20}")
        print("-" * 95)
        for d in downloads:
            peer_id = f"{d['peer_host']}:{d['peer_port']}"
            dl_time = datetime.fromtimestamp(d['download_time']).strftime('%Y-%m-%d %H:%M')
            # Truncate filename if too long
            filename = d['filename']
            if len(filename) > 48:
                filename = filename[:45] + "..."
            print(f"{filename:<50} {peer_id:<25} {dl_time:<20}")

        return 0

    # peer blacklist - add to blacklist
    elif args.peer_command in ('blacklist', 'bl', 'block'):
        host = args.host
        port = getattr(args, 'port', None)
        reason = getattr(args, 'reason', None)

        # Check if already blacklisted
        if db.is_peer_blacklisted(host, port):
            print(_("Peer {host} is already blacklisted.").format(host=host))
            return 0

        db.blacklist_peer(host, port, reason)
        port_str = f":{port}" if port else " " + _("(all ports)")
        print(_("Blacklisted peer: {host}{port_str}").format(host=host, port_str=port_str))
        print(_("Note: use 'urpm peer clean <host>' to remove RPMs downloaded from this peer."))
        return 0

    # peer unblacklist - remove from blacklist
    elif args.peer_command in ('unblacklist', 'unbl', 'unblock'):
        host = args.host
        port = getattr(args, 'port', None)

        if not db.is_peer_blacklisted(host, port):
            print(_("Peer {host} is not blacklisted.").format(host=host))
            return 0

        db.unblacklist_peer(host, port)
        port_str = f":{port}" if port else ""
        print(_("Removed {host}{port_str} from blacklist.").format(host=host, port_str=port_str))
        return 0

    # peer clean - delete files from a peer
    elif args.peer_command == 'clean':
        host = args.host

        # Get files from this peer
        files = db.get_files_from_peer(host)
        if not files:
            print(_("No files recorded from peer: {host}").format(host=host))
            return 0

        # Count existing files
        existing = []
        for f in files:
            p = Path(f)
            if p.exists():
                existing.append(p)

        print(_("Found {count} records from peer {host}").format(count=len(files), host=host))
        print("  " + _("{count} files still exist on disk").format(count=len(existing)))

        if not existing:
            # Just clean up records
            count = db.delete_peer_downloads(host)
            print(_("Removed {count} download records.").format(count=count))
            return 0

        # Confirm deletion
        if not args.yes:
            print("\n" + _("Files to delete:"))
            show_all = getattr(args, 'show_all', False)
            display.print_package_list([str(p) for p in existing], max_lines=10, show_all=show_all)

            try:
                response = input("\n" + ngettext(
                    "Delete {count} file? [y/N] ",
                    "Delete {count} files? [y/N] ",
                    len(existing)).format(count=len(existing)))
                if not confirm_yes(response):
                    print(_("Aborted"))
                    return 0
            except (KeyboardInterrupt, EOFError):
                print(_("\nAborted"))
                return 0

        # Delete files
        deleted = 0
        errors = 0
        for p in existing:
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                print("  " + _("Error deleting {path}: {error}").format(path=p, error=e))
                errors += 1

        # Clean up records
        count = db.delete_peer_downloads(host)

        print(_("Deleted {deleted} files ({errors} errors)").format(deleted=deleted, errors=errors))
        print(_("Removed {count} download records.").format(count=count))
        return 0 if errors == 0 else 1

    else:
        print(_("Unknown peer command: {command}").format(command=args.peer_command))
        return 1
