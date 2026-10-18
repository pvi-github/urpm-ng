"""Yielding the machine, and saying honestly what was obtained.

A tester's desktop froze for minutes during a 2509-package
transaction: no swap used, no OOM, 1.5 GB still free, so neither
memory nor space.  The device was saturated and the session starved
behind it, while ``ionice -c3`` was already being applied — because an
I/O priority covers the reads and synchronous writes a process issues,
never the writeback the kernel flushes later on its behalf.  Only
per-cgroup accounting follows those pages, and only when the
``memory`` controller is enabled too, since writeback is charged
through the page's memory cgroup.

These tests run as an ordinary user, against a fake cgroup tree, so
they exercise the real decisions rather than whatever the developer's
machine happens to delegate.  The module reads :data:`CGROUP_ROOT` at
call time for exactly that reason.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from urpm.core import txresources
from urpm.core.txresources import (
    BY_CGROUP,
    BY_NICE,
    BY_NOTHING,
    CGROUP_NAME,
    IO_WEIGHT,
    lower_priority,
    yielding_the_machine,
)


@pytest.fixture
def fake_cgroup(tmp_path, monkeypatch):
    """A cgroup v2 root that delegates what the module needs."""
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("cpu io memory\n")
    monkeypatch.setattr(txresources, "CGROUP_ROOT", root)
    return root


@pytest.fixture
def no_cgroup(tmp_path, monkeypatch):
    """A root with no cgroup v2 at all."""
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(txresources, "CGROUP_ROOT", root)
    return root


@pytest.fixture
def quiet_fallback(monkeypatch):
    """Keep the fallback from touching this process's own priority."""
    monkeypatch.setattr(txresources.shutil, "which", lambda _name: None)
    monkeypatch.setattr(txresources.os, "nice", lambda _inc: 0)


def created_group(root: Path) -> Path:
    return root / CGROUP_NAME


# ── The cgroup path ─────────────────────────────────────────────────────


def test_the_weights_are_set_and_the_process_moved(fake_cgroup):
    assert lower_priority() == BY_CGROUP

    group = created_group(fake_cgroup)
    assert (group / "io.weight").read_text() == str(IO_WEIGHT)
    assert (group / "cpu.weight").read_text() == str(txresources.CPU_WEIGHT)
    assert (group / "cgroup.procs").read_text() == str(os.getpid())


def test_the_weight_is_set_before_the_process_joins(fake_cgroup):
    """Otherwise the first pages written are charged at weight 100.

    Order is observable: the weight file has to exist and hold its
    value by the time ``cgroup.procs`` is written.
    """
    seen = {}
    real_write = Path.write_text

    def spy(self, data, *args, **kwargs):
        if self.name == "cgroup.procs":
            weight_file = self.parent / "io.weight"
            seen["weight_at_join"] = (
                weight_file.read_text() if weight_file.exists() else None)
        return real_write(self, data, *args, **kwargs)

    Path.write_text = spy
    try:
        lower_priority()
    finally:
        Path.write_text = real_write

    assert seen["weight_at_join"] == str(IO_WEIGHT)


def test_a_second_operation_shares_the_cgroup(fake_cgroup):
    """Two bulk operations are both bulk I/O; one group is right."""
    assert lower_priority() == BY_CGROUP
    assert lower_priority() == BY_CGROUP
    assert created_group(fake_cgroup).is_dir()


# ── Where the memory controller is missing ──────────────────────────────


def test_io_without_memory_is_refused(fake_cgroup, quiet_fallback):
    """The case that would look like a fix and not be one.

    Writeback is charged through the page's memory cgroup, so an io
    weight without the memory controller governs reads and synchronous
    writes only — a more complicated ``ionice``, and the very thing
    that failed in the field.
    """
    (fake_cgroup / "cgroup.subtree_control").write_text("cpu io\n")

    assert lower_priority() == BY_NICE
    assert not created_group(fake_cgroup).exists()


def test_a_parent_that_delegates_nothing_falls_back(fake_cgroup,
                                                    quiet_fallback):
    (fake_cgroup / "cgroup.subtree_control").write_text("\n")

    assert lower_priority() == BY_NICE


