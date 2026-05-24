"""Tests for arch resolution helpers in urpm.cli.helpers.package."""

import argparse
from types import SimpleNamespace

import pytest

from urpm.cli.helpers import package as pkg_helpers
from urpm.cli.helpers.package import resolve_target_arch, system_arch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_system_arch_cache():
    """Drop the ``lru_cache`` between tests.

    ``system_arch()`` caches its first answer for the lifetime of
    the process — perfect in production, lethal in a test suite
    where each scenario mocks the underlying source differently.
    """
    system_arch.cache_clear()
    yield
    system_arch.cache_clear()


def _fake_rpm_with(probe_arches):
    """Build a stand-in for the ``rpm`` Python module.

    ``probe_arches`` is a mapping of probe-package name (``filesystem``,
    ``glibc``) to the arch string the fake rpmdb should report.  A
    missing entry models a not-installed package (empty match).
    """
    class _Header(dict):
        pass

    class _MatchIterator:
        def __init__(self, name, arches):
            self._headers = []
            if name in arches:
                h = _Header()
                h[42] = arches[name]  # 42 stands in for RPMTAG_ARCH
                self._headers.append(h)

        def __iter__(self):
            return iter(self._headers)

    class _TransactionSet:
        def __init__(self):
            self._arches = probe_arches

        def dbMatch(self, _tag, name):
            return _MatchIterator(name, self._arches)

    rpm = SimpleNamespace(
        TransactionSet=_TransactionSet,
        RPMTAG_NAME=1000,
        RPMTAG_ARCH=42,
    )
    return rpm


# ---------------------------------------------------------------------------
# system_arch — rpmdb-driven path (primary)
# ---------------------------------------------------------------------------


def test_system_arch_reads_filesystem_arch_from_rpmdb(monkeypatch):
    """Primary path: ``filesystem``'s arch wins, even if uname differs."""
    rpm = _fake_rpm_with({'filesystem': 'i686'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')

    assert system_arch() == 'i686'


def test_system_arch_falls_back_to_glibc_when_filesystem_missing(monkeypatch):
    """If ``filesystem`` is somehow not installed, ``glibc`` is the
    secondary probe."""
    rpm = _fake_rpm_with({'glibc': 'aarch64'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')

    assert system_arch() == 'aarch64'


def test_system_arch_ignores_noarch_probe(monkeypatch):
    """A probe whose ARCH is ``noarch`` (theoretical, never on Mageia)
    must not be returned: it tells us nothing about the user-space."""
    rpm = _fake_rpm_with({'filesystem': 'noarch', 'glibc': 'i686'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')

    assert system_arch() == 'i686'


def test_system_arch_decodes_bytes_arch(monkeypatch):
    """Some ``python3-rpm`` versions return header strings as bytes —
    the helper must decode transparently."""
    rpm = _fake_rpm_with({'filesystem': b'aarch64'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')

    assert system_arch() == 'aarch64'


# ---------------------------------------------------------------------------
# system_arch — fallback path (rpm absent / unreadable)
# ---------------------------------------------------------------------------


def test_system_arch_falls_back_when_rpm_import_fails(monkeypatch):
    """When the ``rpm`` Python module isn't importable, fall back to
    ``platform.machine()`` so the helper still answers something."""
    import sys
    monkeypatch.setitem(sys.modules, 'rpm', None)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')

    assert system_arch() == 'x86_64'


def test_system_arch_falls_back_when_rpm_raises(monkeypatch):
    """Any rpm-side exception (locked db, corrupted index, …) must
    degrade to ``platform.machine()`` rather than crash."""

    class _Boom:
        def dbMatch(self, *a, **kw):
            raise RuntimeError("rpmdb is on fire")

    rpm = SimpleNamespace(
        TransactionSet=lambda: _Boom(),
        RPMTAG_NAME=1000,
        RPMTAG_ARCH=42,
    )
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'aarch64')

    assert system_arch() == 'aarch64'


def test_system_arch_falls_back_when_both_probes_absent(monkeypatch):
    """In a minimal bootstrap chroot where neither ``filesystem`` nor
    ``glibc`` is installed yet, fall back to the kernel arch."""
    rpm = _fake_rpm_with({})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')

    assert system_arch() == 'x86_64'


# ---------------------------------------------------------------------------
# resolve_target_arch — thin wrapper around system_arch()
# ---------------------------------------------------------------------------


def test_resolve_target_arch_with_arch_set():
    """An explicit ``args.arch`` must take precedence over the host arch."""
    args = argparse.Namespace(arch='i686')
    assert resolve_target_arch(args) == 'i686'


def test_resolve_target_arch_with_arch_none(monkeypatch):
    """A ``None`` args.arch must fall back to ``system_arch()``."""
    rpm = _fake_rpm_with({'filesystem': 'x86_64'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    args = argparse.Namespace(arch=None)
    assert resolve_target_arch(args) == 'x86_64'


def test_resolve_target_arch_with_no_arch_attr(monkeypatch):
    """If ``args`` has no ``arch`` attribute, the ``getattr`` fallback
    must kick in."""
    rpm = _fake_rpm_with({'filesystem': 'armv7hl'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    args = argparse.Namespace()
    assert not hasattr(args, 'arch')
    assert resolve_target_arch(args) == 'armv7hl'


def test_resolve_target_arch_empty_string_falls_back(monkeypatch):
    """An empty-string ``args.arch`` is falsy and must fall back to
    the host arch."""
    rpm = _fake_rpm_with({'filesystem': 'x86_64'})
    monkeypatch.setitem(__import__('sys').modules, 'rpm', rpm)
    args = argparse.Namespace(arch='')
    assert resolve_target_arch(args) == 'x86_64'
