"""Sanity tests for --arch propagation across sub-commands.

These tests prove that the migration to the shared ``arch_parent`` parser
in ``urpm/cli/main.py`` did not break any of the five sub-commands that
declare an ``--arch`` option.
"""

from urpm.cli.main import create_parser


def _parse(argv):
    return create_parser().parse_args(argv)


# --- init --------------------------------------------------------------

def test_init_arch_set():
    args = _parse(['init', '--release', '10', '--arch', 'x86_64'])
    assert args.arch == 'x86_64'


def test_init_arch_default_none():
    args = _parse(['init', '--release', '10'])
    assert args.arch is None


# --- download ----------------------------------------------------------

def test_download_arch_set():
    args = _parse(['download', 'firefox', '--arch', 'x86_64'])
    assert args.arch == 'x86_64'


def test_download_arch_default_none():
    args = _parse(['download', 'firefox'])
    assert args.arch is None


# --- image make --------------------------------------------------------

def test_image_make_arch_set():
    args = _parse(['image', 'make', '-r', '10', '-t', 'foo', '--arch', 'x86_64'])
    assert args.arch == 'x86_64'


def test_image_make_arch_default_none():
    args = _parse(['image', 'make', '-r', '10', '-t', 'foo'])
    assert args.arch is None


# --- mkimage (legacy) --------------------------------------------------

def test_mkimage_arch_set():
    args = _parse(['mkimage', '-r', '10', '-t', 'foo', '--arch', 'x86_64'])
    assert args.arch == 'x86_64'


def test_mkimage_arch_default_none():
    args = _parse(['mkimage', '-r', '10', '-t', 'foo'])
    assert args.arch is None


# --- media autoconfig --------------------------------------------------

def test_media_autoconfig_arch_set():
    args = _parse(['media', 'autoconfig', '-r', '10', '--arch', 'x86_64'])
    assert args.arch == 'x86_64'


def test_media_autoconfig_arch_default_none():
    args = _parse(['media', 'autoconfig', '-r', '10'])
    assert args.arch is None


def test_media_autoconfig_alias_ac_arch():
    args = _parse(['media', 'ac', '-r', '10', '--arch', 'aarch64'])
    assert args.arch == 'aarch64'
