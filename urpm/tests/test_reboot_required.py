"""A distupgrade that updates the machine must stop and ask for a reboot.

A distupgrade starts by bringing the machine up to date (Phase A).  On a
system left un-updated for months, that pulls glibc or the init — and
the machine then runs an in-memory copy that no longer matches what is
on disk.  Scriptlets link against the new libc while pid 1, dbus and
sshd still hold the old one.

The guard for that condition existed but ran only at the door, catching
the operator who did an ``urpm update`` and skipped the reboot.  The
pipeline then created the very same condition itself and carried on.

The remedy differs by moment, and that is the whole design:

* **At the door** — the stale copy is something the operator brought.
  Refuse (``PendingRebootError``), nothing has been done yet.
* **After Phase A** — it is our own doing.  Stop cleanly, persist the
  state, and ask for a reboot (``RebootRequiredError``).  This is the
  only point where rebooting is free: no media swapped, no package of
  the target release installed.
* **From Tx A on** — no check at all.  Tx A installs glibc by design,
  so the guard would fire on every distupgrade, and a reboot prompt
  mid-migration would leave exactly the half-state the ``--abort``
  guard exists to prevent.

The reboot is never performed for the operator, under any flag.
"""

from __future__ import annotations

import rpm

from urpm.core.distupgrade.checks import (
    _REBOOT_CRITICAL_CAPABILITIES,
    PendingRebootError,
    RebootRequiredError,
    check_pending_reboot,
)


class TestCapabilitiesNotNames:
    """``systemd`` as a Name is blind on sysvinit and openrc — the guard
    would sail past the case it exists for, on the very systems the
    zero-systemd rule says we must support."""

    def test_init_is_looked_up_by_path(self):
        assert "/sbin/init" in _REBOOT_CRITICAL_CAPABILITIES

    def test_libc_is_looked_up_by_soname(self):
        """``libc.so.6`` unqualified resolves on 32- and 64-bit alike;
        ``libc.so.6()(64bit)`` would miss i686."""
        assert "libc.so.6" in _REBOOT_CRITICAL_CAPABILITIES

    def test_no_bare_package_name_remains(self):
        assert not [c for c in _REBOOT_CRITICAL_CAPABILITIES
                    if c in ("glibc", "systemd")]

    def test_every_capability_resolves_on_this_machine(self):
        """A guard that finds nothing also passes.  Measured, not
        assumed: file provides live under BASENAMES, so querying
        PROVIDENAME for ``/sbin/init`` silently returns nothing."""
        from urpm.core.distupgrade.checks import _providers_of
        from urpm.core.rpmdb import open_ts
        with open_ts("/") as ts:
            for capability in _REBOOT_CRITICAL_CAPABILITIES:
                found = [h[rpm.RPMTAG_NAME] for h in _providers_of(ts, capability)]
                assert found, f"{capability} resolves to no package"


class TestTheGuardItself:
    """Driven through its test seams: a boot epoch and a name→epoch map."""

    def test_passes_when_nothing_moved_since_boot(self):
        check_pending_reboot(
            boot_time=2000,
            installed_times={"glibc": 1000, "systemd": 1500},
        )

    def test_fires_when_a_package_is_newer_than_boot(self):
        try:
            check_pending_reboot(
                boot_time=1000,
                installed_times={"glibc": 1600},
            )
        except PendingRebootError as exc:
            assert "glibc" in str(exc)
            assert "10" in str(exc), "should report the delay in minutes"
        else:
            raise AssertionError("guard did not fire")

    def test_reports_the_package_not_the_capability(self):
        """An operator reads « systemd installed 12 min after the last
        boot », not « /sbin/init »."""
        try:
            check_pending_reboot(
                boot_time=0, installed_times={"systemd": 720})
        except PendingRebootError as exc:
            assert "systemd" in str(exc)
            assert "/sbin/init" not in str(exc)
        else:
            raise AssertionError("guard did not fire")

    def test_equal_to_boot_time_is_not_stale(self):
        """Installed *at* boot is the package the running system loaded."""
        check_pending_reboot(boot_time=1000, installed_times={"glibc": 1000})

    def test_unknown_boot_time_does_not_block(self):
        """No ``btime`` in /proc/stat — a container, an exotic kernel.
        Refusing there would block distupgrade on machines where the
        question cannot even be asked."""
        check_pending_reboot(boot_time=None, installed_times={"glibc": 99})


class TestTwoErrorsTwoMoments:
    """The distinction is the point of the change; collapsing the two
    would either refuse a legitimate distupgrade or carry on into the
    dangerous state."""

    def test_they_are_distinct_types(self):
        assert RebootRequiredError is not PendingRebootError

    def test_neither_inherits_the_other(self):
        """An ``except PendingRebootError`` at the door must not swallow
        the post-Phase-A stop, and the reverse likewise."""
        assert not issubclass(RebootRequiredError, PendingRebootError)
        assert not issubclass(PendingRebootError, RebootRequiredError)


class TestNoAutomaticReboot:
    """Never rebooted for the operator, under any flag."""

    def test_nothing_in_the_pipeline_reboots(self):
        """Walked with ``ast``, not grepped: a reboot smuggled through a
        variable or an f-string would slip past a text pattern, and this
        test exists precisely to catch what nobody wrote on purpose."""
        import ast
        import pathlib

        offenders = []
        for path in pathlib.Path("urpm").rglob("*.py"):
            if "tests" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not our file to fix
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if not isinstance(node.value, str):
                    continue
                text = node.value.strip().lower()
                if text in ("reboot", "/sbin/reboot", "/usr/sbin/reboot"):
                    offenders.append(f"{path}:{node.lineno}")
                elif text.startswith(("shutdown -r", "systemctl reboot")):
                    offenders.append(f"{path}:{node.lineno}")

        assert not offenders, (
            f"a reboot command appears in: {offenders}.  The operator "
            f"reboots; we only ever ask."
        )
