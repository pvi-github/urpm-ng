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

## [0.9.6] — 2026-08-27

Hotfix release for four regressions surfaced by the 0.9.5 beta
runs — three of them on the `distupgrade` path, one on the build
side.  All are safe to apply mid-cycle.

### Bug Fixes

- **`distupgrade --resume` re-ran Stage 0 + Stage 1 on the already
  mutated DB.**  A `--resume` after a Stage 2 interruption fell
  through to the fresh entry point, replayed the Phase A preamble
  and re-transposed the media rows, which then produced a bogus
  small plan on the second solve.  Stage 1 is now idempotent
  (skips media rows already present at the target release), and
  the resume dispatcher inspects `.state.stage` to skip Phase A
  and Stage 1 when they were already applied.

- **Distupgrade Stage 2 downloads never hit LAN peers.**  The
  distupgrade downloader was instantiated without
  `target_version` / `target_arch`, so `query_peers_have` returned
  an empty availability map on every request and all traffic went
  upstream.  Both parameters are now threaded through
  `download_plan` → `download_packages` → `Downloader`.

- **Extraction sub-bar stayed grey during long transactions.**
  The Stage 3 progress dedup filter dropped the `bytes_done`
  component of each event, so INST_PROGRESS updates for big
  packages (LibreOffice, kernel) were coalesced away and the
  bottom bar never re-coloured.  The dedup key now includes
  `bytes_done`, throttled to ~30 Hz to keep the display fluid
  without flooding.  Header lines piling up between Tx B batches
  also fixed — the internal `Waiting for scriptlets…` print is
  now gated on the absence of a progress callback (the display
  already shows it).

### Packaging

- **`make install` picked up cross-release RPMs from
  `rpmbuild/RPMS`.**  The install-core / install / install-all
  find patterns now honour `%{?dist}` so a machine with mga9 +
  mga10 build artefacts on the same tree installs the current
  release only.
- **`urpm-ng-core` gained `Requires: rpm-build` and `Requires:
  bm`.**  Both are needed at runtime for `urpm build` and
  `urpm image make` respectively ; they were previously silently
  pulled by transitive dependencies and started missing on lean
  installs.

---

## [0.9.5] — 2026-08-27

> **⚠️ WARNING — the `distupgrade` feature is still young.**  Only
> use it on a machine you can afford to lose and reinstall, or on a
> machine you can snapshot and restore (VM, Clonezilla, btrfs/LVM
> snapshot…).  Every failure report makes the next version safer.

Distupgrade robustness pass.  Three independent silent-drop bugs
surfaced during real mga9→mga10 beta runs — synthesis metadata
that dropped cockpit + a few KDE apps, Tx B skipping 400+ packages
under disk pressure, and community mirrors left off the target
release — all three now diagnose cleanly and recover automatically.
New interactive orphans triage for post-distupgrade cleanup,
`--preserve-hw` on `system import`, and a full rework of the
`--custom` media add UX with auto-generated names.

### Major Features

- **Interactive orphans triage.**  `urpm autoremove --interactive`
  opens a TUI that walks through the orphans classified into
  previous-release relics, SONAME sublibs and user-facing packages
  with per-package keep/remove/skip decisions, batch shortcuts,
  filter language and a details view.  Distupgrade Stage 4 invites
  the user to run it right after a successful migration.

- **Tx B batching by 200 MB slices.**  Large distupgrade Tx B runs
  (2000+ packages) were exhausting `/var/cache/urpm/rpms` and
  triggering silent skips.  The commit / order phase now slices
  into 200 MB batches, purges each batch's RPMs after commit, and
  progresses a global counter across the slices so the display
  stays honest.

- **`--preserve-hw` on `urpm system import`.**  When importing a
  profile onto a machine with different hardware, keeps the
  target's hardware-specific packages (nvidia drivers, intel
  firmware, hardware-tied kernels) even when the source didn't
  have them.

- **Auto-generated names on `media add --custom`.**  The flag no
  longer requires positional `name` and `short_name` args ; both
  are derived from the URL (blogdrake plate URL →
  `short_name=blgrk_free`, `name=Blogdrake_Free`) via a vowel-drop
  compression rule with a mnemonic table for ambiguous hosts.
  `--name` / `--shortname` are optional overrides.

### Bug Fixes

- **File-provides silent drops in `distupgrade`.**  Mageia's
  `synthesis.hdlist.cz` omits most file-provides ; in a full DUP
  the mga9 rpmdb providers vanish and mga10 packages that
  `Require` a file path (`cockpit-system`, `elograf`, and their
  downstream `cockpit`, `cockpit-networkmanager`) become
  unresolvable and get silently ejected by libsolv.  New reactive
  rescue pass in `resolve_distupgrade` detects silent-drop
  victims, streams each target media's `files.xml.lzma` filtered
  on the missing paths, injects the missing provides on the
  matching solvables and re-solves.  Zero cost when nothing was
  dropped.

