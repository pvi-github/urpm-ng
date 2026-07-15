"""Tests for :mod:`urpm.core.appstream_scan`."""

import lzma
from pathlib import Path

import pytest

from urpm.core.appstream_scan import (
    parse_nevra,
    scan_media_appstream_candidates,
)


# ---------------------------------------------------------------------------
# parse_nevra
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('nevra, expected', [
    ('libreoffice-writer-26.2.3.2-1.mga10.x86_64',
     ('libreoffice-writer', '26.2.3.2', '1.mga10', 'x86_64')),
    ('golang-github-cespare-xxhash-2.1.2-2.mga9.x86_64',
     ('golang-github-cespare-xxhash', '2.1.2', '2.mga9', 'x86_64')),
    ('kernel-6.6.20-1.mga10.x86_64',
     ('kernel', '6.6.20', '1.mga10', 'x86_64')),
    ('0ad-0.0.26-3.mga10.x86_64',
     ('0ad', '0.0.26', '3.mga10', 'x86_64')),
    ('python3-pynvim-0.5.2-4.mga10.noarch',
     ('python3-pynvim', '0.5.2', '4.mga10', 'noarch')),
    ('7zip-24.09-1.mga10.x86_64',
     ('7zip', '24.09', '1.mga10', 'x86_64')),
    ('firefox-140.11.0-1.mga10.i686',
     ('firefox', '140.11.0', '1.mga10', 'i686')),
])
def test_parse_nevra_valid(nevra, expected):
    assert parse_nevra(nevra) == expected


@pytest.mark.parametrize('bogus', [
    '',
    'no-version-at-all',
    'trailing-dash-',
    'no.arch.separator-1.0-1mga10',
])
def test_parse_nevra_invalid(bogus):
    assert parse_nevra(bogus) is None


# ---------------------------------------------------------------------------
# scan_media_appstream_candidates
# ---------------------------------------------------------------------------

def _make_files_xml_lzma(tmp_path: Path, xml_body: str) -> Path:
    """Write ``xml_body`` compressed as legacy lzma (matches upstream Mageia)."""
    payload = xml_body.encode('utf-8')
    # ``format=FORMAT_ALONE`` produces the legacy ``.lzma`` container that
    # xzgrep still supports and that Mageia media use.
    compressed = lzma.compress(payload, format=lzma.FORMAT_ALONE)
    path = tmp_path / 'files.xml.lzma'
    path.write_bytes(compressed)
    return path


# The exact on-wire format observed on Mageia: no whitespace between
# consecutive package blocks (``</files><files fn="…">`` inline).
_SIMPLE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<media_info><files fn="libreoffice-writer-26.2.3.2-1.mga10.x86_64">\n'
    '/usr/bin/lowriter\n'
    '/usr/share/applications/libreoffice-writer.desktop\n'
    '/usr/share/metainfo/libreoffice-writer.appdata.xml\n'
    '/usr/share/man/man1/lowriter.1.gz\n'
    '</files><files fn="lib64qt6core6-6.7.0-1.mga10.x86_64">\n'
    '/usr/lib64/libQt6Core.so.6\n'
    '/usr/lib64/libQt6Core.so.6.7.0\n'
    '</files><files fn="firefox-140.11.0-1.mga10.x86_64">\n'
    '/usr/bin/firefox\n'
    '/usr/share/applications/firefox.desktop\n'
    '/usr/share/appdata/firefox.appdata.xml\n'
    '</files></media_info>\n'
)


def test_scan_returns_only_packages_with_candidates(tmp_path):
    path = _make_files_xml_lzma(tmp_path, _SIMPLE_XML)
    result = scan_media_appstream_candidates(path)

    # ``lib64qt6core6`` has no candidate path → must not appear.
    assert set(result) == {
        'libreoffice-writer-26.2.3.2-1.mga10.x86_64',
        'firefox-140.11.0-1.mga10.x86_64',
    }


def test_scan_collects_all_candidate_paths_per_package(tmp_path):
    path = _make_files_xml_lzma(tmp_path, _SIMPLE_XML)
    result = scan_media_appstream_candidates(path)

    assert sorted(result['libreoffice-writer-26.2.3.2-1.mga10.x86_64']) == [
        '/usr/share/applications/libreoffice-writer.desktop',
        '/usr/share/metainfo/libreoffice-writer.appdata.xml',
    ]
    assert sorted(result['firefox-140.11.0-1.mga10.x86_64']) == [
        '/usr/share/appdata/firefox.appdata.xml',
        '/usr/share/applications/firefox.desktop',
    ]


def test_scan_handles_inline_package_transition(tmp_path):
    # Regression guard: when ``</files><files fn="B">`` share a physical
    # line with A's last path, the parser must attribute the following
    # paths to B, not A.
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<media_info><files fn="a-1.0-1.mga10.noarch">\n'
        '/usr/share/applications/a.desktop\n'
        '</files><files fn="b-1.0-1.mga10.noarch">\n'
        '/usr/share/applications/b.desktop\n'
        '</files></media_info>\n'
    )
    path = _make_files_xml_lzma(tmp_path, xml)
    result = scan_media_appstream_candidates(path)

    assert result == {
        'a-1.0-1.mga10.noarch': ['/usr/share/applications/a.desktop'],
        'b-1.0-1.mga10.noarch': ['/usr/share/applications/b.desktop'],
    }


def test_scan_ignores_non_appstream_files_in_target_dirs(tmp_path):
    # ``/usr/share/applications/mimeinfo.cache`` is *not* a .desktop file
    # and must not be picked up.  Same for random ``.png`` icons under a
    # metainfo path or ``README`` in appdata.
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<media_info><files fn="only-cache-1.0-1.mga10.noarch">\n'
        '/usr/share/applications/mimeinfo.cache\n'
        '/usr/share/metainfo/README\n'
        '/usr/share/appdata/icon.png\n'
        '</files></media_info>\n'
    )
    path = _make_files_xml_lzma(tmp_path, xml)
    assert scan_media_appstream_candidates(path) == {}


def test_scan_returns_empty_dict_on_empty_media(tmp_path):
    xml = '<?xml version="1.0" encoding="utf-8"?>\n<media_info></media_info>\n'
    path = _make_files_xml_lzma(tmp_path, xml)
    assert scan_media_appstream_candidates(path) == {}


def test_scan_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_media_appstream_candidates(tmp_path / 'nope.xml.lzma')
