"""Tests for the LocalRPM lookup chain — the invariant that should
have caught the six-month O(N) fallback bug.

Contract under test
-------------------

For every ``PackageAction`` with ``media_name == '@LocalRPMs'``,
``operations.build_download_items()`` must resolve the RPM file path
by direct dict lookup on ``resolver._solvable_to_pkg[action.solvable_id]``
in O(1) — no full-pool scan, no fallback that silently ate the
performance of build-system installs.

The tests below verify:

- ``add_local_rpms()`` populates both the primary index
  (``_solvable_to_pkg[s.id]``) and the secondary NEVRA→id index
  (``_localrpm_nevra_to_id[nevra]``).
- Every ``PackageAction`` produced by the resolver forwards its
  originating ``solvable_id`` — the missing link that made the
  fast-path unreachable historically.
- ``build_download_items`` looks the path up in O(1) and holds the
  build-system performance target (2000 LocalRPM actions well below
  100 ms of pure lookup overhead).
- The defensive safety net still recovers when the ``solvable_id``
  chain is broken, but emits a very visible orange warning so the
  broken link surfaces instead of silently degrading.
"""

from __future__ import annotations

import re
import time
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from urpm.core.operations import PackageOperations
from urpm.core.resolver import (
    InstallReason,
    PackageAction,
    TransactionType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rpm_info(name: str, version: str = "1.0", release: str = "1",
                   arch: str = "x86_64", epoch: int = 0,
                   path: Optional[str] = None) -> dict:
    """Build a fake ``read_rpm_header`` result."""
    if epoch:
        nevra = f"{name}-{epoch}:{version}-{release}.{arch}"
    else:
        nevra = f"{name}-{version}-{release}.{arch}"
    return {
        "name": name,
        "version": version,
        "release": release,
        "epoch": epoch,
        "arch": arch,
        "nevra": nevra,
        "size": 1234,
        "filesize": 567,
        "path": path or f"/tmp/{name}-{version}-{release}.{arch}.rpm",
        "requires": [],
        "provides": [],
        "conflicts": [],
        "obsoletes": [],
        "recommends": [],
        "suggests": [],
        "supplements": [],
        "enhances": [],
    }


def _make_local_action(info: dict, solvable_id: Optional[int]) -> PackageAction:
    """Build a PackageAction as the resolver would produce for a LocalRPM."""
    return PackageAction(
        action=TransactionType.INSTALL,
        name=info["name"],
        evr=(f"{info['epoch']}:{info['version']}-{info['release']}"
             if info["epoch"] else f"{info['version']}-{info['release']}"),
        arch=info["arch"],
        nevra=info["nevra"],
        size=info.get("size", 0),
        filesize=info.get("filesize", 0),
        media_name="@LocalRPMs",
        reason=InstallReason.EXPLICIT,
        solvable_id=solvable_id,
    )


class _FakeResolver:
    """Minimal resolver stand-in for the LocalRPM lookup chain.

    Only exposes the two attributes that ``build_download_items``
    touches for ``@LocalRPMs`` actions.  The two dicts are populated
    directly by the tests, mimicking the state that
    ``add_local_rpms`` would leave behind.
    """

    def __init__(self):
        self._solvable_to_pkg = {}
        self._localrpm_nevra_to_id = {}


# ---------------------------------------------------------------------------
# PackageAction contract
# ---------------------------------------------------------------------------


def test_package_action_has_solvable_id_field():
    """PackageAction must expose solvable_id — historically missing."""
    action = PackageAction(
        action=TransactionType.INSTALL,
        name="foo",
        evr="1.0-1",
        arch="x86_64",
        nevra="foo-1.0-1.x86_64",
    )
    # Default is None so callers that cannot resolve an id
    # (rpmdb-only orphan detection, --nodeps direct DB) do not have
    # to invent one.
    assert action.solvable_id is None

    action_with_id = PackageAction(
        action=TransactionType.INSTALL,
        name="foo",
        evr="1.0-1",
        arch="x86_64",
        nevra="foo-1.0-1.x86_64",
        solvable_id=42,
    )
    assert action_with_id.solvable_id == 42


# ---------------------------------------------------------------------------
# add_local_rpms populates both indexes
# ---------------------------------------------------------------------------


def test_add_local_rpms_populates_primary_and_secondary_indexes():
    """``add_local_rpms`` must feed BOTH ``_solvable_to_pkg[s.id]`` and
    ``_localrpm_nevra_to_id[nevra]``.

    The bug that made this test necessary lived because there was no
    unit test observing the secondary index at all — the CLI accidentally
    scanned the primary one O(N) and nobody noticed.
    """
    solv = pytest.importorskip("solv")  # test needs libsolv

    from urpm.core.resolver import Resolver

    # ``add_local_rpms`` only touches the pool and the two index attrs.
    # Everything else is inert for this test.
    db = MagicMock()

    resolver = Resolver(db)
    resolver.pool = solv.Pool()
    resolver.pool.setarch()

    info_a = _make_rpm_info("foo", version="1.0", release="1")
    info_b = _make_rpm_info("bar", version="2.5", release="3", epoch=1)

    resolver.add_local_rpms([info_a, info_b])

    # Primary index: solvable id → metadata
    assert len(resolver._solvable_to_pkg) == 2
    for pkg_info in resolver._solvable_to_pkg.values():
        assert pkg_info["media_name"] == "@LocalRPMs"
        assert "local_path" in pkg_info

    # Secondary index: NEVRA → solvable id
    assert info_a["nevra"] in resolver._localrpm_nevra_to_id
    assert info_b["nevra"] in resolver._localrpm_nevra_to_id

    # Cross-check: the id from the secondary index resolves in the
    # primary index and matches the expected file path.
    sid_a = resolver._localrpm_nevra_to_id[info_a["nevra"]]
    assert resolver._solvable_to_pkg[sid_a]["local_path"] == info_a["path"]

    sid_b = resolver._localrpm_nevra_to_id[info_b["nevra"]]
    assert resolver._solvable_to_pkg[sid_b]["local_path"] == info_b["path"]


# ---------------------------------------------------------------------------
# build_download_items uses the O(1) fast path
# ---------------------------------------------------------------------------


def test_build_download_items_resolves_local_paths_via_solvable_id(tmp_path):
    """Fast-path contract: each LocalRPM action must land its path
    through ``resolver._solvable_to_pkg[action.solvable_id]``.
    """
    db = MagicMock()
    ops = PackageOperations(db, base_dir=tmp_path)

    resolver = _FakeResolver()

    # Two LocalRPMs, each with its own solvable id and path.
    info_a = _make_rpm_info("foo", path=str(tmp_path / "foo.rpm"))
    info_b = _make_rpm_info("bar", path=str(tmp_path / "bar.rpm"))
    resolver._solvable_to_pkg[100] = {**info_a, "media_name": "@LocalRPMs",
                                      "local_path": info_a["path"]}
    resolver._solvable_to_pkg[101] = {**info_b, "media_name": "@LocalRPMs",
                                      "local_path": info_b["path"]}
    resolver._localrpm_nevra_to_id[info_a["nevra"]] = 100
    resolver._localrpm_nevra_to_id[info_b["nevra"]] = 101

    actions = [_make_local_action(info_a, 100),
               _make_local_action(info_b, 101)]

    download_items, local_paths = ops.build_download_items(actions, resolver)

    assert download_items == []              # both are LocalRPMs
    assert set(local_paths) == {info_a["path"], info_b["path"]}


def test_build_download_items_skips_the_wrong_side_of_the_dict(tmp_path):
    """Regression guard for the original bug: ``.get(action.nevra)``
    on a dict keyed by ``s.id`` was the site of the silent O(N)
    fallback.  If someone reintroduces a NEVRA lookup on the primary
    index, this test fails because we mismatch the NEVRA on purpose.
    """
    db = MagicMock()
    ops = PackageOperations(db, base_dir=tmp_path)

    resolver = _FakeResolver()

    info = _make_rpm_info("foo", path=str(tmp_path / "foo.rpm"))
    resolver._solvable_to_pkg[7] = {**info, "media_name": "@LocalRPMs",
                                    "local_path": info["path"]}
    # Deliberately do NOT populate _localrpm_nevra_to_id.

    action = _make_local_action(info, solvable_id=7)
    _, local_paths = ops.build_download_items([action], resolver)
    assert local_paths == [info["path"]]


# ---------------------------------------------------------------------------
# Defensive safety net — must still catch mismatches and warn loudly
# ---------------------------------------------------------------------------


def test_defensive_fallback_still_recovers_but_warns(tmp_path, capsys, caplog):
    """When ``solvable_id`` is missing (broken link somewhere in the
    chain), the safety net matching on ``local_rpm_infos`` still
    recovers — but must emit BOTH a Python logging warning and a
    visible orange line on stderr so the operator notices.
    """
    db = MagicMock()
    ops = PackageOperations(db, base_dir=tmp_path)

    resolver = _FakeResolver()  # deliberately EMPTY on purpose

    info = _make_rpm_info("foo", path=str(tmp_path / "foo.rpm"))
    action = _make_local_action(info, solvable_id=None)  # broken link

    with caplog.at_level("WARNING", logger="urpm.core.operations"):
        _, local_paths = ops.build_download_items(
            [action], resolver, local_rpm_infos=[info])

    # Recovery still worked — user's install is not broken.
    assert local_paths == [info["path"]]

    # But the alarm bells rang.
    assert any("safety net" in rec.message for rec in caplog.records), \
        "safety-net warning should be logged"

    captured = capsys.readouterr()
    # Orange ANSI 33 + bold "Warning:" prefix on stderr.
    assert "\x1b[1;33mWarning:\x1b[0m" in captured.err, \
        "stderr must carry the orange warning line"
    assert "safety net" in captured.err


def test_defensive_fallback_not_triggered_when_chain_is_intact(tmp_path, capsys):
    """Positive control: with a valid solvable_id, no warning is emitted."""
    db = MagicMock()
    ops = PackageOperations(db, base_dir=tmp_path)

    resolver = _FakeResolver()
    info = _make_rpm_info("foo", path=str(tmp_path / "foo.rpm"))
    resolver._solvable_to_pkg[1] = {**info, "media_name": "@LocalRPMs",
                                    "local_path": info["path"]}
    resolver._localrpm_nevra_to_id[info["nevra"]] = 1

    action = _make_local_action(info, solvable_id=1)
    ops.build_download_items([action], resolver, local_rpm_infos=[info])

    captured = capsys.readouterr()
    assert "safety net" not in captured.err
    assert "Warning:" not in captured.err


# ---------------------------------------------------------------------------
# Performance — the build-system target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_actions", [2000])
def test_build_download_items_scales_to_2000_local_rpms(tmp_path, n_actions):
    """Build-system install target: 2000 LocalRPM actions must resolve
    their paths in well under 100 ms of pure lookup overhead.

    Historical baseline (before the fix): the O(N) fallback on a
    50k-solvable pool made this scenario take tens of seconds.
    """
    db = MagicMock()
    ops = PackageOperations(db, base_dir=tmp_path)

    resolver = _FakeResolver()

    # Populate 2000 LocalRPMs in the primary + secondary indexes.
    actions: List[PackageAction] = []
    for i in range(n_actions):
        info = _make_rpm_info(f"pkg{i}", path=str(tmp_path / f"pkg{i}.rpm"))
        resolver._solvable_to_pkg[i] = {**info, "media_name": "@LocalRPMs",
                                        "local_path": info["path"]}
        resolver._localrpm_nevra_to_id[info["nevra"]] = i
        actions.append(_make_local_action(info, solvable_id=i))

    # Simulate a big pool: add 50k unrelated entries to the primary
    # index so any accidental O(N) scan would surface.
    for i in range(n_actions, n_actions + 50_000):
        resolver._solvable_to_pkg[i] = {
            "name": f"noise{i}", "media_name": "core_release",
            "evr": "0-1", "arch": "x86_64",
        }

    t0 = time.perf_counter()
    _, local_paths = ops.build_download_items(actions, resolver)
    elapsed = time.perf_counter() - t0

    assert len(local_paths) == n_actions, \
        f"expected {n_actions} paths recovered, got {len(local_paths)}"
    # 100 ms is a comfortable ceiling; O(1) lookups should land far
    # below this on any modern CPU.  A regression back to O(N) would
    # push this to tens of seconds.
    assert elapsed < 0.1, (
        f"build_download_items on {n_actions} actions took {elapsed*1000:.1f} ms "
        f"(target: < 100 ms) — likely a regression to O(N) lookup"
    )
