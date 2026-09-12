"""Tests for the progress bar's phase weighting.

A PackageKit client draws one percentage for a whole operation, but an
upgrade is a download followed by an rpm transaction. The backend used to
give each exactly half of the bar, which is wrong in both directions: a
slow 3 GB fetch crawled to 50 then finished in one jump, and a fully
cached upgrade jumped to 50 at once and then crawled.

These tests pin the properties that replace that constant:

* the split comes from the resolved plan, not from a literal;
* an operation with nothing to fetch starts its bar at zero, with no
  special case written for it;
* the result is monotonic, which is the one thing a progress bar may
  never break.
"""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from urpm.core.progress_scale import (
    PERCENTAGE_UNKNOWN, ProgressScale, item_percentage,
)

GB = 1024 ** 3


def _action(kind, size=0, filesize=0, from_size=0):
    """A stand-in for the resolver's PackageAction."""
    return SimpleNamespace(action=kind, size=size, filesize=filesize,
                           from_size=from_size)


class TestDownloadShare:
    """How much of the bar the download owns."""

    def test_nothing_to_fetch_gives_the_download_no_share(self):
        """The requirement that used to need a special case."""
        scale = ProgressScale(download_bytes=0, install_bytes=4 * GB)

        assert scale.download_share == 0.0
        assert scale.downloading(0, 1) == 0
        assert scale.transacting(0, 10) == 0

    def test_the_share_follows_the_plan(self):
        scale = ProgressScale(download_bytes=3 * GB, install_bytes=1 * GB)

        assert scale.download_share == pytest.approx(0.75)

    def test_a_removal_gives_the_whole_bar_to_rpm(self):
        """Built with no bytes at all, as _run_remove does."""
        scale = ProgressScale()

        assert scale.transacting(0, 8) == 0
        assert scale.transacting(4, 8) == 50
        assert scale.transacting(8, 8) == 100

    def test_a_plan_with_nothing_to_unpack_still_behaves(self):
        """Download-only: the bar belongs to the fetch."""
        scale = ProgressScale(download_bytes=2 * GB, install_bytes=0)

        assert scale.download_share == 1.0
        assert scale.downloading(1, 2) == 50
        assert scale.downloading(2, 2) == 100


class TestBands:
    """Each phase stays inside its own slice of the bar."""

    def test_downloading_spans_zero_to_the_share(self):
        scale = ProgressScale(download_bytes=3 * GB, install_bytes=1 * GB)

        assert scale.downloading(0, 100) == 0
        assert scale.downloading(50, 100) == 38
        assert scale.downloading(100, 100) == 75

    def test_transacting_spans_the_share_to_a_hundred(self):
        scale = ProgressScale(download_bytes=3 * GB, install_bytes=1 * GB)

        assert scale.transacting(0, 100) == 75
        assert scale.transacting(50, 100) == 88
        assert scale.transacting(100, 100) == 100

    def test_the_bar_never_goes_backwards(self):
        """The one property a progress bar may not break."""
        scale = ProgressScale(download_bytes=1 * GB, install_bytes=3 * GB)

        seen = ([scale.downloading(i, 20) for i in range(21)]
                + [scale.transacting(i, 20) for i in range(21)])

        assert seen == sorted(seen)
        assert seen[0] == 0
        assert seen[-1] == 100

    def test_an_unknown_total_pins_the_band_start(self):
        """A mirror that sends no Content-Length must not break the bar."""
        scale = ProgressScale(download_bytes=1 * GB, install_bytes=1 * GB)

        assert scale.downloading(0, 0) == 0
        assert scale.transacting(0, 0) == 50

    def test_a_count_beyond_its_total_is_clamped(self):
        scale = ProgressScale(download_bytes=1 * GB, install_bytes=1 * GB)

        assert scale.downloading(999, 10) == 50
        assert scale.transacting(999, 10) == 100

    def test_a_negative_count_is_clamped(self):
        scale = ProgressScale(download_bytes=1 * GB, install_bytes=1 * GB)

        assert scale.downloading(-5, 10) == 0


