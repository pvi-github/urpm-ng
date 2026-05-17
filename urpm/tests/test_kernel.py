"""Tests for ``urpm.cli.helpers.kernel`` version ordering.

These tests pin the behaviour of :func:`_compare_version_release`, the
comparator used by :func:`find_old_kernels` to decide which installed
kernels are the "newest" and must therefore be kept.

The bug they regress against is subtle: ``find_old_kernels`` keys
installed kernels by ``(version, release)`` tuples and used to call
``sorted(..., reverse=True)`` on those keys directly. That falls back to
Python's per-component string ordering, which silently breaks on
multi-digit version segments — ``'5' > '14'`` as a string, so once the
Linux kernel ticks to a two-digit major (or a maintainer pushes a
``5.100`` micro), the helper would consider the running series "old"
and propose removing it.

The fix routes the comparison through :func:`rpm.labelCompare`, the same
ordering RPM itself uses (and that the ``rpm_version_compare`` SQLite
collation in ``urpm.core.database`` already relies on for the package
table). These tests assert that ordering, plus the end-to-end behaviour
of :func:`find_old_kernels` on a synthetic kernel set whose lex order
disagrees with the RPM order.
"""

from __future__ import annotations

from functools import cmp_to_key
from unittest.mock import MagicMock, patch

import pytest

from urpm.cli.helpers.kernel import (
    _compare_version_release,
    find_old_kernels,
)


# ---------------------------------------------------------------------------
# Comparator: version-only inputs
# ---------------------------------------------------------------------------


def _sort_versions(versions, reverse=False):
    """Helper: sort ``(version, release)`` tuples using the kernel comparator."""
    return sorted(versions, key=cmp_to_key(_compare_version_release), reverse=reverse)


def test_compare_orders_two_digit_major_above_one_digit():
    """``14.x`` must be newer than ``5.x`` (the headline bug)."""
    assert _compare_version_release(('14.1.0', '1'), ('5.10.0', '1')) > 0
    assert _compare_version_release(('5.10.0', '1'), ('14.1.0', '1')) < 0


def test_compare_orders_multi_digit_minor_above_single_digit():
    """``5.10`` must be newer than ``5.9`` (numeric, not lex)."""
    assert _compare_version_release(('5.10', '1'), ('5.9', '1')) > 0
    assert _compare_version_release(('5.100', '1'), ('5.10', '1')) > 0


def test_compare_returns_zero_for_equal_tuples():
    assert _compare_version_release(('6.6.58', '1.mga9'), ('6.6.58', '1.mga9')) == 0


def test_compare_uses_release_when_version_equal():
    """Same version, different release: RPM release ordering must apply."""
    # Release '2.mga9' is newer than '1.mga9' in RPM semantics.
    assert _compare_version_release(('6.6.58', '2.mga9'), ('6.6.58', '1.mga9')) > 0


def test_sorted_reverse_matches_rpm_order_not_lex():
    """Full reverse sort: the RPM-correct order, not the lex one."""
    versions = [('5.10.0', '1'), ('5.4.0', '1'), ('14.1.0', '1'), ('6.0.0', '1')]
    expected_rpm_order = [
        ('14.1.0', '1'),
        ('6.0.0', '1'),
        ('5.10.0', '1'),
        ('5.4.0', '1'),
    ]
    lex_order = sorted(versions, reverse=True)
    rpm_order = _sort_versions(versions, reverse=True)

    assert rpm_order == expected_rpm_order
    # And confirm the bug surface: lex order genuinely disagrees here.
    assert lex_order != expected_rpm_order


def test_sorted_handles_multi_segment_micro_versions():
    """``5.100 > 5.10 > 5.9 > 5.2`` numerically (lex would say the opposite)."""
    versions = [('5.10', '1'), ('5.2', '1'), ('5.9', '1'), ('5.100', '1')]
    expected = [('5.100', '1'), ('5.10', '1'), ('5.9', '1'), ('5.2', '1')]
    assert _sort_versions(versions, reverse=True) == expected


# ---------------------------------------------------------------------------
# End-to-end: find_old_kernels on a synthetic rpmdb
# ---------------------------------------------------------------------------


class _FakeHeader(dict):
    """Minimal stand-in for ``rpm.hdr``: indexable by ``RPMTAG_*`` ints."""

    def __getitem__(self, key):
        return super().__getitem__(key)


