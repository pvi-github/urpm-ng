"""Two display defects on the same failed ``urpm autoremove``.

The screen said the work was done, then said it had failed::

    Suppression de 162 paquets...
      [162/162] terminé

    Échec de la suppression :
      Dependency: (('dhcp-client', '3:4.4.3P1', '7.mga10'),
                   ('dhcp-common', '3:4.4.3P1-7.mga10'), 8, None, 0)

The ``[162/162]`` line was not a progress bar reaching its end: it was
printed unconditionally from ``len(package_names)``, whatever the
transaction did.  rpm rejects at ``ts.check()``, before touching a
single package, so nothing had been removed at all.

The second line dumped the raw rpmlib tuple.  The project already had
a formatter for those, used by the install path; the erase path was
the only caller that did not use it.  Routing it through the *install*
adapter would have been wrong too: that one classifies against the
media and, with no database to consult, answers "for a reason we could
not determine" about the one case where the reason is certain.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from urpm.core.resolution.diagnose import (
    DepIssue,
    format_dependency_issue,
    from_rpmlib_erase_tuple,
)

#: The tuple rpm actually returned, verbatim from the report.
REPORTED = (('dhcp-client', '3:4.4.3P1', '7.mga10'),
            ('dhcp-common', '3:4.4.3P1-7.mga10'), 8, None, 0)


class TestTheDependencyMessage:

    def test_the_packages_are_named_not_dumped(self):
        out = format_dependency_issue(from_rpmlib_erase_tuple(REPORTED))
        assert 'dhcp-client-3:4.4.3P1-7.mga10' in out
        assert 'dhcp-common' in out
        assert '(' not in out.split('dhcp-common')[0], "tuple leaked"

    def test_the_version_constraint_is_decoded(self):
        """Flag 8 is RPMSENSE_EQUAL; the operator must be readable."""
        out = format_dependency_issue(from_rpmlib_erase_tuple(REPORTED))
        assert '= 3:4.4.3P1-7.mga10' in out

    def test_an_erase_does_not_claim_an_unknown_reason(self):
        """The erase adapter exists precisely so the message does not
        end in "for a reason we could not determine" when the reason is
        that we are about to delete the provider."""
        issue = from_rpmlib_erase_tuple(REPORTED)
        assert issue.kind == 'being_removed'

    def test_an_unexpected_tuple_shape_still_says_something(self):
        """A future rpm may change the shape.  An ugly message beats a
        traceback, and beats losing the diagnosis."""
        issue = from_rpmlib_erase_tuple(('unexpected',))
        assert issue.kind == 'unknown'
        assert format_dependency_issue(issue)

    def test_a_conflict_is_not_phrased_as_a_requirement(self):
        issue = DepIssue(kind='being_removed', dep_name='foo',
                         sense_label='conflicts')
        assert 'conflict' in format_dependency_issue(issue).lower()


class TestTheCompletionLine:
    """Source-level: the six call sites must gate the line on success.

    Driving a real rpm transaction to its ts.check() rejection needs
    root and a crafted rpmdb; what actually regressed here is the
    control flow, and that is what this pins.
    """

    SITES = [
        ('cleanup.py', 3),
        ('history.py', 3),
    ]

    @pytest.mark.parametrize('filename,expected', SITES)
    def test_every_done_line_is_gated_on_success(self, filename, expected):
        path = (pathlib.Path(__file__).resolve().parent.parent
                / 'cli' / 'commands' / filename)
        src = path.read_text(encoding='utf-8')

        gated = re.findall(
            r'if (\w+)\.success:\n\s+print\(f"  \[\{[^}]+\}/\{[^}]+\}\] "',
            src)
        assert len(gated) == expected, (
            f'{filename}: {len(gated)} gated completion lines, '
            f'expected {expected}')

        ungated = re.findall(
            r'^\s+print\(f"\\r\\033\[K  \[\{len\(', src, re.MULTILINE)
        assert not ungated, (
            f'{filename}: {len(ungated)} completion line(s) still printed '
            'unconditionally')
