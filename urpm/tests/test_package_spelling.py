"""Finding an available package from any spelling rpm itself prints.

The installed side of this question is answered by rpm's own
``RPMDBI_LABEL`` index (see ``test_rpmdb_label.py``).  The media side
has no such index: the catalogue is our SQLite table, so the
arbitration has to be a query — but it is still an arbitration and not
a parse, for the same reason.  Mageia ships Names shaped exactly like
a name-version-release: ``kernel-desktop-devel-6.18.22-1.mga10`` is a
package Name, and so are ``lua-rpm-macros-1`` and ``xmltex-20020625``.

What this replaces had decided by regular expression,
``re.match(r'^(.+?)-\\d+[.:]', identifier)``, which cut that kernel
Name down to ``kernel-desktop-devel`` and showed the operator a
different package than the one they named.

The rule under test: among the token itself and every prefix of it
ending before a hyphen, the longest one that exists as a package name
wins.  A full Name therefore beats the reading that would treat its
tail as a version, and a version that matches nothing narrows nothing
instead of failing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase


def _pkg(name, version, release, arch="x86_64"):
    """One catalogue row, with the fields the importer requires."""
    return {
        "name": name,
        "version": version,
        "release": release,
        "arch": arch,
        "nevra": f"{name}-{version}-{release}.{arch}",
        "filesize": 1024,
        "summary": f"{name} summary",
    }


CATALOGUE = [
    _pkg("urpm-ng", "0.9.14", "12.mga10", "noarch"),
    _pkg("urpm-ng-core", "0.9.14", "12.mga10", "noarch"),
    # A Name that reads like a name-version-release.  This is the one
    # the old regular expression truncated.
    _pkg("kernel-desktop-devel-6.18.22-1.mga10", "6.18.22", "1.mga10"),
    _pkg("firefox", "153.3.0", "1.mga10"),
    _pkg("lua-rpm-macros-1", "1", "5.mga10", "noarch"),
]


@pytest.fixture
def db():
    """A real PackageDatabase on a per-test temporary SQLite file.

    The catalogue is attached to a medium rather than imported loose:
    every lookup joins ``media`` to filter on the release the machine
    accepts, so rows with no medium are invisible to the very queries
    under test.  The medium takes its version from the database's own
    answer, so the fixture holds wherever the suite runs.
    """
    with tempfile.TemporaryDirectory(prefix="pkg_spelling_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            accepted = database._get_accepted_versions() or ["10"]
            media_id = database.add_media(
                name="Core Release", short_name="core_release",
                mageia_version=str(sorted(accepted)[0]),
                architecture="x86_64",
                relative_path="media/core/release",
                is_official=True, allow_unsigned=False, enabled=True,
                update_media=False, priority=50, url=None,
            )
            database.import_packages(iter(CATALOGUE), media_id=media_id)
            yield database
        finally:
            database.close()


class TestAPlainName:
    def test_it_resolves_to_itself(self, db):
        assert db.resolve_spelling("urpm-ng-core") == "urpm-ng-core"

    def test_the_case_does_not_matter(self, db):
        # ``get_package`` already matches on ``name_lower``; resolving a
        # spelling must not be stricter than the lookup that follows it.
        assert db.resolve_spelling("URPM-NG-CORE") == "urpm-ng-core"

    def test_a_name_nobody_publishes(self, db):
        assert db.resolve_spelling("no-such-package") is None

    def test_an_empty_token_is_answered_not_raised(self, db):
        assert db.resolve_spelling("") is None


class TestAVersionedSpelling:
    def test_what_rpm_q_prints(self, db):
        # No architecture, which is what ``rpm -q`` gives on Mageia.
        assert (db.resolve_spelling("urpm-ng-core-0.9.14-12.mga10")
                == "urpm-ng-core")

    def test_the_full_nevra(self, db):
        assert (db.resolve_spelling("urpm-ng-core-0.9.14-12.mga10.noarch")
                == "urpm-ng-core")

    def test_the_longer_name_wins_over_the_shorter_one(self, db):
        # ``urpm-ng`` is published too, so cutting at the first
        # hyphen-digit would have answered the wrong package.
        assert (db.resolve_spelling("urpm-ng-core-0.9.14-12.mga10")
                != "urpm-ng")

    def test_the_case_does_not_matter_here_either(self, db):
        assert (db.resolve_spelling("URPM-NG-CORE-0.9.14-12.mga10")
                == "urpm-ng-core")


class TestANameShapedLikeAVersion:
    def test_the_whole_name_wins(self, db):
        token = "kernel-desktop-devel-6.18.22-1.mga10"

        assert db.resolve_spelling(token) == token

    def test_a_trailing_number_is_not_a_version(self, db):
        assert db.resolve_spelling("lua-rpm-macros-1") == "lua-rpm-macros-1"


class TestAVersionThatNarrowsNothing:
    def test_the_name_still_answers(self, db):
        # Naming a build the media do not carry must not fail: the
        # extra information narrows the search, it never breaks it.
        assert db.resolve_spelling("firefox-1.0-1.mga10") == "firefox"

    def test_even_with_an_architecture(self, db):
        assert (db.resolve_spelling("firefox-1.0-1.mga10.x86_64")
                == "firefox")


class TestWhatShowConsumes:
    """``get_package_smart`` is the lookup behind ``urpm show``."""

    def test_a_versioned_spelling_finds_the_package(self, db):
        pkg = db.get_package_smart("urpm-ng-core-0.9.14-12.mga10")

        assert pkg is not None
        assert pkg["name"] == "urpm-ng-core"

    def test_a_name_shaped_like_a_version_is_no_longer_truncated(self, db):
        # The defect this pass closes: the operator named a package and
        # was shown another one.
        token = "kernel-desktop-devel-6.18.22-1.mga10"
        pkg = db.get_package_smart(token)

        assert pkg is not None
        assert pkg["name"] == token

    def test_a_plain_name_is_untouched(self, db):
        pkg = db.get_package_smart("firefox")

        assert pkg is not None
        assert pkg["name"] == "firefox"

    def test_a_token_naming_nothing_comes_back_empty(self, db):
        assert db.get_package_smart("no-such-package") is None
