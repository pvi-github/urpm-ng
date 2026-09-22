"""``urpm f`` never downloads: it reads the ``files.xml.lzma`` files
that a previous ``urpm media update`` left on disk.  A medium whose
index is absent, unreadable or empty is therefore skipped, and the
command still prints ``No package contains 'X'``.

That sentence is a lie of omission.  The operator concludes the file
belongs to no package on Mageia, when the truth is that a quarter of
the media were never opened.  Worse in the partial case: matches on
screen make the search look complete, so the gap goes unnoticed
precisely where it is most misleading.

Hence a report naming every skipped medium and why, printed whatever
the outcome and governed by no flag.
"""

from __future__ import annotations

import lzma
from unittest.mock import patch

import pytest

from urpm.cli.commands import query as query_mod
from urpm.core.files_xml import FILES_XML_STUB_SIZE


def _capture(capsys):
    """Return stdout with ANSI attributes already stripped."""
    import re
    out = capsys.readouterr().out
    return re.sub(r'\x1b\[[0-9;]*m', '', out)


def _reason_for(out: str, media_name: str) -> str:
    """Pull one medium's reason out of the report.

    Compared against another medium's reason rather than against a
    literal, so the tests check that the three situations are told
    apart without freezing the wording that says so.
    """
    line = next(ln for ln in out.splitlines() if media_name in ln)
    return line.split(media_name, 1)[1].strip()


class TestTheReportItself:
    """``_report_unsearched_media`` in isolation."""

    def test_a_fully_searched_run_says_nothing(self, capsys):
        query_mod._report_unsearched_media([], 15)
        assert _capture(capsys) == ""

    def test_every_skipped_medium_is_named_with_its_reason(self, capsys):
        query_mod._report_unsearched_media(
            [("Core Release Debug", "index unreadable"),
             ("Tainted Release", "index not downloaded")], 15)
        out = _capture(capsys)
        assert "Core Release Debug" in out
        assert "index unreadable" in out
        assert "Tainted Release" in out
        assert "index not downloaded" in out

    def test_the_list_is_never_truncated(self, capsys):
        """No ``... and N more``: a partial list would reintroduce the
        very doubt the report exists to remove."""
        many = [(f"Medium {i}", "index not downloaded") for i in range(40)]
        query_mod._report_unsearched_media(many, 60)
        out = _capture(capsys)
        for i in range(40):
            assert f"Medium {i}" in out
        assert "more)" not in out

    def test_a_total_wipeout_collapses_to_one_line(self, capsys):
        """Listing all fifteen media one by one would bury the point:
        nothing at all was searched."""
        all_gone = [(f"Medium {i}", "index not downloaded") for i in range(15)]
        query_mod._report_unsearched_media(all_gone, 15)
        out = _capture(capsys)
        assert "Medium 0" not in out
        assert len([ln for ln in out.splitlines() if ln.strip()]) == 2

    def test_no_enabled_media_at_all_is_stated(self, capsys):
        query_mod._report_unsearched_media([], 0)
        assert _capture(capsys).strip() != ""

    def test_it_never_prefixes_the_fix_with_a_privilege_tool(self, capsys):
        """Mageia does not guarantee sudo, and this message is
        informative: naming the bare command leaves the choice of
        ``su -`` to the machine and the operator."""
        query_mod._report_unsearched_media(
            [("Tainted Release", "index not downloaded")], 15)
        out = _capture(capsys)
        assert "urpm media update" in out
        assert "sudo" not in out
        assert "pkexec" not in out


class _FakeDB:
    def __init__(self, media):
        self._media = media

    def list_media(self):
        return self._media