class TestItemPercentage:
    """Progress within the package currently being handled."""

    def test_an_unknown_total_reads_as_unknown(self):
        assert item_percentage(0, 0) == PERCENTAGE_UNKNOWN
        assert item_percentage(10, 0) == PERCENTAGE_UNKNOWN

    def test_it_reports_the_fraction(self):
        assert item_percentage(1, 4) == 25
        assert item_percentage(4, 4) == 100

    def test_it_is_clamped(self):
        assert item_percentage(9, 4) == 100
        assert item_percentage(-1, 4) == 0

    def test_unknown_is_packagekit_s_own_sentinel(self):
        """So the C backend can forward it without translating."""
        assert PERCENTAGE_UNKNOWN == 101


class TestScaleFromPlan:
    """The service builds the scale from the resolved plan."""

    @staticmethod
    def _build(actions, download_items):
        from urpm.dbus.service import UrpmDBusService
        return UrpmDBusService._progress_scale(actions, download_items)

    def test_it_weighs_missing_payloads_against_unpacked_bytes(self):
        """Only what is really missing counts on the download side.

        The plan installs two packages weighing 1 GB unpacked, but just
        one of them is absent from the cache.
        """
        actions = [_action('install', size=512 * 1024 * 1024),
                   _action('install', size=512 * 1024 * 1024)]
        items = [SimpleNamespace(size=1 * GB)]

        scale = self._build(actions, items)

        assert scale.download_bytes == 1 * GB
        assert scale.install_bytes == 1 * GB
        assert scale.download_share == pytest.approx(0.5)

    def test_a_fully_cached_plan_yields_no_download_share(self):
        actions = [_action('upgrade', size=2 * GB, from_size=1 * GB)]

        scale = self._build(actions, [])

        assert scale.download_share == 0.0
        assert scale.transacting(0, 4) == 0

    def test_a_removal_plan_weighs_nothing(self):
        actions = [_action('remove', size=3 * GB)]

        scale = self._build(actions, [])

        assert scale.download_bytes == 0
        assert scale.install_bytes == 0
        assert scale.transacting(1, 2) == 50

    def test_items_without_a_size_do_not_break_the_scale(self):
        """Synthesis metadata is not always complete."""
        actions = [_action('install', size=1 * GB)]
        items = [SimpleNamespace(size=0), SimpleNamespace(size=None)]

        scale = self._build(actions, items)

        assert scale.download_bytes == 0
        assert scale.download_share == 0.0


