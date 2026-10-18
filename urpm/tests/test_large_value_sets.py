"""Queries that match against a set no caller bounds.

Several statements compare a column against a whole set of values: the
packages a media no longer declares, the filenames its synthesis names,
the names a caller asks about.  Written as a literal
``IN (?, ?, …)`` each value is a bound parameter, and SQLite refuses a
statement with more than ``SQLITE_LIMIT_VARIABLE_NUMBER`` of them:
32766 on the SQLite Mageia 10 ships, 999 on older builds.

That ceiling was reached.  Attaching an installation disc to a
configured machine moved ``Core Release``'s metadata source from a
mirror (38861 packages) to the pressing (4488), so the next
``media update`` had 34373 obsolete rows to drop, built one ``DELETE``
with 34373 parameters, and failed on « too many SQL variables » *after*
the download had succeeded.  The source ordering that caused it is
fixed in :func:`urpm.core.sync.servers_for_media`; the unbounded
statement was a latent defect of its own, and these sites now pass the
set as a single JSON parameter that ``json_each`` unrolls.

The tests lower SQLite's **own** limit on the connection instead of
fabricating forty thousand packages.  That is the real constraint, not
a stand-in for it, so a revert to a literal parameter list fails here
immediately and the tests stay fast.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from urpm.core.database import PackageDatabase


#: Small enough that any of the sets below exceeds it, so a literal
#: parameter list cannot be bound.
TIGHT_LIMIT = 8


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="value_sets_") as tmp:
        database = PackageDatabase(db_path=Path(tmp) / "packages.db")
        try:
            yield database
        finally:
            database.close()


def tighten(db, limit=TIGHT_LIMIT):
    """Lower the parameter limit on the connection the code will use.

    ``_get_connection``, ``_conn_read`` and ``_conn_write`` hand back
    the *same* object within one thread (they are thread-local), so
    lowering the limit through one of them constrains the statements
    the code under test runs.  Verified rather than assumed: if they
    ever stop sharing, these tests would pass while constraining
    nothing, which is the failure mode worth guarding against.

    Returns the previous value.
    """
    conn = db._get_connection()
    with db._conn_write() as writing:
        assert writing is conn, (
            "the write path no longer shares this connection, so lowering "
            "the limit here would not constrain it")
    previous = conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) == limit
    return previous


def make_packages(count, *, start=0):
    """*count* synthetic synthesis entries, distinct by name."""
    return [
        {
            "name": f"pkg{i:05d}",
            "version": "1.0",
            "release": "1.mga10",
            "epoch": 0,
            "arch": "x86_64",
            "nevra": f"pkg{i:05d}-1.0-1.mga10.x86_64",
            "summary": f"package {i}",
            "provides": [f"pkg{i:05d}"],
            "requires": [],
            "filesize": 1000 + i,
        }
        for i in range(start, start + count)
    ]


def add_media(db):
    return db.add_media(
        name="Core Release", short_name="core_release",
        mageia_version="10", architecture="x86_64",
        relative_path="10/x86_64/media/core/release",
        is_official=True, enabled=True,
    )


# ── The premise ─────────────────────────────────────────────────────────


def test_the_tightened_limit_really_rejects_a_literal_list(db):
    """Without this, every test below could pass vacuously.

    It asserts the thing the other tests rely on: at this limit, a
    statement with one parameter per value is refused by SQLite, which
    is exactly what the old code built.
    """
    tighten(db)
    conn = db._get_connection()
    values = list(range(TIGHT_LIMIT + 1))
    placeholders = ",".join("?" * len(values))

    with pytest.raises(sqlite3.Error) as raised:
        conn.execute(
            f"SELECT 1 FROM packages WHERE id IN ({placeholders})", values)
    assert "variable" in str(raised.value).lower()

    # And the shape the code now uses binds one parameter, so the same
    # set goes through untouched.
    import json
    conn.execute(
        "SELECT 1 FROM packages WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(values),))


# ── The real call paths ─────────────────────────────────────────────────


def test_a_large_obsolete_set_is_deleted(db):
    """The field failure: a media's metadata replaced by a smaller set.

    Every package no longer declared has to go, however many there
    are, in one statement.
    """
    media_id = add_media(db)
    db.import_packages(iter(make_packages(30)), media_id=media_id)
    assert len(db.get_packages_for_media(media_id)) == 30

    tighten(db)
    db.import_packages(iter(make_packages(4)), media_id=media_id)

    remaining = sorted(p["name"] for p in db.get_packages_for_media(media_id))
    assert remaining == ["pkg00000", "pkg00001", "pkg00002", "pkg00003"]


def test_marking_referenced_cache_files(db):
    """This set holds every filename a media's synthesis names.

    The statement also binds ``media_id``, so an approach that budgeted
    the limit would have had to account for it; passing the set as one
    parameter removes that arithmetic entirely.
    """
    media_id = add_media(db)
    filenames = [f"pkg{i:05d}-1.0-1.mga10.x86_64.rpm" for i in range(20)]
    for name in filenames:
        db.register_cache_file(name, media_id=media_id,
                               file_path=f"official/{name}", file_size=1234)

    tighten(db)
    db.mark_cache_files_unreferenced(media_id, filenames[:12])

    referenced = {
        row["filename"] for row in db.list_cache_files(media_id=media_id)
        if row["is_referenced"]
    }
    assert referenced == set(filenames[:12])


def test_resolving_many_names(db):
    """A dependency closure or a full listing sizes this set.

    The per-name ordering matters here and is unaffected: it is applied
    by the single statement, over the whole set.
    """
    media_id = add_media(db)
    db.import_packages(iter(make_packages(20)), media_id=media_id)

    tighten(db)
    names = [f"pkg{i:05d}" for i in range(20)]
    found = db.get_packages_by_names(names)

    assert {entry["name"] for entry in found} == set(names)


def test_resolving_media_names_for_many_packages(db):
    """Fills ``media_name`` on a result set the caller sized.

    Reached through ``get_packages_by_names``, which leaves the media
    name to the batch lookup rather than joining it per row.
    """
    media_id = add_media(db)
    db.import_packages(iter(make_packages(20)), media_id=media_id)

    tighten(db)
    found = db.get_packages_by_names([f"pkg{i:05d}" for i in range(20)])

    assert found
    assert {entry.get("media_name") for entry in found} == {"Core Release"}


def test_appstream_metadata_lookup(db):
    """Its set has an entry per package name in the media.

    Driven through the query itself rather than a full AppStream
    build, which needs a media tree on disk: what has to hold is that
    the statement binds the name set as one parameter.
    """
    import json

    media_id = add_media(db)
    db.import_packages(iter(make_packages(20)), media_id=media_id)

    tighten(db)
    conn = db._get_connection()
    names = [f"pkg{i:05d}" for i in range(20)]
    rows = conn.execute(
        'SELECT name, arch, summary FROM packages '
        'WHERE media_id = ? AND name IN (SELECT value FROM json_each(?))',
        (media_id, json.dumps(names)),
    ).fetchall()

    assert {row["name"] for row in rows} == set(names)