- **Community-mirror URL support.**  Version detection now
  recognises `mageia<N>` / `mga<N>` in addition to bare `<N>`,
  and parsers scan version and arch independently with a
  uniqueness guard.  Stage 1 URL transposition gained the
  `mageia<src>` → `mageia<tgt>` rewrite so a mga9 blogdrake media
  survives the migration to mga10.  `media discover` used to
  substring-match the bare version inside the URL — hitting `10`
  inside `mageia10` and losing the `mageia` prefix from every
  discovered media's `relative_path`, silently 404-ing at the
  next `media update`.  Now matches segment-wise and preserves
  the actual URL token.

- **Stage 1 mirror probe HEAD fallback.**  Some HTTP mirrors close
  the connection on directory `HEAD` requests
  (`CURLE_GOT_NOTHING`), silently orphaning perfectly reachable
  media.  Probe now falls back to a bounded `GET` (`Range: 0-0`)
  when `HEAD` returns empty, and the previously-swallowed pycurl
  exception is surfaced as a tagged
  `stage1 orphan[probe-unreachable]` warning.

- **Distupgrade robustness pass.**  `rpm-helper` anchored in Tx A
  (fixed `%pre` scriptlet race), silent-skipped packages retried
  automatically in Tx B, RPM cache purged between batches,
  `except` scope fix in the post-`execvp` path, stale
  `url_version` refresh in Stage 1 for cauldron-pinned media.

- **`urpm i --reinstall` on named packages.**  Was matching the
  RPM file path only and silently doing nothing when passed a
  package name.  Now resolves the name via the pool.

- **`urpm download --from-file` best-effort mode.**  Downloads
  what it can and reports failures instead of aborting the batch
  when some packages are unreachable.

- **`urpm history --detail` scriptlet rendering.**  `is_error`
  column crash was masking the scriptlet trace ; the detail view
  now prints the real output.

- **rpmdrake accepts the 9th progress argument.**  DBus signature
  drift crashed rpmdrake on every install progress update.

### System / Robustness

- Safe rpmdb access reworked so an interrupted `system import` no
  longer leaves the rpmdb in a partially-open state.

- Deep opt-in distupgrade diagnostics behind `--debug=distupgrade`
  for beta-tester bug reports.

### Packaging / Docs

- i18n first-pass refresh at 0.9.4 covered the ~1600 msgids of the
  distupgrade UX rework across the six shipped locales
  (de/es/fr/it/nl/pt).  A ~150-msgid delta from the 0.9.5
  additions (rescue, community URLs, orphans triage tweaks) is
  documented in the backlog for the 0.9.6 catch-up, along with
  `urpm.1` de/es/nl/pt content parity with en/fr/it.

---

## [0.9.2] — 2026-08-15

> **⚠️ WARNING — the `distupgrade` feature is very young.**  Only use
> it on a machine you can afford to lose and reinstall, or on a machine
> you can snapshot and restore (Virtual Machine, Clonezilla,
> btrfs/LVM snapshot…).  Every failure report makes the next version
> safer.

Bugfix + one new feature.  The distupgrade pipeline now handles the
mga9→mga10 kernel-desktop rename ambiguity that silently emptied the
plan in 0.9.1.  New `urpm system export` / `urpm system import` verbs
snapshot and restore a machine's package selection + media/server
catalogue via JSON.

### Major Features

- **`urpm system export` and `urpm system import`.**  Clone one
  machine's package selection + media/server catalogue onto another
  via a JSON snapshot.  Export dumps installed pkgs classified into
  explicit / dependency / buildrequires (source of truth = the flat
  files that already drive `urpm autoremove`) + a slimmed media/server
  DB dump.  Import validates the profile, backs up the current state
  to `/var/lib/urpm/system-backup-<timestamp>.json` (skippable via
  `--no-backup`), computes a three-section diff (replace by default,
  `--merge-media` to keep local extras), prompts unless `--yes`, then
  applies : servers → media → sync → install missing → remove local
  explicit extras → rewrite the reason files so future `autoremove`
  behaves the same on the target as on the source.  Names-only
  matching (no NEVRA pinning) — the importer resolves each requested
  name to the best available in its freshly-imported media set.
  `--dry-run` inspects the plan without applying.

### Bug Fixes

