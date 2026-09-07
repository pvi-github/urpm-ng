"""The progress bar must count erases, not just extractions.

``_execute_install`` puts installs and erases in one rpm transaction,
but its counters only ever tracked extractions: ``total`` was
``len(rpm_paths)`` and the done-counter advanced on
``INST_CLOSE_FILE`` alone.  Erases emitted ``phase='erase'`` events
with the counter frozen.

While erases were batched at the very end of Tx B that showed as a
single stall before completion.  Interleaving them — which is what
lets a distupgrade release disk space as it goes — turns it into one
stall per batch, on an operation that runs for an hour and where the
bar is the only feedback the operator has.

Measured against real rpm on a mixed transaction (2 installs + 1
erase), the states are:

    before, counting extractions only ... ERASE 3/2   (overshoots)
    after,  counting all elements ....... ERASE 3/3

``ELEM_PROGRESS`` confirms the intended total: rpm reports
``total=3`` for that transaction, installs and erase alike.

The extraction counter is kept separately, because the README
collection anchors on it — it must fire once every payload is on disk
and before triggers start, which has nothing to do with how many
elements the transaction holds.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

rpm = pytest.importorskip("rpm")

SPEC = """\
Name: {name}
Version: 1.0
Release: 1
Summary: progress-count fixture
License: MIT
BuildArch: noarch
%description
Fixture package for the progress counting tests.
%install
mkdir -p %{{buildroot}}/usr/share/{name}
echo x > %{{buildroot}}/usr/share/{name}/f
%files
/usr/share/{name}
"""


def _have(*tools: str) -> bool:
    return all(shutil.which(t) for t in tools)


pytestmark = pytest.mark.skipif(
    not _have("rpmbuild", "unshare", "rpm"),
    reason="needs rpmbuild + unshare to drive a real rpm transaction",
)


@pytest.fixture(scope="module")
def fixture_rpms(tmp_path_factory) -> dict:
    """Build three trivial noarch packages."""
    top = tmp_path_factory.mktemp("rpmbuild")
    for sub in ("SPECS", "RPMS", "BUILD", "SOURCES", "SRPMS"):
        (top / sub).mkdir()
    built = {}
    for name in ("alpha", "beta", "gamma"):
        spec = top / "SPECS" / f"{name}.spec"
        spec.write_text(SPEC.format(name=name))
        proc = subprocess.run(
            ["rpmbuild", "--define", f"_topdir {top}", "-bb", str(spec)],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            pytest.skip(f"rpmbuild failed for {name}: {proc.stderr[-400:]}")
        built[name] = str(
            top / "RPMS" / "noarch" / f"{name}-1.0-1.noarch.rpm")
    return built


DRIVER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from urpm.core.transaction_queue import TransactionQueue

root, rpms = sys.argv[2], sys.argv[3:]
seen = []
def cb(p):
    seen.append([str(p.phase), p.packages_done, p.packages_total,
                 p.package_name or ""])

q = TransactionQueue(root=root)
q.add_install(rpms, operation_id="install", verify_signatures=False,
              force=True, nodeps=True, erase_names=["alpha"])
res = q.execute(progress_callback=cb, full_sync=True)
print("@@JSON@@" + json.dumps({"success": res.success, "seen": seen}))
'''


@pytest.fixture
def mixed_transaction(tmp_path, fixture_rpms) -> dict:
    """Install beta+gamma while erasing alpha, capture the progress.

    Runs inside ``unshare --user --map-root-user`` so no root is
    needed; the chroot is a throwaway directory.
    """
    import json

    chroot = tmp_path / "root"
    chroot.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo_root = str(Path(__file__).resolve().parents[2])

    script = f"""
rpm --root '{chroot}' --initdb 2>/dev/null
rpm --root '{chroot}' -i --nodeps '{fixture_rpms["alpha"]}' 2>/dev/null
{sys.executable} '{driver}' '{repo_root}' '{chroot}' \
    '{fixture_rpms["beta"]}' '{fixture_rpms["gamma"]}'
