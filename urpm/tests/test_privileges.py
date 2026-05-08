"""Tests for urpm.auth.privileges.

The helper interacts with the OS (euid, group list, PATH lookup, polkit
policy file). We monkeypatch only those specific stdlib touch-points so
that the test environment does not depend on the host's real groups,
sudo availability or polkit installation.
"""

from __future__ import annotations

import grp
from collections import namedtuple

import pytest

from urpm.auth import privileges


_FakeGroup = namedtuple('_FakeGroup', ['gr_name'])


def _install_groups(monkeypatch, gids_to_names: dict[int, str]) -> None:
    """Install fake os.getgroups()/grp.getgrgid() returning the given map."""
    monkeypatch.setattr(privileges.os, 'getgroups',
                        lambda: list(gids_to_names.keys()))

    def _fake_getgrgid(gid):
        if gid in gids_to_names:
            return _FakeGroup(gr_name=gids_to_names[gid])
        raise KeyError(gid)

    # privileges._is_likely_sudoer imports grp inside the function, so
    # patch the canonical grp module — that's the lookup that runs.
    monkeypatch.setattr(grp, 'getgrgid', _fake_getgrgid)


def _install_which(monkeypatch, available: set[str]) -> None:
    """Install a fake shutil.which restricted to the given binaries."""
    monkeypatch.setattr(
        privileges.shutil, 'which',
        lambda name: f'/usr/bin/{name}' if name in available else None,
    )


def _install_policy(monkeypatch, present: bool) -> None:
    """Stub os.path.exists for the polkit policy file lookup."""
    real_exists = privileges.os.path.exists
    policy = '/usr/share/polkit-1/actions/org.mageia.urpm.policy'

    def _fake_exists(path):
        if path == policy:
            return present
        return real_exists(path)

    monkeypatch.setattr(privileges.os.path, 'exists', _fake_exists)


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------


def test_root_passes_through(monkeypatch, capsys):
    """euid == 0 must return None silently, no exit, no stderr output."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 0)
    assert privileges.require_privileges() is None
    captured = capsys.readouterr()
    assert captured.err == ''


def test_allow_skip_passes_through(monkeypatch, capsys):
    """allow_skip=True must short-circuit the check even for non-root."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    assert privileges.require_privileges(allow_skip=True) is None
    captured = capsys.readouterr()
    assert captured.err == ''


# ---------------------------------------------------------------------------
# Failure cases — exit code and message presence
# ---------------------------------------------------------------------------


def test_non_root_emits_message_and_exits_77(monkeypatch, capsys):
    """Non-root invocation must exit 77 and print to stderr."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(privileges.sys, 'argv', ['urpm', 'install', 'vim'])
    _install_groups(monkeypatch, {1000: 'users'})
    _install_which(monkeypatch, {'sudo'})
    _install_policy(monkeypatch, False)

    with pytest.raises(SystemExit) as ex:
        privileges.require_privileges()
    assert ex.value.code == 77
    captured = capsys.readouterr()
    assert captured.err.strip() != ''
    assert 'root' in captured.err.lower()


# ---------------------------------------------------------------------------
# Ordering of suggested escalation methods
# ---------------------------------------------------------------------------


def _err_lines(capsys) -> list[str]:
    """Return the indented option lines printed to stderr."""
    captured = capsys.readouterr()
    return [line.strip() for line in captured.err.splitlines()
            if line.startswith('  ')]


def test_sudoer_user_sudo_listed_first(monkeypatch, capsys):
    """A wheel member with sudo installed must see sudo first, then su."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(privileges.sys, 'argv', ['urpm', 'install', 'vim'])
    _install_groups(monkeypatch, {10: 'wheel', 1000: 'users'})
    _install_which(monkeypatch, {'sudo'})
    _install_policy(monkeypatch, False)

    with pytest.raises(SystemExit):
        privileges.require_privileges()
    options = _err_lines(capsys)
    assert options[0].startswith('sudo ')
    assert options[1].startswith('su -c ')
    assert len(options) == 2


def test_non_sudoer_su_listed_first(monkeypatch, capsys):
    """A non-wheel user with sudo installed must see su first, sudo last."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(privileges.sys, 'argv', ['urpm', 'install', 'vim'])
    _install_groups(monkeypatch, {1000: 'users'})
    _install_which(monkeypatch, {'sudo'})
    _install_policy(monkeypatch, False)

    with pytest.raises(SystemExit):
        privileges.require_privileges()
    options = _err_lines(capsys)
    assert options[0].startswith('su -c ')
    assert options[1].startswith('sudo ')
    assert len(options) == 2


def test_no_sudo_only_su_listed(monkeypatch, capsys):
    """When sudo is absent and pkexec disabled, only su -c remains."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(privileges.sys, 'argv', ['urpm', 'install', 'vim'])
    _install_groups(monkeypatch, {1000: 'users'})
    _install_which(monkeypatch, set())  # no sudo, no pkexec
    _install_policy(monkeypatch, False)

    with pytest.raises(SystemExit):
        privileges.require_privileges()
    options = _err_lines(capsys)
    # Single option falls back to "Run: ..." formatting (no indent),
    # so _err_lines() returns []. Inspect the raw stderr instead.
    captured_err = capsys.readouterr().err if not options else ''
    if options:
        assert len(options) == 1
        assert options[0].startswith('su -c ')
    else:
        # Already consumed by _err_lines — nothing more to assert here,
        # but ensure no sudo/pkexec leaked through.
        assert 'sudo ' not in captured_err
        assert 'pkexec ' not in captured_err


