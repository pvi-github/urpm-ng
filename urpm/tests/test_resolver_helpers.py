"""Tests for ``urpm.cli.helpers.resolver.create_resolver`` arch wiring.

The helper must honour the priority chain:

    explicit ``arch=...`` kwarg  >  ``args.arch``  >  ``system_arch()``

These tests inject a stub Resolver class so we can observe the ``arch``
value the helper passes through, without needing a working libsolv pool.
"""

import argparse

import pytest

from urpm.cli.helpers import package as pkg_helpers
from urpm.cli.helpers import resolver as resolver_helpers


class _StubResolver:
    """Minimal Resolver double that records constructor kwargs.

    The helper does ``Resolver(db, root=..., urpm_root=..., **kwargs)``
    so we accept the same shape and stash everything for inspection.
    """

    def __init__(self, db, root=None, urpm_root=None, **kwargs):
        self.db = db
        self.root = root
        self.urpm_root = urpm_root
        # Forward arch + everything else for assertion inspection.
        self.arch = kwargs.get('arch')
        self.kwargs = kwargs


@pytest.fixture
def stub_resolver(monkeypatch):
    """Patch ``urpm.core.resolver.Resolver`` with the stub.

    The helper does ``from ...core.resolver import Resolver`` *inside*
    the function body, so the patch must target the source module
    rather than the helper's namespace.
    """
    monkeypatch.setattr('urpm.core.resolver.Resolver', _StubResolver)
    return _StubResolver


def _bare_args(**kwargs):
    """Build a Namespace with only the attributes set by the test.

    All other ``getattr(args, ..., default)`` lookups in the helper
    will hit their defaults — this is exactly the scenario when a
    sub-command parser does not declare an option.
    """
    return argparse.Namespace(**kwargs)


# --- Case 1 : args.arch set, no kwarg ---------------------------------

def test_args_arch_propagates_when_no_kwarg(stub_resolver, monkeypatch):
    """args.arch=i686 must reach the Resolver when no kwarg overrides it."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')
    args = _bare_args(arch='i686')
    r = resolver_helpers.create_resolver(db=None, args=args)
    assert r.arch == 'i686'


# --- Case 2 : args.arch=None, explicit kwarg --------------------------

def test_explicit_kwarg_wins_over_none_args(stub_resolver, monkeypatch):
    """An explicit ``arch=`` kwarg must win over a None args.arch."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'aarch64')
    args = _bare_args(arch=None)
    r = resolver_helpers.create_resolver(db=None, args=args, arch='x86_64')
    assert r.arch == 'x86_64'


# --- Case 3 : args.arch set AND kwarg explicit ------------------------

def test_explicit_kwarg_wins_over_args_arch(stub_resolver):
    """Regression-proof: explicit kwarg must beat args.arch.

    This is the case exercised by ``cmd_download``, which precomputes
    ``target_arch = resolve_target_arch(args)`` and passes it in as a
    kwarg.  We must not regress that priority.
    """
    args = _bare_args(arch='i686')
    r = resolver_helpers.create_resolver(db=None, args=args, arch='x86_64')
    assert r.arch == 'x86_64'


# --- Case 4 : args has no arch attribute, no kwarg --------------------

def test_fallback_to_system_arch_when_no_attr(stub_resolver, monkeypatch):
    """When args lacks 'arch' entirely, the helper falls back to system_arch().

    Patches ``system_arch`` directly because the real implementation now
    probes the rpmdb (``filesystem``/``glibc`` ARCH header) before
    falling back to ``platform.machine()``, so mocking ``platform`` no
    longer steers the result on a machine where those packages exist.
    The contract under test is ``create_resolver -> resolve_target_arch
    -> system_arch``, not ``system_arch``'s internal probe order.
    """
    monkeypatch.setattr(pkg_helpers, 'system_arch', lambda: 'armv7hl')
    args = _bare_args()  # No arch attribute at all.
    assert not hasattr(args, 'arch')
    r = resolver_helpers.create_resolver(db=None, args=args)
    assert r.arch == 'armv7hl'


# --- Case 5 : end-to-end through the CLI parser -----------------------

def test_install_arch_flag_propagates_end_to_end(stub_resolver, monkeypatch):
    """``urpm install --arch i686 foo`` must reach the Resolver as i686.

    This is the integration test that proves the parser change *and*
    the helper change cooperate: parse_args produces args.arch='i686',
    create_resolver picks it up, and the Resolver receives arch='i686'.
    """
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')
    from urpm.cli.main import create_parser

    args = create_parser().parse_args(['install', '--arch', 'i686', 'foo'])
    assert args.arch == 'i686'  # parser sanity

    r = resolver_helpers.create_resolver(db=None, args=args)
    assert r.arch == 'i686'


def test_upgrade_no_arch_falls_back(stub_resolver, monkeypatch):
    """``urpm upgrade`` (no --arch) must reach Resolver with system arch."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')
    from urpm.cli.main import create_parser

    args = create_parser().parse_args(['upgrade'])
    assert args.arch is None  # parser sanity

    r = resolver_helpers.create_resolver(db=None, args=args)
    assert r.arch == 'x86_64'
