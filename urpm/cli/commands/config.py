"""Configuration management commands."""

import os
import subprocess
from typing import TYPE_CHECKING

from ...i18n import _, ngettext, confirm_yes
if TYPE_CHECKING:
    from ...core.database import PackageDatabase

from ..helpers.kernel import (
    read_config as _read_config,
    write_config as _write_config,
    get_blacklist as _get_blacklist,
    get_redlist as _get_redlist,
)


def cmd_config(args) -> int:
    """Handle config command - manage urpm configuration."""

    if not hasattr(args, 'config_cmd') or not args.config_cmd:
        print(_("Usage: urpm config <blacklist|redlist|kernel-keep|version-mode> ..."))
        print(_("\nSubcommands:"))
        print(_("  blacklist     Manage blacklist (critical packages)"))
        print(_("  redlist       Manage redlist (packages requiring confirmation)"))
        print(_("  kernel-keep   Number of kernels to keep"))
        print(_("  version-mode  Choose between system version and cauldron"))
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
                print(_("version-mode preference removed (auto-detection)"))
            else:
                db.set_config('version-mode', args.mode)
                print(_("version-mode set to '{mode}'").format(mode=args.mode))
            return 0
        else:
            # Show current state
            current = db.get_config('version-mode')
            system_version = get_system_version()
            accepted, needs_choice, info = get_accepted_versions(db, system_version)

            print("\n" + _("System version: {version}").format(version=system_version or _('unknown')))
            print(_("Configured preference: {pref}").format(pref=current or _('auto (none set)')))

            if info['cauldron_media']:
                extra = _(" (+{count} more)").format(count=len(info['cauldron_media'])-3) if len(info['cauldron_media']) > 3 else ""
                print(_("Cauldron media: {media}").format(media=', '.join(info['cauldron_media'][:3])) + extra)
            if info['system_version_media']:
                extra = _(" (+{count} more)").format(count=len(info['system_version_media'])-3) if len(info['system_version_media']) > 3 else ""
                print(_("System version media: {media}").format(media=', '.join(info['system_version_media'][:3])) + extra)

            if needs_choice:
                print("\n" + _("Conflict: Both {version} and cauldron media are enabled.").format(version=system_version))
                print(_("Use 'urpm config version-mode <system|cauldron>' to choose."))
            elif accepted:
                print("\n" + _("Active version filter: {versions}").format(versions=', '.join(sorted(accepted))))
            print()
            return 0

    config = _read_config()

    # Handle kernel-keep
    if args.config_cmd in ('kernel-keep', 'kk'):
        if hasattr(args, 'count') and args.count is not None:
            if args.count < 0:
                print(_("Error: kernel-keep must be >= 0"))
                return 1
            config['kernel_keep'] = args.count
            if _write_config(config):
                print(_("kernel-keep set to {count}").format(count=args.count))
                return 0
            return 1
        else:
            print(_("kernel-keep = {count}").format(count=config['kernel_keep']))
            return 0

    # Handle blacklist
    if args.config_cmd in ('blacklist', 'bl'):
        list_name = 'blacklist'
        builtin = _get_blacklist()
    elif args.config_cmd in ('redlist', 'rl'):
        list_name = 'redlist'
        builtin = _get_redlist()
    else:
        print(_("Unknown config command: {command}").format(command=args.config_cmd))
        return 1

    action = getattr(args, f'{list_name}_cmd', None)

    if not action or action in ('list', 'ls'):
        # Show list
        user_list = config.get(list_name, set())
        print("\n" + _("{name} (built-in):").format(name=list_name.title()))
        for pkg in sorted(builtin):
            print(f"  {pkg}")

        if user_list:
            print("\n" + _("{name} (user-configured):").format(name=list_name.title()))
            for pkg in sorted(user_list):
                print(f"  {pkg}")
        else:
            print("\n" + _("No user-configured {name} entries").format(name=list_name))

        print()
        return 0

    elif action in ('add', 'a'):
        pkg = args.package
        if pkg in builtin:
            print(_("{pkg} is already in the built-in {name}").format(pkg=pkg, name=list_name))
            return 0
        if pkg in config[list_name]:
            print(_("{pkg} is already in the user {name}").format(pkg=pkg, name=list_name))
            return 0
        config[list_name].add(pkg)
        if _write_config(config):
            print(_("Added {pkg} to {name}").format(pkg=pkg, name=list_name))
            return 0
        return 1

    elif action in ('remove', 'rm'):
        pkg = args.package
        if pkg in builtin:
            print(_("Error: {pkg} is in the built-in {name} and cannot be removed").format(pkg=pkg, name=list_name))
            return 1
        if pkg not in config[list_name]:
            print(_("{pkg} is not in the user {name}").format(pkg=pkg, name=list_name))
            return 1
        config[list_name].remove(pkg)
        if _write_config(config):
            print(_("Removed {pkg} from {name}").format(pkg=pkg, name=list_name))
            return 0
        return 1

    else:
        print(_("Usage: urpm config {name} <list|add|remove> [package]").format(name=list_name))
        return 1


