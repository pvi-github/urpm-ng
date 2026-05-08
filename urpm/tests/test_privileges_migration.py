"""Tests for the migration of CLI commands from check_root to require_privileges.

Commit B of the privilege-helper migration series. Commit A introduced
:func:`urpm.auth.privileges.require_privileges` (covered by 13 tests in
``test_privileges.py``); this module verifies that each CLI call site
that previously used the legacy ``check_root()`` helper now invokes
``require_privileges`` with the expected polkit ``action_id``.

Two test strategies are used:

* End-to-end: when the privilege check sits at (or very near) the top of
  the command function, we drive the real callable with a minimal
  ``argparse.Namespace`` and a mock database, monkeypatch ``os.geteuid``
  to a non-root uid, and assert the function exits 77.  This exercises
  the actual wiring (import, call, exit) without mocking the helper.

* Source-level: when the privilege check is buried behind a confirmation
  prompt, URL parser, or interactive flow that would require heavy
  setup to reach, we assert against the parsed AST that the function
  contains a ``require_privileges`` call with the expected ``action_id``
  string.  This is a lighter-weight check but still verifies the
  migration wiring (the call exists and references the right polkit
  action) without resorting to mocking the helper itself.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import os
from unittest.mock import MagicMock

import pytest

from urpm.auth import privileges as _privileges_module


# ---------------------------------------------------------------------------
# E2E tests — drive the real cmd_* function and assert SystemExit(77)
# ---------------------------------------------------------------------------


def _force_non_root(monkeypatch) -> None:
    """Make every euid lookup go through ``privileges.os.geteuid`` as 1000.

    The helper imports ``os`` at module load and reads ``os.geteuid``
    from that bound reference, so we patch that attribute specifically.
    """
    monkeypatch.setattr(_privileges_module.os, 'geteuid', lambda: 1000)
    # Some helpers may import their own os; patch the global one too
    # for safety.
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)


def _quiet_polkit(monkeypatch) -> None:
    """Stub the polkit-policy lookup to keep stderr deterministic."""
    real_exists = os.path.exists

    def _fake_exists(path):
        if path.endswith('/org.mageia.urpm.policy'):
            return False
        return real_exists(path)

    monkeypatch.setattr(_privileges_module.os.path, 'exists', _fake_exists)


@pytest.mark.parametrize("cmd_path,build_args", [
    # cmd_undo: privilege check is the first executable statement
    (
        'urpm.cli.commands.history:cmd_undo',
        lambda: argparse.Namespace(transaction_id=None),
    ),
    # cmd_rollback: privilege check is the first executable statement
    (
        'urpm.cli.commands.history:cmd_rollback',
        lambda: argparse.Namespace(args=[]),
    ),
    # cmd_media_update: privilege check is the first executable statement
    (
        'urpm.cli.commands.media:cmd_media_update',
        lambda: argparse.Namespace(media=None, files=False, force=False),
    ),
])
def test_command_requires_root_e2e(monkeypatch, capsys, cmd_path, build_args):
    """Each migrated command must exit 77 when run unprivileged.

    The args namespace only needs to satisfy ``getattr`` calls performed
    *before* the privilege check; the helper exits the process before
    any of the heavy machinery is touched.
    """
    _force_non_root(monkeypatch)
    _quiet_polkit(monkeypatch)
    monkeypatch.setattr('sys.argv', ['urpm', 'test'])

    module_path, fn_name = cmd_path.split(':')
    module = __import__(module_path, fromlist=[fn_name])
    fn = getattr(module, fn_name)

    args = build_args()
    db = MagicMock()
    with pytest.raises(SystemExit) as ex:
        fn(args, db)
    assert ex.value.code == 77, (
        f"{cmd_path} did not exit 77 on non-root invocation"
    )
    captured = capsys.readouterr()
    assert 'root' in captured.err.lower(), (
        f"{cmd_path} did not mention 'root' in the error message"
    )


def test_cmd_key_import_requires_root_e2e(monkeypatch, capsys):
    """``urpm key import`` must exit 77 when run unprivileged."""
    from urpm.cli.commands.config import cmd_key
    _force_non_root(monkeypatch)
    _quiet_polkit(monkeypatch)
    monkeypatch.setattr('sys.argv', ['urpm', 'key', 'import', 'foo.gpg'])

    args = argparse.Namespace(key_cmd='import', keyfile='foo.gpg')
    with pytest.raises(SystemExit) as ex:
        cmd_key(args)
    assert ex.value.code == 77
    assert 'root' in capsys.readouterr().err.lower()


def test_cmd_key_remove_requires_root_e2e(monkeypatch, capsys):
    """``urpm key remove`` must exit 77 when run unprivileged."""
    from urpm.cli.commands.config import cmd_key
    _force_non_root(monkeypatch)
    _quiet_polkit(monkeypatch)
    monkeypatch.setattr('sys.argv', ['urpm', 'key', 'remove', 'AABBCCDD'])

    args = argparse.Namespace(key_cmd='remove', keyid='AABBCCDD')
    with pytest.raises(SystemExit) as ex:
        cmd_key(args)
    assert ex.value.code == 77
    assert 'root' in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Source-level tests — assert the AST contains require_privileges(action_id=...)
# ---------------------------------------------------------------------------


def _function_calls_require_privileges(fn, expected_action_id: str) -> bool:
    """True if the function source contains a require_privileges call
    with ``action_id=<expected_action_id>`` (positional or keyword).

    We parse the function source, walk for ``Call`` nodes whose function
    is named ``require_privileges``, and check that one of them carries
    the expected action id either as the first positional argument or
    as the ``action_id=`` keyword.
    """
    import textwrap
    source = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name != 'require_privileges':
            continue
        # Positional first arg
        if node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and arg0.value == expected_action_id:
                return True
        # Keyword action_id=...
        for kw in node.keywords:
            if kw.arg == 'action_id' and isinstance(kw.value, ast.Constant):
                if kw.value.value == expected_action_id:
                    return True
    return False


@pytest.mark.parametrize("import_path,expected_action_id", [
    # Direct e2e-tested ones — also verify AST for the action_id contract.
    ('urpm.cli.commands.history:cmd_undo',
     'org.mageia.urpm.install'),
    ('urpm.cli.commands.history:cmd_rollback',
     'org.mageia.urpm.install'),
    ('urpm.cli.commands.media:cmd_media_update',
     'org.mageia.urpm.refresh'),
    ('urpm.cli.commands.config:cmd_key',
     'org.mageia.urpm.media-manage'),
    # Source-only sites (deep flow, not driven e2e).
    ('urpm.cli.commands.install:cmd_install',
     'org.mageia.urpm.install'),
    ('urpm.cli.commands.upgrade:cmd_upgrade',
     'org.mageia.urpm.upgrade'),
    ('urpm.cli.commands.remove:cmd_erase',
     'org.mageia.urpm.remove'),
    ('urpm.cli.commands.cleanup:cmd_autoremove',
     'org.mageia.urpm.remove'),
    ('urpm.cli.commands.cleanup:cmd_cleandeps',
     'org.mageia.urpm.remove'),
    ('urpm.cli.commands.media:cmd_media_add',
     'org.mageia.urpm.media-manage'),
    ('urpm.cli.commands.config:_cmd_auto_upgrade_policy',
     'org.mageia.urpm.media-manage'),
    ('urpm.cli.commands.query:cmd_find',
     'org.mageia.urpm.media-manage'),
])
def test_command_invokes_require_privileges_with_action_id(
    import_path, expected_action_id,
):
    """Each migrated command's source must contain a require_privileges
    call referencing the expected polkit action id."""
    module_path, fn_name = import_path.split(':')
    module = __import__(module_path, fromlist=[fn_name])
    fn = getattr(module, fn_name)
    assert _function_calls_require_privileges(fn, expected_action_id), (
        f"{import_path} does not call require_privileges("
        f"action_id={expected_action_id!r})"
    )


def test_no_check_root_left_in_cli_commands():
    """No ``urpm/cli/commands/*.py`` file may still reference check_root.

    This is the structural invariant that commit B establishes.  The
    legacy helper survives in ``urpm/core/install.py`` until the
    follow-up cleanup commit, but no CLI command should reach for it.
    """
    import pathlib
    cli_dir = pathlib.Path(__file__).resolve().parent.parent / 'cli' / 'commands'
    offending: list[str] = []
    for py in cli_dir.glob('*.py'):
        text = py.read_text(encoding='utf-8')
        if 'check_root' in text:
            offending.append(str(py))
    assert not offending, (
        "check_root still referenced in CLI commands: " + ", ".join(offending)
    )
