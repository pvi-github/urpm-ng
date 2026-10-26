"""The schema the package declares has to be the one the code expects.

``urpm-ng-core`` declares ``Provides: urpm-db-schema = <n>``, and the
shipped rule ``70-db-schema.cfg`` watches that capability: when an
operation installs the package, a fresh interpreter migrates the
database once this urpm has exited (SPEC_POST_TRANSACTION_HOOKS §5.1).

The number lives in two files that nothing links at build time, so it
drifts in silence: bump ``SCHEMA_VERSION`` and forget the spec, and the
rule keeps firing on a stale capability; bump the spec alone and it
fires for a migration that does not exist.  Either way nothing fails
and nobody is told, which is what this test is for.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

from urpm.core.database import SCHEMA_VERSION
from urpm.core.hooks import Action, Moment

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "rpmbuild" / "SPECS" / "urpm-ng.spec"
RULE = ROOT / "data" / "usr" / "lib" / "urpm" / "hooks.d" / "70-db-schema.cfg"

CAPABILITY = "urpm-db-schema"


def _declared_in_spec() -> str:
    match = re.search(rf'^Provides:\s+{CAPABILITY}\s*=\s*(\S+)\s*$',
                      SPEC.read_text(encoding="utf-8"), re.M)
    assert match, f"{CAPABILITY} is not declared in {SPEC.name}"
    return match.group(1)


class TestTheCapability:
    def test_the_spec_declares_the_schema_the_code_expects(self):
        assert _declared_in_spec() == str(SCHEMA_VERSION)

    def test_it_is_declared_once(self):
        # Two declarations would let rpm pick either.
        found = re.findall(rf'^Provides:\s+{CAPABILITY}\s*=',
                           SPEC.read_text(encoding="utf-8"), re.M)
        assert len(found) == 1


class TestTheShippedRule:
    @pytest.fixture
    def rule(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(str(RULE), encoding="utf-8")
        return parser

    def test_it_watches_that_capability(self, rule):
        section = f"{Moment.AFTER_EXIT}:db-schema"
        assert section in rule.sections()
        assert rule[section]["watch"].strip() == CAPABILITY

    def test_it_asks_for_the_migration(self, rule):
        section = f"{Moment.AFTER_EXIT}:db-schema"
        assert rule[section]["action"].strip() == Action.MIGRATE_DB

    def test_the_moment_is_the_deferred_one(self, rule):
        # At the immediate moment the verb is refused at load time, so
        # a rule shipped under the wrong header would be dead weight.
        assert not any(s.startswith(f"{Moment.ON_COMPLETION}:")
                       for s in rule.sections())

    def test_the_rule_is_picked_up_by_the_spec(self):
        # The spec installs the whole directory by glob; this pins that
        # it still does, since a rule nobody installs does nothing.
        text = SPEC.read_text(encoding="utf-8")
        assert "data/usr/lib/urpm/hooks.d/*.cfg" in text