- **DUP rename disambiguation for kernel-desktop mga9→mga10.**
  Mageia flipped the kernel packaging convention between mga9 and
  mga10 : mga9 kernels have `Name=kernel-desktop` (short, with the
  kernel version in `Version`), mga10 kernels have
  `Name=kernel-desktop-<version>-1.mga10` (long, version-in-name).
  Each mga10 candidate still provides the unversioned capability
  `kernel-desktop`.  A mga9→mga10 distupgrade thus found no target
  with the same Name ; `DUP_ALLOW_NAMECHANGE` fell back to Provides
  matching but found N+ candidates for the same capability, and
  libsolv silently held every one — Stage 2 returned zero actions
  with no diagnostic (reported by the first beta tester).  New
  pre-solve pass in `resolve_distupgrade` scans installed pkgs and,
  when no same-Name target exists but a Provides capability is
  satisfied by two or more target candidates, pins the highest-EVR
  candidate via `SOLVER_INSTALL`.  libsolv then treats the rename as
  deterministic and the plan comes out non-empty.

---

## [0.9.1] — 2026-08-11

Bugfix release addressing two safety issues surfaced by the first
`urpm distupgrade` beta reports.

### Bug Fixes

- **Stage 2 refuses to proceed on an empty plan.**  When libsolv
  silently held every candidate (real beta case : a kernel-desktop
  hold that produced a Resolution with zero actions), Stage 4
  previously ran to completion and flagged the mga N media for
  deferred deletion — with the machine still entirely on mga N.  A
  reboot-time cleanup would then have removed every mga N repo while
  the system still needed them, bricking the machine.  A new
  `Stage2EmptyPlanError` now aborts the pipeline before Stage 3,
  prints the resolver's skipped-jobs diagnosis, and **auto-rolls back
  Stage 1** via the shared `_rollback_stage1` helper (extracted from
  `_cmd_abort` for reuse).  The DB is left bit-for-bit in the
  pre-distupgrade state ; no manual recovery step for the user.

- **libsolv silent-hold diagnostic in distupgrade solve.**
  `resolve_distupgrade` used to return `Resolution(actions=[],
  skipped=None)` on a silent hold, leaving the CLI with the useless
  `"resolver anomaly; see log for details"` message.  New
  `_diagnose_distupgrade_holdback` runs when the DUP transaction
  comes out empty : walks installed packages, finds strictly-newer
  target-repo candidates, runs a fresh targeted `SOLVER_INSTALL` solve
  on the best candidate, and captures libsolv's `Problem` list — the
  concrete reason the DUP wouldn't take it (Conflicts against an
  installed package, unsatisfied requires, etc.).  Wired to
  `Resolution.skipped` so the CLI's existing empty-plan renderer
  surfaces it verbatim.  Zero cost on the happy path (only fires when
  the transaction is empty).

---

## [0.9.0] — 2026-08-10

> **⚠️ WARNING — the `distupgrade` feature is very young.**  Only use
> it on a machine you can afford to lose and reinstall, or on a machine
> you can snapshot and restore (Virtual Machine, Clonezilla,
> btrfs/LVM snapshot…).  Do **not** run it on your daily driver without
> a fresh backup.  Every failure report makes the next version safer.

First release of the `urpm distupgrade` pipeline (mga N → N+1), full
Stage 0 → Stage 5 implementation with target auto-detection, maturity
gate, media transposition, Tx A / Tx B split, execvp handoff,
structured Stage 4 report, and post-reboot deferred cleanup.
Cross-cuts: history vocabulary overhaul, six-language i18n, docs +
man pages + bash completion refresh.

### Major Features

- **`urpm distupgrade` — mga N → N+1 migration.**
  End-to-end pipeline: Stage 0 pre-checks (clock, `--to` auto-detect,
  §591 refuse-downgrade, §592 multi-jump prompt, target maturity
  gate, Phase A live progress) → Stage 1 media swap and third-party
  transposition → Stage 2 solve + download with peer-cache splits and
  `--export-plan` for bandwidth-limited peer preload → Stage 3
  Tx A / Tx B via libsolv transitive closure + `os.execvp` handoff
  (`--yes` propagated across argv) → Stage 4 structured report (rpmnew,
  `.mga<old>` residuals, orphan media, failed scriptlets) → Stage 5
  post-boot fire-and-forget scripts.  New verbs: `urpm distupgrade`
  (with `--to`, `--yes`, `--dry-run`, `--export-plan`, `--resume`,
  `--abort`, `--continue`), `urpm recover`.  New `--distupgraded` on
  `urpm media remove` for post-hoc cleanup ; new `--from-file` on
  `urpm download` to consume a peer-preloaded plan.

- **Deferred post-reboot cleanup of old media.**  Stage 4 no longer
  synchronously purges the transposed old media rows (which on a 40k-
  package DB with 50 media cost 30+ s while the user was about to
  reboot).  Stage 4 just flips `media.disabled_by` to `pending_drop`
  and returns instantly.  A new `urpm/core/deferred_cleanup.py`
  module owns the physical purge, fcntl-serialized on
  `/run/urpm/deferred_cleanup.lock`, fired once at urpmd startup and
  on every subsequent `urpm` CLI invocation.  Post-reboot the daemon
  absorbs the wait during boot ; users don't notice.

### Improvements

