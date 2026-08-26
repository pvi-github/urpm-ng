"""Unit tests for :mod:`urpm.core.resolution.file_provides_rescue`.

Covers the three primitives the rescue pass composes :

* :func:`scan_files_xml` — LZMA + XML iterparse on the sidecar,
  filtered by the paths of interest.
* :func:`collect_unmet_file_requires` — narrows the target-side
  file-Requires down to those with no non-installed provider.
* :func:`inject_provides` — registers file-Provides on the correct
  target solvable and returns the injection count.

The higher-level orchestration in
:meth:`Resolver._rescue_file_provides_dropouts` needs a real repo
and is exercised by the VM smoke test rather than here.
"""

import lzma
import tempfile
from pathlib import Path

import pytest

solv = pytest.importorskip("solv")

from urpm.core.resolution.file_provides_rescue import (
    collect_unmet_file_requires,
    inject_provides,
    scan_files_xml,
)


def _make_files_xml_lzma(payload: bytes) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".xml.lzma", delete=False)
    tmp.write(lzma.compress(payload))
    tmp.close()
    return Path(tmp.name)


def test_scan_files_xml_filters_only_paths_of_interest():
    xml = (
        b'<?xml version="1.0"?>\n'
        b'<media_info>'
        b'<files fn="foo-1-1.mga10.x86_64">\n'
        b'/usr/bin/foo\n'
        b'/usr/share/foo/data\n'
        b'</files>'
        b'<files fn="bar-2-1.mga10.noarch">\n'
        b'/usr/bin/bar\n'
        b'/etc/bar.conf\n'
        b'</files>'
        b'</media_info>'
    )
    path = _make_files_xml_lzma(xml)
    try:
        result = scan_files_xml(
            path, {"/usr/bin/foo", "/etc/bar.conf", "/nowhere"},
        )
    finally:
        path.unlink()
    assert result == {
        "foo-1-1.mga10.x86_64": ["/usr/bin/foo"],
        "bar-2-1.mga10.noarch": ["/etc/bar.conf"],
    }


def test_scan_files_xml_returns_empty_when_no_paths():
    xml = b'<?xml version="1.0"?><media_info></media_info>'
    path = _make_files_xml_lzma(xml)
    try:
        assert scan_files_xml(path, set()) == {}
        assert scan_files_xml(path, {"/x"}) == {}
    finally:
        path.unlink()


def test_scan_files_xml_missing_sidecar_returns_empty():
    assert scan_files_xml(Path("/no/such/file"), {"/x"}) == {}


def _mkpool_with_installed_provider():
    pool = solv.Pool()
    sys_repo = pool.add_repo("@System")
    pool.installed = sys_repo
    inst = sys_repo.add_solvable()
    inst.name = "libpw"
    inst.evr = "1-1.mga9"
    inst.arch = "x86_64"
    inst.add_deparray(
        solv.SOLVABLE_PROVIDES, pool.Dep("/usr/bin/pwscore"),
    )
    return pool, sys_repo


def test_collect_unmet_flags_installed_only_provider_as_unmet():
    pool, _ = _mkpool_with_installed_provider()
    target_repo = pool.add_repo("mga10-core")
    target = target_repo.add_solvable()
    target.name = "cockpit-system"
    target.evr = "356-1.mga10"
    target.arch = "noarch"
    target.add_deparray(
        solv.SOLVABLE_REQUIRES, pool.Dep("/usr/bin/pwscore"),
    )
    target.add_deparray(
        solv.SOLVABLE_REQUIRES, pool.Dep("/usr/bin/grep"),
    )
    pool.createwhatprovides()

    unmet = collect_unmet_file_requires(pool, [target])
    assert unmet == {"/usr/bin/pwscore", "/usr/bin/grep"}


def test_collect_unmet_ignores_non_file_requires():
    pool, _ = _mkpool_with_installed_provider()
    target_repo = pool.add_repo("mga10-core")
    target = target_repo.add_solvable()
    target.name = "cockpit-system"
    target.evr = "356-1.mga10"
    target.arch = "noarch"
    target.add_deparray(
        solv.SOLVABLE_REQUIRES, pool.Dep("cockpit-bridge"),
    )
    pool.createwhatprovides()
    assert collect_unmet_file_requires(pool, [target]) == set()


def test_collect_unmet_skips_when_non_installed_provider_exists():
    pool, _ = _mkpool_with_installed_provider()
    target_repo = pool.add_repo("mga10-core")
    coreutils = target_repo.add_solvable()
    coreutils.name = "coreutils"
    coreutils.evr = "9.8-2.mga10"
    coreutils.arch = "x86_64"
    coreutils.add_deparray(
        solv.SOLVABLE_PROVIDES, pool.Dep("/usr/bin/date"),
    )
    target = target_repo.add_solvable()
    target.name = "cockpit-system"
    target.evr = "356-1.mga10"
    target.arch = "noarch"
    target.add_deparray(
        solv.SOLVABLE_REQUIRES, pool.Dep("/usr/bin/date"),
    )
    pool.createwhatprovides()
    assert collect_unmet_file_requires(pool, [target]) == set()


def test_inject_provides_registers_and_indexes():
    pool = solv.Pool()
    sys_repo = pool.add_repo("@System")
    pool.installed = sys_repo
    target_repo = pool.add_repo("mga10-core")
    s = target_repo.add_solvable()
    s.name = "libpwquality-tools"
    s.evr = "1.4.5-5.mga10"
    s.arch = "x86_64"
    pool.createwhatprovides()
    assert not list(
        pool.whatprovides(pool.str2id("/usr/bin/pwscore", 0)),
    )

    n = inject_provides(
        pool,
        {"libpwquality-tools-1.4.5-5.mga10.x86_64": ["/usr/bin/pwscore"]},
    )
    assert n == 1

    pool.createwhatprovides()
    providers = list(
        pool.whatprovides(pool.str2id("/usr/bin/pwscore", 0)),
    )
    assert len(providers) == 1
    assert providers[0].name == "libpwquality-tools"


def test_inject_provides_ignores_unknown_nevra():
    pool = solv.Pool()
    sys_repo = pool.add_repo("@System")
    pool.installed = sys_repo
    target_repo = pool.add_repo("mga10-core")
    s = target_repo.add_solvable()
    s.name = "foo"
    s.evr = "1-1.mga10"
    s.arch = "noarch"
    pool.createwhatprovides()
    n = inject_provides(pool, {"unknown-9-9.noarch": ["/anywhere"]})
    assert n == 0


def test_inject_provides_never_targets_installed_solvable():
    pool = solv.Pool()
    sys_repo = pool.add_repo("@System")
    pool.installed = sys_repo
    inst = sys_repo.add_solvable()
    inst.name = "foo"
    inst.evr = "1-1.mga9"
    inst.arch = "noarch"
    pool.createwhatprovides()
    n = inject_provides(pool, {"foo-1-1.mga9.noarch": ["/usr/bin/foo"]})
    assert n == 0
