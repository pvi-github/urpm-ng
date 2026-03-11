"""Media management commands."""

from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import _, ngettext, confirm_yes
if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.media import (
    KNOWN_VERSIONS,
    KNOWN_ARCHES,
    KNOWN_CLASSES,
    KNOWN_TYPES,
    generate_media_name as _generate_media_name,
    generate_short_name as _generate_short_name,
    generate_server_name as _generate_server_name,
    parse_mageia_media_url,
    parse_custom_media_url,
    fetch_media_pubkey as _fetch_media_pubkey,
    get_gpg_key_info as _get_gpg_key_info,
    is_key_in_rpm_keyring as _is_key_in_rpm_keyring,
    import_gpg_key as _import_gpg_key,
)


def cmd_media_list(args, db: 'PackageDatabase') -> int:
    """Handle media list command."""
    from .. import colors

    show_all = getattr(args, 'all', False)
    media_list = db.list_media()

    if not media_list:
        print(_("No media configured"))
        return 0

    # Filter to enabled only unless --all
    if not show_all:
        media_list = [m for m in media_list if m['enabled']]
        if not media_list:
            print(_("No enabled media (use --all to see disabled)"))
            return 0

    # Find max lengths for alignment (on raw text, before coloring)
    max_name = max(len(m['name']) for m in media_list)
    max_path = max(len(m.get('relative_path') or '') for m in media_list)

    for m in media_list:
        # Get servers for this media
        servers = db.get_servers_for_media(m['id'], enabled_only=False)

        # Status: [x] or [ ]
        status = colors.success(_("[x]")) if m['enabled'] else colors.dim(_("[ ]"))

        # Update flag: U or space
        update_flag = colors.info(_("U")) if m['update_media'] else " "

        # Files sync flag: F or space
        files_flag = colors.info(_("F")) if m.get('sync_files') else " "

        # Name - pad first, then apply color
        name_raw = m['name']
        name_padded = f"{name_raw:{max_name}}"
        name = colors.dim(name_padded) if not m['enabled'] else name_padded

        # Relative path - pad first, then apply color if needed
        rel_path_raw = m.get('relative_path') or ''
        rel_path_padded = f"{rel_path_raw:{max_path}}"
        rel_path = colors.dim(rel_path_padded) if not m['enabled'] else rel_path_padded

        # Server hosts (green if enabled, dim if disabled)
        if servers:
            server_strs = []
            for s in servers:
                if s['protocol'] == 'file':
                    # Local filesystem - show [local] or path
                    display = f"[local:{s['base_path'][:20]}]" if s['base_path'] else "[local]"
                else:
                    display = s['host']
                if s['enabled']:
                    server_strs.append(colors.success(display))
                else:
                    server_strs.append(colors.dim(display))
            servers_display = " ".join(server_strs)
        else:
            servers_display = colors.warning(_("(no server)"))

        print(f"  {status} {update_flag}{files_flag} {name}  {rel_path}  {servers_display}")

    return 0


# Standard Mageia media types (class/type combinations)
STANDARD_MEDIA_TYPES = [
    ('core', 'release'),
    ('core', 'updates'),
    ('nonfree', 'release'),
    ('nonfree', 'updates'),
    ('tainted', 'release'),
    ('tainted', 'updates'),
]


