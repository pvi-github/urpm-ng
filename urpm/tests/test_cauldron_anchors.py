"""Targeting cauldron must not hinge on the target's *name*.

`urpm distupgrade --to cauldron` from a mga10 refused all ten Tx A
anchors on a plan that contained every one of them, after five minutes
and 2.2 GB of download.  The check built its expected release marker as
``f"mga{identity}"`` and looked for it in each solvable's EVR:

    marker = f"mga{target_identity}"   # -> "mgacauldron"

`mgacauldron` does not exist and never will.  Cauldron packages are
tagged `.mga11`.  The documented freeze form `--to cauldron:11` fared
no better: the call site passed ``version_to.split(":", 1)[0]``, which
keeps the identity and throws away the numeric — the one field
`ReleaseIdentity` documents as « the concrete integer version rendered
into `.mgaN` release tags ».

Keying on the numeric alone would still have been wrong.  A cauldron
repository ships `.mga10` and `.mga11` side by side: whatever has not
been rebuilt keeps its previous tag, and is served by the target media
all the same.  A disttag says which release a package was *built* for,
never which medium it *came from*.

A second refusal followed the first fix: four anchors the target ships
at the version already installed, so the plan holds no action for them.
`rpm-helper` is a noarch bag of shell macros, identical in mga10 and
cauldron.  Nothing to do is not the same as nothing there.

The question worth asking is neither « does it carry the target tag »
nor « is it in the plan », but « will it be on disk once Tx A commits »
— installed by it, or already there and left alone.  No version name
enters into it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

solv = pytest.importorskip("solv")

from urpm.core.distupgrade.manifest import (  # noqa: E402
    which_anchors_available as _which_anchors_available,
)
from urpm.core.distupgrade.stage1 import (  # noqa: E402
    _try_transpose_string,
)

ANCHORS = ("rpm", "python3", "glibc")


class Pool:
    """A pool with a target medium and an installed set.

    Small enough to build per test, real enough that the check runs
    against libsolv rather than against a stand-in for it.
    """

    def __init__(self):
        self.pool = solv.Pool()
        self.pool.setarch("x86_64")
        self.media = self.pool.add_repo("Core Release")
        self.installed = self.pool.add_repo("@System")
        self.pool.installed = self.installed

    def add(self, repo, name, evr):
        s = repo.add_solvable()
        s.name = name
        s.evr = evr
        s.arch = "x86_64"
        s.add_deparray(solv.SOLVABLE_PROVIDES,
                       self.pool.Dep(name).Rel(solv.REL_EQ,
                                               self.pool.Dep(evr)))
        return s

    def resolver(self):
        """Freeze the pool and build its provides index.

        ``whatprovides`` reads an index libsolv only builds on demand :
        calling it on a pool that was never internalized segfaults
        rather than returning nothing.
        """
        self.media.internalize()
        self.installed.internalize()
        self.pool.createwhatprovides()
        return SimpleNamespace(pool=self.pool)


def _action(solvable):
    return SimpleNamespace(solvable_id=solvable.id, name=solvable.name)


class TestACauldronPlanIsAccepted:
    """The reported failure, and the two forms it took."""

    def test_mga11_packages_satisfy_a_cauldron_target(self):
        """`--to cauldron` : the plan is full of `.mga11`, and the
        target is not called 11."""
        pool = Pool()
        actions = [_action(pool.add(pool.media, name, "1.0-1.mga11"))
                   for name in ANCHORS]
        found = _which_anchors_available(actions, resolver=pool.resolver(),
                                       anchors=ANCHORS)
        assert all(found.values()), f"refused: {found}"

    def test_a_mixed_cauldron_repository_is_accepted(self):
        """`.mga10` and `.mga11` coexist in cauldron.  An anchor the
        target media still serve as `.mga10` is not a stale leftover —
        it is simply one that has not been rebuilt yet."""
        pool = Pool()
        actions = [
            _action(pool.add(pool.media, "rpm", "1.0-1.mga11")),
            _action(pool.add(pool.media, "python3", "1.0-1.mga10")),
            _action(pool.add(pool.media, "glibc", "1.0-1.mga11")),
        ]
        found = _which_anchors_available(actions, resolver=pool.resolver(),
                                       anchors=ANCHORS)
        assert all(found.values()), f"refused: {found}"

    def test_an_unchanged_anchor_is_not_missing(self):
        """The second refusal, reported after the first fix landed :
        `rpm-helper` is a noarch bag of shell macros, identical in
        mga10 and cauldron, so the target release produces no action
        for it.  Nothing to do is not the same as nothing there.

        Three python modules were in the same situation.  Four false
        refusals, after 2.2 GB of download.
        """
        pool = Pool()
        for name in ("rpm-helper", "python3-curl", "python3-zstandard"):
            pool.add(pool.installed, name, "1.0-1.mga10")
        actions = [_action(pool.add(pool.media, "rpm", "2.0-1.mga11"))]
        found = _which_anchors_available(
            actions, resolver=pool.resolver(),
            anchors=("rpm", "rpm-helper", "python3-curl",
                     "python3-zstandard"))
        assert all(found.values()), f"refused: {found}"

    def test_an_anchor_is_found_through_its_provides(self):
        """Anchors are Provides, not names : Mageia ships
        `python3-pyyaml` inside the package called `python3-yaml`."""
        pool = Pool()
        s = pool.add(pool.installed, "python3-yaml", "6.0.3-3.mga10")
        s.add_deparray(solv.SOLVABLE_PROVIDES,
                       pool.pool.Dep("python3-pyyaml"))
        actions = [_action(pool.add(pool.media, "rpm", "2.0-1.mga11"))]
        found = _which_anchors_available(actions, resolver=pool.resolver(),
                                         anchors=("python3-pyyaml",))
        assert found["python3-pyyaml"]

    def test_no_version_name_reaches_the_check(self):
        """The signature carries no target identity at all : there is
        nothing left to pass wrongly."""
        import inspect
        params = inspect.signature(_which_anchors_available).parameters
        assert "target_identity" not in params
        assert "version_to" not in params


class TestWhatItStillCatches:
    """Dropping the disttag must not drop the protection."""

    def test_an_anchor_provided_by_nobody_is_missing(self):
        """Neither in the plan nor on the machine : Tx A commits, execvp
        restarts, and the module is not there."""
        pool = Pool()
        actions = [_action(pool.add(pool.media, "rpm", "1.0-1.mga11"))]
        found = _which_anchors_available(actions, resolver=pool.resolver(),
                                         anchors=ANCHORS)
        assert found["rpm"]
        assert not found["python3"]
        assert not found["glibc"]

    def test_an_anchor_the_plan_removes_is_missing(self):
        """Installed, but on its way out and with no replacement."""
        pool = Pool()
        pool.add(pool.installed, "glibc", "1.0-1.mga10")
        actions = [_action(pool.add(pool.media, "rpm", "1.0-1.mga11"))]
        found = _which_anchors_available(actions, resolver=pool.resolver(),
                                         anchors=("glibc",),
                                         erased_names=["glibc"])
        assert not found["glibc"]

    def test_a_medium_build_satisfies_an_upgraded_anchor(self):
        """Both are in the pool under the same name ; the plan points at
        the medium one."""
        pool = Pool()
        pool.add(pool.installed, "rpm", "1.0-1.mga10")
        actions = [_action(pool.add(pool.media, "rpm", "2.0-1.mga11"))]
        found = _which_anchors_available(actions, resolver=pool.resolver(),
                                         anchors=("rpm",))
        assert found["rpm"]


class TestTransposeTellsNumbersFromNames:
    """`cauldron` is a legitimate URL segment and never a disttag.  The
    same string is right in one position and impossible in the other.
    """

    def test_a_glued_tag_does_not_transpose_to_a_name(self):
        assert _try_transpose_string("media/mga10/core", "10",
                                     "cauldron") is None

    def test_a_glued_prefix_does_not_transpose_to_a_name(self):
        """No mirror serves `mageiacauldron/`."""
        assert _try_transpose_string("mageia9/free/x86_64", "9",
                                     "cauldron") is None

    def test_a_standalone_segment_does(self):
        """Mirrors do serve `/cauldron/x86_64/…`."""
        assert _try_transpose_string("/mnt/mgabiz/10/x86_64", "10",
                                     "cauldron") == "/mnt/mgabiz/cauldron/x86_64"

    def test_numeric_targets_are_untouched(self):
        assert _try_transpose_string("media/mga10/core", "10",
                                     "11") == "media/mga11/core"
        assert _try_transpose_string("mageia9/free", "9",
                                     "10") == "mageia10/free"
