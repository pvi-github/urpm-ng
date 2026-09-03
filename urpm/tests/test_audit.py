"""Tests for :mod:`urpm.core.audit` — registry, selection, links check.

The registry is what lets one mechanism serve three callers (the
``urpm audit`` verb, ``--check`` on install/upgrade/erase, and
distupgrade Stage 4) without any of them knowing what a check does.
These tests pin that contract, plus the symlink walker itself.

Regression captured : a mga9→mga10 distupgrade left
``/usr/lib64/libproxy.so.1`` pointing at a mga9-era name that mga10's
``lib64proxy1`` no longer ships.  firefox died at startup and the only
diagnostic was running ``ldd`` by hand.

The scanner walks an isolated ``tmp_path`` tree throughout, so pytest
never touches the real ``/usr/lib``, and owner attribution is stubbed
so no test needs the rpmdb.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import urpm.core.audit as audit
from urpm.core.audit import (
    ALL,
    BrokenLinksCheck,
    CheckOutcome,
    Finding,
    available_names,
    find_broken_symlinks,
    parse_selection,
    register,
    run_checks,
)


@pytest.fixture
def clean_registry(monkeypatch):
    """Swap in an empty registry so tests don't disturb the real one."""
    monkeypatch.setattr(audit, "_REGISTRY", {})
    return audit


class _StubCheck:
    """Minimal Check implementation for registry tests."""

    def __init__(self, name, findings=()):
        self.name = name
        self.summary = f"stub {name}"
        self._findings = list(findings)

    def run(self):
        return CheckOutcome(name=self.name, findings=list(self._findings))


# ── Registry ───────────────────────────────────────────────────────


class TestRegistry:

    def test_register_then_listed(self, clean_registry):
        register(_StubCheck("alpha"))
        assert available_names() == ["alpha"]

    def test_names_are_sorted(self, clean_registry):
        for name in ("zulu", "alpha", "mike"):
            register(_StubCheck(name))
        assert available_names() == ["alpha", "mike", "zulu"]

    def test_duplicate_name_rejected(self, clean_registry):
        """Two checks under one name would make a CLI selection
        ambiguous — fail at import time, not at run time."""
        register(_StubCheck("alpha"))
        with pytest.raises(ValueError, match="already registered"):
            register(_StubCheck("alpha"))

    def test_reserved_all_name_rejected(self, clean_registry):
        """``all`` is the selector — a check claiming it would shadow
        the meaning of ``--check all``."""
        with pytest.raises(ValueError, match="reserved"):
            register(_StubCheck(ALL))


# ── Selection parsing ──────────────────────────────────────────────


class TestParseSelection:

    def test_single_name(self, clean_registry):
        register(_StubCheck("links"))
        assert parse_selection("links") == ["links"]

    def test_comma_separated(self, clean_registry):
        register(_StubCheck("links"))
        register(_StubCheck("orphans"))
        assert parse_selection("links,orphans") == ["links", "orphans"]

    def test_all_selector_expands(self, clean_registry):
        register(_StubCheck("links"))
        register(_StubCheck("orphans"))
        assert parse_selection(ALL) == ["links", "orphans"]

    def test_all_wins_inside_a_list(self, clean_registry):
        """``--check links,all`` means everything, not just links."""
        register(_StubCheck("links"))
        register(_StubCheck("orphans"))
        assert parse_selection("links,all") == ["links", "orphans"]

    def test_whitespace_and_empty_segments_tolerated(self, clean_registry):
        register(_StubCheck("links"))
        register(_StubCheck("orphans"))
        assert parse_selection(" links , , orphans ") == ["links", "orphans"]

    def test_result_is_in_registry_order_not_user_order(self, clean_registry):
        """Report layout must not depend on how the operator typed the
        list."""
        register(_StubCheck("alpha"))
        register(_StubCheck("zulu"))
        assert parse_selection("zulu,alpha") == ["alpha", "zulu"]

    def test_unknown_name_raises_and_lists_valid_ones(self, clean_registry):
        """A typo should tell the operator what they meant."""
        register(_StubCheck("links"))
        with pytest.raises(ValueError) as exc:
            parse_selection("linsk")
        msg = str(exc.value)
        assert "linsk" in msg
        assert "links" in msg
        assert ALL in msg


# ── Running ────────────────────────────────────────────────────────


