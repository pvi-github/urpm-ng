"""Server management commands."""

from typing import TYPE_CHECKING

from ...i18n import _, ngettext, pgettext
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
    print(f"\n{_('Name'):<{name_width}} {_('Protocol'):<8} {_('Host'):<{host_width}}"
          f" {'Pays':>4} {_('Pri'):>4} {_('IP'):>6}"
          f" {_('BW'):>10} {_('Lat'):>6}"
          f" {_('Status'):<8}")
    print("-" * (name_width + host_width + 62))

    for srv in servers:
        status = colors.success(_("enabled")) if srv['enabled'] else colors.dim(_("disabled"))
        ip_mode = srv.get('ip_mode') or 'auto'
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

        # Country
        country = srv.get('country') or '—'
        country_padded = f"{country:>4}"
        if country == '—':
            country_padded = colors.dim(country_padded)

        # Bandwidth and latency
        # Pad first, then colorize (ANSI codes break alignment)
        bw_kbps = srv.get('bandwidth_kbps')
        lat_ms = srv.get('latency_ms')
        if bw_kbps and bw_kbps >= 1024:
            bw_padded = f"{bw_kbps / 1024:.1f} MB/s"
        elif bw_kbps:
            bw_padded = f"{bw_kbps} KB/s"
        else:
            bw_padded = "—"
        bw_padded = f"{bw_padded:>10}"
        if not bw_kbps:
            bw_padded = colors.dim(bw_padded)

        if lat_ms:
            lat_padded = f"{lat_ms}ms"
        else:
            lat_padded = "—"
        lat_padded = f"{lat_padded:>6}"
        if not lat_ms:
            lat_padded = colors.dim(lat_padded)

        pri = srv['priority'] if srv['priority'] is not None else 50
        print(f"{srv['name']:<{name_width}} {srv['protocol']:<8} {srv['host']:<{host_width}}"
              f" {country_padded} {pri:>4} {ip_str}"
              f" {bw_padded} {lat_padded}"
              f" {status}")

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

    ret = 0
    for name in args.name:
        server = db.get_server(name)
        if not server:
            print(colors.error(_("Server not found: {name}").format(name=name)))
            ret = 1
            continue
        db.remove_server(name)
        print(colors.success(_("Removed server: {name}").format(name=name)))
    return ret


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


