import time
from urpm.cli.display import DownloadProgressDisplay, format_size
from urpm.core.download import DownloadProgress, Downloader, DownloadItem, DownloadResult
import pytest
from pathlib import Path

def test_download_progress_samples_and_speed(monkeypatch):
    # ``time.sleep`` under VM/CI load can stretch from 0.1s to 0.5s+ and
    # send the measured speed below the expected band, flaking the test
    # at random.  Replace ``time.time`` with a deterministic counter so
    # ``add_sample`` records exact intervals; the maths under test
    # (delta-bytes / delta-time in :meth:`DownloadProgress.get_speed`)
    # is the same regardless of how it was driven.
    fake_now = [0.0]

    def _fake_time():
        return fake_now[0]

    monkeypatch.setattr(time, 'time', _fake_time)

    def advance(seconds: float):
        fake_now[0] += seconds

    # Test 1: Create DownloadProgress instance
    progress = DownloadProgress(
        name="test-package",
        bytes_done=0,
        bytes_total=10240,
        source="server1",
        source_type="server",
        start_time=time.time(),
        samples=[]
    )
    assert progress.bytes_done == 0
    assert progress.bytes_total == 10240

    # Test 2: ``add_sample`` appends timestamp + byte count
    advance(0.1)
    progress.add_sample(1024)
    advance(0.1)
    progress.add_sample(2048)
    advance(0.1)
    progress.add_sample(3072)
    assert len(progress.samples) == 3
    # Float addition drift (0.1 + 0.1 + 0.1 != 0.3 exactly): match on the
    # byte component and approximate the timestamp.
    last_t, last_b = progress.samples[-1]
    assert last_b == 3072
    assert last_t == pytest.approx(0.3)

    # Test 3: ``get_speed`` returns delta-bytes / delta-time exactly
    # 3072 - 1024 bytes over 0.3 - 0.1 = 0.2 s → 10240 B/s
    speed = progress.get_speed()
    assert speed == pytest.approx(10240.0)

    # Test 4: rolling-window stays at the documented 10 samples
    for i in range(4, 11):
        advance(0.1)
        progress.add_sample(1024 * i)
    # add_sample drops the oldest when len > 10; we added 3 + 7 = 10
    # so nothing has been dropped yet.
    assert len(progress.samples) == 10
    # 10240 - 1024 bytes over 1.0 - 0.1 = 0.9 s → 10240 B/s
    speed = progress.get_speed()
    assert speed == pytest.approx(10240.0)

    # One more sample evicts the oldest:
    advance(0.1)
    progress.add_sample(11 * 1024)
    assert len(progress.samples) == 10
    first_t, first_b = progress.samples[0]
    assert first_b == 2048  # the original (0.1, 1024) sample is gone
    assert first_t == pytest.approx(0.2)

    # Test 5: Test edge cases
    print("Test 5: Testing edge cases")
    empty_progress = DownloadProgress(
        name="empty-test",
        bytes_done=0,
        bytes_total=100,
        source="test",
        source_type="test",
        start_time=time.time(),
        samples=[]
    )
    empty_speed = empty_progress.get_speed()
    print(f"Speed with no samples: {empty_speed:.2f} bytes/sec")

    single_sample_progress = DownloadProgress(
        name="single-test",
        bytes_done=0,
        bytes_total=100,
        source="test",
        source_type="test",
        start_time=time.time(),
        samples=[]
    )
    single_sample_progress.add_sample(50)
    single_speed = single_sample_progress.get_speed()
    print(f"Speed with single sample: {single_speed:.2f} bytes/sec")
    print()

    # Test 6: Test with rapid successive samples
    print("Test 6: Testing with rapid samples")
    rapid_progress = DownloadProgress(
        name="rapid-test",
        bytes_done=0,
        bytes_total=10000,
        source="test",
        source_type="test",
        start_time=time.time(),
        samples=[]
    )
    for i in range(10):
        rapid_progress.add_sample(1000 * (i+1))
    rapid_speed = rapid_progress.get_speed()
    print(f"Rapid sample speed: {rapid_speed:.2f} bytes/sec")
    print()

