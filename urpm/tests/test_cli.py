"""Tests for CLI"""

import pytest
from urpm.cli.main import create_parser


class TestParser:
    """Tests for argument parser."""

    def test_version_flag(self):
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(['--version'])

    def test_install_command(self):
        parser = create_parser()
        args = parser.parse_args(['install', 'firefox', 'vim'])
        assert args.command == 'install'
        assert args.packages == ['firefox', 'vim']

    def test_install_alias(self):
        parser = create_parser()
        args = parser.parse_args(['i', 'firefox'])
        assert args.command == 'i'
        assert args.packages == ['firefox']

    def test_install_with_flags(self):
        parser = create_parser()
        args = parser.parse_args(['install', '-y', '--test', 'firefox'])
        assert args.auto is True
        assert args.test is True

    def test_search_command(self):
        parser = create_parser()
        args = parser.parse_args(['search', 'firefox'])
        assert args.command == 'search'
        assert args.pattern == 'firefox'

    def test_search_alias(self):
        parser = create_parser()
        args = parser.parse_args(['s', 'vim'])
        assert args.command == 's'
        assert args.pattern == 'vim'

    def test_show_command(self):
        parser = create_parser()
        args = parser.parse_args(['show', 'firefox'])
        assert args.command == 'show'
        assert args.package == 'firefox'

    def test_show_aliases(self):
        parser = create_parser()

        args = parser.parse_args(['sh', 'vim'])
        assert args.command == 'sh'

        args = parser.parse_args(['info', 'vim'])
        assert args.command == 'info'

    def test_media_list(self):
        parser = create_parser()
        args = parser.parse_args(['media', 'list'])
        assert args.command == 'media'
        assert args.media_command == 'list'

    def test_media_alias(self):
        parser = create_parser()
        args = parser.parse_args(['m', 'l'])
        assert args.command == 'm'
        assert args.media_command == 'l'

    def test_media_add(self):
        """Test media add command - url is positional, --name/--update are options."""
        parser = create_parser()
        # Current syntax: media add <url> [--name NAME] [--update]
        args = parser.parse_args([
            'media', 'add', 'http://example.com/media/', '--update'
        ])
        assert args.media_command == 'add'
        assert args.url == 'http://example.com/media/'
        assert args.update is True

    def test_media_add_with_name(self):
        """Test media add with legacy --name option."""
        parser = create_parser()
        args = parser.parse_args([
            'media', 'add', 'http://example.com/repo/', '--name', 'My Repo'
        ])
        assert args.media_command == 'add'
        assert args.url == 'http://example.com/repo/'
        assert args.name == 'My Repo'

    def test_media_add_custom(self):
        """Test media add with --custom for third-party repos."""
        parser = create_parser()
        args = parser.parse_args([
            'media', 'add', 'http://example.com/custom/',
            '--custom', 'My Custom Repo', 'mycustom'
        ])
        assert args.media_command == 'add'
        assert args.url == 'http://example.com/custom/'
        assert args.custom == ['My Custom Repo', 'mycustom']

    def test_media_update_command(self):
        """Test media update with optional media name."""
        parser = create_parser()
        # Update all media
        args = parser.parse_args(['media', 'update'])
        assert args.media_command == 'update'
        assert args.name is None

        # Update specific media
        args = parser.parse_args(['media', 'update', 'Core Release'])
        assert args.media_command == 'update'
        assert args.name == 'Core Release'

    def test_upgrade_command(self):
        """Test upgrade command with auto-confirm flag."""
        parser = create_parser()
        args = parser.parse_args(['upgrade', '-y'])
        assert args.command == 'upgrade'
        assert args.auto is True
        assert args.packages == []

    def test_upgrade_specific_packages(self):
        """Test upgrade of specific packages."""
        parser = create_parser()
        args = parser.parse_args(['upgrade', 'firefox', 'vim', '-y'])
        assert args.command == 'upgrade'
        assert args.packages == ['firefox', 'vim']
        assert args.auto is True

    def test_list_command(self):
        parser = create_parser()
        args = parser.parse_args(['list', 'updates'])
        assert args.command == 'list'
        assert args.filter == 'updates'

    def test_list_default(self):
        parser = create_parser()
        args = parser.parse_args(['list'])
        assert args.filter == 'installed'

    def test_depends_command(self):
        parser = create_parser()
        args = parser.parse_args(['depends', 'firefox', '--tree'])
        assert args.command == 'depends'
        assert args.package == 'firefox'
        assert args.tree is True

    def test_global_flags(self):
        """Test global flags that exist: --verbose, --quiet, --nocolor."""
        parser = create_parser()
        args = parser.parse_args(['--verbose', 'search', 'vim'])
        assert args.verbose is True
        assert args.command == 'search'

    def test_global_quiet_flag(self):
        """Test --quiet flag."""
        parser = create_parser()
        args = parser.parse_args(['--quiet', 'install', 'vim'])
        assert args.quiet is True
        assert args.command == 'install'

    def test_root_option(self):
        """Test --root for alternate install root."""
        parser = create_parser()
        args = parser.parse_args(['--root', '/mnt/target', 'install', 'vim'])
        assert args.root == '/mnt/target'
        assert args.command == 'install'

    def test_erase_command(self):
        """Test erase/remove command."""
        parser = create_parser()
        args = parser.parse_args(['erase', 'vim', '-y'])
        assert args.command == 'erase'
        assert args.packages == ['vim']
        assert args.auto is True

    def test_erase_alias(self):
        """Test erase alias: e."""
        parser = create_parser()

        # Only 'e' is a valid alias for 'erase'
        args = parser.parse_args(['e', 'vim'])
        assert args.command == 'e'
        assert args.packages == ['vim']

    def test_autoremove_command(self):
        """Test autoremove command."""
        parser = create_parser()
        args = parser.parse_args(['autoremove', '--orphans'])
        assert args.command == 'autoremove'
        assert args.orphans is True

    def test_server_list(self):
        """Test server list command."""
        parser = create_parser()
        args = parser.parse_args(['server', 'list'])
        assert args.command == 'server'
        assert args.server_command == 'list'

    def test_cache_commands(self):
        """Test cache subcommands."""
        parser = create_parser()

        args = parser.parse_args(['cache', 'clean'])
        assert args.command == 'cache'
        assert args.cache_command == 'clean'

        args = parser.parse_args(['cache', 'stats'])
        assert args.cache_command == 'stats'
