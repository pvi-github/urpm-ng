"""Tests for :mod:`urpm.core.system_profile` — export / import / diff
of a machine's package + media/server profile.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from urpm.core import system_profile as sp


# ---------------------------------------------------------------------------
# _read_name_list_file / _read_deps_set  ── flat-file parsing
# ---------------------------------------------------------------------------


class TestListFileParsing:

    def test_missing_file_returns_empty(self, tmp_path):
        assert sp._read_name_list_file(tmp_path / "no-such") == {}
        assert sp._read_deps_set(tmp_path / "no-such") == set()

    def test_reads_name_source_pairs(self, tmp_path):
        p = tmp_path / "bd.list"
        p.write_text(
            "gcc\turpm-ng.spec\n"
            "make\turpm-ng.spec\n"
            "cmake\tlibsolv.spec\n"
        )
        assert sp._read_name_list_file(p) == {
            "gcc": "urpm-ng.spec",
            "make": "urpm-ng.spec",
            "cmake": "libsolv.spec",
        }

    def test_lowercases_names(self, tmp_path):
        p = tmp_path / "deps.list"
        p.write_text("Firefox\nGCC\n")
        assert sp._read_deps_set(p) == {"firefox", "gcc"}

    def test_ignores_blanks_and_comments(self, tmp_path):
        p = tmp_path / "deps.list"
        p.write_text("# leading comment\n\nfirefox\n\n# trailing\n")
        assert sp._read_deps_set(p) == {"firefox"}


# ---------------------------------------------------------------------------
# save_profile / load_profile  ── roundtrip + schema validation
# ---------------------------------------------------------------------------


class TestProfileIO:

    def _sample(self) -> dict:
        return {
            "schema_version": sp.SCHEMA_VERSION,
            "generated_at": "2026-08-15T10:00:00Z",
            "source": {"hostname": "h", "release": "10", "arch": "x86_64"},
            "servers": [{"name": "srv1", "host": "a.b", "base_path": "/",
                         "is_official": True}],
            "media": [{"name": "Core Release", "short_name": "core_release"}],
            "packages": {"explicit": ["firefox"], "dependency": [],
                         "buildrequires": {}},
        }

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "profile.json"
        sp.save_profile(self._sample(), p)
        loaded = sp.load_profile(p)
        assert loaded == self._sample()

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(sp.ProfileError, match="cannot read"):
            sp.load_profile(tmp_path / "does-not-exist")

    def test_rejects_malformed_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(sp.ProfileError, match="malformed JSON"):
            sp.load_profile(p)

    def test_rejects_non_object_root(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1, 2, 3]")
        with pytest.raises(sp.ProfileError, match="top-level"):
            sp.load_profile(p)

    def test_rejects_missing_schema_version(self, tmp_path):
        p = tmp_path / "no-ver.json"
        p.write_text(json.dumps({"foo": "bar"}))
        with pytest.raises(sp.ProfileError,
                           match="missing.*schema_version"):
            sp.load_profile(p)

    def test_rejects_future_schema_version(self, tmp_path):
        prof = self._sample()
        prof["schema_version"] = sp.SCHEMA_VERSION + 99
        p = tmp_path / "future.json"
        p.write_text(json.dumps(prof))
        with pytest.raises(sp.ProfileError,
                           match="newer than this urpm-ng"):
            sp.load_profile(p)

    def test_rejects_missing_section(self, tmp_path):
        prof = self._sample()
        del prof["media"]
        p = tmp_path / "nomedia.json"
        p.write_text(json.dumps(prof))
        with pytest.raises(sp.ProfileError, match="missing.*'media'"):
            sp.load_profile(p)


# ---------------------------------------------------------------------------
# diff_servers / diff_media  ── replace vs merge semantics
# ---------------------------------------------------------------------------


def _srv(name, host="h", path="/"):
    return {"name": name, "host": host, "base_path": path,
            "protocol": "https", "is_official": True, "enabled": True}


def _med(name):
    return {"name": name, "short_name": name.lower(),
            "mageia_version": "10", "architecture": "x86_64",
            "url": "https://mirror.example/mageia/10/x86_64/media/"
                   f"{name.lower()}"}


class TestDiffServers:

    def test_replace_adds_and_removes(self):
        cur = [_srv("A"), _srv("B", host="b")]
        tgt = [_srv("A"), _srv("C", host="c")]
        d = sp.diff_servers(cur, tgt, replace=True)
        assert [s["name"] for s in d.to_add] == ["C"]
        assert [s["name"] for s in d.to_remove] == ["B"]
        assert [s["name"] for s in d.unchanged] == ["A"]

    def test_merge_keeps_local_extras(self):
        cur = [_srv("A"), _srv("B", host="b")]
        tgt = [_srv("A"), _srv("C", host="c")]
        d = sp.diff_servers(cur, tgt, replace=False)
        assert [s["name"] for s in d.to_add] == ["C"]
        assert d.to_remove == []


class TestDiffMedia:

    def test_replace_adds_and_removes(self):
        cur = [_med("Core"), _med("Nonfree")]
        tgt = [_med("Core"), _med("Tainted")]
        d = sp.diff_media(cur, tgt, replace=True)
        assert [m["name"] for m in d.to_add] == ["Tainted"]
        assert [m["name"] for m in d.to_remove] == ["Nonfree"]

    def test_warns_on_missing_local_media_path(self, tmp_path):
        target_media = [{
            "name": "MyLocal",
            "url": f"file://{tmp_path / 'does-not-exist'}",
        }]
        d = sp.diff_media([], target_media, replace=True)
        assert d.to_add and d.to_add[0]["name"] == "MyLocal"
        assert any("MyLocal" in w and "not present" in w
                   for w in d.warnings)

    def test_does_not_warn_on_existing_local_media_path(self, tmp_path):
        target_media = [{
            "name": "MyLocal",
            "url": f"file://{tmp_path}",
        }]
        d = sp.diff_media([], target_media, replace=True)
        assert d.warnings == []


# ---------------------------------------------------------------------------
# diff_packages  ── install-explicit / install-dep / install-BR / remove
# ---------------------------------------------------------------------------


class TestDiffPackages:

    def test_install_and_remove_split(self):
        current = {
            "explicit": ["a", "b"],
            "dependency": ["libx"],
            "buildrequires": {"gcc": "urpm-ng.spec"},
        }
        target = {
            "explicit": ["b", "c"],       # a → remove, c → install-expl
            "dependency": ["libx", "liby"],   # liby → install-dep
            "buildrequires": {"gcc": "urpm-ng.spec",
                              "make": "libsolv.spec"},  # make → install-BR
        }
        d = sp.diff_packages(current, target)
        assert d.install_explicit == ["c"]
        assert d.install_dependency == ["liby"]
        assert d.install_buildrequires == ["make"]
        assert d.remove_explicit == ["a"]

    def test_removes_only_explicit_no_deps(self):
        """Deps + BR that vanish from target aren't force-removed —
        libsolv autoremove owns that call."""
        current = {
            "explicit": ["a"],
            "dependency": ["orphan-dep"],
            "buildrequires": {"stale-br": "old.spec"},
        }
        target = {"explicit": ["a"], "dependency": [], "buildrequires": {}}
        d = sp.diff_packages(current, target)
        assert d.remove_explicit == []
        assert d.install_explicit == []

    def test_already_installed_in_any_bucket_is_not_reinstalled(self):
        """If ``foo`` is a dep locally but appears as explicit in the
        target, we don't queue a re-install — it's already there."""
        current = {"explicit": [], "dependency": ["foo"],
                   "buildrequires": {}}
        target = {"explicit": ["foo"], "dependency": [], "buildrequires": {}}
        d = sp.diff_packages(current, target)
        assert d.install_explicit == []
        assert d.install_dependency == []
        # No removal either — 'foo' isn't in current.explicit.
        assert d.remove_explicit == []


# ---------------------------------------------------------------------------
# compute_diff  ── end-to-end sanity
# ---------------------------------------------------------------------------


class TestComputeDiff:

    def test_all_sections_wired(self):
        current = {
            "servers": [_srv("Old", host="old.example")],
            "media": [_med("Old")],
            "packages": {"explicit": ["foo"], "dependency": [],
                         "buildrequires": {}},
        }
        target = {
            "servers": [_srv("New", host="new.example")],
            "media": [_med("New")],
            "packages": {"explicit": ["bar"], "dependency": [],
                         "buildrequires": {}},
        }
        d = sp.compute_diff(current, target)
        assert [s["name"] for s in d.servers.to_add] == ["New"]
        assert [s["name"] for s in d.servers.to_remove] == ["Old"]
        assert [m["name"] for m in d.media.to_add] == ["New"]
        assert [m["name"] for m in d.media.to_remove] == ["Old"]
        assert d.packages.install_explicit == ["bar"]
        assert d.packages.remove_explicit == ["foo"]

    def test_merge_mode_keeps_local_extras(self):
        current = {"servers": [_srv("Local")], "media": [_med("Local")],
                   "packages": {"explicit": [], "dependency": [],
                                "buildrequires": {}}}
        target = {"servers": [], "media": [],
                  "packages": {"explicit": [], "dependency": [],
                               "buildrequires": {}}}
        d = sp.compute_diff(current, target,
                            replace_media=False, replace_servers=False)
        assert d.servers.to_remove == []
        assert d.media.to_remove == []