- **Six-language i18n first pass** for the distupgrade user-facing
  flow — 60+ new msgids covering phase markers, Stage 2 summary +
  prompt, download callbacks, Tx A/B headers, Stage 4 report,
  maturity gate, multi-jump prompt, cleanup — translated in
  de / es / fr / it / nl / pt per project convention.
- **History status vocabulary alignment.**  New
  `history_packages.status` column with `planned` / `done` / `failed`
  / `skipped` values ; `urpm history` colours realigned to what the
  DB actually writes back (previous mismatch showed everything grey).
- **PackageAction carries `solvable_id`** for O(1) local-RPM lookup
  during resolve, replacing an O(N) per-package scan.
- **P2P download stats surfaced.**  Stage 2 forwards `peer_stats`
  (from_peers / from_upstream) into the summary dict, CLI renders
  the split like `urpm i` does.
- **AppStream: `URLError` demoted to debug** so `file://` media
  without an appstream blob fall back cleanly.
- **`urpm show` gains `--files` and `--changelog`.**
- **Container-based build chain** : shared container for multi-spec
  `urpm build`, `--exclude PKG` on `urpm image make`, CPU / mem /
  swap flags plumbed through, TMPDIR routed via workdir for large
  `podman commit`.

### Bug Fixes

- **Stage 1 : stale media survived migration.**
  `_disable_source_media` scoped to `is_official=1` so it stops
  overriding the `distupgrade_orphan` tag ; both stage-1 helpers
  dropped the `enabled=1` filter so mga N variants slurped in by
  `urpm media autoconfig` but never activated (debug, backports,
  testing, 32bit) get tagged `distupgrade` and become cleanup-eligible.
- **BuildRequires parser** now uses `rpmspec` and honours `%if`
  guards, matching the resolved deps of an actual build.
- **`urpm build` container** now propagates LANG / LC_ALL for
  correctly-translated `%description` and error messages.
- **`_resolve_version`** : URL wins for release identity (fixes edge
  case where the mirrorlist API and the media URL disagree).
- **`autoconfig_servers`** strip logic corrected — no more duplicated
  arch segments in constructed catalogue URLs.

### Packaging & Distribution

- **Schema migrations v33 → v36.**  v33 : `history.pid_running` for
  orphan-transaction detection ; v34 : `history_scriptlets` (Phase C
  scriptlet capture) ; v35 : `media.disabled_by` (Stage 1 tagging) ;
  v36 : `pkg_id` indexes on `recommends`, `suggests`, `supplements`,
  `enhances` — without them a `DELETE FROM packages` full-scans
  these tables for FK integrity.
- **`urpm-ng-packagekit-backend`** DBus JSON enriched with
  `media_name` ; RPM Group → PK Group enum mapping ; progress
  emission on install / update paths ; refresh signals for Discover
  cache invalidation.

### Documentation

- **README (EN) + six translations** : new « Distribution upgrade
  (mga N → N+1) » section covering every flag and cleanup verb.
- **QUICKSTART.md** : distupgrade quickstart with interactive default
  + `--export-plan` / `--from-file` bandwidth-friendly workflow.
- **man/{en,de,es,fr,it,nl,pt}/man1/urpm.1** : `distupgrade` and
  `recover` sub-commands ; `--distupgraded` on `media remove` ;
  `--from-file` on `download`.
- **`completion/urpm.bash`** : `distupgrade` + `recover` verbs,
  per-verb handlers (release completion on `--to`, file completion
  on `--export-plan` and `--from-file`).

---

## [0.8.7] — 2026-08-02

Bugfix release.  Fixes malformed URL construction when the mirrorlist
API returns freeze-era paths (e.g. `.../distrib/cauldron/x86_64/` for
`--release 11`), which had `urpm mkimage --release 11` 404-ing on
every mirror because the reconstructed discover URL doubled the arch
segment.

### Bug Fixes

- **`urpm mkimage --release 11` no longer builds a malformed URL.**
  During a freeze the mirrorlist API returns URLs pointing to
  `.../distrib/cauldron/x86_64/` for `--release 11`.  The previous
  code appended `/{release}/{arch}/media/` on top of the mirror's own
  path, producing `.../distrib/cauldron/x86_64/11/x86_64/media/` and
  failing every reachability check.  The mirror-side URL segment is
  now detected at server-add time (regardless of whether it matches
  the target identity) and persisted per server; URL reconstruction
  uses that segment and falls back to the release identity for legacy
  rows.

### Packaging & Distribution

- **Schema migration v31 → v32.**  Adds `server.url_version` (nullable
  TEXT).  Fully backward-compatible: existing rows keep working via a
  fallback to the release identity when the column is NULL.

---

## [0.8.6] — 2026-08-01

