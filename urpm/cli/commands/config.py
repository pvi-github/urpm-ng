"""Configuration management commands."""

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from ..helpers.kernel import (
    read_config as _read_config,
    write_config as _write_config,
    get_blacklist as _get_blacklist,
    get_redlist as _get_redlist,
)


def cmd_config(args) -> int:
    """Handle config command - manage urpm configuration."""

    if not hasattr(args, 'config_cmd') or not args.config_cmd:
        print("Usage: urpm config <blacklist|redlist|kernel-keep|version-mode> ...")
        print("\nSubcommands:")
        print("  blacklist     Manage blacklist (critical packages)")
        print("  redlist       Manage redlist (packages requiring confirmation)")
        print("  kernel-keep   Number of kernels to keep")
        print("  version-mode  Choose between system version and cauldron")
        return 1

    # Handle version-mode (uses database, not config file)
    if args.config_cmd in ('version-mode', 'vm'):
        from ...core.database import PackageDatabase
        from ...core.config import get_db_path, get_system_version, get_accepted_versions

        db = PackageDatabase(get_db_path())

        if hasattr(args, 'mode') and args.mode is not None:
            if args.mode == 'auto':
                # Remove preference
                db.set_config('version-mode', None)
                print("version-mode preference removed (auto-detection)")
            else:
                db.set_config('version-mode', args.mode)
                print(f"version-mode set to '{args.mode}'")
            return 0
        else:
            # Show current state
            current = db.get_config('version-mode')
            system_version = get_system_version()
            accepted, needs_choice, info = get_accepted_versions(db, system_version)

            print(f"\nSystem version: {system_version or 'unknown'}")
            print(f"Configured preference: {current or 'auto (none set)'}")

            if info['cauldron_media']:
                print(f"Cauldron media: {', '.join(info['cauldron_media'][:3])}" +
                      (f" (+{len(info['cauldron_media'])-3} more)" if len(info['cauldron_media']) > 3 else ""))
            if info['system_version_media']:
                print(f"System version media: {', '.join(info['system_version_media'][:3])}" +
                      (f" (+{len(info['system_version_media'])-3} more)" if len(info['system_version_media']) > 3 else ""))

            if needs_choice:
                print(f"\nConflict: Both {system_version} and cauldron media are enabled.")
                print("Use 'urpm config version-mode <system|cauldron>' to choose.")
            elif accepted:
                print(f"\nActive version filter: {', '.join(sorted(accepted))}")
            print()
            return 0

    config = _read_config()

    # Handle kernel-keep
    if args.config_cmd in ('kernel-keep', 'kk'):
        if hasattr(args, 'count') and args.count is not None:
            if args.count < 0:
                print("Error: kernel-keep must be >= 0")
                return 1
            config['kernel_keep'] = args.count
            if _write_config(config):
                print(f"kernel-keep set to {args.count}")
                return 0
            return 1
        else:
            print(f"kernel-keep = {config['kernel_keep']}")
            return 0

    # Handle blacklist
    if args.config_cmd in ('blacklist', 'bl'):
        list_name = 'blacklist'
        builtin = _get_blacklist()
    elif args.config_cmd in ('redlist', 'rl'):
        list_name = 'redlist'
        builtin = _get_redlist()
    else:
        print(f"Unknown config command: {args.config_cmd}")
        return 1

    action = getattr(args, f'{list_name}_cmd', None)

    if not action or action in ('list', 'ls'):
        # Show list
        user_list = config.get(list_name, set())
        print(f"\n{list_name.title()} (built-in):")
        for pkg in sorted(builtin):
            print(f"  {pkg}")

        if user_list:
            print(f"\n{list_name.title()} (user-configured):")
            for pkg in sorted(user_list):
                print(f"  {pkg}")
        else:
            print(f"\nNo user-configured {list_name} entries")

        print()
        return 0

    elif action in ('add', 'a'):
        pkg = args.package
        if pkg in builtin:
            print(f"{pkg} is already in the built-in {list_name}")
            return 0
        if pkg in config[list_name]:
            print(f"{pkg} is already in the user {list_name}")
            return 0
        config[list_name].add(pkg)
        if _write_config(config):
            print(f"Added {pkg} to {list_name}")
            return 0
        return 1

    elif action in ('remove', 'rm'):
        pkg = args.package
        if pkg in builtin:
            print(f"Error: {pkg} is in the built-in {list_name} and cannot be removed")
            return 1
        if pkg not in config[list_name]:
            print(f"{pkg} is not in the user {list_name}")
            return 1
        config[list_name].remove(pkg)
        if _write_config(config):
            print(f"Removed {pkg} from {list_name}")
            return 0
        return 1

    else:
        print(f"Usage: urpm config {list_name} <list|add|remove> [package]")
        return 1


