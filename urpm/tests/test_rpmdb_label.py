"""Finding an installed package from any spelling rpm itself prints.

``rpm -q firefox`` answers ``firefox-153.3.0-1.mga10`` on Mageia, with
no architecture, and ``rpm -qa --qf '%{NEVRA}\\n'`` adds one.  An
operator pastes whichever their last command produced, so every one of
those spellings has to name the package it designates.

Nothing here parses the operator's string, and that is deliberate:
Mageia ships Names shaped exactly like a name-version-release.
``kernel-desktop-devel-6.18.22-1.mga10``, ``lua-rpm-macros-1`` and
``xmltex-20020625`` are package Names, so no syntactic rule can tell
them from a versioned spelling.  rpm owns that arbitration in its
``RPMDBI_LABEL`` index, which is what ``rpm -q`` looks through, and
asking it is both exact and one indexed lookup rather than a walk.

The fixtures are derived from the machine's own database rather than
naming a package, so the properties hold wherever the suite runs.
"""

from __future__ import annotations

import shutil

import pytest

from urpm.core import rpmdb

pytestmark = pytest.mark.skipif(
    shutil.which("rpm") is None,
    reason="no rpm on this machine",
)


def _a_real_package() -> rpmdb.InstalledPkg:
    """One installed build with a version and an architecture.

    ``filesystem`` first because every Mageia install has it, then any
    package that is not one of rpm's own pseudo-entries: a
    ``gpg-pubkey`` row has no architecture and no release worth the
    name, so it would exercise nothing.
    """
    for name in ("filesystem", "rpm", "glibc"):
        found = rpmdb.query_by_name(name)
        if found:
            return found[0]
    for name in rpmdb.list_installed_names():
        if name.startswith("gpg-pubkey"):
            continue
        found = rpmdb.query_by_name(name)
        if found and found[0].version and found[0].arch:
            return found[0]
    pytest.skip("no usable installed package found")


PKG = _a_real_package()


def _names(hits) -> set:
    return {pkg.name for pkg in hits}


class TestTheSpellingsOfAnInstalledPackage:
    def test_the_plain_name(self):
        assert _names(rpmdb.query_by_label(PKG.name)) == {PKG.name}

    def test_what_rpm_q_prints(self):
        # name-version-release, no architecture.  This is the spelling
        # the CLI used to reject, and the one an operator pastes most.
        label = f"{PKG.name}-{PKG.version}-{PKG.release}"

        assert _names(rpmdb.query_by_label(label)) == {PKG.name}

    def test_the_full_nevra(self):
        assert _names(rpmdb.query_by_label(PKG.nevra)) == {PKG.name}

    def test_the_epoch_spelling(self):
        # ``evr`` carries the epoch when the header has one; rpm prints
        # it in some queries and omits it in others, and both have to
        # reach the same package.
        label = f"{PKG.name}-{PKG.evr}"

        assert _names(rpmdb.query_by_label(label)) == {PKG.name}


class TestWhatMustNotResolve:
    def test_a_name_nobody_installed(self):
        assert rpmdb.query_by_label("no-such-package-anywhere") == []

    def test_a_build_that_is_not_the_installed_one(self):
        # Naming a version that is not on the machine has to come back
        # empty: acting on another build instead would be silent and
        # wrong.
        label = f"{PKG.name}-0.0.0-0.mga1"

        assert rpmdb.query_by_label(label) == []

    def test_an_empty_label_is_answered_not_raised(self):
        assert rpmdb.query_by_label("") == []


class TestANameShapedLikeAVersion:
    """The property that makes the whole approach necessary."""

    def test_no_installed_name_is_ever_misread(self):
        # Every installed Name that could pass for a
        # name-version-release is looked up as it is written.  Each one
        # must come back as itself, never as the package a syntactic
        # split would have produced.
        suspects = [name for name in rpmdb.list_installed_names()
                    if name.count("-") >= 2 and not name.startswith("gpg-")]
        if not suspects:
            pytest.skip("no hyphenated name on this machine")

        misread = []
        for name in suspects:
            hits = _names(rpmdb.query_by_label(name))
            if hits != {name}:
                misread.append((name, sorted(hits)))

        assert not misread, f"these Names resolved elsewhere: {misread[:5]}"


class TestResolvingWhatTheOperatorTyped:
    """``resolve_installed`` is what the commands consume: one Name,
    or nothing."""

    def test_a_versioned_spelling_gives_the_name(self):
        label = f"{PKG.name}-{PKG.version}-{PKG.release}"

        assert rpmdb.resolve_installed(label) == PKG.name

    def test_a_name_in_the_wrong_case_is_still_understood(self):
        # rpm's index is case-sensitive and Names are too (``perl-Git``
        # against ``perl-git``), but an operator typing by hand should
        # not have to care.  The answer comes back in the stored
        # casing, which is what the rest of the CLI needs.
        assert rpmdb.resolve_installed(PKG.name.upper()) == PKG.name

    def test_the_tolerance_on_case_covers_names_only(self):
        # Recorded rather than discovered: a versioned spelling has to
        # be in rpm's own casing, because that tolerance is rpm's
        # index and not ours.  Nobody types those by hand; they are
        # pasted from the output that produced them.
        label = f"{PKG.name}-{PKG.version}-{PKG.release}".upper()

        assert rpmdb.resolve_installed(label) is None

    def test_an_unknown_token_gives_nothing(self):
        assert rpmdb.resolve_installed("no-such-package-anywhere") is None

    def test_an_empty_token_is_answered_not_raised(self):
        assert rpmdb.resolve_installed("") is None


class TestWhatTheCommandsConsume:
    """The seam between a command and the database.

    ``urpm mark`` keeps the spec the operator typed next to the Name it
    resolved to, so a spec that names nothing can be reported as typed
    while the others are acted on under their real name.
    """

    def test_each_spec_is_paired_with_its_name(self):
        from urpm.cli.commands.cleanup import _resolve_installed_specs

        label = f"{PKG.name}-{PKG.version}-{PKG.release}"
        paired = _resolve_installed_specs([label, "no-such-package-anywhere"])

        assert paired == [(label, PKG.name),
                          ("no-such-package-anywhere", None)]

    def test_nothing_to_resolve_is_an_empty_list(self):
        from urpm.cli.commands.cleanup import _resolve_installed_specs

        assert _resolve_installed_specs([]) == []


class TestSeveralBuildsOfOneName:
    def test_a_name_with_two_builds_returns_both(self):
        # The kernel case.  Asking by Name is asking about the package,
        # so every installed build of it answers; asking by a precise
        # spelling narrows to one.
        # ``gpg-pubkey`` is excluded on purpose: rpm keeps the trusted
        # keys in the database as packages with no architecture and a
        # hexadecimal version, so it has many "builds" and none of
        # them has a NEVRA to narrow with.
        by_name = {}
        for name in rpmdb.list_installed_names():
            if name.startswith("gpg-pubkey"):
                continue
            builds = rpmdb.query_by_name(name)
            if len(builds) > 1 and all(b.arch for b in builds):
                by_name[name] = builds
                break
        if not by_name:
            pytest.skip("no multi-build package installed")

        name, builds = next(iter(by_name.items()))
        assert len(rpmdb.query_by_label(name)) == len(builds)
        one = builds[0]
        narrowed = rpmdb.query_by_label(
            f"{one.name}-{one.version}-{one.release}.{one.arch}")
        assert len(narrowed) == 1