Bugfix release.  Restores `urpm build` on specs that declare no
BuildRequires — a legitimate case for trivial noarch packages —
that had been failing since the recent BuildRequires parser
refactor with an unhelpful `Aborted.` message.  `urpm install
--buildrequires` and `urpm download --buildrequires` now
short-circuit cleanly with a clear informational message on an
empty BR list.

### Bug Fixes

- **`urpm build` on specs without BuildRequires no longer aborts.**
  Internal `urpm install --buildrequires <spec>` now returns
  cleanly with "Spec has no BuildRequires — nothing to install."
  and exit 0 when the parser returns an empty list, instead of
  falling through to the generic install path that would print
  `Aborted.` (empty resolved-packages list) and exit 1.  Symmetric
  fix applied to `urpm download --buildrequires`.

---

## [0.8.5] — 2026-07-24

Cauldron release.  urpm-ng gains a proper release-identity model
that lets a single machine ride cauldron or a numeric stable
without the resolver confusing the two.  Ships `urpm distro-switch`
to bascule, a `--release cauldron:N` syntax for explicit numeric
targets, plus a set of build-container robustness fixes that
finally make `urpm build` a first-class replacement for mock on
Mageia hosts.

### Major Features

- **Cauldron identity + `urpm distro-switch`.**  A machine now
  carries a single release identity at a time (`cauldron`, `10`,
  `11`, …) that pins which media the resolver considers.  Media
  whose `mageia_version` doesn't match are left out of the
  candidate pool even if they stay enabled in the DB — no more
  ambiguous-media aborts on a cauldron chroot.  Switching is a
  deliberate act, exposed as a dedicated verb:
  `urpm distro-switch cauldron` / `urpm distro-switch 11` /
  `urpm distro-switch cauldron:12`.  Preflight verifies the
  target has enabled media, warns about stale media of the
  previous identity, and best-effort refreshes `system-numeric`
  via a `media.cfg` probe.  Underlying fix: `_resolve_version`
  now treats the URL as the source of truth for the release
  directory, undoing the regression where a catalogue's numeric
  version silently overwrote `cauldron` on every media served
  under `/distrib/cauldron/`.
- **`--release cauldron:N` syntax.**  `urpm image make` and
  `urpm init` accept an explicit numeric target alongside the
  cauldron identity, so a packager working offline or during a
  flip window can force the numeric that `.mgaN` release tags
  and `%mgaversion` will resolve to, without depending on the
  mirror catching up.  When omitted, the numeric is probed
  best-effort at init time and cached in `system-numeric`.
  Phase 1 mkimage seeds `/etc/mageia-release` from that value so
  images built from a mid-flip cauldron still emit `.mgaN`
  correctly at rpmbuild time.
- **`urpm build` container resource caps.**  `--build-cpus N`,
  `--build-memory SIZE`, `--full-throttle` cap CPU count and
  container RAM via `podman --cpus` / `--memory`; `--build-cpus`
  also injects `rpmbuild --define '_smp_mflags -jN'` so `%build`
  honours the cap.  Defaults leave two CPUs and two GB of RAM
  free for the host so it stays usable during heavy builds
  (firefox, thunderbird).  Swap is unbounded by default,
  matching mock's systemd-nspawn wrapper — the container can
  spill cold pages onto host swap the way mock does, which is
  what closes the gap on <16 GB hosts.  `--strict-memory`
  re-ties `--memory-swap` for CI use.
- **rpmbuild bcond passthrough.**  `--with FEATURE` and
  `--without FEATURE` on `urpm build` forward verbatim to
  rpmbuild so specs using `%bcond_with` / `%bcond_without`
  can be flipped without shelling out.

### Improvements

- **Server↔media official mesh.**  `cmd_init` now runs a
  full-mesh link between every `is_official=1` server and every
  `is_official=1` media at the end of discovery.  Mageia's
  mirror convention is that any mirror carrying
  `/distrib/<release>/<arch>/media/` also carries the full
  sub-tree at the same relative paths; discovery, however, only
  linked pairs whose catalogue lookup succeeded at that specific
  server, leaving holes whenever a mirror's catalogue timed out.
  In the field this showed up as "All servers failed" on a
  single hard 404 from one CDN clone even though other perfectly
  good mirrors were configured.  The mesh restores the
  assumption the download failover implicitly relies on.
- **Local-RPM disttag N-1 window on cauldron targets.**
  `urpm image make --urpm-ng-source local` now accepts an
  `urpm-ng-core` RPM built for the previous stable when
  targeting cauldron.  Numeric-target images stay strict (only
  `.mgaN.`).  `--allow-disttag-mismatch` bypasses the check
  entirely for the rare cross-release noarch case.
- **`urpm image make --exclude PKG`.**  Repeatable flag that
  removes PKG from the finished image via `urpm erase --force
  --keep-orphans --sync`.  Canonical case:
  `--exclude python3-zstandard` so firefox's `mach` does not
  trip on its own version pin against the system-installed one.
