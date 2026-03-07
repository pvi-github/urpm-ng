"""Server management commands."""

from typing import TYPE_CHECKING

from ...i18n import _, ngettext
if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def cmd_server_list(args, db: 'PackageDatabase') -> int:
    """Handle server list command."""
    from .. import colors

    show_all = getattr(args, 'all', False)
    servers = db.list_servers(enabled_only=not show_all)

    if not servers:
        print(colors.info(_("No servers configured")))
        return 0

    # Calculate column widths dynamically (no truncation)
    name_width = max(4, max(len(srv['name']) for srv in servers))
    host_width = max(4, max(len(srv['host']) for srv in servers))

    # Header
    print(f"\n{'Name':<{name_width}} {'Protocol':<8} {'Host':<{host_width}} {'Pri':>4} {'IP':>6} {'Status':<8}")
    print("-" * (name_width + host_width + 35))

    for srv in servers:
        status = colors.success(_("enabled")) if srv['enabled'] else colors.dim(_("disabled"))
        ip_mode = srv.get('ip_mode', 'auto')
        # Pad first, then colorize (ANSI codes break alignment)
        ip_padded = f"{ip_mode:>6}"
        if ip_mode == 'dual':
            ip_str = colors.success(ip_padded)
        elif ip_mode == 'ipv6':
            ip_str = colors.info(ip_padded)
        elif ip_mode == 'auto':
            ip_str = colors.dim(ip_padded)
        else:
            ip_str = ip_padded

        print(f"{srv['name']:<{name_width}} {srv['protocol']:<8} {srv['host']:<{host_width}} {srv['priority']:>4} {ip_str} {status}")

    print()
    return 0


def cmd_server_add(args, db: 'PackageDatabase') -> int:
    """Handle server add command."""
    from .. import colors
    from urllib.parse import urlparse
    from ...core.config import test_server_ip_connectivity, build_server_url
    import urllib.request
    import socket

    url = args.url.rstrip('/')
    parsed = urlparse(url)

    if parsed.scheme not in ('http', 'https', 'file'):
        print(colors.error(_("Invalid protocol: {protocol}").format(protocol=parsed.scheme)))
        print(_("Supported protocols: http, https, file"))
        return 1

    protocol = parsed.scheme
    host = parsed.netloc or 'localhost'
    base_path = parsed.path

    # Check if server already exists
    existing = db.get_server_by_location(protocol, host, base_path)
    if existing:
        print(colors.warning(_("Server already exists: {name}").format(name=existing['name'])))
        return 1

    # Check if name is taken
    if db.get_server(args.name):
        print(colors.error(_("Server name already exists: {name}").format(name=args.name)))
        return 1

    # Test IP connectivity for remote servers
    ip_mode = 'auto'
    if protocol in ('http', 'https'):
        port = 443 if protocol == 'https' else 80
        print(_("Testing connectivity to {host}...").format(host=host))
        ip_mode = test_server_ip_connectivity(host, port, timeout=5.0)
        print("  " + _("IP mode: {mode}").format(mode=ip_mode))

    # Add server
    is_official = not args.custom
    enabled = not args.disabled
    priority = args.priority

    try:
        server_id = db.add_server(
            name=args.name,
            protocol=protocol,
            host=host,
            base_path=base_path,
            is_official=is_official,
            enabled=enabled,
            priority=priority
        )
        # Set detected ip_mode
        db.set_server_ip_mode_by_id(server_id, ip_mode)

        print(colors.success(_("Added server: {name}").format(name=args.name)))
        print("  " + _("URL: {url}").format(url=url))
        print("  " + _("Priority: {priority}").format(priority=priority))
        print("  " + _("IP mode: {mode}").format(mode=ip_mode))
        if not enabled:
            print(colors.dim(_("  Status: disabled")))
    except Exception as e:
        print(colors.error(_("Failed to add server: {error}").format(error=e)))
        return 1

    # Scan existing media to see which ones this server provides
    media_list = db.list_media()
    if not media_list:
        return 0

    # Filter media with relative_path
    media_to_scan = [(m['id'], m['name'], m.get('relative_path', ''))
                     for m in media_list if m.get('relative_path')]

    if not media_to_scan:
        return 0

    print("\n" + ngettext("Scanning {count} media...", "Scanning {count} media...", len(media_to_scan)).format(count=len(media_to_scan)), end=' ', flush=True)

    # Build base URL
    server = {'protocol': protocol, 'host': host, 'base_path': base_path}
    base_url = build_server_url(server)

    if protocol == 'file':
        # Local filesystem - fast sequential check
        from pathlib import Path
        found = []
        for media_id, media_name, relative_path in media_to_scan:
            md5_path = Path(base_path) / relative_path / "media_info" / "MD5SUM"
            if md5_path.exists():
                found.append((media_id, media_name))
    else:
        # Remote - parallel HEAD requests with ip_mode
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ...core.config import get_socket_family_for_ip_mode

        family = get_socket_family_for_ip_mode(ip_mode)

        def check_media(media_id, media_name, relative_path):
            test_url = f"{base_url}/{relative_path}/media_info/MD5SUM"
            try:
                # Patch getaddrinfo for this thread if needed
                original_getaddrinfo = None
                if family != 0:
                    original_getaddrinfo = socket.getaddrinfo
                    def patched(host, port, fam=0, type=0, proto=0, flags=0):
                        if fam == 0:
                            fam = family
                        return original_getaddrinfo(host, port, fam, type, proto, flags)
                    socket.getaddrinfo = patched

                try:
                    req = urllib.request.Request(test_url, method='HEAD')
                    urllib.request.urlopen(req, timeout=3)
                    return (media_id, media_name)
                finally:
                    if original_getaddrinfo:
                        socket.getaddrinfo = original_getaddrinfo
            except Exception:
                return None

        found = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_media, mid, mname, rpath): mname
                      for mid, mname, rpath in media_to_scan}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    found.append(result)

    # Link found media
    for media_id, media_name in found:
        db.link_server_media(server_id, media_id)

    print(_("{count} found").format(count=len(found)))
    if found:
        for _url, media_name in sorted(found, key=lambda x: x[1]):
            print(f"  {colors.success('+')} {media_name}")
    else:
        print(colors.warning(_("No existing media found on this server")))

    return 0