class TestReporters:
    """What the service actually puts on the wire during an operation."""

    @staticmethod
    def _service():
        """A service whose progress emissions are captured, not sent."""
        from urpm.dbus.service import UrpmDBusService

        service = UrpmDBusService()
        sent = []

        def capture(op_id, phase, package="", **fields):
            sent.append({'phase': phase, 'package': package, **fields})

        service._emit_progress = capture
        return service, sent

    def test_the_download_reports_rate_and_bytes_left(self):
        """Figures the backend used to throw away.

        ``coordinator_speed`` reached the callback and went nowhere, so
        Discover had a bar and nothing else to show.
        """
        service, sent = self._service()
        scale = ProgressScale(download_bytes=3 * GB, install_bytes=1 * GB)

        report = service._download_reporter('op', scale)
        report('firefox', 1, 4, 1 * GB, 3 * GB,
               item_bytes=500, item_total=1000, coordinator_speed=2_500_000.0)

        assert sent[0]['phase'] == 'downloading'
        assert sent[0]['percentage'] == 25
        assert sent[0]['item_percentage'] == 50
        assert sent[0]['speed'] == 2_500_000
        assert sent[0]['download_remaining'] == 2 * GB

    def test_the_download_names_the_build_it_is_fetching(self):
        """Without this there is no progress bar at all on Discover's updates.

        That page hides its single overall bar while a transaction runs
        and draws one bar per package instead, fed only by PackageKit's
        ``ItemProgress`` (``PackageKitUpdater::itemProgress`` ->
        ``resourceProgressed`` -> ``UpdateModel``'s ``resourceProgress``).
        ``ItemProgress`` needs a full ``package_id``, so the version and
        the architecture have to travel with the name. The download used
        to send a bare name, and nothing was drawn until rpm began
        writing.
        """
        service, sent = self._service()
        items = [SimpleNamespace(name='iwlwifi-firmware',
                                 version='20260622',
                                 release='1.mga10.nonfree',
                                 arch='noarch', size=37325049)]

        report = service._download_reporter(
            'op', ProgressScale(download_bytes=37325049), items)
        report('iwlwifi-firmware', 0, 1, 1988637, 37325049,
               item_bytes=1988637, item_total=37325049)

        assert sent[0]['evr'] == '20260622-1.mga10.nonfree'
        assert sent[0]['arch'] == 'noarch'
        assert sent[0]['item_percentage'] == 5

    def test_a_payload_taken_from_cache_names_no_build(self):
        """The downloader reports a literal "(cache)" for those."""
        service, sent = self._service()
        items = [SimpleNamespace(name='firefox', version='153.2.0',
                                 release='1.mga10', arch='x86_64', size=1)]

        report = service._download_reporter('op', ProgressScale(), items)
        report('(cache)', 1, 1, 0, 0)

        assert sent[0]['evr'] == ''
        assert sent[0]['arch'] == ''

    def test_an_unexpected_name_names_no_build(self):
        service, sent = self._service()

        report = service._download_reporter('op', ProgressScale(), ())
        report('mystery', 0, 1, 0, 1)

        assert sent[0]['evr'] == ''
        assert sent[0]['arch'] == ''

    def test_a_mirror_without_content_length_falls_back_to_counting(self):
        service, sent = self._service()
        scale = ProgressScale(download_bytes=4 * GB, install_bytes=0)

        report = service._download_reporter('op', scale)
        report('firefox', 2, 4, 0, 0)

        assert sent[0]['percentage'] == 50

    def test_the_transaction_carries_evr_and_arch(self):
        """So the backend can build a package_id without parsing a NEVRA."""
        from urpm.core.transaction_queue import TransactionPhase

        service, sent = self._service()
        scale = ProgressScale(download_bytes=3 * GB, install_bytes=1 * GB)
        actions = [SimpleNamespace(name='firefox', evr='153.2.0-1.mga10',
                                   arch='x86_64')]

        report = service._transaction_reporter('op', scale, 'upgrading', actions)
        report(SimpleNamespace(phase=TransactionPhase.INSTALL,
                               package_name='firefox', script_name='',
                               packages_done=1, packages_total=4,
                               bytes_done=30, bytes_total=100))

        assert sent[0]['phase'] == 'upgrading'
        assert sent[0]['evr'] == '153.2.0-1.mga10'
        assert sent[0]['arch'] == 'x86_64'
        assert sent[0]['percentage'] == 81
        assert sent[0]['item_percentage'] == 30

    def test_scriptlets_get_a_phase_of_their_own(self):
        """Their counter is frozen and they are often the longest step."""
        from urpm.core.transaction_queue import TransactionPhase

        service, sent = self._service()
        actions = [SimpleNamespace(name='firefox', evr='153.2.0-1.mga10',
                                   arch='x86_64')]

        report = service._transaction_reporter(
            'op', ProgressScale(), 'upgrading', actions)
        report(SimpleNamespace(phase=TransactionPhase.SCRIPT,
                               package_name='', script_name='firefox',
                               packages_done=4, packages_total=4,
                               bytes_done=0, bytes_total=0))

        assert sent[0]['phase'] == 'script'
        assert sent[0]['package'] == 'firefox'
        assert sent[0]['item_percentage'] == PERCENTAGE_UNKNOWN

    def test_a_whole_upgrade_never_goes_backwards(self):
        """Download then transaction, read as one bar."""
        from urpm.core.transaction_queue import TransactionPhase

        service, sent = self._service()
        scale = ProgressScale(download_bytes=3 * GB, install_bytes=1 * GB)

        download = service._download_reporter('op', scale)
        for step in range(5):
            download('pkg', step, 4, step * GB, 4 * GB)

        transact = service._transaction_reporter('op', scale, 'upgrading', ())
        for step in range(5):
            transact(SimpleNamespace(phase=TransactionPhase.INSTALL,
                                     package_name='pkg', script_name='',
                                     packages_done=step, packages_total=4,
                                     bytes_done=0, bytes_total=0))

        bar = [entry['percentage'] for entry in sent]
        assert bar == sorted(bar)
        assert bar[0] == 0
        assert bar[-1] == 100