- **`podman commit` uses workdir as TMPDIR.**
  `Container.commit` routes `TMPDIR` through the mkimage
  workdir at both call sites (image make + image update), so
  podman's blob-staging directory doesn't spill onto a small
  `/tmp` partition during the final commit stage.  Symmetric
  with what `Container.import_from_dir` already did.
- **`urpm show --files` and `--changelog` now honoured.**  Both
  flags were registered on the argparse but silently ignored.
  `--files` uses `rpm -ql` for installed packages, parses the
  media's `files.xml.lzma` for available ones.  `--changelog`
  uses `rpm -q --changelog` for installed packages.
- **`crypto-policies` pulled into the bootstrap profile.**
  openssl on mga9 doesn't declare `Requires: crypto-policies`,
  so every openssl invocation in a fresh mga9-64 container died
  at startup.  The bootstrap profile now tugs
  `crypto-policies` + `crypto-policies-scripts` explicitly.
- **PackageKit backend polish.**  `emit_packages_from_json`
  honours the client's `INSTALLED` / `NOT_INSTALLED` filter
  (Discover no longer serves the two categories mixed together);
  `gpg-pubkey` rpmdb entries are skipped (their `arch=(none)`
  broke the PackageKit `package_id` format); download progress
  now reports bytes instead of package counts, so Discover's
  bar advances continuously.

### Bug Fixes

- **`BuildRequires:` parser respects `%if` / `%{?flag:…}` /
  macros.**  The regex-based scanner used by
  `urpm install --buildrequires` extracted every literal
  `BuildRequires:` line regardless of the surrounding
  conditional block, silently pulling packages the spec author
  had explicitly guarded away.  Real hit:
  `selinux-policy-devel` under `%if 0%{?with_selinux}` dragged
  in `python3-dnf` → `mageia-dnf-conf` → `dnf-data`, which
  itself has a broken auto-Requires and killed the transaction.
  Replaced by a delegation to `rpmspec -q --buildrequires`,
  which evaluates the spec the way rpmbuild will at build time.
  Ships a unit test that reproduces the historical bug case.
- **Ambiguous-media resolver.**  `get_accepted_versions` now
  consults the `mageia-version` pin at the top, so a cauldron
  chroot with mixed-tag media rows no longer hits the "both 11
  and cauldron media are enabled" abort.
- **`coordinator_speed` in download callbacks.**  Five
  `dl_progress` sites refused the 9th positional argument
  that `Container.download_all` had grown, so every Discover
  upgrade died with a `TypeError` mid-transaction.

### Packaging & Distribution

- Version bumped to 0.8.5 across `urpm-ng` and `rpmdrake-ng`.

### Documentation

- New `distro-switch` chapter and `--release cauldron:N`
  syntax section added to all seven READMEs (English +
  fr / de / es / it / nl / pt) and seven man pages.
- Build resource caps + bcond passthrough sections added to
  the same set.
- 42 new msgids across six `.po` files (build-cap argparse
  help, distro-switch messages, cauldron probe status,
  seed-mageia-release confirmation, exclude runtime).  Parity
  1490 msgstrs per locale.

---

## [0.8.4] — 2026-07-17

Discover / GNOME Software integration release.  urpm-ng ships
AppStream metainfo, the PackageKit backend gains cache-refresh
signals, accurate progress reporting, an rpmdb fast path for
`GetFiles`, and correct RPM-Group → PK-enum mapping.  The
AppStream catalog now scans `files.xml` for
`/usr/share/applications` so libreoffice and other lib-prefixed
apps finally show up.  A three-tier zstd decompression fallback
(cext → cffi → `zstdcat`) makes urpm survive a `lib64zstd1` ABI
bump without a matching `python3-zstandard` rebuild.

### Major Features

- **AppStream metainfo shipped.**  Five metainfo files land in
  the right sub-packages so Discover and GNOME Software surface
  our components: `rpmdrake-ng` (desktop-application),
  `urpm-ng-core` (console-application), `urpm-ng-daemon`
  (service, launchable `urpmd.service`),
  `urpm-ng-packagekit-backend` (addon of PackageKit),
  `urpm-ng-appstream` (addon of AppStream).  All six languages
  get name / summary / description translations; a shared
  128×128 icon lands under `hicolor/` via `-core`.  All five
  files pass `appstreamcli validate --no-net`.
- **Scan-based AppStream catalog generation.**  Selects
  packages for the mageia-urpm catalog by scanning each
  media's `files.xml.lzma` for
  `/usr/share/applications/*.desktop` or `metainfo/appdata`
  XML, instead of the previous name / group heuristic that
  dropped every package starting with `lib` (libreoffice-*,
  librecad…).  Two-pass `xzgrep + awk` pipeline returns ~2200
  candidates in under a second on `core.release`; result
  cached per media in a new `appstream_scan_cache` table keyed
  on `(mtime, size)` of the `.lzma`.  Ships with 17 new unit
  tests.

