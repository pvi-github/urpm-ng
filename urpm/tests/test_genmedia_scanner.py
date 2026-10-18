"""Scanning a directory where one RPM cannot be read.

Reported from a real publishing tree: ``urpm genmedia`` stopped on
« error reading package header » and said nothing else.  Three hundred
and forty packages in the directory, one of them a zero-length file
left by an interrupted copy, and nothing in the output pointed at it.
The ``--no-bad-rpm`` option, documented as « skip bad RPMs instead of
failing », was plumbed from the CLI down to the scanner and never
consulted, so there was no way to get past it either.

What has to hold: the failure names the file, says what is wrong with
it, and the option that skips it actually skips it.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest

rpm = pytest.importorskip("rpm", reason="rpm bindings unavailable")

from urpm.genmedia.scanner import RpmScanner


#: A real package built by ``urpm/tests/gen_test_rpms.py``, used as the
#: readable neighbour of the broken ones.
GOOD_RPM = (Path(__file__).parent / "media" / "orphans-2"
            / "t-2-1.x86_64.rpm")


@pytest.fixture
def tree(tmp_path):
    """A directory with one readable package in it."""
    if not GOOD_RPM.is_file():
        pytest.skip("test media not generated (make test-media)")
    shutil.copy(GOOD_RPM, tmp_path / GOOD_RPM.name)
    return tmp_path


def add_empty_rpm(tree, name="truncated-1.0-1.mga10.noarch.rpm") -> Path:
    """The field case: a copy that was interrupted at zero bytes."""
    path = tree / name
    path.write_bytes(b"")
    return path


def add_garbage_rpm(tree, name="garbage-1.0-1.mga10.noarch.rpm") -> Path:
    """A file that has content but is not a package."""
    path = tree / name
    path.write_bytes(b"this is not an RPM, but it is named like one\n")
    return path


def scanned_names(found) -> list:
    """The file names, as a reader would name them.

    ``RpmMetadata.filename`` holds the path the scanner opened, not a
    bare name: AppStream needs it to reopen the package, and every
    other consumer applies ``basename``.  The tests do the same rather
    than hard-code a temporary directory.
    """
    return [os.path.basename(meta.filename) for meta in found]


# ── Failing is the default, and it says which file ──────────────────────


def test_an_empty_rpm_is_named_in_the_error(tree):
    bad = add_empty_rpm(tree)

    with pytest.raises(RuntimeError) as raised:
        list(RpmScanner().scan(tree))

    message = str(raised.value)
    assert bad.name in message
    assert "0 bytes" in message


def test_a_file_that_is_not_a_package_reports_its_size(tree):
    """Distinguishes an interrupted copy from a wrong file."""
    bad = add_garbage_rpm(tree)

    with pytest.raises(RuntimeError) as raised:
        list(RpmScanner().scan(tree))

    message = str(raised.value)
    assert bad.name in message
    assert f"{bad.stat().st_size} bytes" in message


def test_the_error_names_the_way_out(tree):
    """A maintainer should not have to find the option elsewhere."""
    add_empty_rpm(tree)

    with pytest.raises(RuntimeError) as raised:
        list(RpmScanner().scan(tree))

    assert "--no-bad-rpm" in str(raised.value)


# ── The option that was never consulted ─────────────────────────────────


def test_the_readable_packages_are_still_scanned_when_skipping(tree):
    add_empty_rpm(tree)
    add_garbage_rpm(tree)

    found = list(RpmScanner(no_bad_rpm=True).scan(tree))

    assert scanned_names(found) == [GOOD_RPM.name]


def test_every_skipped_file_is_named_and_the_total_given(tree, caplog):
    add_empty_rpm(tree)
    add_garbage_rpm(tree)

    with caplog.at_level(logging.WARNING):
        list(RpmScanner(no_bad_rpm=True).scan(tree))

    warnings = [record.getMessage() for record in caplog.records]
    assert any("truncated-1.0-1.mga10.noarch.rpm" in line for line in warnings)
    assert any("garbage-1.0-1.mga10.noarch.rpm" in line for line in warnings)
    # The count matters on a tree of hundreds: a maintainer needs to
    # know the metadata is incomplete even if the per-file lines
    # scrolled past.
    assert any("2 unreadable" in line for line in warnings)


def test_skipping_does_not_suggest_the_option_already_in_use(tree, caplog):
    add_empty_rpm(tree)

    with caplog.at_level(logging.WARNING):
        list(RpmScanner(no_bad_rpm=True).scan(tree))

    assert not any("--no-bad-rpm" in record.getMessage()
                   for record in caplog.records)


# ── The ordinary path is untouched ──────────────────────────────────────


def test_a_clean_directory_scans_as_before(tree):
    found = list(RpmScanner().scan(tree))

    assert len(found) == 1
    assert found[0].name == "t"
    assert scanned_names(found) == [GOOD_RPM.name]


def test_an_unreadable_file_does_not_stop_the_ones_after_it(tree):
    """The broken file sorts first, so continuation is what is tested.

    ``scan`` walks the directory in sorted filename order, and
    ``aaa-`` comes before the readable package.
    """
    bad = add_empty_rpm(tree, name="aaa-1.0-1.mga10.noarch.rpm")

    found = list(RpmScanner(no_bad_rpm=True).scan(tree))

    assert bad.name < GOOD_RPM.name
    assert scanned_names(found) == [GOOD_RPM.name]