def cmd_server_stats(args, db: 'PackageDatabase') -> int:
    """Handle server stats command — show measured performance statistics."""
    import time
    from .. import colors

    server = db.get_server(args.name)
    if not server:
        print(colors.error(_("Server not found: {name}").format(name=args.name)))
        return 1

    def fmt_bandwidth(kbps):
        """Format KB/s as a human-readable speed string."""
        if kbps is None:
            return colors.dim(_("no data"))
        if kbps >= 10240:   # >= 10 MB/s
            return colors.success(f"{kbps / 1024:.1f} MB/s")
        if kbps >= 1024:
            return colors.info(f"{kbps / 1024:.1f} MB/s")
        return colors.warning(f"{kbps} KB/s")

    def fmt_latency(ms):
        if ms is None:
            return colors.dim(_("no data"))
        if ms < 50:
            return colors.success(f"{ms} ms")
        if ms < 150:
            return colors.info(f"{ms} ms")
        return colors.warning(f"{ms} ms")

    def fmt_age(ts):
        if not ts:
            return colors.dim(_("never"))
        elapsed = int(time.time()) - ts
        if elapsed < 60:
            return _("{n}s ago").format(n=elapsed)
        if elapsed < 3600:
            return _("{n}m ago").format(n=elapsed // 60)
        if elapsed < 86400:
            return _("{n}h ago").format(n=elapsed // 3600)
        return _("{n}d ago").format(n=elapsed // 86400)

    success = server.get('success_count') or 0
    failure = server.get('failure_count') or 0
    total = success + failure
    if total > 0:
        rate = success / total * 100
        rate_str = (colors.success if rate >= 90 else colors.warning if rate >= 70 else colors.error)(
            f"{rate:.0f}%"
        )
    else:
        rate_str = colors.dim(_("no data"))

    media_list = db.get_media_for_server(server['id'])

    print(f"\n{colors.bold(server['name'])}")
    print(f"  {_('URL'):<14} {server['protocol']}://{server['host']}{server.get('base_path','')}")
    print(f"  {_('Status'):<14} {'enabled' if server['enabled'] else colors.dim('disabled')}")
    print(f"  {_('Priority'):<14} {server['priority']}")
    print(f"  {_('IP mode'):<14} {server.get('ip_mode', 'auto')}")
    print()
    print(f"  {_('Bandwidth'):<14} {fmt_bandwidth(server.get('bandwidth_kbps'))}")
    print(f"  {_('Latency'):<14} {fmt_latency(server.get('latency_ms'))}")
    print(f"  {_('Success rate'):<14} {rate_str}  ({success} ok / {failure} {_('failed')})")
    print(f"  {_('Last check'):<14} {fmt_age(server.get('last_check'))}")
    print()
    if media_list:
        print(f"  {pgettext('server', 'Media')} ({len(media_list)}):")
        for m in media_list:
            print(f"    {colors.dim('-')} {m['name']}")
    else:
        print(f"  {colors.dim(_('No linked media'))}")
    print()
    return 0


def autoconfig_servers(
    db: 'PackageDatabase',
    version: str,
    arch: str,
    count: int | None = None,
    custom_mirrorlist: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Discover and add servers from the Mageia mirrorlist.

    Fetches the official mirror list (or a custom one), deduplicates by
    host, tests latency in parallel, and adds the best candidates to the
    database.

    The function prints progress messages as it goes (fetch, latency test,
    additions) so callers get live feedback.

    Args:
        db: Package database.
        version: Mageia version (e.g. ``"10"``).
        arch: Architecture (e.g. ``"x86_64"``).
        count: Number of servers to add.  ``None`` means *auto*: compute
            the gap between existing enabled servers and the minimum
            required by pool settings.  When the pool is already full,
            returns early with an empty list.
        custom_mirrorlist: Custom mirrorlist URL.  ``None`` uses the
            default Mageia API (with geo filtering).  The custom URL
            must return the same ``key=value,...`` CSV format as the
            official API.
        dry_run: When ``True``, select candidates and return them
            **without** adding anything to the database.  The returned
            dicts include a ``latency_ms`` key for display purposes.

    Returns:
        List of added (or candidate, if *dry_run*) server dicts, each
        containing ``id`` (``None`` when dry_run), ``name``, ``protocol``,
        ``host``, ``base_path``, and ``latency_ms``.
        Empty list if no servers could be added.
    """
    from .. import colors
    from urllib.request import urlopen, Request
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    from ...core.settings import get_settings
    from ...core.server_pool import minimum_servers_for, _average_bandwidth
    from ...core.mirrorlist import (
        fetch_mirrors, dedup_mirrors, parse_mirrorlist_content,
    )

    # ── Determine how many servers we need ────────────────────────────
    existing_servers = db.list_servers(enabled_only=True)
    existing_count = len(existing_servers)

    if count is not None:
        needed = count
    else:
        target = minimum_servers_for(get_settings().download.parallel)
        if existing_count >= target:
            print(_("Already have {count} enabled servers (target: {target})").format(
                count=existing_count, target=target))
            print(colors.dim(_("Use 'urpm server remove' to remove some first if needed.")))
            return []
        needed = target - existing_count
        print(_("Have {count} enabled servers, need {needed} more to reach {target}").format(
            count=existing_count, needed=needed, target=target))

    # ── Host-only dedup against existing DB entries ───────────────────
    # For autoconfig we want at most one entry per physical mirror
    # (same host with different paths still shares the same bandwidth,
    # so adding both wouldn't improve parallelism).
    all_servers = db.list_servers()
    existing_hosts = {s['host'] for s in all_servers}
    existing_names = {s['name'] for s in all_servers}

    # ── Fetch mirror list ─────────────────────────────────────────────
    print(_("Fetching mirrorlist for Mageia {version} ({arch})...").format(
        version=version, arch=arch), end=' ', flush=True)

    try:
        if custom_mirrorlist:
            # Custom URL — must return the same key=value CSV format
            req = Request(custom_mirrorlist, headers={'User-Agent': 'urpm-ng'})
            with urlopen(req, timeout=60) as response:
                content = response.read().decode('utf-8').strip()
            mirrors = parse_mirrorlist_content(content)
        else:
            # Default: Mageia API with geo filtering
            mirrors = fetch_mirrors(version, arch, timeout=30)
    except Exception as e:
        print(colors.error(_("failed: {error}").format(error=e)))
        return []

    if not mirrors:
        print(colors.warning(_("empty")))
        print(colors.dim(_("The mirrorlist may not be available yet for this version.")))
        return []

    print(ngettext("{count} mirror", "{count} mirrors", len(mirrors)).format(
        count=len(mirrors)))

    # ── Dedup and build candidate list ────────────────────────────────
    suffix = f"/{version}/{arch}"
    mirrors = dedup_mirrors(mirrors, strip_suffix=suffix)
    total_mirrors = len(mirrors)

    candidates = []
    seen_hosts: set[str] = set()
    for m in mirrors:
        if m.host in existing_hosts or m.host in seen_hosts:
            continue
        seen_hosts.add(m.host)

        base_path = m.base_path
        if base_path.endswith(suffix):
            base_path = base_path[: -len(suffix)]
        base_path = base_path.rstrip("/")

        candidates.append({
            "scheme": m.scheme,
            "host": m.host,
            "base_path": base_path,
            "full_url": m.url.rstrip("/") + "/media/core/release/",
            "country": m.country or None,
        })
    skipped = total_mirrors - len(candidates)

    if not candidates:
        print(_("No new servers to add"))
        if skipped:
            print(colors.dim("  " + _("({count} already configured or duplicates)").format(
                count=skipped)))
        return []

    # ── Parallel latency testing ──────────────────────────────────────
    print(_("Testing latency to {count} candidates...").format(
        count=len(candidates)), end=' ', flush=True)

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
        return []

    # ── Sort by latency and take the best N ───────────────────────────
    results.sort(key=lambda x: x[1])
    best = results[:needed]

    if dry_run:
        return [
            {
                "id": None,
                "name": c['host'],
                "protocol": c['scheme'],
                "host": c['host'],
                "base_path": c['base_path'],
                "country": c.get('country'),
                "latency_ms": latency,
            }
            for c, latency in best
        ]

    # ── Add best servers to the database ──────────────────────────────
    added_servers: list[dict] = []
    avg_kbps = _average_bandwidth(all_servers)

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
                shortname, candidate['scheme'], candidate['host'],
                candidate['base_path'], country=candidate.get('country'),
            )
            # Persist latency + seed bandwidth with the average of existing
            # servers so the download planner gives new mirrors a fair share
            # immediately.  The EWMA will converge after the first downloads.
            db.update_server_stats(server_id, latency_ms=int(latency),
                                   bandwidth_kbps=avg_kbps)
            print(colors.success("  " + _("Added: {name} ({latency}ms)").format(
                name=shortname, latency=f"{latency:.0f}")))
            existing_names.add(shortname)
            added_servers.append({
                "id": server_id,
                "name": shortname,
                "protocol": candidate['scheme'],
                "host": candidate['host'],
                "base_path": candidate['base_path'],
                "country": candidate.get('country'),
                "latency_ms": latency,
            })
        except Exception as e:
            print(colors.warning("  " + _("Failed to add {name}: {error}").format(
                name=shortname, error=e)))

    return added_servers


def cmd_server_autoconfig(args, db: 'PackageDatabase') -> int:
    """Handle server autoconfig command - auto-discover servers from Mageia mirrorlist.

    Detects the local Mageia version and architecture, delegates server
    discovery and addition to :func:`autoconfig_servers`, then links the
    newly added servers to enabled media via parallel HEAD probes.
    """
    from .. import colors
    from urllib.request import urlopen, Request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ── Resolve version and arch ──────────────────────────────────────
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

    # ── Discover and add (or preview) servers ─────────────────────────
    dry_run = getattr(args, 'dry_run', False)
    added = autoconfig_servers(db, version, arch, dry_run=dry_run)

    if dry_run:
        if added:
            print("\n" + ngettext(
                "Would add {count} server:",
                "Would add {count} servers:",
                len(added)).format(count=len(added)))
            for srv in added:
                print(f"  {srv['host']} ({srv['latency_ms']:.0f}ms)")
        return 0

    if not added:
        return 0

    # ── Link new servers to enabled media ─────────────────────────────
    all_media = db.list_media()
    enabled_media = [m for m in all_media if m.get('enabled', 1)]
    if not enabled_media:
        print(_("\nNo enabled media to scan"))
        return 0

    media_to_scan = [(m['id'], m['name'], m.get('relative_path', ''))
                     for m in enabled_media if m.get('relative_path')]

    if not media_to_scan:
        return 0

    print("\n" + _("Scanning {count} enabled media...").format(
        count=len(media_to_scan)), end=' ', flush=True)

    from ...core.config import build_server_url

    total_links = 0
    for srv in added:
        server = db.get_server(srv['name'])
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
                    db.link_server_media(srv['id'], media_id)
                    total_links += 1

    print(_("{count} links created").format(count=total_links))

    # ── Summary ───────────────────────────────────────────────────────
    existing_count = len(db.list_servers(enabled_only=True)) - len(added)
    print("\n" + ngettext(
        "Added {added} server, now have {total} enabled",
        "Added {added} servers, now have {total} enabled",
        len(added)).format(added=len(added), total=existing_count + len(added)))

    return 0
