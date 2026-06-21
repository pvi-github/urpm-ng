# Changelog

All notable changes to urpm-ng are recorded here, version by version.

The format is loosely inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/):
each entry lists the dated release, a short summary, and bullet sub-sections
for major features / improvements / bug fixes / packaging / documentation.

For the cumulative catalogue of features available in urpm-ng — not the
release-by-release history — see [`FEATURES.md`](FEATURES.md).

For an active backlog of what is in progress or planned, see
[`TODO.md`](TODO.md) and the per-topic files under
[`doc/TODO_*.md`](doc/).

---

## [0.8.0] — *unreleased*

The first 0.8.x release brings the genmedia subsystem on board:
a Python rewrite of `genhdlist3` integrated into urpm-ng as
`urpm genmedia`, plus the AppStream extraction and filtering
pipeline that goes with it.  Several review fixes ship alongside.

### Major Features

- **`urpm genmedia`** — generates full media metadata (hdlist.cz,
  synthesis.hdlist.cz, files.xml.lzma, info.xml.lzma,
  changelog.xml.lzma, MD5SUM) from a directory of RPMs. Packaged
  separately as `urpm-ng-genmedia` to keep the base client lean.
- **AppStream extraction** — picks up embedded `metainfo.xml`
  shipped by upstream applications, falls back to a minimal
  component derived from RPM header fields when missing,
  structurally filters out packages whose content is entirely
  non-user-facing (devel headers, debug symbols, static libs,
  pure runtime libraries) so they no longer pollute GNOME
  Software / Discover under a generic `System` category.

### Bug Fixes (review of papoteur's genmedia integration)

- `extract_from_rpm` opens the real RPM file path (not the
  `cache_dir / basename` it formerly used), so RPMs that ship
  an embedded `metainfo.xml` actually have their content
  extracted (`52fa8ad`).
- The scanner no longer pre-escapes `license`, `url`,
  `sourcerpm` and changelog fields — the writer in `files_xml`
  already handles escaping, the double pass was producing
  `&amp;amp;` for any URL containing an ampersand (`ec06bd0`).
- `AppStreamManager` filters non-user-facing packages
  structurally instead of emitting a fallback `System`
  component (`1eb8c3b`).
- DNF references purged from the documentation, source comments,
  and translatable strings — the Mageia ecosystem stands on its
  own vocabulary.

### Tests

- An `autouse` pytest fixture on `BaseUrpmiTest` guarantees the
  per-test tmpdir is cleaned even when a test raises before its
  explicit cleanup; the 12 deterministic leaks per full
  `test_install.py` run are gone (`aca26ca`).

### Documentation

- `README.md` adds a `Media generation (urpm genmedia)` section.
- `doc/ROADMAP.md` lists genmedia under the shipped features.
- `doc/TESTING.md` aligns with the actual `urpm/tests/` layout
  and gives an honest assessment of remaining coverage gaps.
- `doc/TODO_DASHBOARD.md` is now an index into the thematic
  TODO files instead of a duplicated tracker.

**Full Changelog**: https://github.com/pvi-github/urpm-ng/compare/0.7.15...0.8.0

---

## [0.7.15] — 2026-06-12

Mirror handling grows teeth: corrupt cached RPMs retry across distinct
mirrors, signature failures quarantine the offending mirror, and a
sliding-window reputation score reorders the pool.  Media display
names stop showing `mga10-common_release` artefacts.  The install /
upgrade / download stack gets a deep modernisation pass.  `urpm build`
adds `--subrel` and `--rpmmacros` for third-party builders.

### Major Features

