"""Shared pytest fixtures for the urpm-ng test suite.

The main job here is to keep the tests off the network by default.
``ensure_minimum_servers`` (in ``urpm.core.server_pool``) would
otherwise probe the Mageia mirrorlist, latency-test candidate mirrors,
and add servers into whatever chroot DB the test is running against —
resulting in ``Auto-added N mirrors for parallel downloads: …`` lines
in every test's stdout and a real risk of flakes when the CI or dev
box has intermittent network access.

The ``URPM_SKIP_MIRROR_DISCOVERY=1`` env var short-circuits the
discovery function; the fixture below sets it for every test via
``autouse=True``.  Tests that specifically want to exercise the
discovery logic (see ``test_server_pool_auto_add.py``) clear the
variable in their own ``monkeypatch``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_mirror_discovery(monkeypatch):
    """Prevent ``ensure_minimum_servers`` from hitting the network."""
    monkeypatch.setenv("URPM_SKIP_MIRROR_DISCOVERY", "1")
