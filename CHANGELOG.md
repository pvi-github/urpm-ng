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

## [0.8.2] — 2026-07-11

Mkimage tightening pass.  Cross-version chroots (mga9 built from a
mga10 host) work, the `-minimal` image drops from ~1 GB to ~380 MB,
and the whole pipeline runs cleanly rootless.  Under the hood a
critical `PackageDatabase.close()` leak that was shipping ~90 MB
of stale SQLite WAL in every image is fixed, and `urpm install
--nodeps` finally behaves like `rpm -i --nodeps` has since
forever.  `urpm-ng-core` inclusion in fresh images is rewritten
around an explicit four-rule matrix (local / media / github /
auto) with source-override flags.

### Major Features

- **Mkimage cross-version chroot** — `urpm mkimage --release 9`
  from a mga10 host used to blow up at stage 0 with "Package not
  found" because every `get_package` query filtered by the host
  release.  `cmd_init` now pins `mageia-version` into the chroot
  DB's own config; `_get_accepted_versions` honours that pin over
  auto-detect and the host os-release fallback.
- **`urpm-ng-core` inclusion — 4-rule matrix** — explicit decision
  tree deterministic across `urpm image make` and `urpm image
  update`: (1) local match + no target media → local, (2) local
  matches + target media + local newer → prompt (auto keeps
  media), (3) host media covers target arch+release → replicate
  into the container, (4) otherwise → GitHub Releases.  Two new
  flags override the waterfall: `--urpm-ng-source={auto,local,
  media,github}` and `--urpm-ng-core=<path>`.  Media reachability
  is now HEAD-probed against `media_info/media.cfg` before rule 3
  fires, so a third-party repo that publishes mga10/x86_64 but
  nothing for mga9/aarch64 falls through to github instead of
  shipping a doomed media into the container.
- **`-minimal` image size divided by ~2.5** — several cumulative
  wins land together: `basesystem-minimal` → `basesystem-minimal-
  core` in the bootstrap profile (drops cronie / logrotate /
  initscripts / kbd / kmod / iproute2 / binutils via file /
  ncurses full, ~30 MB); `urpm/dbus/` + `urpm/auth/polkit.py`
  moved out of `-core` into `-daemon` so headless containers no
  longer pay for polkit + typelib(Polkit) they never load
  (~10 MB); every file under `/var/lib/urpm/medias/` flushed
  before commit (packages.db alone drives the solver, ~150 MB);
  stage-0 `setup` seed via `--noscripts --nodeps` before
  filesystem, so glibc's Lua `%preinstall` finds `/etc/passwd`
  and `/etc/group` posed and doesn't emit cross-package group
  lookup warnings; SQLite databases compacted at commit
  (`wal_checkpoint(TRUNCATE)` + `journal_mode=DELETE` + `VACUUM`;
  ~90 MB WAL alone); `podman commit --squash` on `urpm image
  update` so successive updates don't snowball the on-disk
  footprint.

### Improvements

- `urpm install --nodeps` now works standalone.  The previous
  guard rejected it outside `--download-only` but the underlying
  implementation already knew how to build the action list
  without a solver pass, mirroring `rpm -i --nodeps`.  Guard
  gone; a warning informs the operator that dep resolution was
  skipped.
- New `urpm install --no-readme` flag surfaces the internal
  `args.no_readme` toggle so `urpm image update` and phase 2 of
  `urpm mkimage` can suppress the "package X's lesspipe.sh is
  available" post-install noise without going through
  argparse.Namespace kludges.  Detection is dynamic on older
  `urpm-ng-core` inside containers (mgabiz mga9 still ships
  `urpm-ng-core-0.8.1-1.mga9`, which predates the flag).
- Phase-1 stderr silencer — benign `rpm` and systemd-sysusers
  messages that fire during `setup` extraction and basesystem
  install (`failed to open /etc/group for id/name lookup`,
  `Creating group X`, `/proc/ is not mounted`, `systemd.catalog:
  No such file`) are filtered behind an `URPM_MKIMAGE_SILENCE=1`
  env var, scoped to phase 1 only.  `urpm install` outside
  mkimage is unaffected.

### Bug Fixes

- **`PackageDatabase.close()` leaked worker-thread connections.**
  The main-thread close only released one connection; every
  connection created by `ThreadPoolExecutor` workers (download,
  resolver, import) stayed open until process exit.  In mkimage
  that left ~13 fd on `packages.db{,-wal,-shm}` at cleanup time,
  which blocked the WAL checkpoint and shipped ~90 MB of stale
  WAL in every committed image.  Fixed by tracking every
  connection in a lock-guarded list and closing them all in
  `close()`.
- `cmd_media_update` honours `args.allow_no_root` for its
  `require_privileges` guard AND skips the `/run/urpm/`
  sync_lock in the same path — the lock file lives on the host,
  is unwritable from rootless podman-unshare, and the mkimage
  pipeline is single-threaded anyway.
- `cmd_media_update` in "update all media" mode was missing
  `urpm_root=` on its `sync_all_media` call (the single-media
  branch had it); sync tried to write synthesis under the host's
  `/var/lib/urpm/` and hit EACCES.  Now propagated.
- `cmd_media_discover` honours `args.allow_no_root` too, so the
  mkimage stage that replicates the host's mgabiz media into a
  fresh chroot can run rootless.
- `_detect_host_source` used to preserve the host's own sub-repo
  suffix (e.g. `urpm/release`) when rebuilding the target URL,
  so `cmd_media_discover` ended up asking for
  `.../media/urpm/release/media_info/media.cfg` — a 404 because
  the real manifest sits one level up.  Rebuild from scratch
  using target release + arch.
- `container.commit(squash=True)` used `--squash-all`, a flag
  that exists on `podman build` but not on `podman commit` —
  every `urpm image update` aborted with "unknown flag" on
  recent podman.  Now uses `--squash`.
- `podman exec` in `container.exec_stream` allocates a
  pseudo-TTY (`-t`) when the host stdout is a terminal, so
  programs run inside the container inherit the real terminal
  width via TIOCGWINSZ instead of drawing truncated progress
  bars at the runtime's 80-column fallback.  Piped or
  redirected stdout still runs without `-t` to keep
  `urpm build 2>&1 | tee log` and CI paths working.

### Packaging

- `urpm-ng-daemon` now declares strict `Requires: polkit` and
  `python3-gobject`, since `urpm/dbus/` + `urpm/auth/polkit.py`
  moved there from `urpm-ng-core`.
- Bootstrap and minimal profiles (`data/profiles/*.yaml`)
  switched to `basesystem-minimal-core`.

### Documentation

- New `doc/TODO_OPTIMIZE_CONTAINER_SIZE.md` in the parent repo
  captures the container-size playbook: what worked this cycle,
  what remains (Recommends-off at the solver level,
  `containersystem-minimal` upstream package, file-level
  stripping à la buildah), and the roof set by python-stdlib +
  glibc + icu when `urpm-ng-core` is kept in the image.

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
