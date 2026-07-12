"""Tests for the shared-container multi-spec build chain."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from urpm.cli.helpers import build_chain


# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------


def _proc(rc: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """Stand-in for ``container.exec`` return value."""
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class TestTopdirFor:
    def test_prefixes_root(self):
        assert build_chain._topdir_for("urpm-ng") == "/root/urpm-ng"


class TestSpecPkgName:
    def test_uses_rpmspec_when_available(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "urpm-ng\n")
        assert build_chain._spec_pkg_name(
            c, "cid", "/root/x/SPECS/foo.spec") == "urpm-ng"

    def test_falls_back_to_stem_when_rpmspec_fails(self):
        c = MagicMock()
        c.exec.return_value = _proc(1, "")
        assert build_chain._spec_pkg_name(
            c, "cid", "/root/x/SPECS/urpm-ng.spec") == "urpm-ng"

    def test_falls_back_to_stem_on_empty_stdout(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "\n\n")
        assert build_chain._spec_pkg_name(
            c, "cid", "/root/x/SPECS/bar.spec") == "bar"


class TestGetLastTxnId:
    def test_parses_first_numeric_row(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "42 install …\n41 upgrade …\n")
        assert build_chain._get_last_txn_id(c, "cid") == 42

    def test_accepts_hash_prefix(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "#42 install …\n")
        assert build_chain._get_last_txn_id(c, "cid") == 42

    def test_skips_non_numeric_headers(self):
        c = MagicMock()
        c.exec.return_value = _proc(
            0, "Date       Action     Command\n---\n17 install …\n")
        assert build_chain._get_last_txn_id(c, "cid") == 17

    def test_returns_none_when_history_is_empty(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "")
        assert build_chain._get_last_txn_id(c, "cid") is None

    def test_returns_none_when_urpm_history_fails(self):
        c = MagicMock()
        c.exec.return_value = _proc(1, "")
        assert build_chain._get_last_txn_id(c, "cid") is None


class TestAddProducedMediaIfMissing:
    def test_creates_dir_and_registers_media(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "")
        build_chain._add_produced_media_if_missing(c, "cid")
        called_cmds = [call.args[1] for call in c.exec.call_args_list]
        # First: mkdir; second: urpm media add.
        assert called_cmds[0][:3] == ['mkdir', '-p',
                                     build_chain.PRODUCED_MEDIA_DIR]
        assert called_cmds[1][:3] == ['urpm', 'media', 'add']
        assert '--allow-unsigned' in called_cmds[1]


class TestPublishProducedRpms:
    def test_no_rpms_is_a_noop(self):
        c = MagicMock()
        build_chain._publish_produced_rpms(c, "cid", [])
        c.exec.assert_not_called()

    def test_copies_each_rpm_and_refreshes_metadata(self):
        c = MagicMock()
        c.exec.return_value = _proc(0, "")
        rpms = ["/root/pkgA/RPMS/noarch/pkgA-1-1.rpm",
                "/root/pkgA/RPMS/x86_64/pkgA-devel-1-1.rpm"]
        build_chain._publish_produced_rpms(c, "cid", rpms)
        cmds = [call.args[1] for call in c.exec.call_args_list]
        # 1× mkdir + 1× media add (idempotent) + 2× cp + 1× media update.
        cp_calls = [cmd for cmd in cmds if cmd[:2] == ['cp', '-f']]
        assert len(cp_calls) == 2
        assert cmds[-1][:3] == ['urpm', 'media', 'update']
        assert cmds[-1][-1] == build_chain.PRODUCED_MEDIA_NAME


# ---------------------------------------------------------------------------
# run_shared_container_chain — high-level orchestration
# ---------------------------------------------------------------------------


class _FakeContainer:
    """Minimal Container stand-in that records lifecycle events.

    Every ``exec`` returns rc=0 with an empty stdout by default; the
    test can override specific commands via ``exec_map`` (matched on
    the first two tokens of the arg list).
    """

    def __init__(self, exec_map: dict = None, stream_map: dict = None):
        self.exec_map = exec_map or {}
        self.stream_map = stream_map or {}
        self.calls = []          # sequence of (kind, argv)
        self.cid = "cid-fake"
        self.removed = False

    def run(self, *a, **k):
        self.calls.append(("run", (a, k)))
        return self.cid

    def probe_arch(self, cid):
        self.calls.append(("probe_arch", cid))

    def cp(self, src, dst):
        self.calls.append(("cp", (src, dst)))
        return True

    def exec(self, cid, argv):
        self.calls.append(("exec", tuple(argv)))
        tup = tuple(argv)
        if tup in self.exec_map:  # exact match wins
            return self.exec_map[tup]
        key = tup[:2] if len(tup) >= 2 else tup
        if key in self.exec_map:
            return self.exec_map[key]
        return _proc(0, "")

    def exec_stream(self, cid, argv):
        self.calls.append(("exec_stream", tuple(argv)))
        key = tuple(argv[:2]) if len(argv) >= 2 else tuple(argv)
        return self.stream_map.get(key, 0)

    def rm(self, cid):
        self.removed = True


def _dummy_find_workspace(source_path):
    """The chain never actually reads the sources for these tests."""
    return (source_path.parent, source_path.parent / 'SOURCES', True)


def _dummy_diagnose(cid, container, deps):
    pass


class TestRunSharedContainerChain:
    def _run(self, container, sources, **overrides):
        return build_chain.run_shared_container_chain(
            container,
            image="mga:10-build",
            valid_sources=sources,
            output_dir=Path("/tmp/out"),
            keep_container=overrides.get("keep_container", False),
            with_rpms=[],
            no_update=True,          # skip network in tests
            subrel=None,
            rpmmacros_path=None,
            stop_on_fail=overrides.get("stop_on_fail", False),
            rollback_between_builds=overrides.get(
                "rollback_between_builds", False),
            _find_workspace_fn=_dummy_find_workspace,
            _diagnose_fn=_dummy_diagnose,
        )

    def test_container_is_created_once_and_removed(self, tmp_path):
        spec_a = tmp_path / "a.spec"
        spec_a.write_text("Name: a\n")
        c = _FakeContainer()
        # exec("rpmbuild -br …") returns rc=0 → shortcut past dynamic BR.
        # exec("rpmbuild -ba …") is exec_stream; default rc=0.
        results = self._run(c, [spec_a])
        # One `run` call for the whole chain, one `rm` at teardown.
        runs = [k for k, _ in c.calls if k == "run"]
        assert len(runs) == 1
        assert c.removed is True
        assert len(results) == 1
        assert results[0][1] is True

    def test_setup_failure_marks_every_source_failed(self, tmp_path):
        spec_a = tmp_path / "a.spec"
        spec_b = tmp_path / "b.spec"
        spec_a.write_text("")
        spec_b.write_text("")
        c = _FakeContainer(stream_map={
            # The last shared-phase command is ``urpm install rpm-build``.
            ('urpm', 'install'): 1,
        })
        results = self._run(c, [spec_a, spec_b])
        assert [ok for _s, ok, _m in results] == [False, False]
        assert all("rpm-build" in msg for _s, _o, msg in results)
        # Container was created but must have been reaped even on setup fail.
        assert c.removed is True

    def test_stop_on_fail_skips_remaining_specs(self, tmp_path):
        spec_a = tmp_path / "a.spec"
        spec_b = tmp_path / "b.spec"
        spec_c = tmp_path / "c.spec"
        for s in (spec_a, spec_b, spec_c):
            s.write_text("")
        # a fails on BuildRequires install (exec_stream returns 1
        # on ``urpm install --buildrequires``).  Stop should kick in.
        c = _FakeContainer(stream_map={
            # Trigger failure on the per-spec BuildRequires install.
            # Match on the first two tokens plus a marker in argv[-1].
        })
        original_exec_stream = c.exec_stream

        def _stream(cid, argv):
            # Fail the first BuildRequires install so ``a`` fails,
            # then let everything else succeed.
            if argv[:2] == ['urpm', 'install'] and '--buildrequires' in argv:
                c.calls.append(("exec_stream", tuple(argv)))
                return 1
            return original_exec_stream(cid, argv)

        c.exec_stream = _stream
        results = self._run(c, [spec_a, spec_b, spec_c], stop_on_fail=True)
        assert [ok for _s, ok, _m in results] == [False, False, False]
        # b and c were skipped, not attempted.
        assert "skipped" in results[1][2]
        assert "skipped" in results[2][2]

    def test_rollback_between_builds_calls_urpm_rollback(self, tmp_path):
        spec_a = tmp_path / "a.spec"
        spec_b = tmp_path / "b.spec"
        spec_a.write_text("")
        spec_b.write_text("")
        # Make ``urpm history`` return a baseline id, and let every
        # build succeed so both rollbacks fire between+after.
        c = _FakeContainer(exec_map={
            ('urpm', 'history'): _proc(0, "99 install …\n"),
        })
        self._run(c, [spec_a, spec_b], rollback_between_builds=True)
        rollback_calls = [
            argv for kind, argv in c.calls
            if kind == "exec" and argv[:2] == ('urpm', 'rollback')
        ]
        # One rollback fires after the successful build of ``a`` (before
        # ``b``).  After ``b`` there is no next spec, so no second call.
        assert len(rollback_calls) == 1
        assert "99" in rollback_calls[0]