def cmd_server_remove(args, db: 'PackageDatabase') -> int:
    """Handle server remove command."""
    from .. import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(_("Server not found: {name}").format(name=args.name)))
        return 1

    db.remove_server(args.name)
    print(colors.success(_("Removed server: {name}").format(name=args.name)))
    return 0


def cmd_server_enable(args, db: 'PackageDatabase') -> int:
    """Handle server enable command."""
    from .. import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(_("Server not found: {name}").format(name=args.name)))
        return 1

    if server['enabled']:
        print(colors.info(_("Server already enabled: {name}").format(name=args.name)))
        return 0

    db.enable_server(args.name, True)
    print(colors.success(_("Enabled server: {name}").format(name=args.name)))
    return 0


def cmd_server_disable(args, db: 'PackageDatabase') -> int:
    """Handle server disable command."""
    from .. import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(_("Server not found: {name}").format(name=args.name)))
        return 1

    if not server['enabled']:
        print(colors.info(_("Server already disabled: {name}").format(name=args.name)))
        return 0

    db.enable_server(args.name, False)
    print(colors.success(_("Disabled server: {name}").format(name=args.name)))
    return 0


def cmd_server_priority(args, db: 'PackageDatabase') -> int:
    """Handle server priority command."""
    from .. import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(_("Server not found: {name}").format(name=args.name)))
        return 1

    db.set_server_priority(args.name, args.priority)
    print(colors.success(_("Set priority for {name}: {priority}").format(name=args.name, priority=args.priority)))
    return 0


def cmd_server_test(args, db: 'PackageDatabase') -> int:
    """Handle server test command - test connectivity and detect IP mode."""
    from .. import colors
    from ...core.config import test_server_ip_connectivity

    if args.name:
        # Test specific server
        server = db.get_server(args.name)
        if not server:
            print(colors.error(_("Server not found: {name}").format(name=args.name)))
            return 1
        servers = [server]
    else:
        # Test all enabled servers
        servers = db.list_servers(enabled_only=True)

    if not servers:
        print(colors.info(_("No servers to test")))
        return 0

    errors = 0
    for srv in servers:
        if srv['protocol'] == 'file':
            print(f"{srv['name']}: " + _("local filesystem (skipped)"))
            continue

        host = srv['host']
        port = 443 if srv['protocol'] == 'https' else 80
        print(_("Testing {name} ({host})...").format(name=srv['name'], host=host), end=' ', flush=True)

        old_mode = srv.get('ip_mode', 'auto')
        new_mode = test_server_ip_connectivity(host, port, timeout=5.0)

        if new_mode == 'auto':
            # Could not test
            print(colors.warning(_("unreachable (keeping {mode})").format(mode=old_mode)))
            errors += 1
        elif new_mode != old_mode:
            db.set_server_ip_mode(srv['name'], new_mode)
            print(colors.success(_("{new_mode} (was {old_mode})").format(new_mode=new_mode, old_mode=old_mode)))
        else:
            print(f"{new_mode}")

    return 1 if errors else 0


