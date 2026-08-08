"""Tests for TDBUS.1 : D-Bus write methods refuse while .state present.

SPEC_DISTUPGRADE §2 « Neutralisation surfaces D-Bus urpm-ng ».
The 5 write handlers each call
:func:`urpm.dbus.service._refuse_if_distupgrade_in_progress` as their
very first action (before polkit), which raises
:class:`DistupgradeInProgressError` — mapped by the dispatcher to
``org.mageia.Urpm.v1.Error.DistupgradeInProgress``.
"""

from __future__ import annotations

import inspect

import pytest


class TestRefuseHelper:
    def _make_db(self, tmp_path):
        from urpm.core.database import PackageDatabase
        return PackageDatabase(db_path=tmp_path / "packages.db")

    def test_no_state_no_raise(self, tmp_path):
        db = self._make_db(tmp_path)
        from urpm.dbus.service import _refuse_if_distupgrade_in_progress
        _refuse_if_distupgrade_in_progress(db)  # must not raise

    def test_state_present_raises(self, tmp_path):
        db = self._make_db(tmp_path)
        from urpm.core.distupgrade.state import write_state
        write_state({"stage": "tx_a_committing"}, db)
        from urpm.dbus.service import (
            DistupgradeInProgressError,
            _refuse_if_distupgrade_in_progress,
        )
        with pytest.raises(DistupgradeInProgressError) as exc:
            _refuse_if_distupgrade_in_progress(db)
        assert "distupgrade" in str(exc.value).lower()


class TestErrorNameConstant:
    def test_matches_spec_string(self):
        from urpm.dbus.service import ERROR_DISTUPGRADE_IN_PROGRESS
        # Frozen contract shared with pk-backend-urpm.c
        assert ERROR_DISTUPGRADE_IN_PROGRESS == \
            "org.mageia.Urpm.v1.Error.DistupgradeInProgress"


class TestEachWriteHandlerCallsRefuse:
    """Every write handler must call _refuse_if_distupgrade_in_progress
    BEFORE any other action (including polkit).  We assert this via
    source inspection so a future edit that moves the check after
    _authorize() fails loudly."""

    _write_handlers = (
        "handle_install_files",
        "handle_install_packages",
        "handle_remove_packages",
        "handle_upgrade_packages",
        "handle_refresh_metadata",
    )

    def _handler_source(self, name):
        from urpm.dbus.service import UrpmDBusService
        return inspect.getsource(getattr(UrpmDBusService, name))

    @pytest.mark.parametrize("name", _write_handlers)
    def test_calls_refuse(self, name):
        src = self._handler_source(name)
        assert "_refuse_if_distupgrade_in_progress" in src

    @pytest.mark.parametrize("name", _write_handlers)
    def test_refuse_comes_before_authorize(self, name):
        src = self._handler_source(name)
        idx_refuse = src.find("_refuse_if_distupgrade_in_progress")
        idx_authz = src.find("_authorize")
        assert idx_refuse >= 0
        assert idx_authz >= 0
        assert idx_refuse < idx_authz, (
            f"{name} : polkit fires before distupgrade check — "
            f"would dialog then refuse")


class TestCShimMapping:
    """Frozen constants shared with pk-backend-urpm.c (TDBUS.2)."""

    def test_c_shim_references_our_error_name(self):
        path = "/home/superadmin/Sources/urpm-ng-0.9.x-wt/" \
               "pk-backend-urpm/pk-backend-urpm.c"
        with open(path) as f:
            src = f.read()
        assert "org.mageia.Urpm.v1.Error.DistupgradeInProgress" in src
        assert "urpm_pk_error_from_dbus" in src
        assert "PK_ERROR_ENUM_CANNOT_GET_LOCK" in src

    def test_c_shim_maps_at_write_call_sites(self):
        """The 5 RPC-failure sites (InstallFiles, InstallPackages,
        RemovePackages, UpgradePackages, RefreshMetadata) must route
        through urpm_pk_error_from_dbus so distupgrade errors surface
        as CANNOT_GET_LOCK."""
        path = "/home/superadmin/Sources/urpm-ng-0.9.x-wt/" \
               "pk-backend-urpm/pk-backend-urpm.c"
        with open(path) as f:
            src = f.read()
        # 1 definition + 5 call sites = 6 mentions.
        assert src.count("urpm_pk_error_from_dbus") >= 6
