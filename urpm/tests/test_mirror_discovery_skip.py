"""Tests for the ``URPM_SKIP_MIRROR_DISCOVERY`` env-var short-circuit.

The conftest.py autouse fixture sets this env var for every test, so
verifying that it *actually* prevents any network work — not just
suppresses the ``Auto-added`` line — matters: a regression that let
the discovery re-run despite the flag would silently reintroduce the
flakes we set out to remove.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from urpm.core.server_pool import PoolCheckResult, ensure_minimum_servers


def test_env_var_short_circuits_before_any_side_effect(monkeypatch):
    """With the env var set, none of the discovery helpers must be
    called.  We replace them with sentinels that would raise if
    reached; the test passes only if ``ensure_minimum_servers``
    returns *before* touching any of them.
    """
    monkeypatch.setenv("URPM_SKIP_MIRROR_DISCOVERY", "1")

    def _fail(*a, **k):
        raise AssertionError(
            "discovery helper called despite URPM_SKIP_MIRROR_DISCOVERY=1")

    monkeypatch.setattr("urpm.core.server_pool._detect_version", _fail)
    monkeypatch.setattr("urpm.core.server_pool._fetch_and_filter", _fail)
    monkeypatch.setattr("urpm.core.server_pool._test_latency", _fail)
    monkeypatch.setattr(
        "urpm.core.mirrorlist.backfill_server_countries", _fail)

    db = MagicMock()
    result = ensure_minimum_servers(db, parallel=4)

    assert isinstance(result, PoolCheckResult)
    # Sufficient=True so the CLI does not print any warning either.
    assert result.sufficient is True
    # DB must not be queried when the short-circuit fires.
    db.list_servers.assert_not_called()


def test_env_var_absent_runs_the_discovery(monkeypatch):
    """Sanity check: without the env var, the discovery path is
    reached.  Otherwise the short-circuit would be inert in production.
    """
    monkeypatch.delenv("URPM_SKIP_MIRROR_DISCOVERY", raising=False)

    called = {"detect": False}

    def _fake_detect():
        called["detect"] = True
        return None  # no version → early bailout after this

    monkeypatch.setattr(
        "urpm.core.server_pool._detect_version", _fake_detect)

    db = MagicMock()
    db.list_servers.return_value = [{'is_official': True}] * 6

    ensure_minimum_servers(db, parallel=4)
    assert called["detect"] is True, (
        "the real discovery flow must run when the env var is unset")


def test_env_var_value_other_than_1_does_not_skip(monkeypatch):
    """Guard against a silent behaviour flip if someone exports
    ``URPM_SKIP_MIRROR_DISCOVERY=0`` or ``=true``.  The contract is
    strict equality with ``'1'``.
    """
    monkeypatch.setenv("URPM_SKIP_MIRROR_DISCOVERY", "0")

    called = {"detect": False}

    def _fake_detect():
        called["detect"] = True
        return None

    monkeypatch.setattr(
        "urpm.core.server_pool._detect_version", _fake_detect)

    db = MagicMock()
    db.list_servers.return_value = []

    ensure_minimum_servers(db, parallel=4)
    assert called["detect"] is True
