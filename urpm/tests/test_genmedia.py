"""Tests for urpm.genmedia contracts.

These tests define the expected behavior of the genmedia writers.
They serve as acceptance criteria for the implementation:
fill the stubs, make the tests pass.
"""

import hashlib
import lzma
import gzip
import struct
import tempfile
from pathlib import Path

import pytest

from urpm.genmedia import RpmMetadata, GenerateResult
from urpm.genmedia.compress import parse_filter, compress_open


# ─── Fixtures ─────────────────────────────────────────────────────


def _make_metadata(**overrides) -> RpmMetadata:
    """Build a RpmMetadata with sensible defaults, overridable."""
    defaults = dict(
        filename='foo-1.0-1.mga10.x86_64.rpm',
        name='foo',
        epoch=0,
        version='1.0',
        release='1.mga10',
        arch='x86_64',
        summary='A test package',
        description='This is a test package for unit tests.',
        group='Development/Tools',
        license='GPLv2+',
        url='https://example.com/foo',
        sourcerpm='foo-1.0-1.mga10.src.rpm',
        packager='Test User <test@example.com>',
        size=12345,
        filesize=4567,
        buildtime=1700000000,
        requires=['libbar[>= 2.0]', 'libc.so.6()(64bit)'],
        provides=['foo[== 1.0-1.mga10]'],
        conflicts=[],
        obsoletes=['oldfoo[< 1.0]'],
        suggests=['baz'],
        files=['/usr/bin/foo', '/usr/lib64/libfoo.so', '/usr/share/doc/foo/README'],
        changelog=[
            (1700000000, 'Test User <test@example.com>', '- Initial release'),
        ],
        header_bytes=b'\x8e\xad\xe8\x01' + b'\x00' * 100,  # fake RPM header
        header_sha256=hashlib.sha256(b'\x8e\xad\xe8\x01' + b'\x00' * 100).hexdigest(),
    )
    defaults.update(overrides)
    return RpmMetadata(**defaults)


@pytest.fixture
def sample_packages():
    """Three packages with varied metadata for testing writers."""
    return [
        _make_metadata(
            filename='foo-1.0-1.mga10.x86_64.rpm',
            name='foo',
            version='1.0',
            requires=['libbar[>= 2.0]'],
            provides=['foo[== 1.0-1.mga10]'],
            suggests=['baz'],
            obsoletes=['oldfoo[< 1.0]'],
            files=['/usr/bin/foo', '/usr/lib64/libfoo.so'],
        ),
        _make_metadata(
            filename='bar-2.3-5.mga10.x86_64.rpm',
            name='bar',
            version='2.3',
            release='5.mga10',
            summary='Bar library',
            description='The bar library.',
            group='System/Libraries',
            requires=[],
            provides=['libbar.so.1()(64bit)', 'bar[== 2.3-5.mga10]'],
            suggests=[],
            obsoletes=[],
            files=['/usr/lib64/libbar.so.1', '/usr/lib64/libbar.so.1.0.0'],
            changelog=[
                (1700000000, 'Dev <dev@mga.org>', '- Update to 2.3'),
                (1690000000, 'Dev <dev@mga.org>', '- Initial import'),
            ],
        ),
        _make_metadata(
            filename='baz-0.1-1.mga10.noarch.rpm',
            name='baz',
            version='0.1',
            release='1.mga10',
            arch='noarch',
            summary='Baz utility',
            description='A noarch utility.',
            group='Terminals',
            requires=['python3'],
            provides=['baz[== 0.1-1.mga10]'],
            suggests=[],
            obsoletes=[],
            files=['/usr/bin/baz'],
            changelog=[],
        ),
    ]


@pytest.fixture
def tmp_media_info(tmp_path):
    """Provide a temporary media_info directory."""
    d = tmp_path / 'media_info'
    d.mkdir()
    return d


# ─── RpmMetadata ──────────────────────────────────────────────────


