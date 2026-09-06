"""Tx B erases run at their libsolv position, not all at the end.

``split_plan_for_tx_a_and_b`` works on installs and returns bare
NEVRAs, so the interleaving of removals among them was lost — and the
whole flat ``erase_names`` list was replayed on the last batch.

That kept every outgoing package on disk for the entire length of Tx B.
On a cross-release upgrade that is most of the library set: a SONAME
bump (``lib64foo1`` → ``lib64foo2``) is an ERASE plus an INSTALL, not
an upgrade pair, so ``addInstall(..., 'u')`` never reclaims it.
Measured at ×2 the nominal footprint on a real mga9→mga10 — enough to
fill a partition sized for one release, which is most of them.

The position is not recomputed here: it is libsolv's, and it was
simply being discarded.  rpm then orders installs and erases inside
each batch itself, ``_build_rpm_ts`` handing it both in one
``TransactionSet``.

Encoding is chosen so an older ``.state`` still reads: installs stay
plain strings, erases become ``{"erase": name}``.  A state written
before this change decodes as an all-install plan whose separate
``erase_names`` field still applies — the previous behaviour exactly,
so a machine interrupted mid-migration stays resumable.
"""

from __future__ import annotations

import pytest

from urpm.core.distupgrade.stage3 import (
    _purge_installed_batch_rpms,
    _split_plan_by_size,
    entry_name,
    erase_entry,
    is_erase_entry,
    split_entries,
)


class TestPlanEntries:

    def test_string_is_an_install(self):
        """The legacy shape, and still the common one."""
        assert not is_erase_entry("foo-1.0-1.mga10.x86_64")

    def test_dict_is_an_erase(self):
        assert is_erase_entry(erase_entry("lib64foo1"))

    def test_entry_name_unwraps_both(self):
        assert entry_name("foo-1.0-1.mga10.x86_64") == "foo-1.0-1.mga10.x86_64"
        assert entry_name(erase_entry("lib64foo1")) == "lib64foo1"

    def test_split_preserves_order_within_each_nature(self):
        plan = ["a-1-1.x86_64", erase_entry("old1"),
                "b-1-1.x86_64", erase_entry("old2"), "c-1-1.x86_64"]
        installs, erases = split_entries(plan)
        assert installs == ["a-1-1.x86_64", "b-1-1.x86_64", "c-1-1.x86_64"]
        assert erases == ["old1", "old2"]

    def test_split_of_an_all_install_plan(self):
        """What an older ``.state`` decodes to."""
        installs, erases = split_entries(["a-1-1.x86_64", "b-1-1.x86_64"])
        assert installs == ["a-1-1.x86_64", "b-1-1.x86_64"]
        assert erases == []

    def test_split_of_an_empty_plan(self):
        assert split_entries([]) == ([], [])


class TestBatchingWeighsErasesZero:
    """The size budget caps the payload peak.  An erase has no payload
    and frees space, so making it push a batch boundary would be
    backwards."""

    def _paths(self, tmp_path, names, size):
        out = {}
        for n in names:
            f = tmp_path / f"{n}.rpm"
            f.write_bytes(b"\0" * size)
            out[n] = str(f)
        return out

    def test_erase_does_not_consume_budget(self, tmp_path):
        """Three 100-byte installs with a 300-byte cap fit in one batch
        even with erases scattered among them."""
        paths = self._paths(tmp_path, ["a", "b", "c"], 100)
        plan = ["a", erase_entry("x"), "b", erase_entry("y"), "c"]
        batches = _split_plan_by_size(plan, paths, max_batch_bytes=300)
        assert len(batches) == 1

    def test_erase_travels_in_its_own_slice(self, tmp_path):
        """Position is the whole point : an erase must land in the batch
        its index puts it in, not be hoisted elsewhere."""
        paths = self._paths(tmp_path, ["a", "b"], 100)
        plan = ["a", erase_entry("x"), "b"]
        batches = _split_plan_by_size(plan, paths, max_batch_bytes=100)
        assert [len(b) for b in batches] == [2, 1]
        assert is_erase_entry(batches[0][1])
        assert batches[1] == ["b"]

    def test_erase_only_plan_is_one_batch(self, tmp_path):
        batches = _split_plan_by_size(
            [erase_entry("x"), erase_entry("y")], {}, max_batch_bytes=100)
        assert batches == [[erase_entry("x"), erase_entry("y")]]

    def test_install_boundaries_are_unchanged(self, tmp_path):
        """Without erases the batching must behave exactly as before."""
        paths = self._paths(tmp_path, ["a", "b", "c"], 100)
        batches = _split_plan_by_size(
            ["a", "b", "c"], paths, max_batch_bytes=200)
        assert [len(b) for b in batches] == [2, 1]


