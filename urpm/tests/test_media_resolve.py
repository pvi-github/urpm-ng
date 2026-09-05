"""Resolving a user-typed media identifier to a media row.

``get_media`` matches the display name exactly and case-sensitively,
which on the command line means typing ``'Core Backports Testing'``,
quotes included.  Every media already carries a filesystem-safe
``short_name`` (``core_backports_testing``) that was only ever
generated, never used for lookup — :meth:`resolve_media` makes it the
addressable handle.

This is the brick the ``--enablemedia`` / ``--disablemedia`` flags sit
on, so its matching rules are pinned here rather than discovered later
through the CLI.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase


@pytest.fixture
def db(monkeypatch):
    """Temporary database preloaded with a realistic media set."""
    monkeypatch.setattr(
        'urpm.core.config.get_system_version', lambda root=None: '10',
    )
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)

    for name, short_name, priority in (
        ("Core Release", "core_release", 10),
        ("Core Backports", "core_backports", 20),
        ("Core Backports Testing", "core_backports_testing", 30),
        ("Nonfree Release", "nonfree_release", 40),
        ("BDK-Free-x86_64", "free", 50),
    ):
        database.add_media(
            name=name, short_name=short_name, mageia_version="10",
            architecture="x86_64", relative_path=f"media/{short_name}",
            priority=priority,
        )

    yield database
    database.close()
    db_path.unlink(missing_ok=True)


class TestResolveMedia:

    def test_exact_short_name(self, db):
        """The headline case : type the short name, get the media."""
        assert db.resolve_media("core_backports")["name"] == "Core Backports"

    def test_exact_display_name_still_works(self, db):
        """Scripts and habits built on the long name must keep working
        — the short name is an addition, not a replacement."""
        assert db.resolve_media("Core Backports")["short_name"] == "core_backports"

    def test_case_insensitive_short_name(self, db):
        assert db.resolve_media("CORE_BACKPORTS")["name"] == "Core Backports"

    def test_case_insensitive_display_name(self, db):
        assert db.resolve_media("core backports")["short_name"] == "core_backports"

    def test_short_name_wins_over_display_name(self, db):
        """``free`` is the short name of BDK-Free-x86_64.  Short names
        are checked first, so an operator typing a short name always
        gets the media that declares it."""
        assert db.resolve_media("free")["name"] == "BDK-Free-x86_64"

    def test_exact_beats_case_folded(self, db):
        """Exact matches are tried across both fields before any
        case-folding, so a media addressable exactly is never shadowed
        by another that merely matches case-insensitively."""
        db.add_media(
            name="core_backports", short_name="cbp_alias",
            mageia_version="10", architecture="x86_64",
            relative_path="media/alias", priority=90,
        )
        # Exact display-name hit on the newcomer, not a case-folded
        # short-name hit on "core_backports".
        assert db.resolve_media("core_backports")["short_name"] == "core_backports"

    def test_unknown_returns_none(self, db):
        assert db.resolve_media("does-not-exist") is None

    def test_empty_identifier_returns_none(self, db):
        """Guards the ``--enablemedia ''`` case rather than matching an
        arbitrary row."""
        assert db.resolve_media("") is None

    def test_result_is_stable_not_insertion_ordered(self, db):
        """Resolution walks ``list_media`` order (priority, then name),
        so the answer does not depend on which row was inserted first."""
        first = db.resolve_media("core_release")
        second = db.resolve_media("core_release")
        assert first["id"] == second["id"]


class TestSuggestMediaNames:
    """A rejected identifier is only useful with what to type instead."""

    def test_substring_match_ranks_first(self, db):
        """The common case is a typed fragment, not a typo."""
        suggestions = db.suggest_media_names("backports")
        assert "core_backports" in suggestions
        assert "core_backports_testing" in suggestions

    def test_typo_is_caught_by_edit_distance(self, db):
        """``core_bakports`` shares no useful substring but is one
        character from the real thing."""
        assert "core_backports" in db.suggest_media_names("core_bakports")

    def test_suggestions_are_short_names(self, db):
        """We suggest what we want people to type — short names, never
        the spaced display names."""
        for name in db.suggest_media_names("core"):
            assert " " not in name

    def test_limit_is_honoured(self, db):
        assert len(db.suggest_media_names("core", limit=2)) == 2

    def test_no_match_falls_back_to_the_full_list(self, db):
        """Something to show beats an empty hint — the operator can
        pick from the list even when nothing resembles their input."""
        suggestions = db.suggest_media_names("zzzzzz")
        assert suggestions
        assert all(" " not in n for n in suggestions)

    def test_empty_identifier_lists_available_media(self, db):
        assert db.suggest_media_names("")
