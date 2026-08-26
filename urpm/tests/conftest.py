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

import gettext

import pytest


@pytest.fixture(autouse=True)
def _disable_mirror_discovery(monkeypatch):
    """Prevent ``ensure_minimum_servers`` from hitting the network."""
    monkeypatch.setenv("URPM_SKIP_MIRROR_DISCOVERY", "1")


@pytest.fixture(autouse=True)
def _force_null_translations(monkeypatch):
    """Force ``_()`` to return the untranslated msgid in tests.

    Assertions on user-facing strings should be written against the
    canonical English msgid — otherwise the suite fails on any
    machine whose locale ships a translation for the message (Mageia
    developer box, i18n first-pass rollouts, ...).  Swapping in
    :class:`gettext.NullTranslations` neutralises every catalogue for
    the duration of the test.
    """
    from urpm import i18n
    monkeypatch.setattr(i18n, "_translation", gettext.NullTranslations())