class TestPurgeSkipsErases:
    """The per-batch cache purge unlinks payloads.  An erase has none."""

    def test_erase_entries_are_ignored(self, tmp_path, monkeypatch):
        from urpm.core.distupgrade import stage3
        kept = tmp_path / "a.rpm"
        kept.write_bytes(b"\0" * 10)
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical", lambda: set())
        freed_files, freed_bytes = _purge_installed_batch_rpms(
            [erase_entry("lib64foo1")], {"lib64foo1": str(kept)})
        assert (freed_files, freed_bytes) == (0, 0)
        assert kept.exists(), "an erase entry must not unlink anything"


class TestRetrySkipsErases:
    """``_retry_missing_installs`` compares the plan to the rpmdb to
    find silently-failed installs.  A planned removal is *supposed* to
    be absent — counting it as missing would put it back.

    ``_installed_nevras_canonical`` must return something non-empty
    here: the function bails out early on an empty rpmdb probe, and a
    first version of these tests stubbed it to ``set()`` and therefore
    never reached the loop it meant to exercise.
    """

    def test_erase_is_not_treated_as_a_missing_install(
            self, tmp_path, monkeypatch):
        from urpm.core.distupgrade import stage3

        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda: {"unrelated-1.0-1.mga10.x86_64"})
        called = []
        monkeypatch.setattr(
            stage3, "_run_one_side",
            lambda *a, **kw: called.append(kw.get("plan")))

        result = stage3._retry_missing_installs(
            None,
            planned_nevras=[erase_entry("lib64foo1")],
            rpm_paths_by_nevra={"lib64foo1": str(tmp_path / "nope.rpm")},
            version_from="9", version_to="10",
        )
        assert result["missing_before_retry"] == []
        assert called == [], "no retry transaction should have been built"

    def test_installs_are_still_examined(self, tmp_path, monkeypatch):
        """The guard must skip erases only — a genuinely missing install
        still has to be caught, or the silent-failure retry stops
        working.

        Needs a real database: spotting a missing install makes the
        function open a retry transaction, which is exactly the
        behaviour under test.
        """
        from urpm.core.database import PackageDatabase
        from urpm.core.distupgrade import stage3

        db = PackageDatabase(tmp_path / "state.db")
        try:
            monkeypatch.setattr(
                stage3, "_installed_nevras_canonical",
                lambda: {"unrelated-1.0-1.mga10.x86_64"})
            monkeypatch.setattr(
                stage3, "_run_one_side", lambda *a, **kw: None)

            rpm_file = tmp_path / "gone.rpm"
            rpm_file.write_bytes(b"\0")
            result = stage3._retry_missing_installs(
                db,
                planned_nevras=["gone-1.0-1.mga10.x86_64",
                                erase_entry("lib64foo1")],
                rpm_paths_by_nevra={
                    "gone-1.0-1.mga10.x86_64": str(rpm_file)},
                version_from="9", version_to="10",
            )
        finally:
            db.close()
        # The install is examined and reported missing ; the erase is
        # not — without the guard the loop would raise a TypeError on
        # the dict long before reaching this assertion.
        assert result["missing_before_retry"] == ["gone-1.0-1.mga10.x86_64"]