"""
    proc = subprocess.run(
        ["unshare", "--user", "--map-root-user", "bash", "-c", script],
        capture_output=True, text=True, timeout=300,
    )
    marker = "@@JSON@@"
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(marker)),
        None,
    )
    if line is None:
        pytest.skip(
            f"driver produced no result.\n"
            f"stdout: {proc.stdout[-600:]}\nstderr: {proc.stderr[-600:]}"
        )
    return json.loads(line[len(marker):])


def _package_phase(seen) -> list:
    """Only the phases that drive the main package bar.

    VERIFY and PREPARE keep their own per-phase counters and the widget
    returns early on them, so they are not part of this contract.
    """
    return [row for row in seen
            if row[0].endswith("INSTALL") or row[0].endswith("ERASE")]


class TestMixedTransactionCounting:

    def test_transaction_succeeds(self, mixed_transaction):
        """Guards the fixture: a failed transaction would make every
        assertion below meaningless."""
        assert mixed_transaction["success"]

    def test_total_counts_installs_and_erases(self, mixed_transaction):
        """2 installs + 1 erase → 3, the same total rpm reports through
        ELEM_PROGRESS."""
        totals = {row[2] for row in _package_phase(mixed_transaction["seen"])}
        assert totals == {3}

    def test_counter_never_exceeds_total(self, mixed_transaction):
        """The observed failure before the fix was ``3/2`` — a bar past
        its own end."""
        for _phase, done, total, _name in _package_phase(
                mixed_transaction["seen"]):
            assert done <= total, f"{done}/{total} overshoots"

    def test_counter_is_monotonic(self, mixed_transaction):
        """No going backwards within the package phases: an operator
        watching an hour-long migration must never see the bar retreat."""
        dones = [row[1] for row in _package_phase(mixed_transaction["seen"])]
        assert dones == sorted(dones), dones

    def test_erase_advances_the_counter(self, mixed_transaction):
        """The point of the change: the bar moves while erasing, instead
        of freezing until the phase ends."""
        rows = _package_phase(mixed_transaction["seen"])
        erase_rows = [r for r in rows if r[0].endswith("ERASE")]
        assert erase_rows, "no erase event captured"
        assert erase_rows[-1][1] > erase_rows[0][1], (
            "counter frozen across the whole erase phase"
        )

    def test_counter_reaches_the_total(self, mixed_transaction):
        """It used to stop one short, the erase never being counted."""
        rows = _package_phase(mixed_transaction["seen"])
        assert rows[-1][1] == rows[-1][2] == 3

    def test_erases_are_labelled_as_erases(self, mixed_transaction):
        """So the widget renders « removing » rather than passing an
        erase off as an install."""
        rows = _package_phase(mixed_transaction["seen"])
        erase_names = {r[3] for r in rows if r[0].endswith("ERASE")}
        assert "alpha" in erase_names

    def test_installs_stay_labelled_as_installs(self, mixed_transaction):
        rows = _package_phase(mixed_transaction["seen"])
        names = {r[3] for r in rows if r[0].endswith("INSTALL")}
        assert {"beta", "gamma"} <= names


# ── The old version rpm drops on its own ────────────────────────────
#
# Upgrading in 'u' mode makes rpm remove the superseded version, and it
# fires UNINST_STOP for it exactly as for an erase we asked for.  Those
# removals are the other half of an install already counted; they are
# not in ``elements_total``.
#
# Counting them took Tx A to 22/11 on a plan of eleven upgrades.  The
# widget computes ``filled = bw * pct / 100`` with nothing bounding
# pct, so past 100% the bar rendered wider than its own box and pushed
# the counter off the right edge until it disappeared.  A tester saw
# exactly that on the critical phase.

SPEC_V2 = SPEC.replace("Version: 1.0", "Version: 2.0")

UPGRADE_DRIVER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from urpm.core.transaction_queue import TransactionQueue

root, rpms = sys.argv[2], sys.argv[3:]
seen = []
def cb(p):
    seen.append([str(p.phase), p.packages_done, p.packages_total,
                 p.package_name or ""])

q = TransactionQueue(root=root)
# No erase_names : the only removal is the one rpm performs itself.
q.add_install(rpms, operation_id="upgrade", verify_signatures=False,
              force=True, nodeps=True)
res = q.execute(progress_callback=cb, full_sync=True)
print("@@JSON@@" + json.dumps({"success": res.success, "seen": seen}))
'''


@pytest.fixture(scope="module")
def alpha_v2(tmp_path_factory) -> str:
    top = tmp_path_factory.mktemp("rpmbuild-v2")
    for sub in ("SPECS", "RPMS", "BUILD", "SOURCES", "SRPMS"):
        (top / sub).mkdir()
    spec = top / "SPECS" / "alpha.spec"
    spec.write_text(SPEC_V2.format(name="alpha"))
    proc = subprocess.run(
        ["rpmbuild", "--define", f"_topdir {top}", "-bb", str(spec)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        pytest.skip(f"rpmbuild failed: {proc.stderr[-400:]}")
    return str(top / "RPMS" / "noarch" / "alpha-2.0-1.noarch.rpm")


@pytest.fixture
def upgrade_transaction(tmp_path, fixture_rpms, alpha_v2) -> dict:
    """Upgrade alpha 1.0 → 2.0, nothing explicitly erased."""
    import json

    chroot = tmp_path / "root"
    chroot.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(UPGRADE_DRIVER)
    repo_root = str(Path(__file__).resolve().parents[2])

    script = f"""
