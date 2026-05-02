"""Tests for arch resolution helpers in urpm.cli.helpers.package."""

import argparse

from urpm.cli.helpers import package as pkg_helpers
from urpm.cli.helpers.package import resolve_target_arch, system_arch


def test_system_arch_returns_platform_machine(monkeypatch):
    """system_arch() must return whatever platform.machine() returns."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'aarch64')
    assert system_arch() == 'aarch64'


def test_resolve_target_arch_with_arch_set():
    """An explicit args.arch must take precedence over the host arch."""
    args = argparse.Namespace(arch='i686')
    assert resolve_target_arch(args) == 'i686'


def test_resolve_target_arch_with_arch_none(monkeypatch):
    """A None args.arch must fall back to system_arch()."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')
    args = argparse.Namespace(arch=None)
    assert resolve_target_arch(args) == 'x86_64'


def test_resolve_target_arch_with_no_arch_attr(monkeypatch):
    """If args has no 'arch' attribute, getattr fallback must kick in."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'armv7hl')
    args = argparse.Namespace()
    assert not hasattr(args, 'arch')
    assert resolve_target_arch(args) == 'armv7hl'


def test_resolve_target_arch_empty_string_falls_back(monkeypatch):
    """An empty-string args.arch is falsy and must fall back to host."""
    monkeypatch.setattr(pkg_helpers.platform, 'machine', lambda: 'x86_64')
    args = argparse.Namespace(arch='')
    assert resolve_target_arch(args) == 'x86_64'
