URPM & Rpmdrake-NG <Testing> 0.9.13:


> ** WARNING - the `distupgrade` feature is still young.**  Only
> use it on a machine you can afford to lose and reinstall, or on a
> machine you can snapshot and restore (VM, Clonezilla, btrfs/LVM
> snapshot…).  Every failure report makes the next version safer.

A release about waiting less and removing less.  `urpm media update`
hands the terminal back once the synthesis is in, instead of holding it
for two minutes over a file the next install will never open.  And
`urpm autoremove` stopped offering to remove packages that are still in
use.  It did so on any machine carrying two versions of a same name,
which is what a distupgrade leaves behind.

## What's Changed

### Major Features

- **`urpm media update` returns once the synthesis is in.**
  - A refresh moves two very different piles: 4.8 MB of synthesis
    across a typical fifteen media, which a resolution reads, and
    36.3 MB of `files.xml.lzma` plus the AppStream catalogue built from
    it, which only `urpm f`, `urpm show --files` and the distupgrade
    file-provides injection read, later.
  - The tail is one file: 23 MB for core/release, on a four-worker pool
    where the other three finish early and wait.  At 300 kB/s, ordinary
    for a busy mirror, that is two minutes of waiting.
  - A bare `urpm media update` now returns after the head and hands the
    tail to urpmd.  Without urpmd it runs synchronously, as before, and
    a named medium is always synchronous.

- **Sizes now say what is left to fetch.**
  - The download total was summed over the whole plan and shown before
    anything looked at the cache, so a resumed distupgrade announced it
    would download everything again.
  - The summary now splits what is already on disk from what will
    really cross the network, at all four confirmation sites.

- **`urpm f` says which media it could not search.**
  - It answers from the indexes already on disk and never downloads, so
    a medium whose index is absent, unreadable or an empty stub was
    skipped in silence, and "no package contains X" read as a complete
    search.
  - Skipped media are now listed after the results, with the reason,
    in full, whatever the outcome.

### Bug Fixes

- **`urpm autoremove` offered to remove packages that were still in
  use.**  rpm refused the transaction after the operator had confirmed
  it.  Two independent causes.
  - The dependency maps were keyed on the package name with a plain
    assignment, so a name installed twice lost one of the two sets of
    requires, and the packages it needed looked unreferenced.  The
    verdict even depended on the order rpm happened to enumerate the
    rpmdb in.
  - Taking a package out of the removal list did not take out what it
    depends on.  Declining to remove `dhcp-client` still queued
    `dhcp-common`, which it requires exactly.

- **The protection lists applied to one removal path out of three.**
  - The blacklist and the redlist were read in exactly one place, so
    `autoremove --interactive` and `cleandeps` were free to offer a
    package the classic path refuses to touch.
  - A list whose purpose is to keep a system bootable did not apply to
    the flow most likely to be used right after a distupgrade, which
    is when the orphans appear.
  - The three paths now share one reading of both lists, and the
    triage says what it pulls back before asking for confirmation
    rather than leaving rpm to reject the transaction afterwards.

- **A rejected removal truncated its own diagnosis** after three
  lines, each of which names a package that has to be kept.

- **Install sizes were wrong in three ways.**  A local `.rpm` announced
  a 0 B footprint and 390.9 MB freed on an upgrade that in fact left
  the disk 2 MB heavier: the compressed payload was read as the space
  the root filesystem would hold, a local package had no file size at
  all, and the freed volume was gross rather than net.

- **Commands the machine cannot run were handed out.**  Mageia installs
  neither `sudo` by default nor the first user into a sudoer group, yet
  eight messages spelled out `sudo …`, the quick-start guide being four
  of them in a row.  Each command is now rendered with the escalation
  this machine can actually offer, or named bare.

- **A `media_info` file left at 0600 by an older build stayed that
  way.**  `urpm f` then answered "nothing found" instead of "I cannot
  read this index".  A sync now restores the published mode on a named,
  closed list of artefacts.

- **Yes/no prompts only understood English and French.**  A prompt that
  defaults to yes needs its own reader; a German or Dutch "no" was
  taken for a "yes".

- **A failed removal claimed it had succeeded.**  Six call sites
  printed `[N/N] done` unconditionally, one line before reporting the
  failure, and rpm rejects a transaction before touching a single
  package.  The dependency error itself was a raw rpmlib tuple.

- **`urpm distupgrade --help` was half French in English.**

- **`media add --import-key` announced a URL it would not fetch**, a
  doubled slash when one was typed.

### Improvements

- **The translation backlog is cleared**, six languages complete, zero
  fuzzy.  `urpm/core/resolution/diagnose.py` was the only module
  calling `_()` that was missing from `POTFILES.in`, and its msgids
  were written in French, so every user in every locale read French
  there.
- **One home for `@System` and `@LocalRPMs`**, the two libsolv
  pseudo-repositories, instead of thirty-six literals.


**Full Changelog**: https://gitweb.mageia.org/software/rpm/urpm-ng/log/?h=release/0.9.x