class TestRunChecks:

    def test_runs_only_what_was_asked(self, clean_registry):
        register(_StubCheck("alpha", [Finding("a", "bad")]))
        register(_StubCheck("bravo", [Finding("b", "bad")]))
        outcomes = run_checks(["alpha"])
        assert [o.name for o in outcomes] == ["alpha"]

    def test_clean_check_still_yields_an_outcome(self, clean_registry):
        """A check that ran and found nothing is distinct from a check
        that was never selected — the report says « clean » for the
        former and stays silent for the latter."""
        register(_StubCheck("alpha"))
        outcomes = run_checks(["alpha"])
        assert len(outcomes) == 1
        assert outcomes[0].clean

    def test_unknown_names_skipped_not_raised(self, clean_registry):
        """Internal callers pass names straight from the registry ;
        run_checks is not the validation layer."""
        register(_StubCheck("alpha"))
        assert run_checks(["alpha", "nope"]) != []

    def test_outcomes_follow_registry_order(self, clean_registry):
        register(_StubCheck("alpha"))
        register(_StubCheck("zulu"))
        outcomes = run_checks(["zulu", "alpha"])
        assert [o.name for o in outcomes] == ["alpha", "zulu"]


# ── The links check ────────────────────────────────────────────────


@pytest.fixture
def dangling_tree(tmp_path: Path):
    """A small tree mixing sound and broken symlinks."""
    (tmp_path / "real.so").write_text("")
    (tmp_path / "good.so").symlink_to("real.so")                  # sound
    (tmp_path / "libproxy.so.1").symlink_to("libproxy.so.1.0.0")  # broken
    (tmp_path / "hop-a").symlink_to("hop-b")                      # broken chain
    (tmp_path / "hop-b").symlink_to("nowhere")
    (tmp_path / "regular").write_text("")                         # not a link
    return tmp_path


class TestFindBrokenSymlinks:

    def test_flags_the_dangling_symlink(self, dangling_tree):
        """The classic case : target missing on disk."""
        names = {p.name for p, _t in find_broken_symlinks([dangling_tree])}
        assert "libproxy.so.1" in names
        assert "good.so" not in names
        assert "real.so" not in names
        assert "regular" not in names

    def test_chained_symlink_flagged(self, dangling_tree):
        """A → B → nowhere : A is broken even though B exists as a
        symlink.  ``os.path.exists`` follows the whole chain."""
        names = {p.name for p, _t in find_broken_symlinks([dangling_tree])}
        assert "hop-a" in names
        assert "hop-b" in names

    def test_target_is_readlink_verbatim(self, dangling_tree):
        """``target`` carries what readlink returned, not a resolved
        path : the report shows what the RPM actually declared."""
        by_name = {p.name: t for p, t in find_broken_symlinks([dangling_tree])}
        assert by_name["libproxy.so.1"] == "libproxy.so.1.0.0"

    def test_missing_root_skipped_silently(self, tmp_path):
        """Not every install has /usr/libexec — an absent root is not
        an error."""
        assert find_broken_symlinks([tmp_path / "nowhere"]) == []


class TestBrokenLinksCheck:

    def test_produces_findings_with_remedy(self, dangling_tree, monkeypatch):
        """Each owned finding carries the reinstall command that would
        restore the symlink."""
        monkeypatch.setattr(
            audit, "attribute_owners",
            lambda paths: {p: "lib64proxy1" for p in paths},
        )
        outcome = BrokenLinksCheck(roots=[dangling_tree]).run()
        assert outcome.name == "links"
        assert not outcome.clean
        assert all(f.owner == "lib64proxy1" for f in outcome.findings)
        assert all(
            f.remedy == "urpm i --reinstall lib64proxy1"
            for f in outcome.findings
        )

    def test_orphan_finding_has_no_remedy(self, dangling_tree, monkeypatch):
        """Nothing owns it, so no reinstall can help — the finding is
        still reported, just without a suggestion."""
        monkeypatch.setattr(audit, "attribute_owners", lambda paths: {})
        outcome = BrokenLinksCheck(roots=[dangling_tree]).run()
        assert outcome.findings
        assert all(f.owner is None for f in outcome.findings)
        assert all(f.remedy is None for f in outcome.findings)

    def test_clean_tree_yields_clean_outcome(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audit, "attribute_owners", lambda paths: {})
        (tmp_path / "real.so").write_text("")
        (tmp_path / "good.so").symlink_to("real.so")
        outcome = BrokenLinksCheck(roots=[tmp_path]).run()
        assert outcome.clean

    def test_registered_by_default(self):
        """The real registry ships the links check — this is what makes
        ``urpm audit`` and ``--check links`` work out of the box."""
        assert "links" in available_names()
