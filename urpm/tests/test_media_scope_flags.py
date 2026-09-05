"""Per-transaction media scoping — ``--enablemedia`` / ``--disablemedia``.

Reaching into a normally-disabled media for one operation today takes
three commands (``media enable``, the operation, ``media disable``),
mutates persistent state in between, and leaves the machine in an
unintended configuration whenever the last one is forgotten.  These
flags do it without writing to the database at all.

Layering under test:

* the CLI resolves what the operator typed into canonical display names
  and fails there, where suggestions can be offered ;
* the Resolver receives canonical names only and knows nothing about
  short names ;
* ``_create_pool`` lifts the enabled gate for those names, and widens
  the accepted-versions set so the media it just admitted is not
  dropped one gate later.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase
from urpm.cli.helpers.resolver import (
    MediaNotFoundError,
    MediaNotSyncedError,
    assert_media_synced,
    create_resolver,
    resolve_media_identifiers,
)


@pytest.fixture
def db(monkeypatch):
    """Media set mixing enabled/disabled and synced/unsynced."""
    monkeypatch.setattr(
        'urpm.core.config.get_system_version', lambda root=None: '10',
    )
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)

    #                name                short_name        enabled  synced
    rows = [
        ("Core Release",           "core_release",           True,  True),
        ("Core Backports",         "core_backports",         False, True),
        ("Core Backports Testing", "core_backports_testing", False, False),
        ("Tainted Release",        "tainted_release",        True,  True),
    ]
    for i, (name, short, enabled, synced) in enumerate(rows):
        media_id = database.add_media(
            name=name, short_name=short, mageia_version="10",
            architecture="x86_64", relative_path=f"media/{short}",
            enabled=enabled, priority=10 * (i + 1),
        )
        if synced:
            database.update_media_sync_info(media_id, synthesis_md5="deadbeef")

    yield database
    database.close()
    db_path.unlink(missing_ok=True)


def _args(**kw) -> argparse.Namespace:
    ns = argparse.Namespace(
        root=None, urpm_root=None, arch=None, allow_arch=None,
        media=None, excludemedia=None, sortmedia=None,
        enablemedia=None, disablemedia=None,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestResolveIdentifiers:

    def test_short_name_becomes_display_name(self, db):
        """The Resolver matches on display names, so that is what the
        CLI must hand it."""
        assert resolve_media_identifiers(db, ["core_backports"]) \
            == ["Core Backports"]

    def test_repeated_flag(self, db):
        """``--enablemedia a --enablemedia b`` (argparse append)."""
        got = resolve_media_identifiers(
            db, ["core_backports", "tainted_release"])
        assert got == ["Core Backports", "Tainted Release"]

    def test_comma_separated(self, db):
        """``--enablemedia a,b`` — same result as repeating the flag."""
        got = resolve_media_identifiers(db, ["core_backports,tainted_release"])
        assert got == ["Core Backports", "Tainted Release"]

    def test_duplicates_collapse(self, db):
        """Naming one media twice, by both its handles, yields it once."""
        got = resolve_media_identifiers(
            db, ["core_backports", "Core Backports"])
        assert got == ["Core Backports"]

    def test_none_yields_empty(self, db):
        assert resolve_media_identifiers(db, None) == []

    def test_unknown_raises_with_suggestions(self, db):
        """Silently ignoring a typo is the trap : the transaction would
        run without the media asked for, and the resulting « package not
        found » would read as a repository problem."""
        with pytest.raises(MediaNotFoundError) as exc:
            resolve_media_identifiers(db, ["core_bakports"])
        assert "core_backports" in str(exc.value)


class TestSyncedGuard:
    """A disabled media is usually one that was never synced."""

    def test_unsynced_media_is_refused(self, db):
        with pytest.raises(MediaNotSyncedError) as exc:
            assert_media_synced(db, ["Core Backports Testing"])
        msg = str(exc.value)
        assert "core_backports_testing" in msg
        assert "urpm media update" in msg, (
            "the message must carry the command that fixes it"
        )

    def test_synced_but_disabled_media_passes(self, db):
        """The whole point of the flag : a media that has metadata but
        is switched off must go through."""
        assert_media_synced(db, ["Core Backports"]) is None

    def test_empty_list_is_a_noop(self, db):
        assert assert_media_synced(db, []) is None


class TestCreateResolverWiring:

    def test_enablemedia_reaches_the_resolver(self, db):
        r = create_resolver(db, _args(enablemedia=["core_backports"]))
        assert r.enablemedia == frozenset({"Core Backports"})

    def test_disablemedia_feeds_excludemedia(self, db):
        """``--disablemedia`` is exactly the existing excludemedia
        semantics, so it reuses that mechanism rather than adding a
        parallel one."""
        r = create_resolver(db, _args(disablemedia=["tainted_release"]))
        assert r.excludemedia == {"Tainted Release"}

    def test_disablemedia_merges_with_existing_excludemedia(self, db):
        """Both may legitimately be in play — the flag must not clobber
        an excludemedia set elsewhere."""
        r = create_resolver(
            db,
            _args(disablemedia=["tainted_release"], excludemedia="Core Release"),
        )
        assert r.excludemedia == {"Tainted Release", "Core Release"}

    def test_no_flags_leaves_defaults(self, db):
        r = create_resolver(db, _args())
        assert r.enablemedia == frozenset()
        assert r.excludemedia is None

    def test_unsynced_media_refused_at_resolver_creation(self, db):
        """The guard fires before any resolution work starts."""
        with pytest.raises(MediaNotSyncedError):
            create_resolver(db, _args(enablemedia=["core_backports_testing"]))

    def test_both_flags_together(self, db):
        r = create_resolver(db, _args(
            enablemedia=["core_backports"],
            disablemedia=["tainted_release"],
        ))
        assert r.enablemedia == frozenset({"Core Backports"})
        assert r.excludemedia == {"Tainted Release"}


# ── Container side (urpm build) ────────────────────────────────────


class _FakeContainer:
    """Records issued commands; ``fail_on`` makes a media call fail."""

    def __init__(self, fail_on=()):
        self.calls: list[list[str]] = []
        self.fail_on = set(fail_on)

    def exec(self, cid, cmd, **kw):
        self.calls.append(list(cmd))
        from unittest.mock import MagicMock
        rv = MagicMock()
        rv.returncode = 1 if cmd[-1] in self.fail_on else 0
        rv.stdout = ""
        rv.stderr = ""
        return rv


def _media_calls(container: _FakeContainer) -> list[list[str]]:
    return [c for c in container.calls if c[:2] == ["urpm", "media"]]


class TestContainerMediaScope:
    """``urpm build`` applies the perimeter through the container's own
    long-standing ``media enable|disable`` subcommands.

    No new inner flag is involved, on purpose : the ``urpm`` inside an
    image is whatever version that image was built with, and handing an
    unknown flag to an older one aborts the whole run.
    """

    def test_enable_issues_the_subcommand(self):
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer()
        apply_media_scope(c, "cid", ["core_backports"], None)
        assert _media_calls(c) == [
            ["urpm", "media", "enable", "core_backports"]]

    def test_disable_issues_the_subcommand(self):
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer()
        apply_media_scope(c, "cid", None, ["tainted_release"])
        assert _media_calls(c) == [
            ["urpm", "media", "disable", "tainted_release"]]

    def test_disable_runs_before_enable(self):
        """Order is deliberate : disabling first means an operator who
        both narrows and widens gets the widening applied last and
        therefore honoured."""
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer()
        apply_media_scope(c, "cid", ["a"], ["b"])
        verbs = [call[2] for call in _media_calls(c)]
        assert verbs == ["disable", "enable"]

    def test_comma_separated_expands(self):
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer()
        apply_media_scope(c, "cid", ["a,b"], None)
        assert [call[-1] for call in _media_calls(c)] == ["a", "b"]

    def test_no_flags_issues_nothing(self):
        """A build without the flags must be byte-identical to before."""
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer()
        apply_media_scope(c, "cid", None, None)
        assert c.calls == []

    def test_failure_aborts_the_build(self):
        """A media that cannot be applied must stop the build.

        Carrying on would hand back a package whose build inputs are
        not the ones the operator asked for, with nothing in the
        artefact to reveal it — worse than no package at all.
        """
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer(fail_on={"nope"})
        with pytest.raises(RuntimeError) as exc:
            apply_media_scope(c, "cid", ["nope"], None)
        assert "nope" in str(exc.value)

    def test_failure_message_points_at_the_naming_pitfall(self):
        """Most images predate short-name resolution in ``media
        enable``, so the likeliest cause of a rejection is a short name
        where the display name was required.  The message has to say
        so, or the operator is left guessing."""
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer(fail_on={"core_backports"})
        with pytest.raises(RuntimeError) as exc:
            apply_media_scope(c, "cid", ["core_backports"], None)
        assert "display name" in str(exc.value)

    def test_enable_is_not_attempted_after_a_disable_failure(self):
        """Fail fast : once the perimeter is known to be wrong there is
        no point mutating the container further."""
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer(fail_on={"bad"})
        with pytest.raises(RuntimeError):
            apply_media_scope(c, "cid", ["good"], ["bad"])
        assert [call[-1] for call in _media_calls(c)] == ["bad"]

    def test_identifiers_are_passed_through_verbatim(self):
        """No host-side resolution : the container owns its own media
        names, and a host short name may mean nothing there."""
        from urpm.cli.helpers.build_chain import apply_media_scope
        c = _FakeContainer()
        apply_media_scope(c, "cid", ["Some Media Name"], None)
        assert _media_calls(c)[0][-1] == "Some Media Name"