def cmd_key(args) -> int:
    """Handle key command - manage GPG keys for package verification."""
    import rpm
    from ...core.install import check_root

    if not hasattr(args, 'key_cmd') or not args.key_cmd:
        print("Usage: urpm key <list|import|remove> ...")
        print("\nCommands:")
        print("  list            List installed GPG keys")
        print("  import <file>   Import GPG key from file or HTTPS URL")
        print("  remove <keyid>  Remove GPG key")
        return 1

    # List keys
    if args.key_cmd in ('list', 'ls', 'l'):
        ts = rpm.TransactionSet()
        keys = []

        for hdr in ts.dbMatch('name', 'gpg-pubkey'):
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            summary = hdr[rpm.RPMTAG_SUMMARY]
            keys.append((version, release, summary))

        if not keys:
            print("No GPG keys installed")
            return 0

        print(f"\nInstalled GPG keys ({len(keys)}):\n")
        for version, release, summary in sorted(keys):
            print(f"  {version}-{release}")
            print(f"    {summary}")
        print()
        return 0

    # Import key
    elif args.key_cmd in ('import', 'i', 'add'):
        if not check_root():
            print("Error: importing keys requires root privileges")
            return 1

        if not hasattr(args, 'keyfile') or not args.keyfile:
            print("Usage: urpm key import <keyfile|url>")
            return 1

        key_source = args.keyfile

        # Check if it's an HTTPS URL
        if key_source.startswith('https://'):
            import tempfile
            import urllib.request
            import urllib.error

            print(f"Downloading key from {key_source}...")
            try:
                with urllib.request.urlopen(key_source, timeout=30) as response:
                    key_data = response.read()

                # Write to temporary file and import
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.gpg', delete=False) as tmp:
                    tmp.write(key_data)
                    tmp_path = tmp.name

                try:
                    result = subprocess.run(
                        ['rpm', '--import', tmp_path],
                        capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        print(f"Key imported from {key_source}")
                        return 0
                    else:
                        print(f"Failed to import key: {result.stderr}")
                        return 1
                finally:
                    os.unlink(tmp_path)

            except urllib.error.URLError as e:
                print(f"Error: failed to download key: {e.reason}")
                return 1
            except Exception as e:
                print(f"Error: {e}")
                return 1

        elif key_source.startswith('http://'):
            print("Error: HTTP URLs are not allowed for security reasons. Use HTTPS.")
            return 1

        else:
            # Import from local file
            if not os.path.exists(key_source):
                print(f"Error: file not found: {key_source}")
                return 1

            result = subprocess.run(
                ['rpm', '--import', key_source],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"Key imported from {key_source}")
                return 0
            else:
                print(f"Failed to import key: {result.stderr}")
                return 1

    # Remove key
    elif args.key_cmd in ('remove', 'rm', 'del'):
        import rpm
        if not check_root():
            print("Error: removing keys requires root privileges")
            return 1

        keyid = args.keyid.lower()

        # Find the key
        ts = rpm.TransactionSet()
        found = None
        for hdr in ts.dbMatch('name', 'gpg-pubkey'):
            version = hdr[rpm.RPMTAG_VERSION]
            if version.lower() == keyid:
                found = f"gpg-pubkey-{version}-{hdr[rpm.RPMTAG_RELEASE]}"
                break

        if not found:
            print(f"Key not found: {keyid}")
            print("Use 'urpm key list' to see installed keys")
            return 1

        # Confirm
        print(f"Removing key: {found}")
        try:
            response = input("Are you sure? [y/N] ")
            if response.lower() not in ('y', 'yes'):
                print("Aborted")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\nAborted")
            return 0

        result = subprocess.run(
            ['rpm', '-e', found],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("Key removed")
            return 0
        else:
            print(f"Failed to remove key: {result.stderr}")
            return 1

    else:
        print(f"Unknown key command: {args.key_cmd}")
        return 1
