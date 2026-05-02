"""UX fix regression tests for ``urpm media`` add/list.

Covers three pre-existing UX bugs caught during the in-vivo arch-series
test:

* Bug 1 — ``urpm media add`` did not re-enable a previously-disabled
  media when the user re-added it.
* Bug 2 — the final summary line printed by ``urpm media add`` used
  the URL-derived display name instead of the existing media's actual
  name, causing the user to see a different media in the report.
* Bug 3 — ``urpm media list --all`` rendered disabled media with a
  near-invisible ``[ ]`` marker.  The list now uses an unmistakable
  ``[D]`` and prints a hint with the count when the default filter
  hides disabled media.

All tests use a real on-disk SQLite database via the ``db`` fixture.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pytest

from urpm.cli.commands.media import cmd_media_add, cmd_media_list
from urpm.core.database import PackageDatabase


@pytest.fixture
def db(monkeypatch):
    """Throwaway SQLite-backed PackageDatabase for one test."""
    monkeypatch.setattr('urpm.core.config.get_system_version', lambda: '9')
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    database = PackageDatabase(db_path)
    yield database
    database.close()
    db_path.unlink(missing_ok=True)


def _make_args(**overrides) -> argparse.Namespace:
    """Build an argparse-shaped object with sensible media add/list defaults."""
    base = dict(
        url=None,
        custom=None,
        name=None,
        version=None,
        disabled=False,
        update=False,
        import_key=False,
        allow_unsigned=False,
        auto=False,
        all=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Bug 1 — ``urpm media add`` re-enables a disabled media
# ---------------------------------------------------------------------------


class TestMediaAddReactivates:
    """``urpm media add`` on an existing disabled media must re-enable it."""

    URL = "https://distrib-coffee.example.org/mageia/9/x86_64/media/core/release"

    def test_reactivates_disabled_media(self, db, capsys):
        # Pre-create the media as disabled — same identity (version, arch,
        # short_name) the URL above will resolve to.
        db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="9/x86_64/media/core/release",
            enabled=False,
        )
        before = db.get_media("Core Release")
        assert before['enabled'] == 0, "fixture sanity: media must start disabled"

        rc = cmd_media_add(_make_args(url=self.URL, auto=True), db)
        assert rc == 0
        after = db.get_media("Core Release")
        assert after['enabled'] == 1, "media should be re-enabled by ``media add``"

        out = capsys.readouterr().out
        assert "re-enabled" in out.lower()

    def test_keeps_disabled_when_user_passes_disabled_flag(self, db, capsys):
        """``--disabled`` is the explicit user opt-out — honour it."""
        db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="9/x86_64/media/core/release",
            enabled=False,
        )
        rc = cmd_media_add(_make_args(url=self.URL, disabled=True, auto=True), db)
        assert rc == 0
        after = db.get_media("Core Release")
        assert after['enabled'] == 0, "--disabled must keep the media off"


# ---------------------------------------------------------------------------
# Bug 2 — summary uses the existing media name, not the URL-derived one
# ---------------------------------------------------------------------------


class TestMediaAddSummaryUsesActualName:
    """The final summary line must reference the media actually processed."""

    URL = "https://distrib-coffee.example.org/mageia/9/x86_64/media/core/release"

    def test_summary_uses_existing_media_name(self, db, capsys):
        # The user named the existing media differently from what the
        # URL parser would generate ("Core Release"). The summary must
        # echo *the existing name*, not the URL-derived one.
        db.add_media(
            name="Core 64bit Release",
            short_name="core_release",  # same short_name → same identity
            mageia_version="9",
            architecture="x86_64",
            relative_path="9/x86_64/media/core/release",
            enabled=True,
        )
        rc = cmd_media_add(_make_args(url=self.URL, auto=True), db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Core 64bit Release" in out
        # Locate the summary block: it is the last non-empty line(s)
        # after the trailing blank line emitted by ``cmd_media_add``.
        lines = out.splitlines()
        try:
            blank_idx = max(i for i, ln in enumerate(lines) if ln.strip() == '')
        except ValueError:
            blank_idx = -1
        summary_block = "\n".join(lines[blank_idx + 1:])
        assert summary_block.strip(), "summary line should be present"
        # Critical: the summary must NOT name the URL-derived display
        # name ("Core Release") — that was the Bug-2 misreport.  It must
        # name the actual media we operated on.
        assert "Core 64bit Release" in summary_block, (
            f"summary should mention the actual media name: {summary_block!r}"
        )
        # Defensively reject the phantom URL-derived name as a standalone
        # token (i.e. not as a substring of "Core 64bit Release").
        assert "'Core Release'" not in summary_block


# ---------------------------------------------------------------------------
# Bug 3 — ``urpm media list --all`` shows disabled with a clear marker
# ---------------------------------------------------------------------------


class TestMediaListShowsDisabled:
    """``--all`` must include disabled media, marked clearly."""

    def _seed(self, db):
        db.add_media(
            name="Core Release",
            short_name="core_release",
            mageia_version="9",
            architecture="x86_64",
            relative_path="9/x86_64/media/core/release",
            enabled=True,
        )
        db.add_media(
            name="Core 32bit Release",
            short_name="core_release_32",
            mageia_version="9",
            architecture="i586",
            relative_path="9/i586/media/core/release",
            enabled=False,
        )

    def test_default_hides_disabled_and_announces_count(self, db, capsys):
        self._seed(db)
        rc = cmd_media_list(_make_args(all=False), db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Core Release" in out
        assert "Core 32bit Release" not in out
        assert "1 disabled" in out, "must hint at hidden disabled media count"

    def test_all_flag_lists_disabled_with_marker(self, db, capsys):
        self._seed(db)
        rc = cmd_media_list(_make_args(all=True), db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Core Release" in out
        assert "Core 32bit Release" in out
        # The disabled-marker contract: a clear ``[D]`` next to the
        # disabled row, distinct from the ``[x]`` marker on the enabled
        # one.  The line containing the disabled media name must contain [D].
        disabled_line = next(
            ln for ln in out.splitlines() if "Core 32bit Release" in ln
        )
        assert "[D]" in disabled_line