def cmd_server_ipmode(args, db: 'PackageDatabase') -> int:
    """Handle server ip-mode command - manually set IP mode."""
    from .. import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(_("Server not found: {name}").format(name=args.name)))
        return 1

    old_mode = server.get('ip_mode', 'auto')
    db.set_server_ip_mode(args.name, args.mode)
    print(colors.success(_("Set IP mode for {name}: {new_mode} (was {old_mode})").format(name=args.name, new_mode=args.mode, old_mode=old_mode)))
    return 0


def cmd_server_autoconfig(args, db: 'PackageDatabase') -> int:
    """Handle server autoconfig command - auto-discover servers from Mageia mirrorlist."""
    from .. import colors
    from ..helpers.media import generate_server_name as _generate_server_name
    from urllib.request import urlopen, Request
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import re
    import time

    TARGET_SERVERS = 5  # Target number of enabled servers

    # Get system version and arch
    version = getattr(args, 'release', None)
    if not version:
        try:
            with open('/etc/os-release') as f:
                for line in f:
                    if line.startswith('VERSION_ID='):
                        version = line.strip().split('=')[1].strip('"')
                        break
        except (IOError, OSError):
            pass

    if not version:
        print(colors.error(_("Cannot detect Mageia version from /etc/os-release")))
        print(colors.dim(_("Use --release to specify manually (e.g., --release 9)")))
        return 1

    import platform
    arch = platform.machine()

    # Count existing enabled servers
    existing_servers = db.list_servers(enabled_only=True)
    existing_count = len(existing_servers)

    if existing_count >= TARGET_SERVERS:
        print(_("Already have {count} enabled servers (target: {target})").format(count=existing_count, target=TARGET_SERVERS))
        print(colors.dim(_("Use 'urpm server remove' to remove some first if needed.")))
        return 0

    needed = TARGET_SERVERS - existing_count
    print(_("Have {count} enabled servers, need {needed} more to reach {target}").format(count=existing_count, needed=needed, target=TARGET_SERVERS))

    # Get all servers for duplicate check
    all_servers = db.list_servers()
    existing_urls = set()
    existing_names = set()
    for s in all_servers:
        url = f"{s['protocol']}://{s['host']}{s.get('base_path', '')}".rstrip('/')
        existing_urls.add(url)
        existing_names.add(s['name'])

    # Fetch mirrorlist
    mirrorlist_url = f"https://www.mageia.org/mirrorlist/?release={version}&arch={arch}&section=core&repo=release"

    print(_("Fetching mirrorlist for Mageia {version} ({arch})...").format(version=version, arch=arch), end=' ', flush=True)

    try:
        with urlopen(mirrorlist_url, timeout=60) as response:
            content = response.read().decode('utf-8').strip()
            mirror_urls = content.split('\n') if content else []
    except (URLError, HTTPError) as e:
        print(colors.error(_("failed: {error}").format(error=e)))
        return 1

    if not mirror_urls or not any(u.strip() for u in mirror_urls):
        print(colors.warning(_("empty")))
        print(colors.dim(_("The mirrorlist may not be available yet for this version.")))
        return 0

    print(ngettext("{count} mirror", "{count} mirrors", len(mirror_urls)).format(count=len(mirror_urls)))

    # Pattern to strip from URLs: {version}/{arch}/media/core/release/
    suffix_pattern = re.compile(rf'{re.escape(version)}/{re.escape(arch)}/media/core/release/?$')

    # Parse and filter candidates
    candidates = []
    skipped_protocol = 0
    skipped_duplicate = 0

    for url in mirror_urls:
        url = url.strip()
        if not url:
            continue

        parsed = urlparse(url)

        # Filter: only http/https
        if parsed.scheme not in ('http', 'https'):
            skipped_protocol += 1
            continue

        # Extract base path by stripping the suffix
        base_path = suffix_pattern.sub('', parsed.path).rstrip('/')
        full_base = f"{parsed.scheme}://{parsed.hostname}{base_path}"

        # Check for duplicate
        if full_base in existing_urls:
            skipped_duplicate += 1
            continue

        candidates.append({
            'scheme': parsed.scheme,
            'host': parsed.hostname,
            'base_path': base_path,
            'full_url': url,  # Original URL for latency test
        })

    if not candidates:
        print(_("No new servers to add"))
        if skipped_duplicate:
            print(colors.dim("  " + _("({count} already configured)").format(count=skipped_duplicate)))
        return 0

    print(_("Testing latency to {count} candidates...").format(count=len(candidates)), end=' ', flush=True)

    # Test latency to each candidate in parallel
    def test_latency(candidate):
        """Test latency with HEAD request, return (candidate, latency_ms) or (candidate, None)."""
        test_url = candidate['full_url']
        try:
            start = time.time()
            req = Request(test_url, method='HEAD')
            with urlopen(req, timeout=5) as resp:
                latency = (time.time() - start) * 1000
                return (candidate, latency)
        except Exception:
            return (candidate, None)

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(test_latency, c): c for c in candidates}
        for future in as_completed(futures):
            candidate, latency = future.result()
            if latency is not None:
                results.append((candidate, latency))

    print(_("{count} reachable").format(count=len(results)))

    if not results:
        print(colors.warning(_("No reachable mirrors found")))
        return 0

    # Sort by latency and take the best N
    results.sort(key=lambda x: x[1])
    best = results[:needed]

    if args.dry_run:
        print("\n" + ngettext("Would add {count} server:", "Would add {count} servers:", len(best)).format(count=len(best)))
        for candidate, latency in best:
            print(f"  {candidate['host']} ({latency:.0f}ms)")
        return 0

    # Add best servers
    added_servers = []
    for candidate, latency in best:
        shortname = candidate['host']
        # Ensure unique name
        original = shortname
        counter = 1
        while shortname in existing_names:
            shortname = f"{original}-{counter}"
            counter += 1

        try:
            server_id = db.add_server(
                shortname, candidate['scheme'], candidate['host'], candidate['base_path']
            )
            # Persist the latency measured during autoconfig so it immediately
            # influences server ordering on the first real download.
            db.update_server_stats(server_id, latency_ms=int(latency))
            print(colors.success("  " + _("Added: {name} ({latency}ms)").format(name=shortname, latency=f"{latency:.0f}")))
            existing_names.add(shortname)
            added_servers.append((server_id, shortname))
        except Exception as e:
            print(colors.warning("  " + _("Failed to add {name}: {error}").format(name=shortname, error=e)))

    if not added_servers:
        return 0

    # Scan enabled media to link with new servers
    all_media = db.list_media()
    enabled_media = [m for m in all_media if m.get('enabled', 1)]
    if not enabled_media:
        print(_("\nNo enabled media to scan"))
        return 0

    media_to_scan = [(m['id'], m['name'], m.get('relative_path', ''))
                     for m in enabled_media if m.get('relative_path')]

    if not media_to_scan:
        return 0

    print("\n" + _("Scanning {count} enabled media...").format(count=len(media_to_scan)), end=' ', flush=True)

    # For each new server, check which media it provides
    from ...core.config import build_server_url

    total_links = 0
    for server_id, server_name in added_servers:
        server = db.get_server(server_name)
        base_url = build_server_url(server)

        def check_media(media_id, media_name, relative_path):
            test_url = f"{base_url}/{relative_path}/media_info/MD5SUM"
            try:
                req = Request(test_url, method='HEAD')
                urlopen(req, timeout=3)
                return media_id
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_media, mid, mname, rpath): mid
                      for mid, mname, rpath in media_to_scan}
            for future in as_completed(futures):
                media_id = future.result()
                if media_id:
                    db.link_server_media(server_id, media_id)
                    total_links += 1

    print(_("{count} links created").format(count=total_links))

    # Summary
    print("\n" + ngettext(
        "Added {added} server, now have {total} enabled",
        "Added {added} servers, now have {total} enabled",
        len(added_servers)).format(added=len(added_servers), total=existing_count + len(added_servers)))
    if skipped_protocol:
        print(colors.dim(_("Skipped {count} (ftp/other protocol)").format(count=skipped_protocol)))

    return 0
