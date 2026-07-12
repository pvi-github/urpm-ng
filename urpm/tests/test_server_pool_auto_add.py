"""Tests for the ``PoolCheckResult.disabled_by_config`` distinction.

When the admin sets ``[server] auto_add = false`` we must NOT surface
the same "not enough mirrors" warning that a genuine network failure
produces — the admin made a deliberate choice and the CLI should
acknowledge it factually instead of alarming.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.server_pool import (
    PoolCheckResult,
    ensure_minimum_servers,
    minimum_servers_for,
)


def _fake_settings(auto_add: bool, pool_ratio: float = 1.5):
    """Build a stand-in for :func:`get_settings` returning
    ``settings.server.auto_add`` / ``settings.server.pool_ratio``.
    """
    return SimpleNamespace(
        server=SimpleNamespace(auto_add=auto_add, pool_ratio=pool_ratio),
    )


@pytest.fixture(autouse=True)
def _reenable_mirror_discovery(monkeypatch):
    """Undo the suite-wide ``URPM_SKIP_MIRROR_DISCOVERY`` from conftest.

    This file is the only place that exercises the real branch structure
    of ``ensure_minimum_servers``, so we need the short-circuit gone;
    other tests keep the default protection.
    """
    monkeypatch.delenv("URPM_SKIP_MIRROR_DISCOVERY", raising=False)


def test_pool_check_result_default_disabled_by_config_is_false():
    """The default value must be False so existing call sites that
    construct :class:`PoolCheckResult` without naming the field keep
    their previous semantics.
    """
    r = PoolCheckResult(sufficient=True, had=6, needed=6)
    assert r.disabled_by_config is False


def test_auto_add_false_marks_disabled_by_config(monkeypatch):
    """The main invariant: with ``auto_add=false`` and a too-small pool,
    the returned result must carry ``disabled_by_config=True`` so the
    CLI can pick the factual dim message instead of the anxious warning.
    """
    db = MagicMock()
    db.list_servers.return_value = [{'is_official': True}]  # only 1 mirror
    monkeypatch.setattr(
        "urpm.core.settings.get_settings",
        lambda: _fake_settings(auto_add=False),
    )
    # Skip network + version detection paths — they are exercised by
    # other tests; here we care about the branch decision only.
    monkeypatch.setattr(
        "urpm.core.server_pool._detect_version", lambda: None)

    result = ensure_minimum_servers(db, parallel=4)
    assert result.disabled_by_config is True
    assert result.sufficient is False
    assert result.had == 1
    # ceil(4 * 1.5) = 6
    assert result.needed == 6


def test_auto_add_true_but_no_version_does_not_flag_disabled(monkeypatch):
    """Absence of ``disabled_by_config`` is just as important — a fetch
    failure (here: version undetected) must NOT masquerade as an admin
    decision.
    """
    db = MagicMock()
    db.list_servers.return_value = [{'is_official': True}]
    monkeypatch.setattr(
        "urpm.core.settings.get_settings",
        lambda: _fake_settings(auto_add=True),
    )
    monkeypatch.setattr(
        "urpm.core.server_pool._detect_version", lambda: None)

    result = ensure_minimum_servers(db, parallel=4)
    assert result.disabled_by_config is False
    assert result.sufficient is False


def test_sufficient_pool_does_not_flag_disabled_even_if_auto_add_false(monkeypatch):
    """Corner: ``auto_add=false`` should not colour a state where the
    admin's own pool is already big enough.  The disabled flag exists
    only to explain the *insufficient* path.
    """
    db = MagicMock()
    db.list_servers.return_value = [{'is_official': True}] * 6
    monkeypatch.setattr(
        "urpm.core.settings.get_settings",
        lambda: _fake_settings(auto_add=False),
    )
    monkeypatch.setattr(
        "urpm.core.server_pool._detect_version", lambda: None)

    result = ensure_minimum_servers(db, parallel=4)
    assert result.sufficient is True
    assert result.disabled_by_config is False


def test_minimum_servers_for_parallel_1_returns_1():
    """Sanity: the pool-ratio formula still treats parallel=1 as a
    single-slot case, so no admin ever gets the "need 2 mirrors"
    surprise for a serial setup."""
    assert minimum_servers_for(1) == 1
    assert minimum_servers_for(1, pool_ratio=2.5) == 1