@pytest.fixture
def three_media(tmp_path, monkeypatch):
    """Core searchable, Debug unreadable, Tainted never downloaded."""
    media = [
        {"id": 1, "name": "Core Release", "enabled": 1},
        {"id": 2, "name": "Core Debug", "enabled": 1},
        {"id": 3, "name": "Tainted Release", "enabled": 1},
        {"id": 4, "name": "Core Updates", "enabled": 0},
    ]

    def local_path(m, base_dir=None):
        return tmp_path / str(m["id"])

    from urpm.core.sync import FILES_XML_PATH
    for m in media:
        (tmp_path / str(m["id"]) / FILES_XML_PATH).parent.mkdir(parents=True)

    # A one-package index compresses below the stub threshold, so pad
    # it out: a real medium carries thousands of entries, and the point
    # here is to exercise the searchable case, not the stub one.
    entries = b''.join(
        b'<files fn="filler%d-1.0-1.mga10.x86_64">\n/usr/lib64/filler%d.so\n'
        b'/usr/share/doc/filler%d/README\n</files>' % (i, i, i)
        for i in range(200)
    )
    payload = lzma.compress(
        b'<media_info><files fn="foo-1.0-1.mga10.x86_64">\n'
        b'/usr/bin/foo\n</files>' + entries + b'</media_info>')
    assert len(payload) > FILES_XML_STUB_SIZE

    (tmp_path / "1" / FILES_XML_PATH).write_bytes(payload)
    unreadable = tmp_path / "2" / FILES_XML_PATH
    unreadable.write_bytes(payload)
    unreadable.chmod(0o000)

    monkeypatch.setattr("urpm.core.config.get_base_dir", lambda **kw: tmp_path)
    monkeypatch.setattr("urpm.core.config.get_media_local_path", local_path)
    return _FakeDB(media)


@pytest.fixture
def args():
    class _Args:
        pattern = "/usr/bin/nowhere"
        available = True
        installed = False
        show_all = False
        all_versions = False
        limit = 0
    return _Args()


@pytest.mark.skipif(
    __import__("os").geteuid() == 0,
    reason="root reads a 0000 file, so unreadability cannot be staged")
class TestWhatCmdFindReports:

    def test_an_empty_result_names_the_gaps(self, three_media, args, capsys):
        assert query_mod.cmd_find(args, three_media) == 1
        out = _capture(capsys)
        assert "Core Debug" in out
        assert "Tainted Release" in out

    def test_a_disabled_medium_is_not_a_gap(self, three_media, args, capsys):
        """It was not searched because the operator turned it off.
        Reporting it would train them to ignore the block."""
        query_mod.cmd_find(args, three_media)
        assert "Core Updates" not in _capture(capsys)

    def test_a_searchable_medium_is_not_a_gap(self, three_media, args, capsys):
        """Its index was opened and scanned; the pattern simply is not
        in it.  Listing it would make the report meaningless."""
        query_mod.cmd_find(args, three_media)
        assert "Core Release" not in _capture(capsys)

    def test_the_gaps_are_reported_alongside_results(
            self, three_media, args, capsys):
        """The whole point: a hit on screen must not imply the search
        was complete."""
        args.pattern = "/usr/bin/foo"
        assert query_mod.cmd_find(args, three_media) == 0
        out = _capture(capsys)
        assert "foo-1.0-1.mga10.x86_64" in out
        assert "Core Debug" in out
        assert "Tainted Release" in out

    def test_the_unreadable_one_is_told_apart_from_the_absent_one(
            self, three_media, args, capsys):
        """Same empty result, opposite fixes: one needs a download, the
        other needs the file modes repaired."""
        query_mod.cmd_find(args, three_media)
        out = _capture(capsys)
        assert _reason_for(out, "Core Debug") != \
            _reason_for(out, "Tainted Release")

    def test_a_stub_is_told_apart_from_an_absent_index(
            self, three_media, args, capsys, tmp_path):
        """genhdlist2 ships a well-formed but empty index for a medium
        that carries no package.  Calling that "not downloaded" would
        send the operator chasing a sync that already succeeded."""
        from urpm.core.sync import FILES_XML_PATH

        query_mod.cmd_find(args, three_media)
        absent = _reason_for(_capture(capsys), "Tainted Release")

        stub = tmp_path / "3" / FILES_XML_PATH
        stub.write_bytes(lzma.compress(b"<media_info></media_info>"))
        assert stub.stat().st_size <= FILES_XML_STUB_SIZE

        query_mod.cmd_find(args, three_media)
        assert _reason_for(_capture(capsys), "Tainted Release") != absent