### Improvements

- **PackageKit backend — Discover UX overhaul.**  Write paths
  now emit `installed_db_changed` / `updates_changed` /
  `repo_list_changed` at the end of each successful
  install / remove / upgrade / refresh, so Discover no longer
  serves its pre-transaction cache after the operation lands.
  The `package_id` `data` field carries the actual repository
  origin (or `installed`) instead of a hardcoded `urpm`,
  restoring the installed / available split and AppStream
  matching.  A new `rpm_group_to_pk_enum()` maps the RPM Group
  tag to the closest `PkGroupEnum`, so categories in Discover
  finally populate instead of being all-Other.  D-Bus payloads
  now include `media_name`, batch-resolved in a single SQL
  round-trip.
- **`GetPackageFiles` fast path via rpmdb.**  Tries `rpm -ql
  <name>` before scanning the media `files.xml.lzma`.
  Measurement on grisbi post-install: 815 ms → 12 ms,
  0 → 572 files.  The lzma scan stays as fallback for packages
  that are not installed.
- **Progress reporting matches actual state on
  install / upgrade / erase.**  D-Bus write paths switched
  from `full_sync=True` to `full_sync=False`: the bar tracks
  extraction, post-install triggers run in the background via
  urpmd.  Discover's bar no longer sits at 100 % for several
  seconds after the payload lands.
- **`RefreshMetadata` no longer forces a full re-download.**
  Switched the D-Bus method from `sync_all_media(force=True)`
  to `force=False` (`If-Modified-Since` HEAD).  Discover's
  periodic `RefreshCache` drops from ~17 s to ~3 s and stops
  blocking the UI.  The `pkexec-refresh` path
  (`urpm media update --force`) is unaffected.
- **Robust zstd decompression on ABI breaks.**
  `_ZstdWrapper` tries the cext first (fastest), then
  re-imports with `PYTHON_ZSTANDARD_IMPORT_POLICY=cffi`
  (survives any `lib64zstd1` ABI change), then falls back to
  a `zstdcat` subprocess.  `check_dependencies` exercises the
  full chain and surfaces the actual `ImportError` string when
  everything fails, so the next packaging break doesn't
  wrongly point at urpm.

### Bug Fixes

- **Permissions on `/var/lib/urpm/medias/*/media_info/`.**
  `shutil.move` from a `NamedTemporaryFile` carries `0o600`
  and `shutil.copy2` preserves the source mode; both left the
  media metadata unreadable outside root, breaking
  unprivileged `urpm f`, AppStream generation, and any client
  reading the media metadata.  Explicit `chmod` after each
  move / copy on `files.xml.lzma`, `synthesis.hdlist.cz`,
  `hdlist.cz` and `MD5SUM`, plus `0755` on the `media_info`
  directory itself.
- **Services restart on upgrade.**  `%post daemon` and `%post
  packagekit-backend` only ran `systemctl daemon-reload`, so
  after an upgrade the previous urpmd / urpm-dbus / packagekit
  processes kept serving the pre-upgrade Python code.  Added
  `systemctl try-restart <svc>` in both scriptlets, gated on
  `$1 -ge 2` so fresh installs are not disturbed.

### Packaging & Distribution

- Version bumped to 0.8.4 across `urpm-ng` and `rpmdrake-ng`.
- `Recommends: python3-cffi` and `Requires: zstd` added to
  `-core` to back the zstd fallback chain.  `zstd` is
  `Requires:` (not `Recommends:`) because `zstdcat` is the
  last-resort subprocess when both cext and cffi fail.
- Shared `urpm-ng.svg` / `.png` icon installed into
  `hicolor/{scalable,128x128}/apps/` via `-core`.

### Documentation

- All six languages (fr / de / es / it / nl / pt) refreshed
  for the new metainfo strings.

---

## [0.8.3] — 2026-07-12

Focus release on the daily-use surface.  `urpm build` chains
multi-spec runs in a single container, LocalRPM installation is
O(1) instead of scanning the whole pool, the "not enough mirrors"
warning stops shouting at admins who configured their own setup,
and the test suite runs 5× faster after mirror auto-discovery is
inhibited off-network.

### Major Features

- **Shared-container multi-spec build** — `urpm build spec1
  spec2 ...` now instantiates a single container for the whole
  run.  Shared setup (`urpm media update`, `urpm upgrade`,
  `rpm-build` install) executes once; each spec then compiles
  inside its own `/root/<pkg>` topdir, and the RPMs produced by
  an earlier spec are re-injected as a local media so a later
  spec whose BuildRequires depend on them picks them up via the
  resolver.  Two new flags cover the failure and cleanup cases:
  `--stop-on-fail` aborts the chain at the first failing spec
  instead of trying the remaining ones; `--rollback-between-
  builds` / `--rbb` rewinds per-spec BuildRequires between builds
  while keeping the shared setup.  `--parallel N` is kept for
  isolated multi-container experiments.
