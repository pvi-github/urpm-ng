import time
from urpm.cli.display import DownloadProgressDisplay, format_size
from urpm.core.download import DownloadProgress, Downloader, DownloadItem, DownloadResult
import pytest
from pathlib import Path

def test_download_progress_samples_and_speed():
    # Test 1: Create DownloadProgress instance
    print("Test 1: Creating DownloadProgress instance")
    progress = DownloadProgress(
        name="test-package",
        bytes_done=0,
        bytes_total=10240,
        source="server1",
        source_type="server",
        start_time=time.time(),
        samples=[]
    )
    print(f"Initial state - Bytes done: {progress.bytes_done}, Total: {progress.bytes_total}")
    print()

    # Test 2: Test add_sample method
    print("Test 2: Testing add_sample method")
    time.sleep(0.1)
    progress.add_sample(1024)
    time.sleep(0.1)
    progress.add_sample(2048)
    time.sleep(0.1)
    progress.add_sample(3072)
    print(f"Number of samples: {len(progress.samples)}")
    print(f"Samples: {progress.samples}")
    print()

    # Test 3: Test get_speed method
    print("Test 3: Testing get_speed method")
    speed = progress.get_speed()
    assert(speed < 10250.0 and speed > 10100.0)
    print(f"Calculated speed: {speed:.2f} bytes/sec")
    print()

    # Test 4: Test speed calculation with more samples
    print("Test 4: Testing with more samples")
    for i in range(4, 11):
        time.sleep(0.1)
        progress.add_sample(1024 * i)
    speed = progress.get_speed()
    assert(speed < 10250.0 and speed > 10100.0)
    print(f"Updated speed: {speed:.2f} bytes/sec")
    print(f"Total samples: {len(progress.samples)}")
    print()

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

    return 0

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
    assert("[2/4] 27% 175.8KB/s" in result)
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