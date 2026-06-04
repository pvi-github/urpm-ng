"""Tests for the unified configuration loader.

Focuses on the new ``[daemon]`` knobs that gate urpmd's periodic
network jobs (``auto_update_metadata``, ``auto_predownload``,
``auto_replication``, ``auto_fetch_server_dates``,
``metadata_interval``).  Older sections are exercised in passing
to guard against accidental regressions in the parser.
"""

from pathlib import Path

import pytest

from urpm.core.settings import load_settings


def _write(tmp_path: Path, body: str, name: str = "20-daemon.cfg") -> Path:
    """Drop a config file in a fresh ``conf.d/`` and return the parent dir."""
    confdir = tmp_path / "conf.d"
    confdir.mkdir(exist_ok=True)
    (confdir / name).write_text(body, encoding="utf-8")
    return tmp_path


class TestDaemonDefaults:
    def test_all_auto_flags_default_true(self, tmp_path):
        settings = load_settings(tmp_path)
        assert settings.daemon.auto_update_metadata is True
        assert settings.daemon.auto_predownload is True
        assert settings.daemon.auto_replication is True
        assert settings.daemon.auto_fetch_server_dates is True

    def test_metadata_interval_default_none(self, tmp_path):
        settings = load_settings(tmp_path)
        assert settings.daemon.metadata_interval is None

    def test_discovery_interfaces_default_all(self, tmp_path):
        settings = load_settings(tmp_path)
        assert settings.daemon.discovery_interfaces == "all"


class TestDaemonAutoFlags:
    @pytest.mark.parametrize("key", [
        "auto_update_metadata",
        "auto_predownload",
        "auto_replication",
        "auto_fetch_server_dates",
    ])
    def test_each_flag_can_be_disabled(self, tmp_path, key):
        body = f"[daemon]\n{key} = false\n"
        settings = load_settings(_write(tmp_path, body))
        assert getattr(settings.daemon, key) is False
        # Other three stay at their default of True
        for other in (
            "auto_update_metadata",
            "auto_predownload",
            "auto_replication",
            "auto_fetch_server_dates",
        ):
            if other != key:
                assert getattr(settings.daemon, other) is True

    @pytest.mark.parametrize("falsey", ["false", "no", "0", "off", "FALSE", "Off"])
    def test_boolean_aliases_recognised(self, tmp_path, falsey):
        body = f"[daemon]\nauto_update_metadata = {falsey}\n"
        settings = load_settings(_write(tmp_path, body))
        assert settings.daemon.auto_update_metadata is False

    def test_invalid_value_keeps_default(self, tmp_path):
        body = "[daemon]\nauto_update_metadata = maybe\n"
        settings = load_settings(_write(tmp_path, body))
        assert settings.daemon.auto_update_metadata is True


class TestMetadataInterval:
    def test_override_is_applied(self, tmp_path):
        body = "[daemon]\nmetadata_interval = 7200\n"
        settings = load_settings(_write(tmp_path, body))
        assert settings.daemon.metadata_interval == 7200

    def test_non_positive_is_rejected(self, tmp_path):
        body = "[daemon]\nmetadata_interval = 0\n"
        settings = load_settings(_write(tmp_path, body))
        assert settings.daemon.metadata_interval is None

    def test_non_int_is_rejected(self, tmp_path):
        body = "[daemon]\nmetadata_interval = soon\n"
        settings = load_settings(_write(tmp_path, body))
        assert settings.daemon.metadata_interval is None


class TestSectionIsolation:
    """Touching [daemon] must not leak into sibling sections."""

    def test_other_sections_unaffected(self, tmp_path):
        body = (
            "[daemon]\n"
            "auto_update_metadata = false\n"
            "auto_predownload = false\n"
        )
        settings = load_settings(_write(tmp_path, body))
        # Resolver / download defaults stay intact
        assert settings.resolver.install_recommends is True
        assert settings.download.parallel == 4
        assert settings.server.auto_add is True