class TestSignalCallSites:
    """Every emission in the service must match the emitter's signature.

    ``_emit_progress`` takes keyword-only arguments, which makes it easy
    to break from a distance: extending it left one caller passing six
    positional arguments, inside a nested callback whose arguments sat on
    the following line, so a grep for the call did not show it. The whole
    suite stayed green and the breakage only surfaced when Discover
    refreshed its metadata and all fourteen media answered with the same
    ``TypeError``.

    So bind every call site against the real signature instead of
    trusting a search. Parsing beats grepping here because the check does
    not care how the call is laid out across lines.
    """

    EMITTERS = ('_emit_progress', '_emit_complete')

    @staticmethod
    def _calls_in_service():
        """Yield ``(method_name, node)`` for every self._emit_* call."""
        from urpm.dbus import service

        tree = ast.parse(Path(inspect.getfile(service)).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'self'
                    and func.attr in TestSignalCallSites.EMITTERS):
                yield func.attr, node

    def test_the_service_still_emits_progress(self):
        """Guard the guard: a walk that finds nothing proves nothing."""
        found = [name for name, _ in self._calls_in_service()]

        assert found.count('_emit_progress') >= 8
        assert '_emit_complete' in found

    def test_every_call_site_binds(self):
        from urpm.dbus.service import UrpmDBusService

        failures = []
        for name, node in self._calls_in_service():
            signature = inspect.signature(getattr(UrpmDBusService, name))

            if any(isinstance(a, ast.Starred) for a in node.args):
                continue  # argument list not knowable without running it
            if any(kw.arg is None for kw in node.keywords):
                continue  # ** expansion, likewise

            # ``self`` is supplied by the bound call, stand in for it.
            positional = ['<self>'] + ['<arg>'] * len(node.args)
            keywords = {kw.arg: '<arg>' for kw in node.keywords}
            try:
                signature.bind(*positional, **keywords)
            except TypeError as exc:
                failures.append(f"line {node.lineno}: {name}(): {exc}")

        assert not failures, (
            "call sites out of step with their signature:\n  "
            + "\n  ".join(failures))


class TestRefreshRun:
    """The path that actually broke for a user.

    ``_run_refresh`` builds its progress callback inline, and no test ever
    ran it, so a stale call to ``_emit_progress`` shipped green: every
    medium answered ``takes from 3 to 4 positional arguments but 7 were
    given`` and Discover reported ``Refreshed 0, failed 14``.
    """

    @staticmethod
    def _run(monkeypatch, media):
        from urpm.dbus import service as service_module

        instance = service_module.UrpmDBusService()
        sent, completed = [], []

        instance._emit_progress = lambda op_id, phase, package="", **fields: (
            sent.append({'phase': phase, 'package': package, **fields}))
        instance._emit_complete = lambda op_id, ok, message="": (
            completed.append((ok, message)))
        instance._return_invocation = lambda *args, **kwargs: None

        def fake_sync(db, progress, force=False):
            for index, name in enumerate(media, start=1):
                progress(name, 'synthesis', index, len(media))
            return [(name, SimpleNamespace(success=True, error=None))
                    for name in media]

        monkeypatch.setattr('urpm.core.sync.sync_all_media', fake_sync)
        instance._run_refresh('op', None, None)
        return sent, completed

    def test_a_refresh_reports_every_medium(self, monkeypatch):
        media = ['Core Release', 'Core Updates', 'Tainted Release']

        sent, completed = self._run(monkeypatch, media)

        reported = [e['package'] for e in sent if e['package']]
        assert reported == media
        assert completed == [(True, 'Refreshed 3 media')]

    def test_the_bar_runs_the_whole_way(self, monkeypatch):
        sent, _ = self._run(monkeypatch, ['a', 'b', 'c', 'd'])

        bar = [entry['percentage'] for entry in sent]
        assert bar == sorted(bar)
        assert bar[-1] == 100

    def test_the_stage_travels_as_the_message(self, monkeypatch):
        sent, _ = self._run(monkeypatch, ['Core Release'])

        assert sent[-1]['message'] == 'synthesis'
