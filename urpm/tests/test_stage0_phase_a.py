"""Tests for TS0.4 Stage 0 Phase A (SPEC_DISTUPGRADE §4.0).

Phase A delegates to the same primitives ``cmd_upgrade`` uses :
:class:`Resolver`, :class:`PackageOperations`.  We mock those and
assert the wiring + error handling rather than re-testing the
underlying subsystems (already covered by their own suites).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from urpm.core.distupgrade.phase_a import (
    PhaseAError,
    run_phase_a_refresh,
    run_phase_a_upgrade,
)


class TestPhaseARefresh:
    def test_delegates_to_sync_all_media(self):
        db = MagicMock()
        with patch("urpm.core.sync.sync_all_media") as sync:
            sync.return_value = [("core", MagicMock())]
            results = run_phase_a_refresh(db, force=True)
        sync.assert_called_once()
        _, kwargs = sync.call_args
        assert kwargs["force"] is True
        assert results == sync.return_value

    def test_wraps_engine_exception(self):
        db = MagicMock()
        with patch("urpm.core.sync.sync_all_media",
                   side_effect=RuntimeError("network down")):
            with pytest.raises(PhaseAError,
                               match="metadata refresh failed"):
                run_phase_a_refresh(db)


class TestPhaseAUpgrade:
    """Phase A upgrade = resolve + build_download_items +
    download_packages + begin_transaction + execute_upgrade +
    complete_transaction + mark_dependencies.  All via
    :class:`PackageOperations`.
    """

    def _fake_action(self, name: str = "foo", action_value: str = "upgrade"):
        """Build a PackageAction-like mock the pipeline can read."""
        return SimpleNamespace(
            name=name,
            action=SimpleNamespace(value=action_value),
        )

    def _make_ops(self, actions, *, downloaded_paths=("/tmp/foo.rpm",),
                  local_paths=(), execute_side_effect=None):
        """Fake PackageOperations returning canned pipeline outputs."""
        ops = MagicMock()
        # build_download_items → (items, local_paths).  Items only need
        # to be countable (their internal structure isn't inspected
        # by phase_a itself when downloads succeed).
        ops.build_download_items.return_value = (
            [MagicMock()] * len(downloaded_paths),
            list(local_paths),
        )
        # download_packages → (dl_results, downloaded, cached, peers)
        dl_results = [
            MagicMock(success=True, path=p, error=None,
                      item=MagicMock(filename=f"pkg-{i}.rpm"))
            for i, p in enumerate(downloaded_paths)
        ]
        ops.download_packages.return_value = (
            dl_results, len(dl_results), 0, {"from_peers": 0})
        ops.begin_transaction.return_value = 42
        if execute_side_effect is not None:
            ops.execute_upgrade.side_effect = execute_side_effect
        return ops

    def _make_resolver(self, actions, success=True, problems=None):
        resolver = MagicMock()
        resolver.resolve_upgrade.return_value = SimpleNamespace(
            success=success,
            actions=list(actions),
            problems=problems or [],
        )
        return resolver

    def test_no_actions_returns_zero(self):
        db = MagicMock()
        resolver = self._make_resolver(actions=[])
        with patch("urpm.core.resolver.Resolver", return_value=resolver):
            assert run_phase_a_upgrade(db) == 0

    def test_solver_failure_raises_phase_a_error(self):
        db = MagicMock()
        resolver = self._make_resolver(
            actions=[], success=False,
            problems=["nothing provides libfoo.so.0()(64bit)"])
        with patch("urpm.core.resolver.Resolver", return_value=resolver):
            with pytest.raises(PhaseAError, match="did not converge"):
                run_phase_a_upgrade(db)

    def test_solver_raises_wrapped(self):
        db = MagicMock()
        resolver = MagicMock()
        resolver.resolve_upgrade.side_effect = RuntimeError("pool blew up")
        with patch("urpm.core.resolver.Resolver", return_value=resolver):
            with pytest.raises(PhaseAError, match="solve failed"):
                run_phase_a_upgrade(db)

    def test_happy_path_chains_all_primitives(self):
        db = MagicMock()
        actions = [self._fake_action("foo")]
        resolver = self._make_resolver(actions=actions)
        ops = self._make_ops(actions=actions)
        with patch("urpm.core.resolver.Resolver", return_value=resolver), \
             patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            assert run_phase_a_upgrade(db) == 0
        ops.build_download_items.assert_called_once()
        ops.download_packages.assert_called_once()
        ops.begin_transaction.assert_called_once()
        ops.execute_upgrade.assert_called_once()
        ops.mark_dependencies.assert_called_once()
        ops.complete_transaction.assert_called_once_with(42)

    def test_download_failure_raises(self):
        db = MagicMock()
        actions = [self._fake_action("foo")]
        resolver = self._make_resolver(actions=actions)
        ops = MagicMock()
        ops.build_download_items.return_value = ([MagicMock()], [])
        ops.download_packages.return_value = (
            [MagicMock(success=False, path=None, error="404",
                       item=MagicMock(filename="foo-1.rpm"))],
            0, 0, {"from_peers": 0})
        with patch("urpm.core.resolver.Resolver", return_value=resolver), \
             patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            with pytest.raises(PhaseAError, match="download failed"):
                run_phase_a_upgrade(db)

    def test_execute_upgrade_failure_aborts_transaction(self):
        db = MagicMock()
        actions = [self._fake_action("foo")]
        resolver = self._make_resolver(actions=actions)
        ops = self._make_ops(
            actions=actions,
            execute_side_effect=RuntimeError("rpm scriptlet failed"))
        with patch("urpm.core.resolver.Resolver", return_value=resolver), \
             patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            with pytest.raises(PhaseAError, match="commit"):
                run_phase_a_upgrade(db)
        ops.abort_transaction.assert_called_once_with(42)
        ops.complete_transaction.assert_not_called()

    def test_no_downloads_no_removes_returns_zero(self):
        """resolve says something to do, but every action was already
        satisfied by local rpms path or drops to empty in the pipeline."""
        db = MagicMock()
        actions = [self._fake_action("foo")]
        resolver = self._make_resolver(actions=actions)
        ops = MagicMock()
        # No downloads, no local paths → nothing to hand to the queue.
        ops.build_download_items.return_value = ([], [])
        with patch("urpm.core.resolver.Resolver", return_value=resolver), \
             patch("urpm.core.operations.PackageOperations",
                   return_value=ops):
            assert run_phase_a_upgrade(db) == 0
        ops.execute_upgrade.assert_not_called()
        ops.begin_transaction.assert_not_called()
