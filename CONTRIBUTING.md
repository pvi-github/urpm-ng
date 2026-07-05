# Contributing to urpm-ng

urpm-ng is a small volunteer project (hopefully growing). A handful of maintainers, a tiny group of regular testers, and a lot to do. If you use Mageia and something here catches your eye, we would appreciate your help — even a five-minute "tried it, it broke at step X" is worth more than you might think.

This document is here to make it obvious *how* you can help, no matter your level of commitment. Nothing here assumes you have patched a distribution tool before.

## How you can help

Five paths, from the lightest to the heaviest. Pick whichever matches the time you have — none is second-class.

### 1. Try it and tell us what happens (the good and the bad)

The single most useful thing a newcomer can do. Install urpm-ng on your box (follow the [`README.md`](README.md) *Installation* section for the current RPM instructions), use it for a couple of days for whatever you normally use ``urpmi`` for, and report anything that surprised you — a crash, a wrong message, a missing translation, a workflow that felt clumsy, repetitive, or unnatural.

- Where to report: **GitHub issues** at <https://github.com/pvi-github/urpm-ng/issues>.
- Please include, at minimum:
  - The Mageia release (``cat /etc/mageia-release``).
  - The architecture (``uname -m``).
  - The urpm-ng version (``urpm --version`` — and ``rpm -q urpm-ng-core`` to confirm which RPM is installed and whether it is the system one).
  - The exact command line that misbehaved, what you got and what you expected instead.
- No need to attach logs unless we ask.

### 2. Translate — or polish existing translations

Six languages ship translated (fr / de / es / it / nl / pt). Coverage is broad but not complete: strings slip through untranslated, some msgstrs read stilted, and a native ear catches false friends an initial pass cannot. If one of those is your first language, a pass over the existing translations to smooth phrasing and adopt local idiomatic turns is highly welcome.

- Strings live in ``.po`` files under [`po/`](po/); open them in your editor of choice (poedit is fine).
- Empty or ``fuzzy`` entries are new / possibly-out-of-date strings — the easiest place to start.
- Run ``msgfmt --check-format po/<lang>.po -o /dev/null`` — if it passes, so does the build.
- Same story for docs: the canonical ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` have per-language siblings (``README_fr.md`` etc.); they too would benefit from a native re-read.

### 3. Improve the documentation

Man pages, the README, the migration cheat sheet, the changelog — anything that is prose. Even a typo fix is useful. Man pages live in ``man/<lang>/man1/urpm.1``; validate with ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Fix a bug or add a small feature

The backlog lives in two places:

- [`TODO.md`](TODO.md) at the repo root — the visible list.
- The various ``doc/TODO_*.md`` files — thematic backlogs and per-topic notes. Some are ready to code, some need discussion first. Ask before you invest a full weekend.

Read on for the build / test / patch workflow.

### 5. Join the plumbing (the heaviest lifting)

Refactors, resolver work, ``urpmd`` background jobs, spec-file work, mkimage / build container hardening. This is where the project's technical roadmap lives. Say hi first — coordinating avoids stepping on toes, or being stepped on. We don't bite, promise.

## Get the sources and build

Two build paths. The **simple** one uses ``bm`` (the ``build-mageia`` wrapper) on your host and needs only ``urpmi``. The **reproducible** one uses ``urpm build`` inside a container, and needs urpm-ng already installed.

### Bootstrap dependencies (once)

On a fresh Mageia box, ``urpmi`` is available but ``sudo`` may not be configured — the classic ``su -c`` form works everywhere:

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# The build tool (bm) plus every BuildRequires the spec declares.
# --buildrequires reads the spec directly, so the list stays in
# sync automatically.  bm itself is not in the spec's BuildRequires
# (it invokes rpmbuild rather than being consumed by %build), hence
# the two commands.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Simple path — ``bm`` on the host

```sh
make rpm-all
```

Then install the freshly built RPMs.

**First time — no urpm-ng on the system yet** — feed every RPM to ``urpmi`` in one shot (the version-release filter avoids picking up any older build still sitting in ``RPMS/``):

```sh
RPMS=$(find rpmbuild/RPMS rpmdrake/rpmbuild/RPMS \
            -name "*-$(cat VERSION)-$(cat RELEASE).*.rpm")
su -c "urpmi $RPMS"
```

**Subsequent iterations** — urpm-ng's resolver auto-scans the sibling directory for local RPMs (it reports "Found N sibling RPMs (available for dependencies)"), so pointing at the two meta packages is enough:

```sh
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Reproducible path — container build

Only usable once urpm-ng is installed on the host (chicken-and-egg on a very first install). It guarantees a clean, isolated build and lets you target other Mageia releases or architectures from a single workstation without touching the host.