def test_download_progress_display():
    # Initialisation du display
    display = DownloadProgressDisplay(num_workers=4, bar_width=20, name_width=20)

    # Création de faux objets DownloadProgress pour simuler des téléchargements
    class MockProgress:
        def __init__(self, name, bytes_done, bytes_total, speed):
            self.name = name
            self.bytes_done = bytes_done
            self.bytes_total = bytes_total
            self.speed = speed
            self.source = "fr2.rpmfind.net"

        def get_speed(self):
            return self.speed

    # Test 1: Affichage initial avec aucun téléchargement
    print("Test 1: Aucun téléchargement")
    slots_status = [(0, None), (1, None), (2, None), (3, None)]
    result = display.render(0, 4, 0, 1000000, slots_status)
    print(result)

    # Test 2: Affichage avec des téléchargements en cours
    print("Test 2: Téléchargements en cours")
    progress1 = MockProgress("neovim-data", 123456, 4321000, 12345.6)
    progress2 = MockProgress("blablabla", 456789, 4321000, 45678.9)
    progress3 = MockProgress("prout", 1000000, 1600000, 100000.0)
    slots_status = [
        (0, progress1),
        (1, None),
        (2, progress2),
        (3, progress3)
    ]
    result = display.render(2, 4, 2700000, 10000000, slots_status, global_speed=180000.5)
    # Percentage is count-based: 2/4 = 50%
    assert("[2/4] 50% 175.8KB/s" in result)
    assert("neovim-data" in result)
    assert("blablabla" in result)
    assert("prout" in result)
    print(result)

    # Test 3: Affichage avec des noms longs (troncature)
    print("Test 3: Noms longs (troncature)")
    long_name = "very-long-package-name-that-exceeds-the-name-width"
    progress4 = MockProgress(long_name, 500000, 1000000, 50000.0)
    slots_status = [(0, progress4)]
    result = display.render(1, 1, 500000, 1000000, slots_status)
    assert("very-long-package-n…" in result)
    assert("[██████████░░░░░░░░░░]" in result)

    # Test 4: Affichage avec des vitesses différentes
    print("Test 4: Vitesses différentes")
    fast_progress = MockProgress("fast-pkg", 100000, 1000000, 1000000.0)
    slow_progress = MockProgress("slow-pkg", 10000, 1000000, 1000.0)
    slots_status = [(0, fast_progress), (2, slow_progress)]
    result = display.render(0, 2, 110000, 2000000, slots_status)
    assert("97.7KB/976.6KB (fr2.rpmfind.net)" in result)
    assert("9.8KB/976.6KB (fr2.rpmfind.net)" in result)
    print(result)

    # Test 5: Affichage avec des tailles différentes
    print("Test 5: Tailles différentes")
    small_progress = MockProgress("small", 50000, 100000, 5000.0)
    large_progress = MockProgress("large", 50000000, 100000000, 5000000.0)
    slots_status = [(0, small_progress), (1, large_progress)]
    result = display.render(0, 2, 50500000, 200000000, slots_status)
    print(result)

if __name__ == "__main__":
    test_download_progress_display()

class TestDownloadErrorGrammar:
    """Tests for the typed network error classification.

    Replaces the prior ``error.startswith("HTTP")`` discrimination
    scattered through the multi-server retry loop, and fixes the
    silent misclassification of pycurl errors as hard HTTP failures.
    """

    def test_hard_http_marks_is_hard(self):
        from urpm.core.download import DownloadError, DownloadErrorKind
        err = DownloadError(
            kind=DownloadErrorKind.HARD_HTTP,
            message="HTTP 404",
            http_code=404,
        )
        assert err.is_hard is True
        assert err.http_code == 404

    def test_transient_network_is_not_hard(self):
        from urpm.core.download import DownloadError, DownloadErrorKind
        err = DownloadError(
            kind=DownloadErrorKind.TRANSIENT_NETWORK,
            message="libcurl error: timeout reached",
        )
        assert err.is_hard is False

    def test_local_io_is_hard(self):
        from urpm.core.download import DownloadError, DownloadErrorKind
        err = DownloadError(
            kind=DownloadErrorKind.LOCAL_IO,
            message="No space left on device",
        )
        assert err.is_hard is True

    def test_str_returns_message_for_logging_backward_compat(self):
        from urpm.core.download import DownloadError, DownloadErrorKind
        err = DownloadError(
            kind=DownloadErrorKind.TRANSIENT_NETWORK,
            message="DNS resolution failed",
        )
        # Existing log/append sites do f"{server}: {err}" — must keep
        # producing the readable message.
        assert str(err) == "DNS resolution failed"

    def test_dataclass_is_frozen(self):
        from urpm.core.download import DownloadError, DownloadErrorKind
        err = DownloadError(
            kind=DownloadErrorKind.UNKNOWN, message="x",
        )
        with pytest.raises(Exception):
            err.message = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Where a payload lives, and whether it is already there
# ---------------------------------------------------------------------------
#
# The cache check used to be a method on ``Downloader``, reachable only
# once the download stage had started — long past the point where the
# operator was asked to confirm.  The summary therefore announced the
# whole payload to someone whose cache already held most of it.
#
# Pulling the two out as functions makes the probe affordable before the
# prompt.  The risk that buys is divergence: a second copy of the layout
# rules drifting from the one the downloader writes through.  The last
# test here is the one that would catch that.

