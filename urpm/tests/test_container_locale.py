"""Tests for locale propagation from the host into build containers.

Two failure modes to guard against:

* Deriving a single language code from ``LANG`` dropped the operator's
  ``LANGUAGE`` fallback list (``"en_US:fr_FR:fr"`` on a bilingual
  setup) — the container's gettext then only saw ``en`` and printed
  urpm messages in English even when a French catalogue was present.
* Forcing ``LC_ALL=C.UTF-8`` on Mageia 9 containers (glibc < 2.35, no
  compiled locales in -minimal) caused every perl and bash
  spec-helper to spam ``"Setting locale failed"`` warnings during
  rpmbuild.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.container import Container, ContainerRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_container():
    """Build a Container against a stub runtime — no real podman needed."""
    runtime = ContainerRuntime(
        name="podman", path="/usr/bin/podman", version="0")
    return Container(runtime)


def _captured_args(subprocess_mock):
    """Return the argv of the last streaming exec, verbatim."""
    # The last positional call is exec_stream; probe calls come first.
    last_call = subprocess_mock.call_args_list[-1]
    return last_call.args[0]


# ---------------------------------------------------------------------------
# C.UTF-8 detection
# ---------------------------------------------------------------------------


class TestCUtf8Detection:
    def test_present_locale_maps_to_true(self, monkeypatch):
        c = _make_container()
        probe = SimpleNamespace(returncode=0, stdout="C\nC.UTF-8\nen_US.utf8\n")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: probe)
        assert c._container_has_c_utf8("cid1") is True

    def test_absent_locale_maps_to_false(self, monkeypatch):
        c = _make_container()
        probe = SimpleNamespace(returncode=0, stdout="C\nPOSIX\n")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: probe)
        assert c._container_has_c_utf8("cid2") is False

    def test_non_zero_returncode_maps_to_false(self, monkeypatch):
        """A container missing ``locale`` (or any other failure mode)
        must fall back to False so the caller picks LC_ALL=C — the
        safer choice than assuming a locale that will not resolve.
        """
        c = _make_container()
        probe = SimpleNamespace(returncode=127, stdout="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: probe)
        assert c._container_has_c_utf8("cid3") is False

    def test_timeout_maps_to_false(self, monkeypatch):
        c = _make_container()

        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="locale", timeout=10)

        monkeypatch.setattr(subprocess, "run", _raise)
        assert c._container_has_c_utf8("cid4") is False

    def test_result_is_cached_per_container_id(self, monkeypatch):
        """Second call for the same cid must not re-run subprocess."""
        c = _make_container()
        calls = {"n": 0}

        def _run(*a, **k):
            calls["n"] += 1
            return SimpleNamespace(returncode=0, stdout="C.UTF-8\n")

        monkeypatch.setattr(subprocess, "run", _run)
        c._container_has_c_utf8("cid5")
        c._container_has_c_utf8("cid5")
        c._container_has_c_utf8("cid5")
        assert calls["n"] == 1

    def test_different_ids_are_probed_independently(self, monkeypatch):
        c = _make_container()
        calls = {"n": 0}

        def _run(*a, **k):
            calls["n"] += 1
            return SimpleNamespace(returncode=0, stdout="C.UTF-8\n")

        monkeypatch.setattr(subprocess, "run", _run)
        c._container_has_c_utf8("cidA")
        c._container_has_c_utf8("cidB")
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# LANGUAGE forwarding by exec_stream
# ---------------------------------------------------------------------------


def _exec_stream_capture(monkeypatch, env, has_c_utf8: bool) -> list:
    """Run exec_stream against a fully-mocked backend and return argv.

    The subprocess for ``exec_stream`` itself is captured; the probe
    call is short-circuited via a pre-populated cache.
    """
    c = _make_container()
    c._has_c_utf8_by_cid["cid"] = has_c_utf8  # skip real probe

    # Blank the host env then apply the requested keys.
    for k in ("URPM_HOST_LANGUAGE", "URPM_HOST_LANG", "LANGUAGE", "LANG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    captured = {}

    def _run(argv, *a, **k):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _run)
    # exec_stream inspects sys.stdout.isatty(); force False so ``-t``
    # is not added to argv (irrelevant for what we assert on).
    monkeypatch.setattr(
        "sys.stdout",
        SimpleNamespace(isatty=lambda: False, write=lambda *a, **k: None,
                        flush=lambda: None),
    )
    c.exec_stream("cid", ["urpm", "install"])
    return captured["argv"]


class TestExecStreamLanguageForwarding:
    def test_host_language_list_forwarded_verbatim(self, monkeypatch):
        """The bilingual case: LANGUAGE=en_US:fr_FR:fr must reach the
        container as-is so its gettext keeps the fallback chain.
        """
        argv = _exec_stream_capture(
            monkeypatch,
            env={"LANGUAGE": "en_US:fr_FR:fr", "LANG": "en_US.UTF-8"},
            has_c_utf8=True,
        )
        assert "-e" in argv and "LANGUAGE=en_US:fr_FR:fr" in argv
        assert "LC_ALL=C.UTF-8" in argv

    def test_urpm_host_language_wins_over_LANGUAGE(self, monkeypatch):
        """cmd_mkimage saves URPM_HOST_LANGUAGE *before* clamping the
        process to LANGUAGE=C. exec_stream must prefer the saved value.
        """
        argv = _exec_stream_capture(
            monkeypatch,
            env={
                "URPM_HOST_LANGUAGE": "fr_FR:fr",
                "LANGUAGE": "C",  # what cmd_mkimage set on its own env
                "URPM_HOST_LANG": "fr_FR.UTF-8",
                "LANG": "C",
            },
            has_c_utf8=True,
        )
        assert "LANGUAGE=fr_FR:fr" in argv

    def test_falls_back_to_LANG_when_no_LANGUAGE(self, monkeypatch):
        """When neither URPM_HOST_LANGUAGE nor a usable LANGUAGE is
        available we derive a single code from LANG — the legacy path,
        preserved for callers that don't set LANGUAGE at all.
        """
        argv = _exec_stream_capture(
            monkeypatch,
            env={"LANG": "de_DE.UTF-8"},
            has_c_utf8=True,
        )
        assert "LANGUAGE=de" in argv

    def test_all_c_env_sends_nothing(self, monkeypatch):
        """No language hint anywhere → do not inject LC_ALL/LANGUAGE.
        The container keeps whatever default it has.
        """
        argv = _exec_stream_capture(
            monkeypatch,
            env={"LANG": "C", "LANGUAGE": "C"},
            has_c_utf8=True,
        )
        assert not any(
            arg.startswith(("LC_ALL=", "LANGUAGE=")) for arg in argv)


class TestExecStreamLocaleFallback:
    def test_container_without_c_utf8_uses_lc_all_c(self, monkeypatch):
        """Mageia 9 minimal has no C.UTF-8: LC_ALL=C is safer than
        propagating a locale that setlocale cannot resolve.
        """
        argv = _exec_stream_capture(
            monkeypatch,
            env={"LANGUAGE": "fr_FR:fr", "LANG": "fr_FR.UTF-8"},
            has_c_utf8=False,
        )
        assert "LC_ALL=C" in argv
        # LC_ALL=C makes gettext ignore LANGUAGE; injecting it would
        # be dead weight and misleading if someone greps the exec call.
        assert not any(
            arg.startswith("LANGUAGE=") for arg in argv)