class TestRpmMetadata:
    """Test the shared data class."""

    def test_nevra(self):
        m = _make_metadata()
        assert m.nevra == 'foo-1.0-1.mga10.x86_64'

    def test_defaults(self):
        m = _make_metadata(requires=[], provides=[], files=[])
        assert m.requires == []
        assert m.provides == []
        assert m.files == []

    def test_header_sha256_matches_bytes(self):
        m = _make_metadata()
        assert m.header_sha256 == hashlib.sha256(m.header_bytes).hexdigest()


# ─── compress.py ──────────────────────────────────────────────────


class TestParseFilter:
    """Test compression filter string parsing."""

    def test_gzip(self):
        ext, comp, level = parse_filter('.cz:gzip -9')
        assert ext == '.cz'
        assert comp == 'gzip'
        assert level == 9

    def test_xz(self):
        ext, comp, level = parse_filter('.cz:xz -7')
        assert ext == '.cz'
        assert comp == 'xz'
        assert level == 7

    def test_lzma(self):
        ext, comp, level = parse_filter('.lzma:xz -7')
        assert ext == '.lzma'
        assert comp == 'xz'
        assert level == 7

    def test_no_level(self):
        ext, comp, level = parse_filter('.cz:gzip')
        assert level == 9  # default

    def test_invalid_no_colon(self):
        with pytest.raises(ValueError, match='expected'):
            parse_filter('gzip -9')

    def test_invalid_no_dot(self):
        with pytest.raises(ValueError, match="must start with '.'"):
            parse_filter('cz:gzip -9')


class TestCompressOpen:
    """Test compressed file opening for write."""

    def test_gzip_roundtrip(self, tmp_path):
        path = tmp_path / 'test.gz'
        with compress_open(path, 'gzip', 9) as f:
            f.write('hello world')
        with gzip.open(path, 'rt') as f:
            assert f.read() == 'hello world'

    def test_xz_roundtrip(self, tmp_path):
        path = tmp_path / 'test.xz'
        with compress_open(path, 'xz', 7) as f:
            f.write('hello world')
        with lzma.open(path, 'rt') as f:
            assert f.read() == 'hello world'

    def test_unsupported(self, tmp_path):
        with pytest.raises(ValueError, match='Unsupported'):
            compress_open(tmp_path / 'x', 'brotli', 5)


# ─── Synthesis writer contract ────────────────────────────────────


class TestWriteSynthesis:
    """Contract tests for write_synthesis().

    The synthesis format is @field@value lines, LZMA-compressed.
    Each package ends with an @info line.
    """

    def _write_and_read(self, packages, tmp_path):
        """Helper: write synthesis, decompress, return lines."""
        from urpm.core.synthesis import write_synthesis
        out = tmp_path / 'synthesis.hdlist.cz'
        count = write_synthesis(out, packages, compression_filter='xz -7')
        # Decompress and read
        with lzma.open(out, 'rt') as f:
            lines = f.read().splitlines()
        return count, lines

    def test_package_count(self, sample_packages, tmp_path):
        count, _ = self._write_and_read(sample_packages, tmp_path)
        assert count == 3

    def test_info_lines_present(self, sample_packages, tmp_path):
        """Each package must have exactly one @info line."""
        _, lines = self._write_and_read(sample_packages, tmp_path)
        info_lines = [l for l in lines if l.startswith('@info@')]
        assert len(info_lines) == 3

    def test_info_line_format(self, sample_packages, tmp_path):
        """@info line: @info@NEVRA@epoch@installed_size@group."""
        _, lines = self._write_and_read(sample_packages, tmp_path)
        info = [l for l in lines if l.startswith('@info@foo-')]
        assert len(info) == 1
        parts = info[0].split('@')[1:]  # skip leading empty
        assert parts[0] == 'info'
        assert parts[1] == 'foo-1.0-1.mga10.x86_64'
        assert parts[2] == '0'           # epoch
        assert parts[3] == '12345'       # size
        assert parts[4] == 'Development/Tools'  # group

    def test_requires_format(self, sample_packages, tmp_path):
        """@requires line with versioned deps."""
        _, lines = self._write_and_read(sample_packages, tmp_path)
        # foo requires libbar[>= 2.0]
        req_lines = [l for l in lines if l.startswith('@requires@')]
        req_with_libbar = [l for l in req_lines if 'libbar' in l]
        assert len(req_with_libbar) == 1
        assert 'libbar[>= 2.0]' in req_with_libbar[0]

    def test_suggests_format(self, sample_packages, tmp_path):
        _, lines = self._write_and_read(sample_packages, tmp_path)
        sug = [l for l in lines if l.startswith('@suggests@')]
        sug_baz = [l for l in sug if 'baz' in l]
        assert len(sug_baz) == 1

    def test_filesize_present(self, sample_packages, tmp_path):
        _, lines = self._write_and_read(sample_packages, tmp_path)
        fs = [l for l in lines if l.startswith('@filesize@')]
        assert len(fs) == 3

    def test_roundtrip(self, sample_packages, tmp_path):
        """Write then read back with parse_synthesis — must match."""
        from urpm.core.synthesis import write_synthesis, parse_synthesis
        out = tmp_path / 'synthesis.hdlist.cz'
        write_synthesis(out, sample_packages, compression_filter='xz -7')
        parsed = list(parse_synthesis(out))
        assert len(parsed) == 3
        names = {p['name'] for p in parsed}
        assert names == {'foo', 'bar', 'baz'}