rpm --root '{chroot}' --initdb 2>/dev/null
rpm --root '{chroot}' -i --nodeps '{fixture_rpms["alpha"]}' 2>/dev/null
{sys.executable} '{driver}' '{repo_root}' '{chroot}' '{alpha_v2}'
"""
    proc = subprocess.run(
        ["unshare", "--user", "--map-root-user", "bash", "-c", script],
        capture_output=True, text=True, timeout=300,
    )
    marker = "@@JSON@@"
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(marker)),
        None,
    )
    if line is None:
        pytest.skip(
            f"driver produced no result.\n"
            f"stdout: {proc.stdout[-600:]}\nstderr: {proc.stderr[-600:]}"
        )
    return json.loads(line[len(marker):])


class TestImplicitUpgradeRemovalIsNotCounted:

    def test_the_upgrade_succeeds(self, upgrade_transaction):
        assert upgrade_transaction["success"]

    def test_rpm_really_did_remove_the_old_version(self, upgrade_transaction):
        """Without this the test would pass for the wrong reason: no
        UNINST callback at all means nothing to miscount."""
        phases = {row[0] for row in upgrade_transaction["seen"]}
        assert any("ERASE" in p for p in phases), (
            f"no erase phase observed, phases were {phases}"
        )

    def test_the_bar_total_counts_the_install_only(self, upgrade_transaction):
        """Scoped to the phases that drive the bar.

        PREPARE legitimately reports rpm's own element count — two here,
        the install plus the removal rpm performs itself — and the
        widget treats that phase separately.  INSTALL and ERASE are what
        the package counter renders.
        """
        totals = {
            row[2] for row in upgrade_transaction["seen"]
            if row[2] and ("INSTALL" in row[0] or "ERASE" in row[0])
        }
        assert totals == {1}, (
            f"one package installed, nothing planned for removal; got {totals}"
        )

    def test_the_implicit_erase_does_not_advance_the_counter(
            self, upgrade_transaction):
        """The removal is the other half of an install already counted."""
        erase_rows = [r for r in upgrade_transaction["seen"]
                      if "ERASE" in r[0]]
        assert erase_rows, "no erase phase to check"
        assert all(done == 1 for _p, done, _t, _n in erase_rows), (
            f"counter moved during the implicit removal: {erase_rows}"
        )

    def test_the_counter_never_exceeds_the_total(self, upgrade_transaction):
        for phase, done, total, name in upgrade_transaction["seen"]:
            if total:
                assert done <= total, (
                    f"{phase} reported {done}/{total} for {name!r} -- "
                    f"the bar renders wider than its box past 100%"
                )
