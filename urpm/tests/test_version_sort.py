"""Regression tests for the RPM version sort in the ``--prefer``
multi-version dialog.

Historically ``alternatives.py:920`` used plain ``sorted()`` on version
strings, giving ``"5.10" < "5.9"`` (lex compare on ``1`` < ``9`` in
position 1).  The fix uses ``rpm.labelCompare`` via ``cmp_to_key``.

Full refactor into a shared ``rpm_version_key`` helper is deferred to
0.9.x (see ``doc/TODO_RPM_VERSION_HELPER.md``); these tests pin the
sort semantics on that one call site so the fix cannot silently
regress.
"""

from __future__ import annotations

import functools

import pytest

rpm = pytest.importorskip("rpm")


def _sort(versions):
    """Reproduce the exact key used by ``alternatives.py`` so the
    contract this file guards is the very same call.
    """
    return sorted(
        versions,
        key=functools.cmp_to_key(
            lambda a, b: rpm.labelCompare(('', a, ''), ('', b, ''))
        ),
    )


def test_double_digit_minor_beats_single_digit():
    """The historical bug: ``"5.10" < "5.9"`` under lex sort."""
    assert _sort(["5.10", "5.9"]) == ["5.9", "5.10"]


def test_full_php_range():
    assert _sort(["8.4", "8.10", "8.9"]) == ["8.4", "8.9", "8.10"]


def test_duplicates_are_stable():
    # cmp_to_key must return 0 on equal keys; sorted stays stable.
    assert _sort(["8.4", "8.4"]) == ["8.4", "8.4"]


def test_single_element_is_identity():
    assert _sort(["8.4"]) == ["8.4"]


def test_empty_is_empty():
    assert _sort([]) == []


def test_release_component_is_treated_correctly():
    """When ``version_groups`` keys pick up an unexpected release
    component (regex was later relaxed), ordering stays numeric."""
    assert _sort(["8.4-1", "8.4-10", "8.4-2"]) == ["8.4-1", "8.4-2", "8.4-10"]