# ─── Files XML writer contract ───────────────────────────────────


class TestWriteFilesXml:
    """Contract tests for write_files_xml()."""

    def test_basic_output(self, sample_packages, tmp_path):
        from urpm.core.files_xml import write_files_xml
        out = tmp_path / 'files.xml.lzma'
        count = write_files_xml(out, sample_packages)
        assert count == 3
        with lzma.open(out, 'rt') as f:
            content = f.read()
        assert '<media_info>' in content
        assert 'fn="foo-1.0-1.mga10.x86_64.rpm"' in content
        assert '/usr/bin/foo' in content

    def test_roundtrip(self, sample_packages, tmp_path):
        """Write then read back with parse_files_xml."""
        from urpm.core.files_xml import write_files_xml, parse_files_xml
        out = tmp_path / 'files.xml.lzma'
        write_files_xml(out, sample_packages)
        parsed = dict(parse_files_xml(out))
        assert 'foo-1.0-1.mga10.x86_64.rpm' in parsed
        assert '/usr/bin/foo' in parsed['foo-1.0-1.mga10.x86_64.rpm']


# ─── Info XML writer contract ────────────────────────────────────


class TestWriteInfoXml:
    """Contract tests for write_info_xml()."""

    def test_basic_output(self, sample_packages, tmp_path):
        from urpm.core.files_xml import write_info_xml
        out = tmp_path / 'info.xml.lzma'
        count = write_info_xml(out, sample_packages)
        assert count == 3
        with lzma.open(out, 'rt') as f:
            content = f.read()
        assert '<media_info>' in content
        assert "sourcerpm='foo-1.0-1.mga10.src.rpm'" in content
        assert "license='GPLv2+'" in content
        assert 'This is a test package' in content


# ─── Changelog XML writer contract ──────────────────────────────


class TestWriteChangelogXml:
    """Contract tests for write_changelog_xml()."""

    def test_basic_output(self, sample_packages, tmp_path):
        from urpm.core.files_xml import write_changelog_xml
        out = tmp_path / 'changelog.xml.lzma'
        count = write_changelog_xml(out, sample_packages)
        assert count == 3
        with lzma.open(out, 'rt') as f:
            content = f.read()
        assert '<media_info>' in content
        assert '<changelogs' in content
        assert '<log_name>' in content

    def test_empty_changelog(self, tmp_path):
        """Package with no changelog entries should still appear."""
        from urpm.core.files_xml import write_changelog_xml
        pkg = _make_metadata(changelog=[])
        out = tmp_path / 'changelog.xml.lzma'
        count = write_changelog_xml(out, [pkg])
        assert count == 1


# ─── Hdlist writer contract ──────────────────────────────────────