def test_no_cgroup_v2_falls_back(no_cgroup, quiet_fallback):
    assert lower_priority() == BY_NICE


def test_an_unwritable_root_falls_back(fake_cgroup, quiet_fallback):
    """The ordinary case of not being root."""
    fake_cgroup.chmod(0o500)
    try:
        assert lower_priority() == BY_NICE
    finally:
        fake_cgroup.chmod(0o700)


def test_nothing_obtained_is_reported_as_such(no_cgroup, monkeypatch):
    """Silence would read as success."""
    monkeypatch.setattr(txresources.shutil, "which", lambda _name: None)

    def refuse(_inc):
        raise OSError("restricted kernel")

    monkeypatch.setattr(txresources.os, "nice", refuse)
    assert lower_priority() == BY_NOTHING


# ── Scoped to a phase ───────────────────────────────────────────────────


def test_the_phase_puts_the_process_back(fake_cgroup, monkeypatch):
    """The download runs in a process that outlives it.

    Staying in the cgroup afterwards would quietly deprioritise
    everything the command does next.
    """
    monkeypatch.setattr(txresources, "_own_cgroup_path", lambda: "/origin")
    (fake_cgroup / "origin").mkdir()

    with yielding_the_machine() as obtained:
        assert obtained == BY_CGROUP
        assert (created_group(fake_cgroup) / "cgroup.procs").read_text() \
            == str(os.getpid())

    assert (fake_cgroup / "origin" / "cgroup.procs").read_text() \
        == str(os.getpid())
    # Removal is not asserted here: on cgroupfs ``rmdir`` succeeds on
    # an empty cgroup despite its control files, while on the ordinary
    # filesystem this fake is built from it cannot.  What the contract
    # actually promises is below — the attempt is made and its failure
    # is never raised at the caller.


def test_the_process_is_put_back_even_on_failure(fake_cgroup, monkeypatch):
    monkeypatch.setattr(txresources, "_own_cgroup_path", lambda: "/origin")
    (fake_cgroup / "origin").mkdir()

    with pytest.raises(RuntimeError):
        with yielding_the_machine():
            raise RuntimeError("the download blew up")

    assert (fake_cgroup / "origin" / "cgroup.procs").read_text() \
        == str(os.getpid())


def test_a_phase_without_a_cgroup_does_not_touch_the_process(no_cgroup,
                                                             monkeypatch):
    """No ``nice`` fallback for a phase, on purpose.

    ``nice`` and ``ionice`` are per-process and would outlive the
    phase, which is the thing this context manager exists to avoid.
    """
    called = []
    monkeypatch.setattr(txresources.os, "nice",
                        lambda inc: called.append(inc))

    with yielding_the_machine() as obtained:
        assert obtained == BY_NOTHING

    assert called == []


def test_a_cgroup_that_will_not_go_away_is_tolerated(fake_cgroup,
                                                     monkeypatch):
    """What ``rmdir`` reports when another operation is still inside.

    On a real cgroup that is ``EBUSY``, and the arbitration is exactly
    what we want: whoever leaves last removes it.  Here the refusal is
    arranged, because what has to hold is that it does not surface as
    an exception in the middle of a download.
    """
    monkeypatch.setattr(txresources, "_own_cgroup_path", lambda: "/origin")
    (fake_cgroup / "origin").mkdir()

    attempted = []

    def busy(group):
        attempted.append(group)
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(Path, "rmdir", busy)

    with yielding_the_machine():
        pass

    assert attempted, "the cgroup was never offered for removal"
    assert created_group(fake_cgroup).is_dir()


# ── The transaction child goes through the same door ────────────────────


def test_the_transaction_child_uses_this_module(fake_cgroup):
    """It used to carry its own ionice call.

    Routing both the commit and the download through one function is
    what keeps the two from drifting apart.
    """
    from urpm.core.transaction_queue import _apply_transaction_niceness

    _apply_transaction_niceness()

    assert (created_group(fake_cgroup) / "cgroup.procs").read_text() \
        == str(os.getpid())
