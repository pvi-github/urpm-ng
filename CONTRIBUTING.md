# Contributing to urpm-ng

Thanks for taking the time to read this!  urpm-ng aims to be the Python
replacement for the legacy `urpmi` toolchain on Mageia, and any
improvement — fix, feature, doc, translation, test — is welcome.

This is a community project staffed by volunteers.  The tone in our
reviews and discussions is *peer-to-peer*: we point at code, not at
people; we phrase suggestions, not commands; we ask honest questions
rather than rhetorical ones.

> Translations of this document are kept in
> [`CONTRIBUTING_de.md`](CONTRIBUTING_de.md),
> [`CONTRIBUTING_es.md`](CONTRIBUTING_es.md),
> [`CONTRIBUTING_fr.md`](CONTRIBUTING_fr.md),
> [`CONTRIBUTING_it.md`](CONTRIBUTING_it.md),
> [`CONTRIBUTING_nl.md`](CONTRIBUTING_nl.md), and
> [`CONTRIBUTING_pt.md`](CONTRIBUTING_pt.md).

## Project goals

- **Readable** code: explicit names, short functions, clear
  responsibilities.
- **Documented**: docstrings on all public API, comments where the *why*
  is not obvious.
- **Easy to onboard**: a new contributor should be able to understand a
  module without reading the whole project first.
- **Consistent**: uniform conventions across the codebase.
- **Performant**: quality and performance are not opposites — we aim
  for both.

## How to set up

You need:

- Python 3.13 or newer
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (for the test fixtures and the build pipeline)

A working checkout:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

See [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) for the pytest cheat
sheet, and [`doc/TESTING.md`](doc/TESTING.md) for the coverage status
and known gaps.

## Workflow

1. Branch from `main` (or from the active release branch if there is
   one — e.g. `0.7.x` was active until release 0.7.15, `0.8.x` is the
   one being prepared at the time of writing).
2. Write the change with tests when possible.
3. Run the relevant tests locally.  Manual testing is required for
   anything user-visible — the suite is a regression net, not a
   functional guarantee.
4. Open a pull request describing what the change does *and why*.

## Commit conventions

- **Language**: English.  Mixed-language commit messages confuse the
  history.
- **Subject line**: ≤ 50 characters, conventional-commit prefix
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Body**: explain the *why*, not just the *what*.  The diff already
  shows the what.
- **No AI attribution**: the commit hook rejects `Co-Authored-By` lines
  that name AI assistants.  The author of the commit is the human who
  pressed `git commit`.

## Git hygiene rules

- Never run `git add .` or `git add -A`.  Add files one by one — it
  prevents accidentally committing a `.env`, a draft, or a generated
  artefact.
- Always run `git status` before committing so the staged file list is
  visible.
- Never commit without explicit confirmation from the reviewer or
  yourself after a `git status` check.
- Never skip the commit hooks.  If a hook complains, fix the underlying
  cause rather than `--no-verify`.

## What not to commit

- **Drafts** in the repo root or under `doc/`.  Working drafts
  (`RELEASE_NOTE.md`, work-in-progress `doc/TODO_*.md`, `SPEC_*.md`
  before it has been validated) live in your local checkout and are
  promoted to the worktree only when validated.
- **Local test artefacts**: `essais/` is gitignored — drop transient
  scripts, smoke-test working directories, `mktemp` outputs there.
  Never at the repo root.
- **Anything labelled `bin/rebuild.sh` or `bin/recup.sh`** — these are
  contributor-personal scripts that never belong in upstream.
- **DNF vocabulary**: urpm-ng targets the Mageia ecosystem.  Comparing
  to DNF in a doc footnote is fine, but avoid borrowing DNF command
  names (`skip-broken`, `distro-sync`, ...) for our own flags.
- **Generated files**: `.mo` catalogues, `__pycache__/`, `*.pyc`,
  Sphinx builds — all gitignored, please leave that way.

## Documentation conventions

- **Languages**: code, comments, and commit messages in English.  User
  documentation under `doc/` is mixed French/English (status quo);
  user-facing strings translated through `po/` and man pages live in
  `man/<lang>/`.
- **Tone**: pedagogical and exploitable.  A report must be readable by
  someone who did not follow the conversation.  Avoid telegraphic
  tables with cryptic codes (C1/N7) unless every cell is explained.
- **Length is not a virtue**, but neither is brevity at the cost of
  clarity.  Each paragraph should give the reader something they
  could not deduce on their own.

## Tests

- Tests live in `urpm/tests/`, **not** in a top-level `tests/`
  directory.  Synthesize RPMs with
  `urpm/tests/gen_test_rpms.py` and run pytest from
  `urpm/tests/` (the test infrastructure expects that working
  directory for some integration tests).
- Per-test temporary directories are now cleaned by an autouse fixture
  on `BaseUrpmiTest`, so a test that fails before its explicit cleanup
  no longer leaks `/tmp` entries.
- For changes to man pages, validate with
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` — pre-existing
  UTF-8 warnings are not blocking but new errors are.

## Code review

We review each other as peers.  When you receive a review:

- The reviewer is pointing at code, not at you.
- "Why did you do X?" is a real question; default to an honest answer,
  not a defence.
- "Could we do Y instead?" is a proposal, not a verdict.

When you give a review:

- Self-deprecation is fine, prescription is not.  Suggest, do not
  dictate.
- Ask before you assume.  "I might be missing something, but ..." is a
  good opener.
- Mark blockers clearly; let style nitpicks be style nitpicks.

## Releasing

Release work happens on a version branch (`0.7.x`, `0.8.x`, ...) and
fast-forward-merges to `main` at the release point.  `main` carries
the released history; the active branch carries in-progress work.

`urpm/__init__.py` and `pyproject.toml` are bumped together when a
release is tagged.  Never decide on a version number unilaterally —
ask the project owner.

## Where things live

```
urpm/                  # Source
  cli/                 # Command-line interface
  core/                # Resolver, database, download, install...
  daemon/              # urpmd
  genmedia/            # Server-side media generation (urpm genmedia)
  tests/               # All tests live here, NOT in /tests/
rpmdrake/              # The GUI front-end
man/<lang>/man1/       # Translated man pages
po/                    # Translation catalogues (per-language .po)
doc/                   # Design docs, plans, TODO files
rpmbuild/SPECS/        # Mageia packaging .spec files
data/                  # systemd units, polkit rules, etc.
```

For the cumulative catalogue of features urpm-ng provides, see
[`FEATURES.md`](FEATURES.md).  For the per-release history, see
[`CHANGELOG.md`](CHANGELOG.md).  For backlogs, see
[`TODO.md`](TODO.md) and the per-topic files under
[`doc/TODO_*.md`](doc/).