class TestLegacyStateStillWorks:
    """A ``.state`` written before this change holds plain strings and a
    separate ``erase_names``.  It must keep meaning what it meant."""

    def test_all_string_plan_yields_no_erases(self):
        installs, erases = split_entries(
            ["a-1-1.x86_64", "b-1-1.x86_64", "c-1-1.x86_64"])
        assert erases == []
        assert len(installs) == 3

    def test_legacy_plan_batches_as_before(self, tmp_path):
        paths = {}
        for n in ("a", "b"):
            f = tmp_path / f"{n}.rpm"
            f.write_bytes(b"\0" * 100)
            paths[n] = str(f)
        assert _split_plan_by_size(["a", "b"], paths, 100) == [["a"], ["b"]]


class TestResumeDropsAlreadyApplied:
    """``--resume`` at ``tx_b_running`` replays the persisted plan.

    ``_purge_installed_batch_rpms`` unlinks the payload of everything a
    batch committed, so replaying the whole plan means opening files
    that are gone — any Tx B past its first batch was structurally
    unresumable.  A tester hit exactly this: the resume failed, the
    message was swallowed, and they reached for ``--abort``.
    """

    def test_installed_entries_are_dropped(self, monkeypatch):
        from urpm.core.distupgrade import stage3
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda: {stage3._canonical_nevra("done-1.0-1.mga10.x86_64")})
        plan = ["done-1.0-1.mga10.x86_64", "todo-1.0-1.mga10.x86_64"]
        assert stage3._drop_already_applied(plan) == [
            "todo-1.0-1.mga10.x86_64"]

    def test_order_of_survivors_is_preserved(self, monkeypatch):
        """Batch slicing relies on libsolv's order — filtering must not
        reshuffle what it keeps."""
        from urpm.core.distupgrade import stage3
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda: {stage3._canonical_nevra("b-1.0-1.mga10.x86_64")})
        plan = ["a-1.0-1.mga10.x86_64", "b-1.0-1.mga10.x86_64",
                "c-1.0-1.mga10.x86_64"]
        assert stage3._drop_already_applied(plan) == [
            "a-1.0-1.mga10.x86_64", "c-1.0-1.mga10.x86_64"]

    def test_erases_are_kept(self, monkeypatch):
        """A bare name gives no NEVRA to compare, and erasing an absent
        package is a no-op for rpm — keeping it is the safe side."""
        from urpm.core.distupgrade import stage3
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda: {"whatever-1.0-1.mga10.x86_64"})
        plan = [erase_entry("lib64foo1")]
        assert stage3._drop_already_applied(plan) == plan

    def test_first_pass_drops_nothing(self, monkeypatch):
        """Nothing of Tx B is installed yet, so a fresh run is
        untouched — the filter must not cost the normal path anything."""
        from urpm.core.distupgrade import stage3
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda: {"unrelated-1.0-1.mga10.x86_64"})
        plan = ["a-1.0-1.mga10.x86_64", erase_entry("old"),
                "b-1.0-1.mga10.x86_64"]
        assert stage3._drop_already_applied(plan) == plan

    def test_empty_rpmdb_probe_keeps_everything(self, monkeypatch):
        """No rpm bindings, unreadable root : replaying an applied entry
        is wasteful, dropping a pending one would be a silent hole."""
        from urpm.core.distupgrade import stage3
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical", lambda: set())
        plan = ["a-1.0-1.mga10.x86_64", "b-1.0-1.mga10.x86_64"]
        assert stage3._drop_already_applied(plan) == plan

    def test_fully_applied_plan_becomes_empty(self, monkeypatch):
        """The state a resume lands in when the previous run actually
        finished Tx B but died before Stage 4."""
        from urpm.core.distupgrade import stage3
        monkeypatch.setattr(
            stage3, "_installed_nevras_canonical",
            lambda: {stage3._canonical_nevra("a-1.0-1.mga10.x86_64"),
                     stage3._canonical_nevra("b-1.0-1.mga10.x86_64")})
        assert stage3._drop_already_applied(
            ["a-1.0-1.mga10.x86_64", "b-1.0-1.mga10.x86_64"]) == []