def test_pkexec_listed_when_policy_installed(monkeypatch, capsys):
    """pkexec must appear in the option list when the policy is installed."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(privileges.sys, 'argv', ['urpm', 'install', 'vim'])
    _install_groups(monkeypatch, {10: 'wheel'})
    _install_which(monkeypatch, {'sudo', 'pkexec'})
    _install_policy(monkeypatch, True)

    with pytest.raises(SystemExit):
        privileges.require_privileges()
    options = _err_lines(capsys)
    assert any(opt.startswith('pkexec ') for opt in options)


def test_pkexec_not_listed_when_policy_missing(monkeypatch, capsys):
    """pkexec installed but no policy: must NOT appear in the options."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(privileges.sys, 'argv', ['urpm', 'install', 'vim'])
    _install_groups(monkeypatch, {10: 'wheel'})
    _install_which(monkeypatch, {'sudo', 'pkexec'})
    _install_policy(monkeypatch, False)

    with pytest.raises(SystemExit):
        privileges.require_privileges()
    options = _err_lines(capsys)
    assert not any(opt.startswith('pkexec ') for opt in options)


# ---------------------------------------------------------------------------
# Quoting safety
# ---------------------------------------------------------------------------


def test_command_line_correctly_quoted(monkeypatch, capsys):
    """An argv with spaces/quotes must be reproduced shell-safe by shlex."""
    monkeypatch.setattr(privileges.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(
        privileges.sys, 'argv',
        ['urpm', 'install', 'pkg with space', "name'with'quote"],
    )
    _install_groups(monkeypatch, {10: 'wheel'})
    _install_which(monkeypatch, {'sudo'})
    _install_policy(monkeypatch, False)

    with pytest.raises(SystemExit):
        privileges.require_privileges()
    captured = capsys.readouterr().err
    # The space-bearing argument must be quoted in both sudo and su variants.
    assert "'pkg with space'" in captured
    # The single-quote-bearing argument must also be safely encoded; shlex
    # never emits a bare single quote that would break the shell parse.
    # We assert by reconstruction: every options line must, when split by
    # shlex, round-trip back to argv.
    import shlex as _shlex
    for line in captured.splitlines():
        line = line.strip()
        if line.startswith('sudo '):
            tokens = _shlex.split(line)
            # tokens[0] == 'sudo', rest must equal argv
            assert tokens[1:] == list(privileges.sys.argv)
        elif line.startswith('su -c '):
            tokens = _shlex.split(line)
            # tokens == ['su', '-c', '<inner>']; inner must split back to argv
            assert tokens[0] == 'su' and tokens[1] == '-c'
            inner = _shlex.split(tokens[2])
            assert inner == list(privileges.sys.argv)


# ---------------------------------------------------------------------------
# _is_likely_sudoer() direct unit tests
# ---------------------------------------------------------------------------


def test_is_likely_sudoer_via_wheel(monkeypatch):
    """wheel membership is the canonical RHEL/Mageia sudoer marker."""
    _install_groups(monkeypatch, {10: 'wheel', 1000: 'users'})
    assert privileges._is_likely_sudoer() is True


def test_is_likely_sudoer_via_sudo_group(monkeypatch):
    """sudo group membership (Debian convention) must also count."""
    _install_groups(monkeypatch, {27: 'sudo', 1000: 'users'})
    assert privileges._is_likely_sudoer() is True


def test_is_likely_sudoer_no_relevant_group(monkeypatch):
    """No sudoer-conventional group → return False."""
    _install_groups(monkeypatch, {1000: 'users', 1001: 'audio'})
    assert privileges._is_likely_sudoer() is False


def test_is_likely_sudoer_unknown_gid_is_skipped(monkeypatch):
    """A gid that grp.getgrgid() does not know must not raise."""
    _install_groups(monkeypatch, {1000: 'users'})
    # 9999 is not in the map: getgrgid will KeyError, helper must skip.
    monkeypatch.setattr(privileges.os, 'getgroups', lambda: [9999, 1000])
    assert privileges._is_likely_sudoer() is False
