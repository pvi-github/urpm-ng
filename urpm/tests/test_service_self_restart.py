"""Tests for the D-Bus service stepping aside after upgrading itself.

The ``%post`` of ``urpm-ng-packagekit-backend`` used to run ``systemctl
try-restart urpm-dbus.service`` on upgrade. That scriptlet runs *inside*
the rpm transaction, and the process executing that transaction is the
service itself, so the package killed the transaction installing it:
packages ordered after it were never installed, the history row stayed at
``running``, and the client watching the job died with it. Observed in
the field as ``upgrade | running`` for ``dbus:UpgradePackages`` with two
packages left behind and no progress bar.

The restart now belongs to the service, which is the only party that
knows when its transaction is finished and answered.
"""

from pathlib import Path

import pytest

from urpm.dbus import service as service_module
from urpm.dbus.service import UrpmDBusService, _code_fingerprint


@pytest.fixture
def code_tree(tmp_path):
    """A miniature package tree standing in for the installed code."""
    (tmp_path / 'core').mkdir()
    (tmp_path / '__init__.py').write_text("__version__ = '1.0'\n")
    (tmp_path / 'core' / 'database.py').write_text("VALUE = 1\n")
    return tmp_path


class TestCodeFingerprint:
    """Has the code under this process been replaced?"""

    def test_an_untouched_tree_reads_the_same(self, code_tree):
        assert _code_fingerprint(code_tree) == _code_fingerprint(code_tree)

    def test_rewriting_a_file_shows_up(self, code_tree):
        before = _code_fingerprint(code_tree)

        (code_tree / 'core' / 'database.py').write_text("VALUE = 22222\n")

        assert _code_fingerprint(code_tree) != before

    def test_a_new_file_shows_up(self, code_tree):
        before = _code_fingerprint(code_tree)

        (code_tree / 'core' / 'resolver.py').write_text("pass\n")

        assert _code_fingerprint(code_tree) != before

    def test_a_removed_file_shows_up(self, code_tree):
        before = _code_fingerprint(code_tree)

        (code_tree / 'core' / 'database.py').unlink()

        assert _code_fingerprint(code_tree) != before

    def test_only_python_files_count(self, code_tree):
        """Byte-compiled files follow their source; they are not the source."""
        before = _code_fingerprint(code_tree)

        (code_tree / 'core' / 'database.pyc').write_bytes(b'\x00\x01')

        assert _code_fingerprint(code_tree) == before

    def test_it_covers_the_real_tree(self):
        """Guard the guard: an empty fingerprint would never notice anything."""
        assert len(_code_fingerprint()) > 50
        assert service_module._CODE_ROOT.name == 'urpm'


class TestStopWhenReplaced:
    """When the service decides to step aside, and when it must not."""

    @staticmethod
    def _service(monkeypatch, fingerprint):
        """A service whose view of the code on disk the test controls."""
        instance = UrpmDBusService()
        instance._code_at_startup = 'before'
        monkeypatch.setattr(service_module, '_code_fingerprint',
                            lambda *args, **kwargs: fingerprint)

        scheduled = []
        from gi.repository import GLib
        monkeypatch.setattr(GLib, 'idle_add',
                            lambda fn, **kw: scheduled.append((fn, kw)))
        return instance, scheduled

    def test_untouched_code_means_staying(self, monkeypatch):
        instance, scheduled = self._service(monkeypatch, 'before')

        instance._stop_if_code_replaced()

        assert scheduled == []

    def test_replaced_code_schedules_the_stop(self, monkeypatch):
        instance, scheduled = self._service(monkeypatch, 'after')

        instance._stop_if_code_replaced()

        assert len(scheduled) == 1
        assert scheduled[0][0] == instance._stop_for_new_code

    def test_it_yields_to_the_reply_already_queued(self, monkeypatch):
        """_emit_complete and _return_invocation queue at default priority.

        Ordering by priority rather than by a timer is what makes the
        client's answer reach it before the process goes away.
        """
        from gi.repository import GLib

        instance, scheduled = self._service(monkeypatch, 'after')

        instance._stop_if_code_replaced()

        assert scheduled[0][1]['priority'] == GLib.PRIORITY_LOW
        assert GLib.PRIORITY_LOW > GLib.PRIORITY_DEFAULT_IDLE

    def test_a_transaction_still_running_defers_the_stop(self, monkeypatch):
        """Leaving mid-transaction is the very thing being fixed."""
        instance, scheduled = self._service(monkeypatch, 'after')
        instance._active_operations = {'op2': 'install'}

        instance._stop_if_code_replaced()

        assert scheduled == []


class TestStopForNewCode:
    """What actually happens when the scheduled stop fires."""

    class _Loop:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    class _Connection:
        def __init__(self):
            self.flushed = False

        def flush_sync(self, cancellable):
            self.flushed = True

    def test_it_flushes_then_quits(self):
        instance = UrpmDBusService()
        instance._loop = self._Loop()
        instance._connection = self._Connection()

        assert instance._stop_for_new_code() is False
        assert instance._connection.flushed
        assert instance._loop.quit_called

    def test_a_failing_flush_still_quits(self):
        """A bus that will not flush must not strand the old code running."""
        class _Broken:
            def flush_sync(self, cancellable):
                raise OSError("bus is gone")

        instance = UrpmDBusService()
        instance._loop = self._Loop()
        instance._connection = _Broken()

        instance._stop_for_new_code()

        assert instance._loop.quit_called

    def test_it_survives_having_no_loop_yet(self):
        instance = UrpmDBusService()
        instance._loop = None
        instance._connection = None

        assert instance._stop_for_new_code() is False


class TestPackagingDoesNotRestartUs:
    """The scriptlet must stay out of it.

    Pinned here rather than left to review: the line that caused the
    incident was two words long and read as an obvious convenience.
    """

    @staticmethod
    def _post_packagekit_backend():
        spec = (Path(__file__).resolve().parents[2]
                / 'rpmbuild' / 'SPECS' / 'urpm-ng.spec')
        if not spec.exists():
            pytest.skip("spec file not shipped with the package")
        body, capturing = [], False
        for line in spec.read_text().splitlines():
            if line.startswith('%post packagekit-backend'):
                capturing = True
                continue
            if capturing and line.startswith('%') and not line.startswith('%{'):
                break
            if capturing:
                body.append(line)
        assert body, "%post packagekit-backend not found in the spec"
        return body

    def test_it_restarts_neither_daemon(self):
        offenders = [line for line in self._post_packagekit_backend()
                     if 'restart' in line and not line.lstrip().startswith('#')]

        assert not offenders, (
            "this scriptlet runs inside the transaction the service is "
            "executing; restarting either daemon kills it:\n  "
            + "\n  ".join(offenders))