def cmd_key(args) -> int:
    """Handle key command - manage GPG keys for package verification."""
    import rpm
    from ...core.install import check_root

    if not hasattr(args, 'key_cmd') or not args.key_cmd:
        print(_("Usage: urpm key <list|import|remove> ..."))
        print(_("\nCommands:"))
        print(_("  list            List installed GPG keys"))
        print(_("  import <file>   Import GPG key from file or HTTPS URL"))
        print(_("  remove <keyid>  Remove GPG key"))
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
            print(_("No GPG keys installed"))
            return 0

        print("\n" + _("Installed GPG keys ({count}):").format(count=len(keys)) + "\n")
        for version, release, summary in sorted(keys):
            print(f"  {version}-{release}")
            print(f"    {summary}")
        print()
        return 0

    # Import key
    elif args.key_cmd in ('import', 'i', 'add'):
        if not check_root():
            print(_("Error: importing keys requires root privileges"))
            return 1

        if not hasattr(args, 'keyfile') or not args.keyfile:
            print(_("Usage: urpm key import <keyfile|url>"))
            return 1

        key_source = args.keyfile

        # Check if it's an HTTPS URL
        if key_source.startswith('https://'):
            import tempfile
            import urllib.request
            import urllib.error

            print(_("Downloading key from {source}...").format(source=key_source))
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
                        print(_("Key imported from {source}").format(source=key_source))
                        return 0
                    else:
                        print(_("Failed to import key: {error}").format(error=result.stderr))
                        return 1
                finally:
                    os.unlink(tmp_path)

            except urllib.error.URLError as e:
                print(_("Error: failed to download key: {error}").format(error=e.reason))
                return 1
            except Exception as e:
                print(_("Error: {error}").format(error=e))
                return 1

        elif key_source.startswith('http://'):
            print(_("Error: HTTP URLs are not allowed for security reasons. Use HTTPS."))
            return 1

        else:
            # Import from local file
            if not os.path.exists(key_source):
                print(_("Error: file not found: {path}").format(path=key_source))
                return 1

            result = subprocess.run(
                ['rpm', '--import', key_source],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(_("Key imported from {source}").format(source=key_source))
                return 0
            else:
                print(_("Failed to import key: {error}").format(error=result.stderr))
                return 1

    # Remove key
    elif args.key_cmd in ('remove', 'rm', 'del'):
        import rpm
        if not check_root():
            print(_("Error: removing keys requires root privileges"))
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
            print(_("Key not found: {keyid}").format(keyid=keyid))
            print(_("Use 'urpm key list' to see installed keys"))
            return 1

        # Confirm
        print(_("Removing key: {key}").format(key=found))
        try:
            response = input(_("Are you sure? [y/N] "))
            if not confirm_yes(response):
                print(_("Aborted"))
                return 0
        except (KeyboardInterrupt, EOFError):
            print(_("\nAborted"))
            return 0

        result = subprocess.run(
            ['rpm', '-e', found],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(_("Key removed"))
            return 0
        else:
            print(_("Failed to remove key: {error}").format(error=result.stderr))
            return 1

    else:
        print(_("Unknown key command: {command}").format(command=args.key_cmd))
        return 1
