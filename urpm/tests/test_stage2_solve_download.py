"""Tests for TS2 Stage 2 solve + download (SPEC_DISTUPGRADE §4.2).

Stage 2 delegates to :meth:`Resolver.resolve_distupgrade` and
:class:`PackageOperations` — the same primitives ``cmd_upgrade``
uses.  We mock those at the boundary and assert wiring + state
bookkeeping rather than re-testing the underlying subsystems.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.stage2 import (
    Stage2Error,
    download_plan,
    run_stage2,
    solve_distupgrade,
)
from urpm.core.distupgrade.version import ReleaseIdentity


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = PackageDatabase(db_path=tmp_path / "packages.db")
    yield db
    db.close()


def _mock_resolution(actions, success=True, problems=()):
    """A Resolution-like object the pipeline accepts."""
    return SimpleNamespace(
        success=success,
        actions=list(actions),
        problems=list(problems),
    )


class TestSolveDistupgrade:
    def test_delegates_to_resolver_and_attaches_it(self, state_db):
        actions = [SimpleNamespace(name="foo", nevra="foo-1-1.mga11.x86_64")]
        fake_resolver = MagicMock()
        fake_resolver.resolve_distupgrade.return_value = (
            _mock_resolution(actions))
        target = ReleaseIdentity(identity="11", numeric="11")
        with patch("urpm.core.resolver.Resolver",
                   return_value=fake_resolver):
            result = solve_distupgrade(state_db, target, arch="x86_64")
        assert result.success
        assert list(result.actions) == actions
        # Resolver is attached so download_plan can hand it to
        # PackageOperations.build_download_items.
        assert result._resolver is fake_resolver
        fake_resolver.resolve_distupgrade.assert_called_once()

    def test_solve_failure_raises_stage2error(self, state_db):
        fake_resolver = MagicMock()
        fake_resolver.resolve_distupgrade.return_value = _mock_resolution(
            [], success=False, problems=["libc.so.6 not provided"])
        target = ReleaseIdentity(identity="11", numeric="11")
        with patch("urpm.core.resolver.Resolver",
                   return_value=fake_resolver):
            with pytest.raises(Stage2Error, match="1 problem"):
                solve_distupgrade(state_db, target)


class TestDownloadPlan:
    def _resolution_with_resolver(self, actions, resolver=None):
        r = _mock_resolution(actions)
        r._resolver = resolver or MagicMock()
        return r

    def test_empty_plan(self, state_db):
        r = self._resolution_with_resolver([])
        summary = download_plan(state_db, r)
        assert summary["requested"] == 0
        assert summary["downloaded"] == 0

    def test_no_resolver_attached_raises(self, state_db):
        r = _mock_resolution([SimpleNamespace(name="foo")])
        # No _resolver attribute → the pipeline can't route through
        # build_download_items ; hard error.
        with pytest.raises(Stage2Error, match="resolver"):
            download_plan(state_db, r)

    def test_wires_ops_pipeline(self, state_db):
        r = self._resolution_with_resolver(
            [SimpleNamespace(name="foo", nevra="foo-1-1.mga11.x86_64")])
        ops = MagicMock()
        # (items, local_paths)
        item = SimpleNamespace(name="foo", version="1", release="1",
                                arch="x86_64")
        ops.build_download_items.return_value = ([item], [])
        # (results, downloaded, cached, peer_stats)
        dl_result = SimpleNamespace(
            success=True, path="/cache/foo-1-1.mga11.x86_64.rpm",
            error=None, item=item)
        ops.download_packages.return_value = ([dl_result], 1, 0,
                                              {"from_peers": 0})
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            summary = download_plan(state_db, r)
        assert summary["downloaded"] == 1
        assert summary["already_present"] == 0
        assert summary["nevra_to_path"] == {
            "foo-1-1.x86_64": pytest.importorskip("pathlib").Path(
                "/cache/foo-1-1.mga11.x86_64.rpm"),
        }

    def test_download_failure_surfaces_in_failed_list(self, state_db):
        r = self._resolution_with_resolver(
            [SimpleNamespace(name="bar", nevra="bar-2-1.mga11.x86_64")])
        ops = MagicMock()
        item = SimpleNamespace(name="bar", version="2", release="1",
                                arch="x86_64")
        ops.build_download_items.return_value = ([item], [])
        ops.download_packages.return_value = (
            [SimpleNamespace(success=False, path=None,
                             error="404", item=item)],
            0, 0, {"from_peers": 0})
        with patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            summary = download_plan(state_db, r)
        assert "bar-2-1.x86_64" in summary["failed"]
        assert summary["nevra_to_path"] == {}


class TestRunStage2:
    def test_state_bumped_stage2_running_then_downloaded(self, state_db):
        from urpm.core.distupgrade.state import read_state, write_state
        write_state({
            "version_from": "10", "version_to": "11",
            "started_at": "2026-08-05T00:00:00Z",
            "stage": "media_swapped",
        }, state_db)

        target = ReleaseIdentity(identity="11", numeric="11")
        resolution = _mock_resolution([
            SimpleNamespace(name="foo",
                            nevra="foo-1-1.mga11.x86_64"),
            SimpleNamespace(name="bar",
                            nevra="bar-2-1.mga11.x86_64"),
        ])
        resolution._resolver = MagicMock()
        with patch(
                "urpm.core.distupgrade.stage2.solve_distupgrade",
                return_value=resolution), \
             patch(
                "urpm.core.distupgrade.stage2.download_plan",
                return_value={
                    "requested": 2, "downloaded": 2,
                    "already_present": 0, "failed": [],
                    "nevra_to_path": {},
                }):
            summary = run_stage2(state_db, target=target)

        assert summary["plan_size"] == 2
        assert summary["download"]["downloaded"] == 2
        final = read_state(state_db)
        assert final["stage"] == "downloaded"
        assert final["version_to"] == "11"

    def test_solver_failure_raises(self, state_db):
        from urpm.core.distupgrade.state import write_state
        write_state({
            "version_from": "10", "version_to": "11",
            "stage": "media_swapped",
        }, state_db)
        target = ReleaseIdentity(identity="11", numeric="11")
        with patch("urpm.core.distupgrade.stage2.solve_distupgrade",
                   side_effect=Stage2Error("no candidates")):
            with pytest.raises(Stage2Error, match="no candidates"):
                run_stage2(state_db, target=target)

    def test_generic_exception_wrapped(self, state_db):
        from urpm.core.distupgrade.state import write_state
        write_state({
            "version_from": "10", "version_to": "11",
            "stage": "media_swapped",
        }, state_db)
        target = ReleaseIdentity(identity="11", numeric="11")
        with patch("urpm.core.distupgrade.stage2.solve_distupgrade",
                   side_effect=RuntimeError("pool blew up")):
            with pytest.raises(Stage2Error, match="solve failed"):
                run_stage2(state_db, target=target)

    def test_empty_plan_raises_before_confirm(self, state_db):
        """SAFETY : a solve returning zero actions must raise
        Stage2EmptyPlanError, NOT call the confirm callback, and NOT
        touch the download stage.  Proceeding past Stage 2 with a
        no-op plan would take us to Stage 4, flag mga N media for
        deferred deletion, and brick the still-on-mga-N machine at
        reboot."""
        from urpm.core.distupgrade.state import write_state
        from urpm.core.distupgrade.stage2 import Stage2EmptyPlanError
        write_state({
            "version_from": "10", "version_to": "11",
            "stage": "media_swapped",
        }, state_db)
        target = ReleaseIdentity(identity="11", numeric="11")

        empty = _mock_resolution([])
        empty._resolver = MagicMock()
        confirm_called = {"n": 0}
        download_called = {"n": 0}

        def _confirm(_r):
            confirm_called["n"] += 1
            return True

        def _dp(*_a, **_kw):
            download_called["n"] += 1
            return {}

        with patch("urpm.core.distupgrade.stage2.solve_distupgrade",
                   return_value=empty), \
             patch("urpm.core.distupgrade.stage2.download_plan",
                   side_effect=_dp):
            with pytest.raises(Stage2EmptyPlanError) as excinfo:
                run_stage2(state_db, target=target,
                           confirm_callback=_confirm)

        assert excinfo.value.result is empty
        assert confirm_called["n"] == 0, "confirm must not fire on empty plan"
        assert download_called["n"] == 0, "download must not fire on empty plan"