def _make_header(name, version, release, arch='x86_64', size=10):
    import rpm
    return _FakeHeader({
        rpm.RPMTAG_NAME: name,
        rpm.RPMTAG_VERSION: version,
        rpm.RPMTAG_RELEASE: release,
        rpm.RPMTAG_ARCH: arch,
        rpm.RPMTAG_SIZE: size,
    })


def _build_fake_ts(kernels_by_name):
    """Build a fake ``rpm.TransactionSet`` whose ``dbMatch`` returns our headers.

    ``kernels_by_name`` maps a kernel package name (e.g. ``'kernel-desktop'``)
    to a list of ``(version, release)`` tuples.
    """
    def db_match(_tag, name):
        return [
            _make_header(name, v, r)
            for v, r in kernels_by_name.get(name, [])
        ]

    fake_ts = MagicMock()
    fake_ts.dbMatch.side_effect = db_match
    return fake_ts


def test_find_old_kernels_keeps_highest_rpm_version_not_highest_lex():
    """Headline regression: the kernel kept must be ``14.x``, not ``6.x``.

    With ``kernel_keep=1`` and no running kernel matching any of these,
    the helper should keep exactly one version: the RPM-newest one.
    Under the broken lex sort, it would have kept ``6.0.0`` (because
    ``'6' > '1'`` as a string) and proposed removing the genuine
    newest, ``14.1.0``. That's the data-loss bug.
    """
    kernels = {
        'kernel-desktop': [
            ('5.4.0', '1.mga9'),
            ('5.10.0', '1.mga9'),
            ('6.0.0', '1.mga9'),
            ('14.1.0', '1.mga9'),
        ],
    }
    fake_ts = _build_fake_ts(kernels)

    # Running kernel: pin to something that matches none of the above so
    # ``is_running`` is False everywhere and ``keep_count`` alone decides.
    with patch('rpm.TransactionSet', return_value=fake_ts), \
         patch('os.uname') as uname:
        uname.return_value = MagicMock(release='99.0.0-1.mga9-desktop')
        to_remove = find_old_kernels(keep_count=1)

    removed_versions = {nevra for _, nevra, _ in to_remove}
    # The RPM-newest (14.1.0) must NOT be in the removal set.
    assert not any('14.1.0' in nevra for nevra in removed_versions), (
        f"14.1.0 was incorrectly listed for removal: {removed_versions}"
    )
    # All three older versions must be flagged.
    assert any('5.4.0' in n for n in removed_versions)
    assert any('5.10.0' in n for n in removed_versions)
    assert any('6.0.0' in n for n in removed_versions)


def test_find_old_kernels_keep_two_picks_two_rpm_newest():
    """With ``keep_count=2``, the two RPM-newest are kept; the rest removed."""
    kernels = {
        'kernel-desktop': [
            ('5.4.0', '1.mga9'),
            ('5.10.0', '1.mga9'),
            ('6.0.0', '1.mga9'),
            ('14.1.0', '1.mga9'),
        ],
    }
    fake_ts = _build_fake_ts(kernels)

    with patch('rpm.TransactionSet', return_value=fake_ts), \
         patch('os.uname') as uname:
        uname.return_value = MagicMock(release='99.0.0-1.mga9-desktop')
        to_remove = find_old_kernels(keep_count=2)

    removed = {nevra for _, nevra, _ in to_remove}
    # Kept: 14.1.0 and 6.0.0 (the two RPM-newest).
    assert not any('14.1.0' in n for n in removed)
    assert not any('6.0.0' in n for n in removed)
    # Removed: 5.10.0 and 5.4.0.
    assert any('5.10.0' in n for n in removed)
    assert any('5.4.0' in n for n in removed)


def test_find_old_kernels_protects_running_even_if_not_newest():
    """The running kernel is always kept, regardless of how old it is."""
    kernels = {
        'kernel-desktop': [
            ('5.4.0', '1.mga9'),   # running
            ('6.0.0', '1.mga9'),
            ('14.1.0', '1.mga9'),
        ],
    }
    fake_ts = _build_fake_ts(kernels)

    with patch('rpm.TransactionSet', return_value=fake_ts), \
         patch('os.uname') as uname:
        uname.return_value = MagicMock(release='5.4.0-1.mga9-desktop')
        to_remove = find_old_kernels(keep_count=1)

    removed = {nevra for _, nevra, _ in to_remove}
    # 5.4.0 is running -> kept. 14.1.0 is RPM-newest -> kept.
    # 6.0.0 should be the only one removed.
    assert not any('5.4.0' in n for n in removed)
    assert not any('14.1.0' in n for n in removed)
    assert any('6.0.0' in n for n in removed)
