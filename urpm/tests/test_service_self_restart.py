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


class _Invocation:
    """Enough of a GDBus invocation to be answered or refused."""

    def __init__(self):
        self.errors = []
        self.returned = []

    def return_dbus_error(self, name, message):
        self.errors.append((name, message))

    def return_error_literal(self, domain, code, message):
        self.errors.append((domain, message))

    def return_value(self, value):
        self.returned.append(value)


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
        monkeypatch.setattr(GLib, 'timeout_add_seconds',
                            lambda delay, fn, **kw: scheduled.append(
                                (fn, delay)) or len(scheduled))
        monkeypatch.setattr(GLib, 'source_remove', lambda tag: None)
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

    def test_it_waits_out_the_calls_already_dispatched_to_us(self,
                                                             monkeypatch):
        """Ordering by priority was not enough, and the bus says why.

        Captured on a live machine the 2026-10-25: packagekitd sent
        ``GetUpdates`` to our **unique** connection name five
        milliseconds before our own ``ReleaseName``, so the call was
        already in our queue and no release could reroute it.  We quit
        without answering and Discover got
        ``org.freedesktop.DBus.Error.NoReply: Message recipient
        disconnected from message bus without replying``.

        Handing the public name back still matters for a client that
        resolves it afterwards; it is powerless against a proxy that
        addresses us directly.  So the stop waits for a quiet window
        instead of racing the queue.
        """
        instance, scheduled = self._service(monkeypatch, 'after')

        instance._stop_if_code_replaced()

        assert scheduled[0][1] == service_module.STOP_QUIET_SECONDS

    def test_a_transaction_still_running_defers_the_stop(self, monkeypatch):
        """Leaving mid-transaction is the very thing being fixed."""
        instance, scheduled = self._service(monkeypatch, 'after')
        instance._active_operations = {'op2': 'install'}

        instance._stop_if_code_replaced()

        assert scheduled == []


class TestTheQuietWindow:
    """A call that reaches us after the decision is still answered.

    The decision to step aside cannot unsend what is already on its way
    to our own connection name, so the window exists to serve it.  Each
    incoming call pushes the departure back; a ceiling keeps a chatty
    client from pinning us here for ever, since the point of going is to
    pick up the code that just landed.
    """

    @staticmethod
    def _service(monkeypatch):
        instance = UrpmDBusService()
        instance._code_at_startup = 'before'
        monkeypatch.setattr(service_module, '_code_fingerprint',
                            lambda *a, **kw: 'after')

        armed, removed = [], []
        from gi.repository import GLib
        monkeypatch.setattr(GLib, 'timeout_add_seconds',
                            lambda delay, fn, **kw: armed.append((fn, delay))
                            or len(armed))
        monkeypatch.setattr(GLib, 'source_remove', removed.append)
        return instance, armed, removed

    def test_an_incoming_call_pushes_the_departure_back(self, monkeypatch):
        instance, armed, removed = self._service(monkeypatch)
        instance._stop_if_code_replaced()

        instance._postpone_stop()

        assert len(armed) == 2, "the window was not re-armed"
        assert removed, "the previous window was left running"

    def test_a_call_before_any_decision_arms_nothing(self, monkeypatch):
        # The common case, and it must cost nothing: the service is not
        # going anywhere.
        instance, armed, _removed = self._service(monkeypatch)

        instance._postpone_stop()

        assert armed == []

    def test_the_ceiling_stops_pushing(self, monkeypatch):
        instance, armed, _removed = self._service(monkeypatch)
        instance._stop_if_code_replaced()
        instance._stop_deadline = 0.0  # as if the ceiling were reached

        instance._postpone_stop()

        assert len(armed) == 1, "a late call must not postpone past the cap"

    def test_every_method_call_goes_through_the_postponement(self,
                                                             monkeypatch):
        """The wiring, not the logic: one entry point, one call.

        A window nothing re-arms is the defect with extra steps, and
        nothing else in the file would say so.
        """
        instance, _armed, _removed = self._service(monkeypatch)
        pushed = []
        monkeypatch.setattr(instance, '_postpone_stop',
                            lambda: pushed.append(True))

        instance._on_method_call(None, ':1.1', service_module.OBJECT_PATH,
                                 service_module.INTERFACE_NAME,
                                 'NoSuchMethod', None, _Invocation())

        assert pushed, "a method call did not postpone the departure"

    def test_the_window_is_short_and_the_ceiling_is_not(self):
        # Short enough that an operator does not wait on us, long
        # enough to cover a queue that was five milliseconds ahead.
        assert 1 <= service_module.STOP_QUIET_SECONDS <= 10
        assert service_module.STOP_MAX_SECONDS >= (
            3 * service_module.STOP_QUIET_SECONDS)


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


class TestTheNameIsHandedBack:
    """Leaving without giving up the bus name strands the next caller.

    Seen for real once an upgrade through Discover finally reached the
    point of replacing our own code: Discover sends ``GetUpdates`` right
    away to redraw its list, the bus still believed we owned the name and
    routed the call to a process on its way out, and the client got
    ``org.freedesktop.DBus.Error.NoReply: Message recipient disconnected
    from message bus without replying``.

    Releasing the name first is what turns that into a fresh activation,
    which then runs the code that just landed.
    """

    @staticmethod
    def _instance(monkeypatch, owner_id=7):
        from gi.repository import Gio

        order = []

        class _Loop:
            def quit(self):
                order.append('quit')

        class _Connection:
            def flush_sync(self, cancellable):
                order.append('flush')

        monkeypatch.setattr(
            Gio, 'bus_unown_name',
            lambda oid: order.append(('unown', oid)))

        instance = UrpmDBusService()
        instance._loop = _Loop()
        instance._connection = _Connection()
        instance._bus_name_owner_id = owner_id
        return instance, order

    def test_the_name_goes_back_before_the_loop_ends(self, monkeypatch):
        instance, order = self._instance(monkeypatch)

        instance._stop_for_new_code()

        assert order == [('unown', 7), 'flush', 'quit']

    def test_the_release_is_flushed_before_the_process_goes(self, monkeypatch):
        """Queued and never sent would leave the bus none the wiser."""
        instance, order = self._instance(monkeypatch)

        instance._stop_for_new_code()

        assert order.index(('unown', 7)) < order.index('flush')

    def test_the_name_is_given_back_once(self, monkeypatch):
        """A second pass must not hand back an id the bus already took."""
        instance, order = self._instance(monkeypatch)

        instance._stop_for_new_code()
        instance._stop_for_new_code()

        assert [e for e in order if e != 'flush' and e != 'quit'] == [
            ('unown', 7)]

    def test_owning_nothing_yet_still_quits(self, monkeypatch):
        """The stop must never hang on a service that never took the name."""
        instance, order = self._instance(monkeypatch, owner_id=None)

        instance._stop_for_new_code()

        assert order == ['flush', 'quit']

    def test_a_refused_release_still_quits(self, monkeypatch):
        """Staying alive on old code is worse than an untidy exit."""
        from gi.repository import Gio

        def explode(owner_id):
            raise RuntimeError("the bus went away")

        monkeypatch.setattr(Gio, 'bus_unown_name', explode)

        instance = UrpmDBusService()
        instance._loop = TestStopForNewCode._Loop()
        instance._connection = TestStopForNewCode._Connection()
        instance._bus_name_owner_id = 7

        instance._stop_for_new_code()

        assert instance._loop.quit_called


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
