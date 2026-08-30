"""Tests for :mod:`urpm.core.userns_env`.

Covers the env-leak regression reported by a packager whose
``~/.rpmmacros`` redefined ``%_topdir`` / ``%_tmppath`` to
``$HOME/rpmbuild/...`` — those variables reached rpm inside the
``podman unshare`` bootstrap and broke the whole ``filesystem`` /
``shadow-utils`` scriptlet cascade.  The hermetic env returned by
:func:`bootstrap_env` scrubs the parent environment down to a
whitelist so the bug can't repeat.
"""

from __future__ import annotations

import os

import pytest

from urpm.core import userns_env
from urpm.core.userns_env import PROXY_VARS, bootstrap_env


@pytest.fixture
def clean_env(monkeypatch):
    """Start each test with a minimal, deterministic parent env."""
    for name in list(os.environ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return monkeypatch


def test_returns_fresh_dict_not_environ(clean_env):
    env = bootstrap_env("/var/tmp/chroot")
    assert env is not os.environ
    env["INJECT"] = "should-not-leak"
    assert "INJECT" not in os.environ


def test_scrubs_operator_rpmmacros_related_vars(clean_env):
    """The reported regression : operator's ``$HOME`` reaching the child.

    ``~/.rpmmacros`` set ``%_topdir %(echo $HOME)/rpmbuild`` — with the
    parent's ``HOME`` inherited, rpm expands that to
    ``/home/cyril/rpmbuild`` inside the bootstrap, and every scriptlet
    that tries to write under ``%_tmppath`` fails.  ``bootstrap_env``
    pins ``HOME=/root`` so ``%_topdir`` expands inside the chroot.
    """
    clean_env.setenv("HOME", "/home/packager")
    clean_env.setenv("XDG_CONFIG_HOME", "/home/packager/.config")
    clean_env.setenv("TMPDIR", "/home/packager/tmp")

    env = bootstrap_env("/var/tmp/chroot")

    assert env["HOME"] == "/var/tmp/chroot"
    assert env["TMPDIR"] == "/var/tmp/chroot/tmp"
    assert "XDG_CONFIG_HOME" not in env


def test_locale_is_pinned_deterministic(clean_env):
    clean_env.setenv("LC_ALL", "fr_FR.UTF-8")
    clean_env.setenv("LANG", "fr_FR.UTF-8")
    clean_env.setenv("LANGUAGE", "fr_FR:fr")

    env = bootstrap_env("/var/tmp/chroot")

    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["LANGUAGE"] == "C"


def test_systemd_offline_and_tz_pinned(clean_env):
    env = bootstrap_env("/var/tmp/chroot")
    assert env["SYSTEMD_OFFLINE"] == "1"
    assert env["TZ"] == "UTC"


def test_path_is_deterministic_no_operator_local_bin(clean_env):
    clean_env.setenv("PATH", "/home/packager/.local/bin:/usr/bin")
    env = bootstrap_env("/var/tmp/chroot")
    assert env["PATH"] == "/usr/bin:/usr/sbin:/bin:/sbin"


def test_proxy_off_by_default(clean_env):
    for name in PROXY_VARS:
        clean_env.setenv(name, "http://proxy.example:3128")
    env = bootstrap_env("/var/tmp/chroot")
    for name in PROXY_VARS:
        assert name not in env, f"{name} should not leak when forward_proxy=False"


def test_proxy_forwarded_when_requested(clean_env):
    clean_env.setenv("http_proxy", "http://proxy.example:3128")
    clean_env.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    clean_env.setenv("no_proxy", "localhost,mageia.internal")

    env = bootstrap_env("/var/tmp/chroot", forward_proxy=True)

    assert env["http_proxy"] == "http://proxy.example:3128"
    assert env["HTTPS_PROXY"] == "http://proxy.example:3128"
    assert env["no_proxy"] == "localhost,mageia.internal"


def test_proxy_absent_in_parent_still_absent_when_on(clean_env):
    env = bootstrap_env("/var/tmp/chroot", forward_proxy=True)
    for name in PROXY_VARS:
        assert name not in env


def test_passthrough_forwards_only_when_set(clean_env):
    clean_env.setenv("URPM_MKIMAGE_SILENCE", "1")
    # URPM_HOST_LANG deliberately unset

    env = bootstrap_env(
        "/var/tmp/chroot",
        passthrough=("URPM_MKIMAGE_SILENCE", "URPM_HOST_LANG"),
    )

    assert env["URPM_MKIMAGE_SILENCE"] == "1"
    assert "URPM_HOST_LANG" not in env


def test_term_forwarded_only_with_tty(clean_env, monkeypatch):
    clean_env.setenv("TERM", "xterm-256color")

    monkeypatch.setattr(userns_env, "_stdout_is_tty", lambda: True)
    env = bootstrap_env("/var/tmp/chroot")
    assert env["TERM"] == "xterm-256color"

    monkeypatch.setattr(userns_env, "_stdout_is_tty", lambda: False)
    env = bootstrap_env("/var/tmp/chroot")
    assert env["TERM"] == "dumb"


def test_tmpdir_pinned_inside_chroot_root(clean_env):
    env = bootstrap_env("/opt/build/mga11-chroot")
    assert env["TMPDIR"] == "/opt/build/mga11-chroot/tmp"


def test_no_arbitrary_operator_var_leaks(clean_env):
    """Sanity : anything the operator sets that isn't whitelisted stays out."""
    clean_env.setenv("SSH_AGENT_PID", "12345")
    clean_env.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    clean_env.setenv("EDITOR", "vim")
    clean_env.setenv("GPG_TTY", "/dev/pts/3")

    env = bootstrap_env("/var/tmp/chroot")

    for leaked in ("SSH_AGENT_PID", "DBUS_SESSION_BUS_ADDRESS", "EDITOR", "GPG_TTY"):
        assert leaked not in env
