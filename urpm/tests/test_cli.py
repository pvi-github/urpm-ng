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
        parser = create_parser()
        args = parser.parse_args(['media', 'add', 'Core', 'http://example.com', '--update'])
        assert args.media_command == 'add'
        assert args.name == 'Core'
        assert args.url == 'http://example.com'
        assert args.update is True

    def test_update_command(self):
        parser = create_parser()
        args = parser.parse_args(['update', '--all', '-y'])
        assert args.command == 'update'
        assert args.all is True
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
        parser = create_parser()
        args = parser.parse_args(['--verbose', '--json', 'search', 'vim'])
        assert args.verbose is True
        assert args.json is True
        assert args.command == 'search'