- **Trustworthy mirror pipeline** (bug #3, full plan: iterations A + B)
  - Cached RPMs that fail signature or structural verification retry
    across up to `[download] max_retries` (default 3) distinct mirrors,
    with a cheap preflight (size + 4-byte RPM magic) catching empty
    bodies and HTML-error-pages-served-as-RPM before rpmlib.
  - Signature failures auto-blacklist the serving server; reactivation
    requires explicit `urpm server unblacklist` after manual GPG /
    source verification — no time-based auto-unblock.
  - Sliding 24h reputation score (baseline 100) drains on corrupt
    bodies, HTTP 4xx/5xx, network errors and slow transfers; the
    mirror selector orders the pool by score without excluding outright.
  - `cache_files.served_by_server_id` provenance survives restarts:
    the retry loop excludes the bad mirror on the FIRST attempt.
  - New CLI: `urpm server status` / `unblacklist` / `ack-blacklist`;
    `server list` flags blacklisted servers in red.
  - Persistent red banner at install / upgrade / media-update entry
    naming every unacknowledged blacklist, with reactivation
    instructions.
  - Schema v29 → v30; ~50 new tests covering migration, scoring,
    blacklist lifecycle, provenance routing and the preflight loop.

- **Daemon scheduler** — opt-out knobs for automatic media traffic
  - Five new `[daemon]` options: `auto_update_metadata`,
    `auto_predownload`, `auto_replication`,
    `auto_fetch_server_dates`, `metadata_interval`.
  - Manual `urpm media update` is unaffected by any knob.

- **`urpm build`** — `--subrel` and `--rpmmacros` for third-party
  builders
  - `--subrel TAG` injects `%subrel TAG` so Mageia `%mkrel`-using specs
    produce `NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm`.
  - `--rpmmacros FILE` drops FILE as `/root/.rpmmacros` inside the
    build container to override `%packager` / `%vendor` / `%dist`
    without touching the spec.

### Improvements

- **Modernised install / download / resilient_install stack**:
  unified `InstallResult` / `ResilientInstallResult`,
  `retry_failed_downloads` honours `exclude_server_ids`, `cmd_download`
  shares `ops.build_download_items`, typed `DownloadError` /
  `DownloadErrorKind` replaces `error.startswith("HTTP")`
  discrimination, six `except Exception: pass` blocks now log at
  WARNING.  Drops the string-match signature detection in
  `Installer.install` in favour of a clean two-pass install.
- **Shared post-resolution transaction pipeline**: extracted
  `run_install_transaction` covering SIGINT, `InstallLock`,
  progress, resilient install, classification, scriptlets, restart
  advice and `mark_dependencies`; cmd_install and cmd_upgrade lose
  ~120 lines of duplication.
- **Test suite hygiene**: `test_download_progress_samples_and_speed`
  deflaked with a deterministic `time.time` monkeypatch; four
  drifted assertions repaired (locale pinning, `urpm find`
  classification, `system_arch` patching).

### Bug Fixes

- **Human-readable media display names end-to-end**:
  `urpm media discover` no longer rewrites the upstream `name=`
  field as `f"mga{version}-{short_name}"`; one-shot rename of
  databases poisoned by the pre-3fafe62 discover via a self-deleting
  `/var/lib/urpm/pending-name-cleanup.list` queue; cleanup hook
  runs through a new `read_only=True` kwarg on `PackageDatabase`
  to avoid contention with running urpmd.
- **`urpm media add --name FOO`** honoured on official-layout URLs
  (URL-derived label no longer silently wins); explicit `--name`
  collisions raise a clear error instead of auto-suffixing.

### Packaging & Distribution

- Translated man pages refreshed for the new `urpm build` flags in
  six locales (de / es / fr / it / nl / pt) with matching `.po`
  files; README and English man page updated in lockstep.

**Full Changelog**: https://github.com/pvi-github/urpm-ng/compare/0.7.14...0.7.15

---

## [0.7.14] and earlier — historical

Earlier release notes were published on GitHub Releases.  Their content
should be reimported here over time.  Until then, see the canonical
sources:

- [0.7.14 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.14)
- [0.7.13 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.13)
- [0.7.12 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.12)
- [0.7.11 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.11)
- [0.7.10 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.10)
- [0.7.9 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.9)
- [0.7.8 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.8)
- [0.7.7 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.7)
- [0.7.6 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.6)
- [0.7.5 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.5)
- [0.7.4 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.4)
- [0.7.3 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.3)
- [0.7.2 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.2)
- [0.7.1 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.1)
- [0.7.0 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.7.0)
- [0.6.1 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.6.1)
- [0.6.0 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.6.0)
- [0.5.0 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.5.0)
- [0.4.1 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.4.1)
- [0.3.3 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.3.3)
- [0.3.2 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.3.2)
- [0.3.1 release](https://github.com/pvi-github/urpm-ng/releases/tag/0.3.1)
