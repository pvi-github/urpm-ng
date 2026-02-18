"""Peer discovery and P2P download management commands."""

import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
            print("No peers discovered and no download history.")
            print("Make sure urpmd is running for peer discovery.")
            return 0

        # Show discovered peers from daemon
        if discovered_peers:
            print(colors.bold("Discovered peers on LAN:\n"))
            print(f"{'Peer':<30} {'Media':>8} {'Last seen':<20} {'Status'}")
            print("-" * 70)
            for p in discovered_peers:
                peer_id = f"{p['host']}:{p['port']}"
                media_count = len(p.get('media', []))
                last_seen = p.get('last_seen', '')[:19].replace('T', ' ')  # ISO format to readable

                # Check status
                if (p['host'], p['port']) in blacklisted_hosts or \
                   (p['host'], None) in blacklisted_hosts:
                    status = colors.error("BLACKLISTED")
                elif p.get('alive', True):
                    status = colors.ok("online")
                else:
                    status = colors.warning("offline")

                print(f"{peer_id:<30} {media_count:>8} {last_seen:<20} {status}")
            print()
        else:
            print(colors.warning("No peers discovered on LAN (is urpmd running?)\n"))

        # Show download statistics
        if stats:
            print(colors.bold("Download history:\n"))
            print(f"{'Peer':<30} {'Downloads':>10} {'Size':>12} {'Last download':<20}")
            print("-" * 75)
            for s in stats:
                peer_id = f"{s['peer_host']}:{s['peer_port']}"
                size_mb = (s['total_bytes'] or 0) / (1024 * 1024)
                last_dl = datetime.fromtimestamp(s['last_download']).strftime('%Y-%m-%d %H:%M')
                print(f"{peer_id:<30} {s['download_count']:>10} {size_mb:>10.1f}MB {last_dl:<20}")
            print()

        if blacklisted:
            print(colors.bold("Blacklisted peers:\n"))
            for b in blacklisted:
                port_str = f":{b['peer_port']}" if b['peer_port'] else " (all ports)"
                bl_time = datetime.fromtimestamp(b['blacklist_time']).strftime('%Y-%m-%d %H:%M')
                reason = f" - {b['reason']}" if b['reason'] else ""
                print(f"  {b['peer_host']}{port_str} (since {bl_time}){reason}")

        return 0

    # peer downloads - list packages downloaded from peers
    elif args.peer_command in ('downloads', 'dl'):
        downloads = db.get_peer_downloads(peer_host=args.host, limit=args.limit)

        if not downloads:
            if args.host:
                print(f"No downloads recorded from peer: {args.host}")
            else:
                print("No peer downloads recorded yet.")
            return 0

        print(colors.bold(f"Packages downloaded from peers (last {args.limit}):\n"))
        print(f"{'Filename':<50} {'Peer':<25} {'Date':<20}")
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
            print(f"Peer {host} is already blacklisted.")
            return 0

        db.blacklist_peer(host, port, reason)
        port_str = f":{port}" if port else " (all ports)"
        print(f"Blacklisted peer: {host}{port_str}")
        print("Note: use 'urpm peer clean <host>' to remove RPMs downloaded from this peer.")
        return 0

    # peer unblacklist - remove from blacklist
    elif args.peer_command in ('unblacklist', 'unbl', 'unblock'):
        host = args.host
        port = getattr(args, 'port', None)

        if not db.is_peer_blacklisted(host, port):
            print(f"Peer {host} is not blacklisted.")
            return 0

        db.unblacklist_peer(host, port)
        port_str = f":{port}" if port else ""
        print(f"Removed {host}{port_str} from blacklist.")
        return 0

    # peer clean - delete files from a peer
    elif args.peer_command == 'clean':
        host = args.host

        # Get files from this peer
        files = db.get_files_from_peer(host)
        if not files:
            print(f"No files recorded from peer: {host}")
            return 0

        # Count existing files
        existing = []
        for f in files:
            p = Path(f)
            if p.exists():
                existing.append(p)

        print(f"Found {len(files)} records from peer {host}")
        print(f"  {len(existing)} files still exist on disk")

        if not existing:
            # Just clean up records
            count = db.delete_peer_downloads(host)
            print(f"Removed {count} download records.")
            return 0

        # Confirm deletion
        if not args.yes:
            print(f"\nFiles to delete:")
            show_all = getattr(args, 'show_all', False)
            display.print_package_list([str(p) for p in existing], max_lines=10, show_all=show_all)

            try:
                response = input(f"\nDelete {len(existing)} files? [y/N] ")
                if response.lower() not in ('y', 'yes'):
                    print("Aborted")
                    return 0
            except (KeyboardInterrupt, EOFError):
                print("\nAborted")
                return 0

        # Delete files
        deleted = 0
        errors = 0
        for p in existing:
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                print(f"  Error deleting {p}: {e}")
                errors += 1

        # Clean up records
        count = db.delete_peer_downloads(host)

        print(f"Deleted {deleted} files ({errors} errors)")
        print(f"Removed {count} download records.")
        return 0 if errors == 0 else 1

    else:
        print(f"Unknown peer command: {args.peer_command}")
        return 1