```sh
# One-time: create the build image (example: mga10 on x86_64).
# The ``tag`` is the name you use to invoke this image in later
# builds — create several if you want to target multiple
# releases and/or architectures from one workstation.
su -c "urpm image make --release 10 --tag mga10-64"

# Every build after that — both urpm-ng and rpmdrake-ng specs
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Install — urpm-ng is already on the host here (prerequisite of
# this build path), so ``urpm i`` on the two meta packages is enough:
# the resolver auto-picks the sibling RPMs from the same directory.
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Run the tests

```sh
# Heads up: the full pytest takes a while — 30 to 60 minutes.
pytest urpm/tests/
```

See [`doc/TESTING.md`](doc/TESTING.md) for a pytest cheat sheet and known coverage gaps.

For dev-mode iteration without rebuilding a RPM every time, source files run directly from the checkout — ``python -m urpm.cli.main <subcommand>`` works with ``$PYTHONPATH`` including the checkout root.

## Your first contribution — the round trip

1. **Branch.** From the active version branch (currently ``0.8.x`` — check the ``VERSION`` file at the repo root if in doubt). ``main`` carries the released history only; new work never lands there directly, it fast-forward-merges from the version branch at release time.
2. **Change.** Write the fix or feature. If you are touching the resolver, the transaction queue, or ``urpmd``, adding a test in ``urpm/tests/`` is close to mandatory. For CLI or docs work, manual testing on your box is enough.
3. **Test locally.** Run ``pytest urpm/tests/`` (full suite for anything user-visible, targeted file otherwise). Fix any regression before continuing.
4. **Update the visible surface** if your change is user-facing (a bug fix on a code path rarely needs this):
   - update the ``.po`` catalogues (any new user-facing English string is a new msgid);
   - update ``man/<lang>/man1/urpm.1`` if a flag was added, renamed, or removed;
   - update the README / MIGRATION cheat sheet if the change affects everyday commands.

   The ``CHANGELOG.md`` entry itself is the maintainer's job at release time, not part of a PR.
5. **Commit.** Short subject (~50 chars), conventional prefix (``fix(area):``, ``feat(area):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). Body explains the *why* — the diff already shows the *what*.

Before you open a pull request, walk through this checklist:

- [ ] ``make rpm-all`` (or the container build) succeeds.
- [ ] ``pytest urpm/tests/`` passes with no regression.
- [ ] You have **installed your locally-built RPMs** and tested from that installed copy (bump the ``release`` line in ``rpmbuild/SPECS/urpm-ng.spec`` locally so the RPM number is higher than the system one and installs cleanly on top — this is a local convenience only, never commit that bump).
- [ ] The obvious smoke commands still work on the installed build, without the change you are proposing breaking any of them:
  - ``urpm i <somepackage>`` — install path
  - ``urpm q <somepackage>`` — query
  - ``urpm e <somepackage>`` — erase
  - ``urpm f /path/to/file`` — find
  - ``urpm m u`` — media update
  - ``urpm u`` — system upgrade
- [ ] Your branch is **rebased** on the target branch (no merge commits between your work and the tip).
- [ ] Docs / man pages / translations updated as per step 4.

6. **Push** to your fork or your branch.
7. **Open a pull request** on GitHub. Describe the intent, the test coverage, and any known limitation. Mention the release line you targeted and confirm the checklist above.
8. **Iterate on review.** A reviewer will look at your diff and ask questions or suggest tweaks. We aim for peer-level exchange — nothing personal, everything on the code. We try to phrase reviews kindly; if a comment ever misses the mark, the intent is never hostile — the project and Mageia are the compass.

## Where to reach us

- **Issues & PRs**: <https://github.com/pvi-github/urpm-ng>
- **Direct contact — Matrix**: [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Where the code lives

```
urpm/                  # Python source
  cli/                 # Command-line interface (urpm, subcommands)
  core/                # Resolver, download, install, database, sync
  daemon/              # urpmd (background service, LAN P2P)
  genmedia/            # Server-side media metadata generation
  tests/               # All tests live here (not in a top-level tests/)
rpmdrake/              # Qt6 GUI front-end (rpmdrake-ng)
pk-backend-urpm/       # C plugin: PackageKit backend on urpm-ng
man/<lang>/man1/       # Translated man pages
po/                    # Translation catalogues (.po)
doc/                   # Design docs, plans, TODOs, specs
rpmbuild/SPECS/        # Mageia packaging (.spec)
data/                  # systemd units, polkit rules, config templates
```

For a deeper map, see [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). For the cumulative feature catalogue, [`FEATURES.md`](FEATURES.md).

## Style expectations (short)

- **English** in code, comments, and commit messages. Mixed language in the history is disorienting.
- **Docstrings** on any public function or class. A one-liner is fine; explain the *why* only when it is not obvious from the name.
- **Tests** when practical — the suite is a regression net, not a formal proof. User-visible changes should ship with at least a manual test note.
- **Comments** where the code hides a surprise (workaround, race, invariant). Never a comment that duplicates the code.

## Release cycle

Work happens on a version branch (``0.8.x``, ``0.9.x``, …). When a version is ready, the branch is fast-forward-merged into ``main``; ``main`` therefore carries the released history. Tags are cut from ``main`` at that point and RPMs are published to the project's binary channel.

Version bumps in ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` are the maintainer's call — do not commit a bump in your contribution. That said, feel free to **locally** raise the ``release`` line in the spec so your built RPM installs on top of the system one; just do not stage that line.