from urpm.core.download import (  # noqa: E402
    RPM_MAGIC,
    cache_path_for,
    cached_payload_bytes,
    is_cached_at,
)


def _item(name="foo", version="1.0", release="1", arch="x86_64",
          size=1000, is_official=True,
          relative_path="10/x86_64/media/core/release", **kw):
    return DownloadItem(name=name, version=version, release=release,
                        arch=arch, size=size, media_id=1,
                        relative_path=relative_path,
                        is_official=is_official, **kw)


def _write_rpm(path: Path, body=b"x" * 64):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(RPM_MAGIC + body)


class TestCachePathFor:

    def test_official_media_land_under_their_relative_path(self, tmp_path):
        path = cache_path_for(_item(), tmp_path)
        assert path == (tmp_path / "medias" / "official"
                        / "10/x86_64/media/core/release"
                        / "foo-1.0-1.x86_64.rpm")

    def test_custom_media_land_under_their_name(self, tmp_path):
        item = _item(is_official=False, media_name="BDK-Free")
        path = cache_path_for(item, tmp_path)
        assert path.parent == tmp_path / "medias" / "custom" / "BDK-Free"

    def test_a_probe_creates_nothing(self, tmp_path):
        """The probe runs before the confirmation.  Answering « no » must
        not leave a tree of empty directories behind."""
        cache_path_for(_item(), tmp_path)
        assert not (tmp_path / "medias").exists()

    def test_a_download_creates_its_directory(self, tmp_path):
        path = cache_path_for(_item(), tmp_path, create=True)
        assert path.parent.is_dir()


class TestIsCachedAt:

    def test_absent_file(self, tmp_path):
        assert is_cached_at(_item(), tmp_path) is False

    def test_present_and_valid(self, tmp_path):
        _write_rpm(cache_path_for(_item(), tmp_path))
        assert is_cached_at(_item(), tmp_path) is True

    def test_empty_file_is_not_a_package(self, tmp_path):
        """An interrupted download leaves a zero-length file behind."""
        path = cache_path_for(_item(), tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        assert is_cached_at(_item(), tmp_path) is False

    def test_wrong_magic_is_not_a_package(self, tmp_path):
        """A proxy error page saved under the package's name."""
        path = cache_path_for(_item(), tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"<html>403</html>")
        assert is_cached_at(_item(), tmp_path) is False


class TestCachedPayloadBytes:

    def test_counts_only_what_is_there(self, tmp_path):
        here = _item("here", size=700)
        missing = _item("missing", size=300)
        _write_rpm(cache_path_for(here, tmp_path))
        assert cached_payload_bytes([here, missing], tmp_path) == 700

    def test_an_empty_plan_costs_nothing(self, tmp_path):
        assert cached_payload_bytes([], tmp_path) == 0

    def test_a_truncated_file_does_not_count(self, tmp_path):
        item = _item(size=700)
        path = cache_path_for(item, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        assert cached_payload_bytes([item], tmp_path) == 0


class TestTheProbeAndTheDownloaderAgree:
    """Two copies of the layout rules would drift; there is only one."""

    def test_same_path(self, tmp_path):
        downloader = Downloader(cache_dir=tmp_path, use_peers=False)
        item = _item()
        assert (downloader.get_cache_path(item)
                == cache_path_for(item, downloader.payload_dir))

    def test_same_verdict(self, tmp_path):
        downloader = Downloader(cache_dir=tmp_path, use_peers=False)
        item = _item()
        assert downloader.is_cached(item) is False
        _write_rpm(downloader.get_cache_path(item))
        assert downloader.is_cached(item) is True
        assert is_cached_at(item, downloader.payload_dir) is True


class TestThePathDoesNotDependOnTheMirrorCount:
    """``uses_new_schema`` also requires a non-empty server list.

    That is the right question for fetching and the wrong one for a
    path.  The probe runs before the mirror pool is enlarged, so an
    item reaches it with whatever servers the database held at the
    time — sometimes none.  Keying the layout on that would send the
    probe looking in the flat fallback while the download writes under
    ``medias/official/``, and every already-cached package would read
    as missing.
    """

    def test_same_path_with_and_without_servers(self, tmp_path):
        bare = _item()
        served = _item(servers=[{"protocol": "https", "host": "a.b",
                                 "base_path": "/mageia"}])
        assert not bare.uses_new_schema()
        assert served.uses_new_schema()
        assert (cache_path_for(bare, tmp_path)
                == cache_path_for(served, tmp_path))

    def test_a_serverless_item_is_still_found_in_the_cache(self, tmp_path):
        served = _item(servers=[{"protocol": "https", "host": "a.b",
                                 "base_path": "/mageia"}])
        _write_rpm(cache_path_for(served, tmp_path))
        assert is_cached_at(_item(), tmp_path) is True