def cmd_init(args, db: 'PackageDatabase') -> int:
    """Initialize urpm setup with standard Mageia media from mirrorlist.

    Creates database and adds all standard media (core, nonfree, tainted × release, updates)
    using mirrors from the provided mirrorlist URL.
    """
    from .. import colors
    from urllib.request import urlopen, Request
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import re
    import time
    import platform

    mirrorlist_url = args.mirrorlist
    version = getattr(args, 'release', None)
    arch = getattr(args, 'arch', None) or platform.machine()

    # If no mirrorlist but --release provided, auto-construct URL
    if not mirrorlist_url:
        if version:
            mirrorlist_url = f"https://mirrors.mageia.org/api/mageia.{version}.{arch}.list"
            print(_("Using mirrorlist: {url}").format(url=mirrorlist_url))
        else:
            print(colors.error(_("Either --mirrorlist or --release is required")))
            print(colors.dim(_("Examples:")))
            print(colors.dim(_("  urpm init --release 10")))
            print(colors.dim(_("  urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'")))
            return 1
    elif not version or not arch:
        # Try to extract version and arch from mirrorlist URL if not provided
        # URL format: https://mirrors.mageia.org/api/mageia.10.x86_64.list
        match = re.search(r'mageia\.([^.]+)\.([^.]+)\.list', mirrorlist_url)
        if match:
            if not version:
                version = match.group(1)
            if not arch:
                arch = match.group(2)

    # Fallback to system version if still not determined
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
        print(colors.error(_("Cannot determine Mageia version")))
        print(colors.dim(_("Use --release to specify (e.g., --release 10 or --release cauldron)")))
        return 1

    urpm_root = getattr(args, 'urpm_root', None)
    if urpm_root:
        print(_("Initializing urpm in {path}").format(path=f"{urpm_root}/var/lib/urpm/"))
        import subprocess
        import os
        import stat

        # Prepare chroot filesystem structure
        print(_("Preparing chroot filesystem..."))
        root_path = Path(urpm_root)

        # Create essential directories
        essential_dirs = [
            'dev', 'dev/pts', 'dev/shm',
            'proc', 'sys',
            'etc', 'var/tmp', 'var/lib/rpm',
            'run', 'tmp',
            # UsrMerge target directories
            'usr/bin', 'usr/sbin', 'usr/lib', 'usr/lib64'
        ]
        for d in essential_dirs:
            (root_path / d).mkdir(parents=True, exist_ok=True)

        # Note: UsrMerge symlinks (/bin -> usr/bin, etc.) are created by
        # the filesystem package. Don't create them here or it will conflict.
        # We only create the target directories (usr/bin, etc.) above.

        # Set proper permissions for /tmp and /var/tmp
        (root_path / 'tmp').chmod(0o1777)
        (root_path / 'var/tmp').chmod(0o1777)

        # Skip mount operations if no_mount flag is set (used by mkimage)
        # Container runtimes handle /dev and /proc mounting internally
        no_mount = getattr(args, 'no_mount', False)

        # Check if filesystem supports device nodes (nodev mount option)
        def is_nodev_filesystem(path: Path) -> bool:
            """Check if path is on a filesystem mounted with nodev."""
            try:
                with open('/proc/mounts', 'r') as f:
                    # Find the mount point for this path
                    best_match = None
                    best_len = 0
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 4:
                            mount_point = parts[1]
                            options = parts[3]
                            # Check if this mount point is a prefix of our path
                            try:
                                if str(path.resolve()).startswith(mount_point):
                                    if len(mount_point) > best_len:
                                        best_len = len(mount_point)
                                        best_match = options
                            except (OSError, ValueError):
                                pass
                    if best_match and 'nodev' in best_match.split(','):
                        return True
            except (OSError, IOError):
                pass
            return False

        # Bind mount /dev from host (works on any filesystem including nodev)
        chroot_dev = root_path / 'dev'
        dev_mounted = False

        # Check if already mounted
        def is_dev_mounted(chroot_dev: Path) -> bool:
            try:
                with open('/proc/mounts', 'r') as f:
                    chroot_dev_str = str(chroot_dev.resolve())
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2 and parts[1] == chroot_dev_str:
                            return True
            except (OSError, IOError):
                pass
            return False

        if no_mount:
            print(_("  Skipping mount operations (container mode)"))
        elif not is_dev_mounted(chroot_dev):
            if is_nodev_filesystem(root_path):
                print(_("  Filesystem has nodev - bind mounting /dev from host..."))
            else:
                print(_("  Bind mounting /dev from host..."))

            result = subprocess.run(
                ['mount', '--bind', '/dev', str(chroot_dev)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                dev_mounted = True
                print(colors.dim(_("  (unmount with: umount {path})").format(path=chroot_dev)))
            else:
                print(colors.warning(_("  Failed to mount /dev: {error}").format(error=result.stderr.strip())))
                # Fall back to creating device nodes if mount failed
                print(_("  Falling back to creating device nodes..."))
                old_umask = os.umask(0)
                try:
                    dev_nodes = [
                        ('null', stat.S_IFCHR | 0o666, 1, 3),
                        ('zero', stat.S_IFCHR | 0o666, 1, 5),
                        ('random', stat.S_IFCHR | 0o666, 1, 8),
                        ('urandom', stat.S_IFCHR | 0o666, 1, 9),
                        ('console', stat.S_IFCHR | 0o600, 5, 1),
                        ('tty', stat.S_IFCHR | 0o666, 5, 0),
                    ]
                    for name, mode, major, minor in dev_nodes:
                        dev_path = root_path / 'dev' / name
                        if not dev_path.exists():
                            try:
                                os.mknod(str(dev_path), mode, os.makedev(major, minor))
                            except (PermissionError, OSError):
                                pass
                finally:
                    os.umask(old_umask)
        else:
            print(_("  /dev already mounted"))
            dev_mounted = True

        # Create /dev/fd symlink (only if not using bind mount and not container mode)
        if not no_mount:
            fd_link = root_path / 'dev/fd'
            if not dev_mounted and not fd_link.exists():
                try:
                    fd_link.symlink_to('/proc/self/fd')
                except OSError:
                    pass

            # Create /dev/stdin, stdout, stderr symlinks (only if not using bind mount)
            if not dev_mounted:
                for i, name in enumerate(['stdin', 'stdout', 'stderr']):
                    link_path = root_path / 'dev' / name
                    if not link_path.exists():
                        try:
                            link_path.symlink_to(f'/proc/self/fd/{i}')
                        except OSError:
                            pass

            # Mount /proc (needed by many scriptlets)
            chroot_proc = root_path / 'proc'
            def is_proc_mounted(chroot_proc: Path) -> bool:
                try:
                    with open('/proc/mounts', 'r') as f:
                        chroot_proc_str = str(chroot_proc.resolve())
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 2 and parts[1] == chroot_proc_str:
                                return True
                except (OSError, IOError):
                    pass
                return False

            if not is_proc_mounted(chroot_proc):
                print(_("  Mounting /proc..."))
                result = subprocess.run(
                    ['mount', '-t', 'proc', 'proc', str(chroot_proc)],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    print(colors.dim(_("  (unmount with: umount {path})").format(path=chroot_proc)))
                else:
                    print(colors.warning(_("  Failed to mount /proc: {error}").format(error=result.stderr.strip())))
            else:
                print(_("  /proc already mounted"))

            # Create /etc/mtab symlink to /proc/mounts
            mtab_link = root_path / 'etc/mtab'
            if not mtab_link.exists():
                try:
                    mtab_link.symlink_to('/proc/mounts')
                except OSError:
                    pass

        # Copy /etc/resolv.conf for DNS resolution
        resolv_src = Path('/etc/resolv.conf')
        resolv_dst = root_path / 'etc/resolv.conf'
        if resolv_src.exists() and not resolv_dst.exists():
            try:
                import shutil
                shutil.copy2(str(resolv_src), str(resolv_dst))
            except (OSError, IOError):
                pass

        # Create minimal /etc/passwd and /etc/group for RPM
        # These are needed before the first package installation
        passwd_file = root_path / 'etc/passwd'
        if not passwd_file.exists():
            try:
                passwd_file.write_text("root:x:0:0:root:/root:/bin/bash\n")
            except (OSError, IOError):
                pass

        group_file = root_path / 'etc/group'
        if not group_file.exists():
            try:
                # Minimal groups needed by common packages
                group_file.write_text(
                    "root:x:0:\n"
                    "bin:x:1:\n"
                    "daemon:x:2:\n"
                    "sys:x:3:\n"
                    "tty:x:5:\n"
                    "disk:x:6:\n"
                    "wheel:x:10:\n"
                    "mail:x:12:\n"
                    "man:x:15:\n"
                    "utmp:x:22:\n"
                    "audio:x:63:\n"
                    "video:x:39:\n"
                    "users:x:100:\n"
                    "nobody:x:65534:\n"
                )
            except (OSError, IOError):
                pass

        # Initialize empty rpmdb in the chroot
        rpmdb_dir = root_path / "var/lib/rpm"
        print(_("Initializing rpmdb..."))
        result = subprocess.run(
            ['rpm', '--root', urpm_root, '--initdb'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(colors.error(_("Failed to initialize rpmdb: {error}").format(error=result.stderr)))
            return 1

        # Import Mageia GPG key into the chroot
        print(_("Importing Mageia GPG key..."))
        # Try to copy host's Mageia key to chroot
        key_paths = [
            '/etc/pki/rpm-gpg/RPM-GPG-KEY-Mageia',
            '/usr/share/distribution-gpg-keys/mageia/RPM-GPG-KEY-Mageia'
        ]
        key_imported = False
        for key_path in key_paths:
            if Path(key_path).exists():
                result = subprocess.run(
                    ['rpm', '--root', urpm_root, '--import', key_path],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    print(_("  Imported key from {path}").format(path=key_path))
                    key_imported = True
                    break
        if not key_imported:
            print(colors.warning(_("  Could not import GPG key (use --nosignature if needed)")))
    else:
        print(_("Initializing urpm for Mageia {version} ({arch})").format(version=version, arch=arch))

    # Check if media already exist
    existing_media = db.list_media()
    if existing_media:
        print(colors.warning(_("Warning: {count} media already configured").format(count=len(existing_media))))
        auto = getattr(args, 'auto', False)
        if not auto:
            try:
                response = input(_("Continue and add more? [y/N] "))
                if not confirm_yes(response):
                    print(_("Aborted"))
                    return 1
            except (KeyboardInterrupt, EOFError):
                print(_("\nAborted"))
                return 1

    # Fetch mirrorlist
    print(_("Fetching mirrorlist..."), end=' ', flush=True)

    try:
        req = Request(mirrorlist_url, headers={'User-Agent': 'urpm/0.1'})
        with urlopen(req, timeout=60) as response:
            content = response.read().decode('utf-8').strip()
            lines = [line.strip() for line in content.split('\n') if line.strip()]
    except (URLError, HTTPError) as e:
        print(colors.error(_("failed: {error}").format(error=e)))
        return 1

    if not lines:
        print(colors.warning(_("empty")))
        print(colors.dim(_("The mirrorlist may not be available yet for this version.")))
        return 1

    # Parse mirrorlist format: key=value,key=value,...,url=https://...
    # Example: continent=EU,zone=FR,...,url=https://ftp.belnet.be/mageia/distrib/10/x86_64
    mirror_urls = []
    for line in lines:
        # Extract url= field from CSV-like format
        for field in line.split(','):
            if field.startswith('url='):
                mirror_urls.append(field[4:])  # Remove 'url=' prefix
                break

    print(ngettext("{count} mirror", "{count} mirrors", len(mirror_urls)).format(count=len(mirror_urls)))

    if not mirror_urls:
        print(colors.warning(_("No URLs found in mirrorlist")))
        return 1

    # Parse mirror URLs to extract base paths
    # Mirror URLs look like: https://ftp.belnet.be/mageia/distrib/10/x86_64
    # We need to extract the base: https://ftp.belnet.be/mageia/distrib/
    # The suffix to strip is: {version}/{arch}
    suffix_pattern = re.compile(rf'{re.escape(version)}/{re.escape(arch)}/?$')

    candidates = []
    for url in mirror_urls:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            continue

        # Extract base path by stripping the suffix
        base_path = suffix_pattern.sub('', parsed.path).rstrip('/')

        candidates.append({
            'scheme': parsed.scheme,
            'host': parsed.hostname,
            'base_path': base_path,
            'full_url': url,
        })

    if not candidates:
        print(colors.error(_("No valid HTTP/HTTPS mirrors found")))
        return 1

    # Test latency to find best mirrors
    print(_("Testing latency to {count} mirrors...").format(count=len(candidates)), end=' ', flush=True)

    def test_latency(candidate):
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
        print(colors.error(_("No reachable mirrors found")))
        return 1

    # Sort by latency and take best 3
    results.sort(key=lambda x: x[1])
    best_mirrors = results[:3]

    print(_("\nBest mirrors:"))
    for candidate, latency in best_mirrors:
        print(_("  {host} ({latency:.0f}ms)").format(host=candidate['host'], latency=latency))

    # Add servers
    print(_("\nAdding servers..."))
    servers_added = []

    for candidate, latency in best_mirrors:
        # Check if server already exists
        existing = db.get_server_by_location(
            candidate['scheme'],
            candidate['host'],
            candidate['base_path']
        )
        if existing:
            print(_("  {host}: already exists").format(host=candidate['host']))
            servers_added.append(existing)
            continue

        # Generate server name from hostname
        server_name = _generate_server_name(candidate['scheme'], candidate['host'])

        # Make name unique if needed
        base_name = server_name
        counter = 1
        while True:
            try:
                server_id = db.add_server(
                    name=server_name,
                    protocol=candidate['scheme'],
                    host=candidate['host'],
                    base_path=candidate['base_path'],
                    is_official=True,
                    enabled=True,
                    priority=50
                )
                print(_("  {name} (id={id})").format(name=server_name, id=server_id))
                servers_added.append({'id': server_id, 'name': server_name})
                break
            except Exception as e:
                if 'UNIQUE constraint' in str(e) and 'name' in str(e):
                    counter += 1
                    server_name = f"{base_name}-{counter}"
                else:
                    print(colors.error(_("  Failed to add {host}: {error}").format(host=candidate['host'], error=e)))
                    break

    if not servers_added:
        print(colors.error(_("No servers could be added")))
        return 1

    # Add standard media
    print(_("\nAdding standard media for Mageia {version} ({arch})...").format(version=version, arch=arch))
    media_added = []

    for media_class, media_type in STANDARD_MEDIA_TYPES:
        name = f"{media_class.capitalize()} {media_type.capitalize()}"
        short_name = f"{media_class}_{media_type}"
        relative_path = f"{version}/{arch}/media/{media_class}/{media_type}"
        is_update = (media_type == 'updates')

        # Check if media already exists
        existing = db.get_media_by_version_arch_shortname(version, arch, short_name)
        if existing:
            print(_("  {name}: already exists").format(name=name))
            media_added.append(existing)
            continue

        try:
            media_id = db.add_media(
                name=name,
                short_name=short_name,
                mageia_version=version,
                architecture=arch,
                relative_path=relative_path,
                is_official=True,
                allow_unsigned=False,
                enabled=True,
                update_media=is_update,
                priority=50,
                url=None
            )
            print(_("  {name} (id={id})").format(name=name, id=media_id))
            media_added.append({'id': media_id, 'name': name, 'short_name': short_name})
        except Exception as e:
            print(colors.error(_("  Failed to add {name}: {error}").format(name=name, error=e)))

    if not media_added:
        print(colors.error(_("No media could be added")))
        return 1

    # Link servers to media
    print(_("\nLinking servers to media..."))
    for server in servers_added:
        for media in media_added:
            if not db.server_media_link_exists(server['id'], media['id']):
                db.link_server_media(server['id'], media['id'])

    print(colors.success(_("\nInitialized with {servers} server(s) and {media} media").format(servers=len(servers_added), media=len(media_added))))

    # Sync media unless --no-sync
    if not getattr(args, 'no_sync', False):
        print(_("\nSyncing media metadata..."))
        # Trigger sync for all media
        for media in media_added:
            media_name = media.get('name', '')
            short_name = media.get('short_name', media_name)
            print(_("  Syncing {name}...").format(name=short_name), end=' ', flush=True)
            try:
                from ...core.sync import sync_media
                result = sync_media(db, media_name, urpm_root=urpm_root)
                if result.success:
                    print(ngettext("{count} package", "{count} packages", result.packages_count).format(count=result.packages_count))
                else:
                    print(colors.warning(_("failed: {error}").format(error=result.error or 'unknown')))
            except Exception as e:
                print(colors.warning(_("failed: {error}").format(error=e)))

    print(colors.success(_("\nDone! You can now install packages.")))
    if urpm_root:
        print(colors.dim(_("Example: urpm --urpm-root {root} --root {root} install basesystem-minimal").format(root=urpm_root)))

    return 0


def cmd_media_add(args, db: 'PackageDatabase') -> int:
    """Handle media add command.

    Supports two modes:
    1. Official Mageia media: urpm media add <url>
       Auto-parses URL to extract version, arch, class, type
    2. Custom media: urpm media add --custom <name> <short_name> <url>
       User provides name and short_name explicitly

    Uses v8 schema with server/media/server_media tables.
    Falls back to legacy mode if URL parsing fails.
    """
    from .. import colors
    from ...core.install import check_root

    url = args.url
    custom_args = getattr(args, 'custom', None)
    is_custom = custom_args is not None

    # Parse URL based on mode
    if is_custom:
        # Custom mode: user provides name and short_name via --custom "Name" short_name
        name = custom_args[0]
        short_name = custom_args[1]

        parsed = parse_custom_media_url(url)
        if not parsed:
            print(colors.error(_("Error: could not parse URL: {url}").format(url=url)))
            return 1

        parsed['name'] = name
        parsed['short_name'] = short_name
        # Version priority: --version flag > detected from URL > system version
        import platform
        from ...core.config import get_system_version
        machine = platform.machine()
        explicit_version = getattr(args, 'version', None)
        if explicit_version:
            parsed['version'] = explicit_version
        elif not parsed.get('version'):
            parsed['version'] = get_system_version()
        parsed['arch'] = parsed.get('arch') or (machine if machine in KNOWN_ARCHES else 'x86_64')

    else:
        # Official mode: auto-parse URL
        parsed = parse_mageia_media_url(url)

        if not parsed:
            # Fallback: try legacy mode if --name is provided
            if hasattr(args, 'name') and args.name:
                print(colors.dim(_("URL not recognized as official Mageia, using legacy mode")))
                media_id = db.add_media_legacy(
                    name=args.name,
                    url=url,
                    enabled=not getattr(args, 'disabled', False),
                    update=getattr(args, 'update', False)
                )
                print(_("Added media '{name}' (id={id}) [legacy mode]").format(name=args.name, id=media_id))
                return 0
            else:
                print(colors.error(_("Error: URL not recognized as official Mageia media")))
                print(_("For official media, URL must contain: .../version/arch/media/class/type/"))
                print(_("For custom media, use: urpm media add --custom <name> <short_name> <url>"))
                return 1

    # Extract parsed values
    protocol = parsed['protocol']
    host = parsed['host']
    base_path = parsed['base_path']
    relative_path = parsed['relative_path']
    name = parsed['name']
    short_name = parsed['short_name']
    version = parsed['version']
    arch = parsed['arch']
    is_official = parsed['is_official']

    # Check --allow-unsigned is only used with custom media
    allow_unsigned = getattr(args, 'allow_unsigned', False)
    if allow_unsigned and is_official:
        print(colors.error(_("Error: --allow-unsigned can only be used with custom media")))
        return 1

    # GPG key import (optional, only with --import-key)
    # Signature verification happens at package install time, not here
    import_key = getattr(args, 'import_key', False)

    if import_key and protocol != 'file':
        print(_("Fetching GPG key from {url}/media_info/pubkey...").format(url=url))
        try:
            key_data = _fetch_media_pubkey(url)
        except Exception as e:
            print(colors.error(_("Error: could not fetch pubkey: {error}").format(error=e)))
            return 1

        if not key_data:
            print(colors.error(_("Error: no pubkey found at media")))
            return 1

        key_info = _get_gpg_key_info(key_data)
        if not key_info:
            print(colors.error(_("Error: could not parse pubkey")))
            return 1

        keyid = key_info['keyid']
        print(_("  Key ID:      {keyid}").format(keyid=key_info.get('keyid_long', keyid)))
        if key_info.get('fingerprint'):
            fp = key_info['fingerprint']
            fp_formatted = ' '.join([fp[i:i+4] for i in range(0, len(fp), 4)])
            print(_("  Fingerprint: {fingerprint}").format(fingerprint=fp_formatted))
        if key_info.get('uid'):
            print(_("  User ID:     {uid}").format(uid=key_info['uid']))

        if _is_key_in_rpm_keyring(keyid):
            print(colors.success(_("  Key {keyid} already in keyring").format(keyid=keyid)))
        else:
            # Import the key
            auto = getattr(args, 'auto', False)
            if not auto:
                try:
                    response = input(_("\nImport this key? [y/N] "))
                    if not confirm_yes(response):
                        print(_("Aborted"))
                        return 1
                except (KeyboardInterrupt, EOFError):
                    print(_("\nAborted"))
                    return 1

            if not check_root():
                print(colors.error(_("Error: importing keys requires root privileges")))
                return 1

            if _import_gpg_key(key_data):
                print(colors.success(_("  Key {keyid} imported").format(keyid=keyid)))
            else:
                print(colors.error(_("  Failed to import key")))
                return 1

    # --- Server upsert ---
    # Check if server already exists by protocol+host+base_path
    server = db.get_server_by_location(protocol, host, base_path)
    server_created = False

    if not server:
        # Create new server
        server_name = _generate_server_name(protocol, host)
        # Make server name unique if needed
        base_server_name = server_name
        counter = 1
        while True:
            try:
                server_id = db.add_server(
                    name=server_name,
                    protocol=protocol,
                    host=host,
                    base_path=base_path,
                    is_official=is_official,
                    enabled=True,
                    priority=50
                )
                server_created = True
                print(_("  Created server '{name}' (id={id})").format(name=server_name, id=server_id))
                server = {'id': server_id, 'name': server_name}
                break
            except Exception as e:
                if 'UNIQUE constraint' in str(e) and 'name' in str(e):
                    counter += 1
                    server_name = f"{base_server_name}-{counter}"
                else:
                    raise
    else:
        print(_("  Using existing server '{name}' (id={id})").format(name=server['name'], id=server['id']))

    # --- Media upsert ---
    # Check if media already exists by version+arch+short_name
    media = db.get_media_by_version_arch_shortname(version, arch, short_name)
    media_created = False

    if not media:
        # Create new media
        media_id = db.add_media(
            name=name,
            short_name=short_name,
            mageia_version=version,
            architecture=arch,
            relative_path=relative_path,
            is_official=is_official,
            allow_unsigned=allow_unsigned,
            enabled=not getattr(args, 'disabled', False),
            update_media=getattr(args, 'update', False),
            priority=50,
            url=None  # No legacy URL needed with server/media model
        )
        media_created = True
        print(_("  Created media '{name}' (id={id})").format(name=name, id=media_id))
        media = {'id': media_id, 'name': name}
    else:
        print(_("  Using existing media '{name}' (id={id})").format(name=media['name'], id=media['id']))
        media_id = media['id']

    # --- Link server to media ---
    if not db.server_media_link_exists(server['id'], media['id']):
        db.link_server_media(server['id'], media['id'])
        print(_("  Linked server '{server}' -> media '{media}'").format(server=server['name'], media=media['name']))
    else:
        print(_("  Link already exists: server '{server}' -> media '{media}'").format(server=server['name'], media=media['name']))

    # Summary
    print()
    if server_created and media_created:
        print(colors.success(_("Added media '{name}' with new server").format(name=name)))
    elif media_created:
        print(colors.success(_("Added media '{name}' to existing server").format(name=name)))
    elif server_created:
        print(colors.success(_("Added new server for existing media '{name}'").format(name=name)))
    else:
        print(colors.success(_("Linked existing server to existing media '{name}'").format(name=name)))

    return 0


def cmd_media_remove(args, db: 'PackageDatabase') -> int:
    """Handle media remove command."""
    name = args.name

    if not db.get_media(name):
        print(_("Media '{name}' not found").format(name=name))
        return 1

    db.remove_media(name)
    print(_("Removed media '{name}'").format(name=name))
    return 0


def cmd_media_enable(args, db: 'PackageDatabase') -> int:
    """Handle media enable command."""
    name = args.name

    if not db.get_media(name):
        print(_("Media '{name}' not found").format(name=name))
        return 1

    db.enable_media(name, enabled=True)
    print(_("Enabled media '{name}'").format(name=name))
    return 0


def cmd_media_disable(args, db: 'PackageDatabase') -> int:
    """Handle media disable command."""
    name = args.name

    if not db.get_media(name):
        print(_("Media '{name}' not found").format(name=name))
        return 1

    db.enable_media(name, enabled=False)
    print(_("Disabled media '{name}'").format(name=name))
    return 0


def cmd_media_update(args, db: 'PackageDatabase') -> int:
    """Handle media update command."""
    from .. import colors
    from ...core.sync import sync_media, sync_all_media, sync_files_xml, sync_all_files_xml
    from ...core.install import check_root
    from ...core.sync_lock import SyncLock
    import threading

    # Check root privileges (media update writes to database)
    if not check_root():
        print(colors.error(_("Error: root privileges required for media update")))
        print(_("Try: sudo urpm media update"))
        return 1

    # Prevent concurrent media syncs (CLI or daemon)
    sync_lock = SyncLock()
    acquired, holder_pid = sync_lock.try_acquire()
    if not acquired:
        if holder_pid:
            print(colors.warning(
                _("Media update already in progress (PID {pid})").format(pid=holder_pid)
            ))
        else:
            print(colors.warning(_("Media update already in progress")))
        return 0

    try:
        return _do_media_update(args, db, sync_lock)
    finally:
        sync_lock.release()


def _do_media_update(args, db: 'PackageDatabase', sync_lock) -> int:
    """Execute media update (called with sync lock held)."""
    from .. import colors
    from ...core.sync import sync_media, sync_all_media, sync_files_xml, sync_all_files_xml
    from ...i18n import _, ngettext
    import threading

    sync_files = getattr(args, 'files', False)
    skip_appstream = getattr(args, 'no_appstream', False)

    def progress(media_name, stage, current, total):
        # Clear line with ANSI escape code, then print
        if total > 0:
            msg = f"  {media_name}: {stage} ({current}/{total})"
        else:
            msg = f"  {media_name}: {stage}"
        print(f"\r\033[K{msg}", end='', flush=True)

    if args.name:
        # Update specific media
        media = db.get_media(args.name)
        if not media:
            print(colors.error(_("Media '{name}' not found").format(name=args.name)))
            return 1

        print(_("Updating {name}...").format(name=args.name))

        def single_progress(stage, current, total):
            progress(args.name, stage, current, total)

        urpm_root = getattr(args, 'urpm_root', None)
        result = sync_media(db, args.name, single_progress, force=True,
                           urpm_root=urpm_root, skip_appstream=skip_appstream)
        print()  # newline after progress

        if result.success:
            print(colors.success(ngettext("  {count} package", "  {count} packages", result.packages_count).format(count=result.packages_count)))

            # Sync files.xml if requested
            if sync_files:
                print(_("  Downloading files.xml for {name}...").format(name=args.name))
                files_result = sync_files_xml(db, args.name, single_progress, force=True)
                print()  # newline after progress
                if files_result.success:
                    if files_result.skipped:
                        print(colors.info(_("  files.xml: up-to-date ({count} files)").format(count=files_result.file_count)))
                    else:
                        print(colors.success(_("  files.xml: {count} files from {pkg_count} packages").format(count=files_result.file_count, pkg_count=files_result.pkg_count)))
                else:
                    print("  " + colors.warning(_("Warning")) + ": files.xml: " + str(files_result.error))

            return 0
        else:
            print("  " + colors.error(_("Error")) + ": " + str(result.error))
            return 1
    else:
        # Update all media in parallel
        import time
        print(_("Updating all media (parallel)..."))

        # Helper to format elapsed time
        def format_elapsed(seconds):
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            if mins > 0:
                return f"{mins}m{secs}s"
            else:
                return f"{secs}s"

        # Track status for each media
        media_status = {}
        status_lock = threading.Lock()
        media_list = [m['name'] for m in db.list_media() if m['enabled']]
        num_lines = 0

        def parallel_progress(media_name, stage, current, total):
            nonlocal num_lines
            with status_lock:
                # Update status
                if total > 0:
                    media_status[media_name] = f"{stage} ({current}/{total})"
                else:
                    media_status[media_name] = stage

                # Redraw all status lines
                if num_lines > 0:
                    print(f"\033[{num_lines}F", end='', flush=True)

                for name in media_list:
                    status = media_status.get(name, "waiting...")
                    print(f"\033[K  {name}: {status}")

                num_lines = len(media_list)

        sync_start = time.time()
        results = sync_all_media(db, parallel_progress, force=True,
                                 skip_appstream=skip_appstream)
        sync_elapsed = time.time() - sync_start

        # Clear progress lines
        if num_lines > 0:
            print(f"\033[{num_lines}F", end='', flush=True)
            for _i in range(num_lines):
                print("\033[K", end='')
                print("\033[1B", end='')
            print(f"\033[{num_lines}F", end='', flush=True)

        total_packages = 0
        errors = 0

        for name, result in results:
            if result.success:
                count = result.packages_count
                count_str = colors.success(str(count)) if count > 0 else str(count)
                print("  " + colors.info(name) + ": " + count_str + " " + ngettext("package", "packages", count))
                total_packages += count
            else:
                print("  " + colors.error(name) + ": " + _("ERROR - {error}").format(error=result.error))
                errors += 1

        if errors:
            print("\n" + colors.info(_("Total")) + ": " + colors.success(str(total_packages)) + " " + _("packages from {count} media in {elapsed} ({errors} errors)").format(count=len(results), elapsed=format_elapsed(sync_elapsed), errors=colors.error(str(errors))))
        else:
            print("\n" + colors.info(_("Total")) + ": " + colors.success(str(total_packages)) + " " + _("packages from {count} media in {elapsed}").format(count=len(results), elapsed=format_elapsed(sync_elapsed)))

        # Sync files.xml if requested (separate lock from metadata sync)
        if sync_files:
            from ...core.sync_lock import SyncLock, FILES_LOCK_PATH
            files_sync_lock = SyncLock(FILES_LOCK_PATH)
            f_acquired, f_holder = files_sync_lock.try_acquire()
            if not f_acquired:
                if f_holder:
                    print(colors.warning(
                        _("files.xml sync already in progress (PID {pid})").format(pid=f_holder)
                    ))
                else:
                    print(colors.warning(_("files.xml sync already in progress")))
                return 1 if errors else 0

            print(_("\nSyncing files.xml..."))

            try:
                # Track status for each media (same pattern as synthesis sync)
                # Filter by version/arch like sync_all_files_xml does
                from ...core.config import get_accepted_versions, get_compatible_arches
                import platform

                accepted_versions, _ignored, _ignored2 = get_accepted_versions(db)
                arch = platform.machine()
                compatible_arches = get_compatible_arches(arch)

                files_status = {}
                files_lock = threading.Lock()
                files_media_list = []
                for m in db.list_media():
                    if not m['enabled'] or not m.get('sync_files'):
                        continue
                    # Same filter as sync_all_files_xml
                    media_version = m.get('mageia_version', '')
                    media_arch = m.get('architecture', '')
                    if accepted_versions:
                        version_ok = not media_version or media_version in accepted_versions
                    else:
                        version_ok = True
                    arch_ok = not media_arch or media_arch in compatible_arches
                    if version_ok and arch_ok:
                        files_media_list.append(m['name'])
                files_num_lines = 0

                def files_progress(media_name, stage, dl_current, dl_total, import_current, import_total):
                    nonlocal files_num_lines
                    with files_lock:
                        # Build status string
                        if stage == 'checking':
                            status = "checking..."
                        elif stage == 'skipped':
                            status = "up-to-date"
                        elif stage == 'downloading':
                            if dl_total > 0:
                                pct = int(100 * dl_current / dl_total)
                                status = f"downloading {pct}%"
                            else:
                                status = "downloading..."
                        elif stage == 'downloaded':
                            status = "downloaded"
                        elif stage in ('syncing', 'analyzing', 'diff'):
                            status = "analyzing..."
                        elif stage == 'importing':
                            if import_total > 0:
                                pct = min(99, int(100 * import_current / import_total))
                                status = f"importing {pct}%"
                            else:
                                status = "importing..."
                        elif stage == 'indexing':
                            status = "creating indexes..."
                        elif stage == 'done':
                            status = colors.success(_("done"))
                        elif stage == 'error':
                            status = colors.error(_("error"))
                        else:
                            status = stage

                        files_status[media_name] = status

                        # Redraw all status lines
                        if files_num_lines > 0:
                            print(f"\033[{files_num_lines}F", end='', flush=True)

                        for name in files_media_list:
                            st = files_status.get(name, "waiting...")
                            print(f"\033[K  {name}: {st}")

                        files_num_lines = len(files_media_list)

                # Run parallel sync (force=False to respect MD5 checks)
                files_start = time.time()
                files_results = sync_all_files_xml(
                    db,
                    progress_callback=files_progress,
                    force=False,
                    max_workers=4,
                    filter_version=True
                )
                files_elapsed = time.time() - files_start

                # Clear progress lines
                if files_num_lines > 0:
                    print(f"\033[{files_num_lines}F", end='', flush=True)
                    for _i in range(files_num_lines):
                        print("\033[K", end='')
                        print("\033[1B", end='')
                    print(f"\033[{files_num_lines}F", end='', flush=True)

                # Print final results
                for name, result in files_results:
                    if result.success:
                        if result.skipped:
                            print(f"  {name}: " + _("up-to-date"))
                        else:
                            count_str = colors.success(f"{result.file_count:,}") if result.file_count > 0 else "0"
                            print(f"  {name}: " + count_str + " " + ngettext("file", "files", result.file_count))
                    else:
                        print("  " + colors.error(name) + ": " + _("ERROR - {error}").format(error=result.error))

                # Final summary
                total_files = sum(r.file_count for _name, r in files_results if r.success)
                files_errors = sum(1 for _name, r in files_results if not r.success)

                if files_errors > 0:
                    print("\n" + colors.info(_("Total files")) + ": " + colors.success(f'{total_files:,}') + " " + _("in {elapsed} ({errors} errors)").format(elapsed=format_elapsed(files_elapsed), errors=colors.error(str(files_errors))))
                else:
                    print("\n" + colors.info(_("Total files")) + ": " + colors.success(f'{total_files:,}') + " " + _("in {elapsed}").format(elapsed=format_elapsed(files_elapsed)))
            finally:
                files_sync_lock.release()

        return 1 if errors else 0


def parse_urpmi_cfg(filepath: str) -> list:
    """Parse urpmi.cfg file and return list of media configurations.

    Returns:
        List of dicts with keys: name, url, enabled, update, uses_mirrorlist
    """
    import re

    media_list = []

    with open(filepath, 'r') as f:
        content = f.read()

    # Pattern 1: Media with URL on same line
    # Name\ With\ Spaces URL {
    #   options...
    # }
    pattern_with_url = r'([^\s{]+(?:\\ [^\s{]+)*)\s+((?:https?|file)://[^\s{]+|/[^\s{]+)\s*\{([^}]*)\}'

    # Pattern 2: Media without URL (uses mirrorlist)
    # Name\ With\ Spaces {
    #   mirrorlist: $MIRRORLIST
    #   with-dir: media/core/release
    # }
    # Must NOT match the global empty block at start
    pattern_no_url = r'([^\s{]+(?:\\ [^\s{]+)+)\s*\{([^}]*)\}'

    matched_names = set()

    # First pass: media with direct URLs
    for match in re.finditer(pattern_with_url, content):
        raw_name = match.group(1)
        url_or_path = match.group(2)
        options_block = match.group(3)

        # Normalize local paths to file:// URLs
        if url_or_path.startswith('/') and not url_or_path.startswith('//'):
            url = f'file://{url_or_path}'
        else:
            url = url_or_path

        # Unescape the name (replace '\ ' with ' ')
        name = raw_name.replace('\\ ', ' ')
        matched_names.add(name)

        # Parse options
        enabled = True
        update = False

        for line in options_block.split('\n'):
            line = line.strip()
            if line == 'ignore':
                enabled = False
            elif line == 'update':
                update = True

        media_list.append({
            'name': name,
            'url': url,
            'enabled': enabled,
            'update': update,
            'uses_mirrorlist': False,
        })

    # Second pass: media without URL (mirrorlist-based)
    for match in re.finditer(pattern_no_url, content):
        raw_name = match.group(1)
        options_block = match.group(2)

        # Unescape the name
        name = raw_name.replace('\\ ', ' ')

        # Skip if already matched with URL or if it's an empty/global block
        if name in matched_names:
            continue

        # Check if this uses mirrorlist
        has_mirrorlist = 'mirrorlist:' in options_block
        with_dir = None

        # Parse options
        enabled = True
        update = False

        for line in options_block.split('\n'):
            line = line.strip()
            if line == 'ignore':
                enabled = False
            elif line == 'update':
                update = True
            elif line.startswith('with-dir:'):
                with_dir = line.split(':', 1)[1].strip()

        if has_mirrorlist:
            media_list.append({
                'name': name,
                'url': None,
                'enabled': enabled,
                'update': update,
                'uses_mirrorlist': True,
                'with_dir': with_dir,
            })

    return media_list


def _import_single_media(db: 'PackageDatabase', media: dict, colors) -> bool:
    """Import a single media from urpmi.cfg into v8 schema.

    Args:
        db: Database instance
        media: Dict with 'name', 'url', 'enabled', 'update' from parse_urpmi_cfg
        colors: Colors module

    Returns:
        True if successful, False otherwise
    """
    url = media['url']
    name = media['name']
    enabled = media['enabled']
    update = media['update']

    # Parse URL to extract server and media info
    parsed = parse_mageia_media_url(url)

    if not parsed:
        # Fallback to legacy mode for non-Mageia URLs
        db.add_media_legacy(
            name=name,
            url=url,
            enabled=enabled,
            update=update
        )
        return True

    # Extract parsed values
    protocol = parsed['protocol']
    host = parsed['host']
    base_path = parsed['base_path']
    relative_path = parsed['relative_path']
    version = parsed['version']
    arch = parsed['arch']
    short_name = parsed['short_name']
    is_official = parsed['is_official']

    # --- Server upsert ---
    server = db.get_server_by_location(protocol, host, base_path)

    if not server:
        # Create new server
        server_name = _generate_server_name(protocol, host)
        # Make server name unique if needed
        base_server_name = server_name
        counter = 1
        while True:
            try:
                server_id = db.add_server(
                    name=server_name,
                    protocol=protocol,
                    host=host,
                    base_path=base_path,
                    is_official=is_official,
                    enabled=True,
                    priority=50
                )
                server = {'id': server_id, 'name': server_name}
                break
            except Exception as e:
                if 'UNIQUE constraint' in str(e) and 'name' in str(e):
                    counter += 1
                    server_name = f"{base_server_name}-{counter}"
                else:
                    raise

    # --- Media upsert ---
    existing_media = db.get_media_by_version_arch_shortname(version, arch, short_name)

    if not existing_media:
        # Create new media with the name from urpmi.cfg (preserves user's naming)
        media_id = db.add_media(
            name=name,  # Use original name from urpmi.cfg
            short_name=short_name,
            mageia_version=version,
            architecture=arch,
            relative_path=relative_path,
            is_official=is_official,
            allow_unsigned=False,
            enabled=enabled,
            update_media=update,
            priority=50,
            url=None
        )
        existing_media = {'id': media_id, 'name': name}
    else:
        media_id = existing_media['id']

    # --- Link server to media ---
    if not db.server_media_link_exists(server['id'], existing_media['id']):
        db.link_server_media(server['id'], existing_media['id'])

    return True


def cmd_media_import(args, db: 'PackageDatabase') -> int:
    """Handle media import command - import from urpmi.cfg."""
    from .. import colors
    import os

    filepath = args.file

    if not os.path.exists(filepath):
        print(colors.error(_("File not found: {path}").format(path=filepath)))
        return 1

    try:
        media_list = parse_urpmi_cfg(filepath)
    except Exception as e:
        print(colors.error(_("Failed to parse {path}: {error}").format(path=filepath, error=e)))
        return 1

    if not media_list:
        print(colors.warning(_("No media found in file")))
        return 0

    # Separate mirrorlist-based media from direct URL media
    mirrorlist_media = [m for m in media_list if m.get('uses_mirrorlist', False)]
    direct_media = [m for m in media_list if not m.get('uses_mirrorlist', False)]

    # Get existing media names
    existing = {m['name'].lower(): m['name'] for m in db.list_media()}

    # Categorize direct URL media
    to_add = []
    to_skip = []
    to_replace = []

    for media in direct_media:
        if media['name'].lower() in existing:
            if args.replace:
                to_replace.append(media)
            else:
                to_skip.append(media)
        else:
            to_add.append(media)

    # Show summary
    print("\n" + colors.bold(_("Import from:")) + " " + filepath)
    print("  " + _("Found: {total} media ({direct} with URL, {mirrorlist} mirrorlist-based)").format(total=len(media_list), direct=len(direct_media), mirrorlist=len(mirrorlist_media)))

    if mirrorlist_media:
        print("\n  " + colors.warning(_("Skipped (mirrorlist-based):")) + " " + str(len(mirrorlist_media)))
        print(colors.dim(_("    These use $MIRRORLIST and require autoconfig.")))
        print(colors.dim(_("    Run: urpm media autoconfig -r <version>")))

    if to_add:
        print("\n  " + colors.success(_("To add:")) + " " + str(len(to_add)))
        for m in to_add:
            status = ""
            if not m['enabled']:
                status = " (disabled)"
            if m['update']:
                status += " [update]"
            print(f"    {m['name']}{status}")

    if to_replace:
        print("\n  " + colors.warning(_("To replace:")) + " " + str(len(to_replace)))
        for m in to_replace:
            print(f"    {m['name']}")

    if to_skip:
        print("\n  " + colors.info(_("Skipped (already exist):")) + " " + str(len(to_skip)))
        for m in to_skip:
            print(f"    {m['name']}")

    if not to_add and not to_replace:
        print(colors.info(_("\nNothing to import")))
        return 0

    # Confirmation
    if not args.auto:
        try:
            response = input("\n" + _("Import {count} media? [y/N] ").format(count=len(to_add) + len(to_replace)))
            if not confirm_yes(response):
                print(_("Aborted."))
                return 0
        except (KeyboardInterrupt, EOFError):
            print(_("\nAborted."))
            return 130

    # Import media
    added = 0
    replaced = 0
    errors = 0

    for media in to_replace:
        try:
            # Remove existing first
            orig_name = existing[media['name'].lower()]
            db.remove_media(orig_name)
            _import_single_media(db, media, colors)
            replaced += 1
            print("  " + colors.warning(_("Replaced:")) + " " + media['name'])
        except Exception as e:
            print("  " + colors.error(_("Error:")) + " " + media['name'] + ": " + str(e))
            errors += 1

    for media in to_add:
        try:
            _import_single_media(db, media, colors)
            added += 1
            print("  " + colors.success(_("Added:")) + " " + media['name'])
        except Exception as e:
            print("  " + colors.error(_("Error:")) + " " + media['name'] + ": " + str(e))
            errors += 1

    print("\n" + colors.bold(_("Summary:")) + " " + _("{added} added, {replaced} replaced, {errors} errors").format(added=added, replaced=replaced, errors=errors))

    if added + replaced > 0:
        print(colors.info(_("\nRun 'urpm media update' to fetch package lists")))

    return 1 if errors else 0


def cmd_media_set(args, db: 'PackageDatabase') -> int:
    """Handle media set command - modify media settings."""
    from .. import colors
    from datetime import datetime

    # Handle --all option for sync_files
    use_all = getattr(args, 'all', False)
    sync_files = getattr(args, 'sync_files', None)

    if use_all:
        # --all only works with --sync-files / --no-sync-files for now
        if sync_files is None:
            print(colors.error(_("--all requires --sync-files or --no-sync-files")))
            return 1

        count = db.set_all_media_sync_files(sync_files, enabled_only=True)
        status = "enabled" if sync_files else "disabled"
        print(colors.success(_("sync_files {status} on {count} media").format(status=status, count=count)))
        return 0

    # Normal mode: require media name
    if not args.name:
        print(colors.error(_("Media name required (or use --all with --sync-files)")))
        return 1

    media = db.get_media(args.name)
    if not media:
        print(colors.error(_("Media '{name}' not found").format(name=args.name)))
        return 1

    changes = []

    # Parse and apply changes
    shared = None
    if args.shared:
        shared = args.shared == 'yes'
        changes.append(f"shared: {'yes' if shared else 'no'}")

    replication_policy = None
    replication_seeds = None
    if args.replication:
        if args.replication in ('none', 'on_demand', 'seed'):
            replication_policy = args.replication
            changes.append(f"replication: {replication_policy}")
        else:
            print(colors.error(_("Invalid replication policy: {policy}").format(policy=args.replication)))
            print(_("Valid values: none, on_demand, seed"))
            return 1

    if hasattr(args, 'seeds') and args.seeds:
        # Parse comma-separated sections
        replication_seeds = [s.strip() for s in args.seeds.split(',')]
        changes.append(f"seeds: {', '.join(replication_seeds)}")

    quota_mb = None
    if args.quota:
        # Parse size like 5G, 500M
        size_str = args.quota.upper()
        try:
            if size_str.endswith('G'):
                quota_mb = int(float(size_str[:-1]) * 1024)
            elif size_str.endswith('M'):
                quota_mb = int(float(size_str[:-1]))
            elif size_str.endswith('K'):
                quota_mb = max(1, int(float(size_str[:-1]) / 1024))
            else:
                quota_mb = int(size_str)
            changes.append(f"quota: {quota_mb} MB")
        except ValueError:
            print(colors.error(_("Invalid size format: {size}").format(size=args.quota)))
            return 1

    retention_days = args.retention
    if retention_days is not None:
        changes.append(f"retention: {retention_days} days")

    priority = args.priority
    if priority is not None:
        changes.append(f"priority: {priority}")

    # Handle sync_files option
    sync_files = None
    if getattr(args, 'sync_files', None) is not None:
        sync_files = args.sync_files
        changes.append(f"sync_files: {'yes' if sync_files else 'no'}")

    if not changes:
        print(colors.warning(_("No changes specified")))
        print(_("Use --shared, --replication, --seeds, --quota, --retention, --priority, --sync-files, or --no-sync-files"))
        return 1

    # Apply mirror settings
    if any([shared is not None, replication_policy, replication_seeds is not None,
            quota_mb is not None, retention_days is not None]):
        db.update_media_mirror_settings(
            media['id'],
            shared=shared,
            replication_policy=replication_policy,
            replication_seeds=replication_seeds,
            quota_mb=quota_mb,
            retention_days=retention_days
        )

    # Apply priority separately (it's in the base media table)
    if priority is not None:
        db.conn.execute(
            "UPDATE media SET priority = ? WHERE id = ?",
            (priority, media['id'])
        )
        db.conn.commit()

    # Apply sync_files
    if sync_files is not None:
        db.set_media_sync_files(args.name, sync_files)

    print(colors.success(_("Updated '{name}':").format(name=args.name)))
    for change in changes:
        print(f"  - {change}")

    return 0


def cmd_media_seed_info(args, db: 'PackageDatabase') -> int:
    """Show seed set info for a media."""
    from .. import colors
    import json
    from pathlib import Path
    from ...core.rpmsrate import RpmsrateParser, DEFAULT_RPMSRATE_PATH

    media = db.get_media(args.name)
    if not media:
        print(colors.error(_("Media '{name}' not found").format(name=args.name)))
        return 1

    policy = media.get('replication_policy', 'on_demand')
    if policy != 'seed':
        print(colors.warning(_("Media '{name}' has replication_policy='{policy}', not 'seed'").format(name=args.name, policy=policy)))
        print(_("Use: urpm media set <name> --replication=seed --seeds=INSTALL,CAT_PLASMA5,..."))
        return 1

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

    # Parse seeds
    seeds_json = media.get('replication_seeds')
    if seeds_json:
        try:
            sections = json.loads(seeds_json)
        except json.JSONDecodeError:
            print(colors.error(_("Invalid replication_seeds JSON in database")))
            return 1
    else:
        sections = DEFAULT_SEED_SECTIONS

    print(_("Media: {name}").format(name=colors.bold(args.name)))
    print(_("Sections: {sections}").format(sections=', '.join(sections)))

    # Check rpmsrate-raw
    if not DEFAULT_RPMSRATE_PATH.exists():
        print(colors.warning(_("\nrpmsrate-raw not found at {path}").format(path=DEFAULT_RPMSRATE_PATH)))
        print(_("Install the meta-task package to enable seed-based replication"))
        return 1

    # Parse rpmsrate
    try:
        parser = RpmsrateParser(DEFAULT_RPMSRATE_PATH)
        parser.parse()
    except Exception as e:
        print(colors.error(_("Error parsing rpmsrate-raw: {error}").format(error=e)))
        return 1

    # Get active categories
    active_categories = [s for s in sections if s.startswith('CAT_')]

    # Get seed packages
    seed_packages = parser.get_packages(
        sections=sections,
        active_categories=active_categories,
        ignore_conditions=['DRIVER', 'HW', 'HW_CAT'],
        min_priority=4
    )

    print("\n" + _("Packages from rpmsrate: {count}").format(count=colors.count(len(seed_packages))))

    # Count how many are in this media
    all_packages = db.get_packages_for_media(media['id'])
    media_pkg_names = {p['name'] for p in all_packages}
    matching = seed_packages & media_pkg_names

    print(_("Matching in this media: {count}").format(count=colors.count(len(matching))))

    # Note: 'size' is installed size, not RPM download size (typically ~3x smaller)
    seed_size = sum(p.get('size', 0) or 0 for p in all_packages if p['name'] in seed_packages)
    print(_("Installed size (seeds only): {size} GB").format(size=colors.bold(f'{seed_size / 1024 / 1024 / 1024:.1f}')))

    # Collect dependencies (not resolve - we want all packages for replication, conflicts OK)
    missing_seeds = seed_packages - media_pkg_names
    if missing_seeds:
        print(colors.dim("  (" + _("{count} seeds not in media: {names}...").format(count=len(missing_seeds), names=', '.join(sorted(missing_seeds)[:5])) + ")"))

    print(colors.dim(_("\nCollecting dependencies...")))
    try:
        # Use collect_dependencies which ignores conflicts (for DVD/mirror replication)
        result = db.collect_dependencies(seed_packages)

        full_set = result['packages']
        not_found = result['not_found']
        total_size = result['total_size']

        print(_("With dependencies: {count} packages").format(count=colors.count(len(full_set))))
        est_download = total_size / 3
        print(_("Estimated download: ~{download} GB (installed: {installed:.1f} GB)").format(download=colors.bold(f'{est_download / 1024 / 1024 / 1024:.1f}'), installed=total_size / 1024 / 1024 / 1024))

        # Show breakdown
        deps_only = full_set - seed_packages
        print(_("  - Seeds: {seeds}, Dependencies: {deps}").format(seeds=len(seed_packages & full_set), deps=len(deps_only)))

        if not_found:
            print(colors.dim("  - " + _("Not found: {count} ({names}...)").format(count=len(not_found), names=', '.join(sorted(not_found)[:5]))))

    except Exception as e:
        print(colors.warning(_("Dependency collection failed: {error}").format(error=e)))
        import traceback
        traceback.print_exc()

    # Show some examples
    if matching:
        print("\n" + _("Example seed packages: {packages}...").format(packages=', '.join(sorted(matching)[:10])))

    return 0


def cmd_media_link(args, db: 'PackageDatabase') -> int:
    """Handle media link command - link/unlink servers to a media."""
    from .. import colors
    from ...core.config import build_server_url
    import urllib.request
    from pathlib import Path

    # Find media
    media = db.get_media(args.name)
    if not media:
        print(colors.error(_("Media '{name}' not found").format(name=args.name)))
        return 1

    media_id = media['id']
    relative_path = media.get('relative_path', '')
    added = []
    removed = []
    skipped = []
    errors = []

    # Get all servers for +all/-all
    all_servers = db.list_servers()

    def check_server_has_media(server: dict) -> bool:
        """Check if server has this media available."""
        if not relative_path:
            return True  # Can't check without relative_path

        if server['protocol'] == 'file':
            # Local filesystem check
            md5_path = Path(server['base_path']) / relative_path / "media_info" / "MD5SUM"
            return md5_path.exists()
        else:
            # Remote check via HEAD request
            base_url = build_server_url(server)
            url = f"{base_url}/{relative_path}/media_info/MD5SUM"
            try:
                req = urllib.request.Request(url, method='HEAD')
                urllib.request.urlopen(req, timeout=5)
                return True
            except:
                return False

    def try_add_server(server: dict) -> bool:
        """Try to add a server, returns True if added."""
        if db.server_media_link_exists(server['id'], media_id):
            return False  # Already linked

        if not check_server_has_media(server):
            skipped.append(server['name'])
            return False

        db.link_server_media(server['id'], media_id)
        added.append(server['name'])
        return True

    for change in args.changes:
        if change == '+all':
            # Link all servers that have the media
            print(_("Checking {count} servers...").format(count=len(all_servers)), flush=True)
            for server in all_servers:
                try_add_server(server)

        elif change == '-all':
            # Unlink all servers
            for server in all_servers:
                if db.server_media_link_exists(server['id'], media_id):
                    db.unlink_server_media(server['id'], media_id)
                    removed.append(server['name'])

        elif change.startswith('+'):
            server_name = change[1:]
            server = db.get_server(server_name)
            if not server:
                errors.append(f"Server '{server_name}' not found")
                continue
            if db.server_media_link_exists(server['id'], media_id):
                errors.append(f"Server '{server_name}' already linked")
                continue
            if not check_server_has_media(server):
                skipped.append(server_name)
                continue
            db.link_server_media(server['id'], media_id)
            added.append(server_name)

        elif change.startswith('-'):
            server_name = change[1:]
            server = db.get_server(server_name)
            if not server:
                errors.append(f"Server '{server_name}' not found")
                continue
            if not db.server_media_link_exists(server['id'], media_id):
                errors.append(f"Server '{server_name}' not linked")
                continue
            db.unlink_server_media(server['id'], media_id)
            removed.append(server_name)

        else:
            errors.append(f"Invalid change '{change}' - use +server or -server")

    # Report results
    if added:
        print(colors.success(_("Added: {names}").format(names=', '.join(added))))
    if removed:
        print(_("Removed: {names}").format(names=', '.join(removed)))
    if skipped:
        print(colors.warning(_("Skipped (media not available): {names}").format(names=', '.join(skipped))))
    if errors:
        for err in errors:
            print(colors.error(err))
        return 1

    # Show current servers
    servers = db.get_servers_for_media(media_id, enabled_only=False)
    if servers:
        print("\n" + _("Servers for '{name}':").format(name=args.name))
        for s in servers:
            status = colors.success(_("[x]")) if s['enabled'] else colors.dim(_("[ ]"))
            print(f"  {status} {s['name']} (priority: {s['priority']})")
    else:
        print(colors.dim("\n" + _("No servers linked to '{name}'").format(name=args.name)))

    return 0


def cmd_media_autoconfig(args, db: 'PackageDatabase') -> int:
    """Handle media autoconfig command - auto-add official Mageia media for a release."""
    from .. import colors
    from urllib.request import urlopen, Request
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import platform
    import time
    import re

    # Get release and arch
    release = args.release
    arch = getattr(args, 'arch', None) or platform.machine()
    dry_run = getattr(args, 'dry_run', False)
    no_nonfree = getattr(args, 'no_nonfree', False)
    no_tainted = getattr(args, 'no_tainted', False)

    print(_("Auto-configuring media for Mageia {release} ({arch})").format(release=release, arch=arch))

    # Define media types to add
    # Format: (type, repo, name_suffix)
    media_types = [
        ('core', 'release', 'Core Release'),
        ('core', 'updates', 'Core Updates'),
    ]
    if not no_nonfree:
        media_types.extend([
            ('nonfree', 'release', 'Nonfree Release'),
            ('nonfree', 'updates', 'Nonfree Updates'),
        ])
    if not no_tainted:
        media_types.extend([
            ('tainted', 'release', 'Tainted Release'),
            ('tainted', 'updates', 'Tainted Updates'),
        ])

    # Fetch mirrorlist to get a good server
    # Format: key=value,key=value,...,url=<url>
    mirrorlist_url = f"https://mirrors.mageia.org/api/mageia.{release}.{arch}.list"
    print(_("Fetching mirrorlist from {url}...").format(url=mirrorlist_url), end=' ', flush=True)

    try:
        req = Request(mirrorlist_url)
        req.add_header('User-Agent', 'urpm-ng')
        with urlopen(req, timeout=30) as response:
            content = response.read().decode('utf-8').strip()
            lines = [line.strip() for line in content.split('\n') if line.strip()]
    except (URLError, HTTPError) as e:
        print(colors.error(_("failed: {error}").format(error=e)))
        return 1

    if not lines:
        print(colors.warning(_("empty mirrorlist")))
        return 1

    # Parse mirrorlist format: continent=XX,zone=XX,...,url=<url>
    mirror_urls = []
    for line in lines:
        # Extract url= field
        url_match = re.search(r'url=(.+)$', line)
        if url_match:
            url = url_match.group(1)
            # Only keep http/https
            if url.startswith('http://') or url.startswith('https://'):
                mirror_urls.append(url)

    if not mirror_urls:
        print(colors.warning(_("no http/https mirrors found")))
        return 1

    print(ngettext("{count} http(s) mirror", "{count} http(s) mirrors", len(mirror_urls)).format(count=len(mirror_urls)))

    # Test a few mirrors to find a fast one
    print(_("Testing mirror latency..."), end=' ', flush=True)

    def test_mirror(url):
        """Test mirror latency by fetching a small file."""
        try:
            # URL is like: https://host/path/distrib/<release>/<arch>
            # Append /media/core/release/ and test with HEAD
            test_url = url.rstrip('/') + '/media/core/release/'
            req = Request(test_url, method='HEAD')
            req.add_header('User-Agent', 'urpm-ng')
            start = time.time()
            with urlopen(req, timeout=5) as response:
                latency = time.time() - start
                return (latency, url)
        except Exception:
            return (float('inf'), url)

    # Test first 15 mirrors (prefer https)
    https_mirrors = [u for u in mirror_urls if u.startswith('https://')]
    http_mirrors = [u for u in mirror_urls if u.startswith('http://') and not u.startswith('https://')]
    test_urls = (https_mirrors[:10] + http_mirrors[:5])[:15]

    latencies = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(test_mirror, url): url for url in test_urls}
        for future in as_completed(futures):
            result = future.result()
            if result[0] < float('inf'):
                latencies.append(result)

    if not latencies:
        print(colors.warning(_("all mirrors unreachable")))
        return 1

    # Sort by latency and pick top 3
    latencies.sort(key=lambda x: x[0])
    best_mirrors = latencies[:3]
    print(_("best: {latency:.0f}ms").format(latency=best_mirrors[0][0]*1000))

    # Extract base URL from mirror URL
    # Mirror URL format: https://mirror.example.com/path/distrib/<release>/<arch>
    # We need: https://mirror.example.com/path/distrib/
    def extract_base_url(mirror_url, release, arch):
        """Extract base URL from distrib URL."""
        # Pattern to match and remove: /<release>/<arch> at the end
        pattern = rf'/{re.escape(str(release))}/{re.escape(arch)}/?$'
        base = re.sub(pattern, '', mirror_url).rstrip('/')
        return base

    # Check existing media to avoid duplicates
    existing_media = db.list_media()
    existing_names = {m['name'] for m in existing_media}

    # Add media
    added = 0
    skipped = 0

    # First, add servers from best mirrors
    server_to_use = None
    for latency, mirror_url in best_mirrors[:1]:  # Just use the best one
        base_url = extract_base_url(mirror_url, release, arch)
        parsed = urlparse(base_url)
        server_name = parsed.hostname

        # Check if server already exists
        existing_server = db.get_server(server_name)
        if existing_server:
            server_to_use = existing_server
        elif not dry_run:
            db.add_server(
                name=server_name,
                protocol=parsed.scheme,
                host=parsed.hostname,
                base_path=parsed.path
            )
            print(_("  Added server: {name}").format(name=server_name))
            server_to_use = db.get_server(server_name)
        else:
            print(_("  Would add server: {name} ({url})").format(name=server_name, url=base_url))

    # Add each media type
    for media_type, repo, name_suffix in media_types:
        # Media name: e.g., "mga10-core-release" or "mga10-x86_64-core-release" for non-host arch
        if arch == platform.machine():
            media_name = f"mga{release}-{media_type}-{repo}"
        else:
            media_name = f"mga{release}-{arch}-{media_type}-{repo}"

        if media_name in existing_names:
            print(_("  Skipping {name} (already exists)").format(name=media_name))
            skipped += 1
            continue

        # Relative path for this media: <release>/<arch>/media/<type>/<repo>/
        relative_path = f"{release}/{arch}/media/{media_type}/{repo}"

        if dry_run:
            print(_("  Would add media: {name} -> {path}").format(name=media_name, path=relative_path))
        else:
            # Add the media
            is_update = (repo == 'updates')
            db.add_media(
                name=media_name,
                short_name=media_name,  # Already filesystem-safe
                mageia_version=str(release),
                architecture=arch,
                relative_path=relative_path,
                is_official=True,
                update_media=is_update
            )
            print(_("  Added media: {name}").format(name=media_name))

            # Link media to all enabled servers
            media = db.get_media(media_name)
            if media:
                for server in db.list_servers(enabled_only=True):
                    db.link_server_media(server['id'], media['id'])

        added += 1

    # Summary
    print()
    if dry_run:
        print(colors.warning(_("Dry run: would add {added} media, {skipped} already exist").format(added=added, skipped=skipped)))
    else:
        print(colors.success(_("Added {added} media, {skipped} already existed").format(added=added, skipped=skipped)))
        if added > 0:
            print(colors.dim(_("Run 'urpm media update' to sync metadata")))

    return 0