class TestWriteHdlist:
    """Contract tests for write_hdlist()."""

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_package_count(self, sample_packages, tmp_path):
        from urpm.core.hdlist import write_hdlist
        out = tmp_path / 'hdlist.cz'
        count = write_hdlist(out, sample_packages)
        assert count == 3

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_output_is_gzip(self, sample_packages, tmp_path):
        """Default compression is gzip."""
        from urpm.core.hdlist import write_hdlist
        out = tmp_path / 'hdlist.cz'
        write_hdlist(out, sample_packages)
        with open(out, 'rb') as f:
            magic = f.read(2)
        assert magic == b'\x1f\x8b'  # gzip magic


# ─── Scanner contract ────────────────────────────────────────────


class TestRpmScanner:
    """Contract tests for RpmScanner using real test RPMs."""

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_scan_suggests_dir(self):
        """Scan existing test RPMs from the suggests fixture."""
        from urpm.genmedia.scanner import RpmScanner
        rpms_dir = Path(__file__).parent / 'media' / 'suggests'
        scanner = RpmScanner()
        packages = list(scanner.scan(rpms_dir))
        assert len(packages) >= 10
        names = {p.name for p in packages}
        assert 'a' in names
        assert 'b' in names
        assert 'suggested_b' in names

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_metadata_fields(self):
        """Each RpmMetadata must have all required fields populated."""
        from urpm.genmedia.scanner import RpmScanner
        rpms_dir = Path(__file__).parent / 'media' / 'suggests'
        scanner = RpmScanner()
        for pkg in scanner.scan(rpms_dir):
            assert pkg.name, f"empty name for {pkg.filename}"
            assert pkg.version, f"empty version for {pkg.filename}"
            assert pkg.arch, f"empty arch for {pkg.filename}"
            assert pkg.filename.endswith('.rpm')
            assert pkg.header_bytes, f"empty header_bytes for {pkg.filename}"
            assert pkg.header_sha256, f"empty sha256 for {pkg.filename}"
            assert isinstance(pkg.files, list)
            assert isinstance(pkg.requires, list)
            assert isinstance(pkg.provides, list)

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_sorted_order(self):
        """Packages must be yielded in sorted filename order."""
        from urpm.genmedia.scanner import RpmScanner
        rpms_dir = Path(__file__).parent / 'media' / 'suggests'
        scanner = RpmScanner()
        packages = list(scanner.scan(rpms_dir))
        filenames = [p.filename for p in packages]
        assert filenames == sorted(filenames)


# ─── MediaGenerator contract ─────────────────────────────────────


class TestMediaGenerator:
    """Contract tests for the orchestrator."""

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_generate_returns_result(self, tmp_path):
        """generate() must return a GenerateResult."""
        from urpm.genmedia.generator import MediaGenerator
        rpms_dir = Path(__file__).parent / 'media' / 'suggests'
        gen = MediaGenerator(rpms_dir=rpms_dir, media_info_dir=tmp_path, lock=False)
        result = gen.generate(hdlist=False, xml_info=False, md5sum=False)
        assert isinstance(result, GenerateResult)

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_generate_creates_synthesis(self, tmp_path):
        """Default generate must produce synthesis.hdlist.cz."""
        from urpm.genmedia.generator import MediaGenerator
        rpms_dir = Path(__file__).parent / 'media' / 'suggests'
        gen = MediaGenerator(rpms_dir=rpms_dir, media_info_dir=tmp_path, lock=False)
        result = gen.generate(hdlist=False, xml_info=False)
        assert result.success
        assert (tmp_path / 'synthesis.hdlist.cz').exists()

    @pytest.mark.xfail(reason='stub not yet implemented', raises=NotImplementedError)
    def test_generate_md5sum(self, tmp_path):
        """MD5SUM must list all generated files."""
        from urpm.genmedia.generator import MediaGenerator
        rpms_dir = Path(__file__).parent / 'media' / 'suggests'
        gen = MediaGenerator(rpms_dir=rpms_dir, media_info_dir=tmp_path, lock=False)
        result = gen.generate(hdlist=True, xml_info=False)
        assert result.md5sum_written
        md5_path = tmp_path / 'MD5SUM'
        assert md5_path.exists()
        content = md5_path.read_text()
        assert 'synthesis.hdlist.cz' in content
