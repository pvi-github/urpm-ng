"""The media list, as PackageKit asks for it.

Discover queries the repository list on every start, and the backend
answered ``GetRepoList not supported by backend`` because neither side
implemented it. Its Sources page stayed empty while urpm had the media
all along.

What these tests pin is the one property a repo id must have, and that
the obvious choice does not have it. ``short_name`` reads better, but
the table is unique on ``(mageia_version, architecture, short_name)``,
so two media of different releases may share one: ``resolve_media``
says as much when it documents breaking ties by priority. ``name`` is
``NOT NULL UNIQUE``, and :meth:`resolve_media` accepts it too, so the id
we publish stays something the operator can type back at urpm.
"""

import json

import pytest

from urpm.dbus.service import UrpmDBusService


def _medium(name, short_name, version="10", arch="x86_64", enabled=1):
    return {
        'name': name,
        'short_name': short_name,
        'mageia_version': version,
        'architecture': arch,
        'enabled': enabled,
    }


def _repos(monkeypatch, media):
    """Run the handler over a stand-in database."""
    instance = UrpmDBusService()
    monkeypatch.setattr(instance, '_init_core', lambda: None)

    class _Db:
        @staticmethod
        def list_media():
            return list(media)

    instance._db = _Db()
    return json.loads(instance.handle_get_repo_list(None, None))


def test_every_medium_is_reported(monkeypatch):
    repos = _repos(monkeypatch, [
        _medium("Core Release", "core_release"),
        _medium("URPM Testing", "urpm_testing"),
    ])

    assert [r['id'] for r in repos] == ["Core Release", "URPM Testing"]


def test_media_sharing_a_short_name_keep_distinct_ids(monkeypatch):
    """The schema allows it; a repo id may not be ambiguous."""
    repos = _repos(monkeypatch, [
        _medium("Core Release", "core_release", version="9"),
        _medium("Core Release mga10", "core_release", version="10"),
    ])

    assert len({r['id'] for r in repos}) == 2


def test_the_description_tells_the_releases_apart(monkeypatch):
    """With several Mageia versions side by side, the name is not enough."""
    repos = _repos(monkeypatch, [
        _medium("Core Release", "core_release", version="9", arch="i586"),
    ])

    assert repos[0]['description'] == "Core Release (Mageia 9, i586)"


def test_a_disabled_medium_is_listed_as_disabled(monkeypatch):
    """Listed, not hidden: the Sources page is where one re-enables it."""
    repos = _repos(monkeypatch, [
        _medium("Core Release", "core_release"),
        _medium("Core SRPMS", "core_srpms", enabled=0),
    ])

    assert [r['enabled'] for r in repos] == [True, False]


def test_a_nameless_medium_is_left_out(monkeypatch):
    """An empty id is nothing a client could ever quote back."""
    repos = _repos(monkeypatch, [
        _medium("", "orphan"),
        _medium("Core Release", "core_release"),
    ])

    assert [r['id'] for r in repos] == ["Core Release"]


def test_nothing_configured_is_not_an_error(monkeypatch):
    assert _repos(monkeypatch, []) == []


def test_the_method_is_declared_in_the_introspection():
    """Implemented but undeclared would still answer "not supported"."""
    xml = UrpmDBusService()._build_introspection_xml()

    assert '<method name="GetRepoList">' in xml
    assert '<arg name="repos" type="s" direction="out"/>' in xml
