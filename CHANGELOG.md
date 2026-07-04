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

## [0.8.1] — 2026-07-04

First 0.8.x tag.  Ships the `genmedia` subsystem, a from-scratch
rewrite of the media-creation pipeline unified behind a single
primitive with four hard invariants, and a mkimage bootstrap that
finally delegates chroot setup to the Mageia packages themselves
(`filesystem`, `setup`, `basesystem-minimal`) instead of
hand-rolling the FHS layout.  Six translation catalogues reach
100%.

### Major Features

- **`urpm genmedia`** — regenerate full media metadata (hdlist.cz,
  synthesis.hdlist.cz, files.xml.lzma, info.xml.lzma,
  changelog.xml.lzma, MD5SUM) from a directory of RPMs. Packaged
  separately as `urpm-ng-genmedia` to keep the base client lean.
  Idempotent: a second run detects an unchanged tree and re-scans
  only new / removed / touched RPMs.  Documented in the `urpm(1)`
  man page for all seven locales.
- **AppStream extraction** — picks up the `metainfo.xml` shipped
  by upstream applications, falls back to a minimal component
  derived from RPM header fields when missing, structurally
  filters out packages whose content is entirely non-user-facing
  (devel headers, debug symbols, static libs, pure runtime
  libraries) so they no longer pollute GNOME Software / Discover
  under a generic `System` category.
- **Media pipeline refactor** — new `upsert_media_tree` primitive
  in `urpm/core/media_pipeline.py` is the single canonical entry
  point for every command that creates a media row (`init`,
  `media add`, `media discover`, `media autoconfig`, `media
  import`). Four invariants enforced: (a) no orphan media,
  (b) no ugly `mga9-core-release`-style names, (c) no `unknown`
  placeholders, (d) no legacy `add_media_legacy` fallback path.
  45 `MIRRORLIST` entries from a typical `urpmi.cfg`
  (Debug/Testing/Backports/32bit) used to be silently dropped on
  `urpm media import`; they now import as pending media and get
  server links from a subsequent `urpm server autoconfig`, which
  HEAD-probes every media against every candidate mirror,
  disabled ones included.
- **`urpm mkimage` — chroot bootstrap delegated to RPM helpers**
  — Phase 1 no longer pre-creates the FHS layout, UsrMove
  symlinks or `/etc/passwd` system-account entries by hand.
  `filesystem` is installed first with scriptlets active (its
  pure-Lua `%pretrans` lays down `/usr/{bin,sbin,lib,lib64}` and
  the UsrMove symlinks); `setup` ships `/etc/passwd/group/shadow`
  via `%files`; only `makedev` keeps `--noscripts` because its
  shell `%posttrans` needs a populated chroot.

### Improvements

- `urpm download --show-all` and `urpm media remove --all`
  (`ae89031`) — the download flag lifts the 20-row truncation
  the parser already advertised but the code ignored;
  `media remove --all` bulk-removes with a refuse-by-default
  `[y/N]` confirmation (`-y`/`--auto` skips it), and cascades
  orphan servers (no media left) in the same pass.
- **Disabled-media availability probe** — after `urpm init` and
  `urpm media import`, every disabled media is HEAD-probed
  against every mirror it is linked to (one worker per server,
  sequential intra-server to stay respectful with per-mirror
  load).  Report is compact: `N/N covered by at least one
  mirror`, with explicit lines for partial-coverage and orphan
  media.
- **Six translation catalogues (fr/de/es/it/nl/pt) hit 100%** —
  message extraction now catches sources outside `POTFILES.in`
  that had been silently skipping the `.pot` build.
- `TransactionQueue._script_error_packages` is initialised in
  `__init__`, so a scriptlet error callback firing via the
  userns child path no longer crashes rpm's Python callback
  with `FATAL ERROR: python callback ??? failed, aborting!`.

### Bug Fixes

- `urpm media import` no longer silently drops the 45
  mirrorlist-based entries of a typical `urpmi.cfg` (`16378db`).
- The child `podman unshare python3 -c ...` used by
  `TransactionQueue` scrubs `sys.path[0]` before importing urpm
  — a source checkout at CWD can no longer shadow the
  RPM-installed package during `urpm image make` (`79d2c76`).
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
- `pytest -n auto` no longer races on `/var/lib/rpm`: tests set
  up their own tmpdir-scoped rpmdb (`5194678`).

### Packaging & Distribution

- The RPM `%install` post-processes the auto-generated `urpm` /
  `urpmd` / `urpm-dbus-service` wrappers to normalise the shebang
  to `#! /usr/bin/python3 -s` and insert an explicit
  `sys.path[0]` CWD scrub — equivalent to what Python 3.11+
  `-P` does, but works on mga9's Python 3.10 too (`ea7bc3e`).
- Test isolation: rpmdb writes moved to per-test tmpdirs — the
  suite now runs in parallel without races (`5194678`,
  `aca26ca`).
- DNF references purged from the documentation, source comments,
  and translatable strings — the Mageia ecosystem stands on its
  own vocabulary (`44810da`).

### Documentation

- `CONTRIBUTING.md` added in seven languages (`dcd2870`).
- Man pages document `urpm genmedia` in all seven locales
  (`a9a7da3`).
- `FEATURES.md` split off from `CHANGELOG.md` (`c25b3e1`) so
  this file stays release-focused; the cumulative feature
  catalogue lives in [`FEATURES.md`](FEATURES.md).
- `README.md` adds a `Media generation (urpm genmedia)` section
  and documents the new `--show-all` / `--all` flags.
- `doc/ROADMAP.md` lists genmedia under the shipped features.
- `doc/TESTING.md` aligns with the actual `urpm/tests/` layout
  and gives an honest assessment of remaining coverage gaps.
- `doc/TODO_DASHBOARD.md` is now an index into the thematic
  TODO files instead of a duplicated tracker.

**Full Changelog**: https://github.com/pvi-github/urpm-ng/compare/0.7.15...0.8.1

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