- **`urpm upgrade --with-suggests` finally wired.**  The flag
  was parsed but never consumed on the upgrade path.  Extracted
  the iterative resolution into `cli/helpers/suggests.py`
  (dropping ~185 lines of duplicated `PackageAction`
  construction) and hooked it into both `install.py` and
  `upgrade.py`.

### Improvements

- **LocalRPM install pipeline — O(N) → O(1).**  `PackageAction`
  now carries a `solvable_id`, populated at every construction
  site.  `operations.build_download_items` resolves LocalRPM
  paths via a proper index instead of scanning the whole
  `_solvable_to_pkg` dict on every action.  Historical impact
  grew with the pool size — measurable already at ~20 local
  RPMs, painful (tens of seconds) at 2000 (build-system-in-
  container scenarios).  The defensive fallback stays but now
  emits a loud orange warning when it fires, so a broken
  `solvable_id` chain surfaces instead of silently degrading.
- **`depends` / `rdepends` orthogonalised.**  `--tree` now
  controls format, `--all` controls scope; combining them yields
  an unlimited-depth tree (previously inconsistent between the
  two commands — one gave a flat list, the other a tree).
  `rdepends` default depth aligned to 5 (was 3); a header prints
  the active depth so users know how to raise it.
  `--hide-uninstalled` now also applies inside `depends --tree`.
- **`rdepends` performance rewrite.**  Full pool rdeps map is
  built once instead of scanned per package.  `rdepends --all
  openssl` returns 18074 rdeps in 2.7 s (was minutes-or-worse
  on a heavy cauldron pool).  BFS switched to a deque with
  visited-at-enqueue so cyclic ancestors no longer inflate the
  queue.
- **Container locale propagation for `urpm build`.**  `LANGUAGE`
  is now forwarded verbatim from the host instead of being
  derived from a single code out of `LANG` — a bilingual
  `en_US:fr_FR:fr` no longer loses its fallback chain in the
  container.  `C.UTF-8` availability is probed once per
  container (cached); mga9-minimal images now fall back to
  `LC_ALL=C` instead of spamming "Setting locale failed" from
  every perl spec-helper.

### Bug Fixes

- **"Not enough mirrors" warning silenced when
  `[server] auto_add = false`.**  The admin has made a
  deliberate choice; the CLI now shows a factual dim line
  ("Not adding servers to reach N (auto_add disabled)")
  instead of the anxious warning.
- **Multi-version dialog under `--prefer` sorts RPM-style.**
  `sorted()` on version strings gave `"5.10" < "5.9"` under
  lex sort.  Wrapped with `cmp_to_key(rpm.labelCompare)` on
  that call site; the broader helper refactor is captured in
  `doc/TODO_RPM_VERSION_HELPER.md` for the 0.9.x cycle (four
  other latent sort sites still use lex on `epoch:version`
  keys).
- **SRPMS URL on `urpm media add` gets a dedicated hint.**  The
  official parser rightly rejects `.../SRPMS/...` URLs (no
  arch segment), but the generic "URL not recognized" message
  sent users hunting for a formatting bug.  Now detects the
  `/SRPMS/` segment and explains that SRPMS are sources, not
  installable binary media, with a pointer to `--custom` for
  the mirroring use case.
- **Test suite: mirror auto-discovery inhibited.**  A new
  `URPM_SKIP_MIRROR_DISCOVERY=1` env var short-circuits
  `ensure_minimum_servers` before any network work; a pytest
  autouse fixture sets it for every test.  Full suite drops
  from ~32 min to under 6 min because the offline mirrorlist
  retries are gone.

### Packaging & Distribution

- Version bumped to 0.8.3 across `urpm-ng` and `rpmdrake-ng`.
- `install_recommends` config comment reworded to a self-
  contained description; the cross-manager reference is gone.

### Documentation

- Man page updated with the chained build mode and the new
  flags.
- READMEs (en + fr / de / es / it / nl / pt) list the new
  build flags in their Options tables; English gets the full
  descriptive paragraph and worked examples.
- `doc/TODO_LOCAL_RPM_INDEX.md` captures the LocalRPM audit
  that led to the O(1) rewrite (kept as a reference for the
  0.9.x resilient-install chantier).
- `doc/TODO_RPM_VERSION_HELPER.md` captures the wider
  version-sort refactor deferred to 0.9.x.
- `doc/TODO_BUILD_MULTI_IMAGE_DASHBOARD.md` captures the
  long-term multi-image + dashboard vision for `urpm build`.
- Full i18n refresh: 1442 messages translated across the six
  languages; 0 untranslated, 0 fuzzy.

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
