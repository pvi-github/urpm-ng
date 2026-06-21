# TODO — README translations (`README_<lang>.md`)

The English `README.md` at the repo root has reached ~1300 lines and
covers the entire command surface of urpm-ng (CLI commands, options,
configuration, examples).  Translating it into the seven languages
the project already maintains for man pages and `.po` catalogues is a
substantial but worthwhile chunk of work.

## Target file set

```
README.md       # English source (reference, ~1300 lines today)
README_de.md    # German
README_es.md    # Spanish
README_fr.md    # French
README_it.md    # Italian
README_nl.md    # Dutch
README_pt.md    # Portuguese (European)
```

Convention identical to the `CONTRIBUTING.md` family delivered on
0.8.x: the English file stays the authoritative reference; each
`README_<lang>.md` is a faithful translation that mirrors structure
and section ordering one-to-one.

## Status

- [ ] README_de.md
- [ ] README_es.md
- [ ] README_fr.md
- [ ] README_it.md
- [ ] README_nl.md
- [ ] README_pt.md

(Untranslated state at the moment of writing this TODO.)

## Translation guidelines

Carry over the rules already applied to the `CONTRIBUTING_*.md` files
and the man pages:

- **Preserve all proper names verbatim**: `urpm`, `urpmd`, `urpm-ng`,
  `Mageia`, `Python`, `pytest`, `git`, command names (`install`,
  `upgrade`, `genmedia`, `appstream`, ...), option names (`--auto`,
  `--without-recommends`, ...), file names (`hdlist.cz`, `synthesis`,
  `pyproject.toml`, ...), URLs.
- **Preserve all code blocks unchanged** — shell commands, sample
  configuration, sample output stay in the source language they were
  originally in (English/Bash).
- **No DNF vocabulary** in the translations (a recurring project
  rule).  If the English README mentions DNF for context, translate
  carefully and avoid borrowing DNF-derived command names for new
  options.
- **Tone**: descriptive and pedagogical, not telegraphic.  The
  README is an entry point for a user discovering urpm-ng; it should
  read like a tour, not a reference grid.

## Suggested batching

Splitting the work into chunks survives interruption better than a
single monolithic translation pass:

1. **Header + "Prerequisites" + "Installation" + "Configuration"
   + "Global Options" + "Display Options"** — the first ~200 lines.
   Covers the entry-point material a new user reads first.
2. **"Package Management" + "Search and Query"** — the bulk of the
   CLI documentation; longest section.
3. **"Media Management" + "Server Management" + "Peer Management"
   + "Cache Management"** — the infrastructure-oriented chapters.
4. **"GPG Keys" + "Build Dependencies" + "Container Build System"**
   — packaging-oriented chapters.
5. **"AppStream metadata" + "Media generation (urpm genmedia)" +
   "Package README messages" + "Orphan Cleanup"** — newer features
   added on 0.7.x / 0.8.x.
6. **"API Endpoints" + "Scheduled Tasks" + "P2P Package Sharing"**
   + remaining sections.

Each batch is ~150-250 lines, translatable in one focused pass.

## Cross-reference policy

When `README_<lang>.md` is created, every other `README_<lang>.md`
should be updated to list it in the cross-reference block at the
top, the same way `CONTRIBUTING_<lang>.md` files do.  Use the same
formatting for consistency.

## When this lands

The first 0.8.x release ships with the README only in English.  The
translation effort tracks separately from the release schedule — a
language can be added at any time as a discrete commit, and a
translation getting stale is not a release blocker (the English
remains the source of truth).
