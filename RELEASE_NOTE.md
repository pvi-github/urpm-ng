URPM & Rpmdrake-NG <Testing> 0.9.14:


> ** WARNING - the `distupgrade` feature is still young.**  Only
> use it on a machine you can afford to lose and reinstall, or on a
> machine you can snapshot and restore (VM, Clonezilla, btrfs/LVM
> snapshot…).  Every failure report makes the next version safer.

A release about a mechanism that had never run.  The post-operation
rules shipped in the 0.9 cycle were inert on every machine: the
per-package verdicts they are conditioned on were never written, so
every condition keyed on a finished package was false.  They fire now,
and the first rule to use them keeps urpmi's own configuration on the
release you have just upgraded to.

## What's Changed

### Major Features

- **Post-operation rules actually fire.**
  - The method that closes a package's history row had no caller.
    Every row stayed `planned`, and a distupgrade recorded no package
    rows at all, so a rule waiting for a package to be installed waited
    forever.
  - The verdict is now read from the rpmdb once the transaction is
    over, rather than from rpm's callback, which reports the end of an
    installation even when the payload failed to extract.  Install,
    upgrade, erase, distupgrade and the Tx B retry all write it.  A
    `--test` runs no transaction and leaves the history untouched.
  - Rules are read from `/usr/lib/urpm/hooks.d/` and can be masked by a
    file of the same name in `/etc/urpm/hooks.d/`.

- **urpmi's media follow the release.**
  - A distupgrade left urpmi pointing at the release you came from, so
    the two package managers disagreed about the machine they share.
  - The shipped rule moves the URL-direct media in the two shapes
    measured in the field: the release as a path segment
    (`/distrib/9/x86_64/media/…`, including the `mageia9` and `mga9`
    spellings community mirrors use) and the release inside the mirror
    API file name (`mageia.9.x86_64.list`), which is what
    `urpmi.addmedia --distrib` leaves behind for every official medium.
  - A number that only looks like a release is left alone: a mirror
    publishing Mageia under its own numbered directory keeps it.  An
    entry no rule could move is named to the operator rather than
    silently skipped, because the shapes a release can take in a URL
    are not enumerable.
  - The previous file is kept as `urpmi.cfg.urpm-ng.bak`.

- **A medium with nothing published for the new release is switched
  off.**
  - A third-party repository is free to lag behind a release, or to
    publish it under another URL scheme, and the entry we just rewrote
    then answers 404 on every urpmi run.
  - Each URL that was rewritten is checked with a single `HEAD` on its
    `media_info/synthesis.hdlist.cz`.  A 404 adds `ignore` to the
    block and names the medium, because putting it back means finding
    the new URL by hand.  Any other answer, and an unreachable server,
    change nothing.
  - The official media travel by mirror list and are cloned from one
    reference, so they are not checked: an absent one is absent
    everywhere.

- **The end-of-distupgrade report covers the whole distupgrade.**  Tx B
  does not commit once, it commits in batches, twenty-two of them on
  the machine this was measured on.  A single transaction id named only
  the last, so the scriptlet output and the `.rpmnew` list dropped the
  other twenty-one.  The perimeter is now read from the history, from a
  boundary kept in the distupgrade state.

- **Phase A is treated as the upgrade it is.**  The preparatory upgrade
  runs its own post-upgrade rules, and keeps the scriptlet output it
  used to discard.

### Bug Fixes

- **A dependency was matched on its name, and its version dropped.**
  - Two packages installable side by side provide the same capability
    at two versions: `lib64gnome-desktop-gir3.0` provides
    `typelib(GnomeDesktop) = 3.0`, `lib64gnome-desktop-gir4.0` the same
    name at `4.0`, and each consumer asks for one of them precisely.
  - Every reverse-dependency walk compared capability names alone.  The
    dead package looked required by every consumer of the live one, so
    `urpm autoremove` never proposed it, `urpm rdepends` named a
    package as its own predecessor's dependent, and `urpm e` could
    offer to remove the version that was still in use.  On a machine
    fresh out of a distupgrade, `urpme --auto-orphans` found two
    leftovers that `urpm autoremove` could not see.
  - One module now answers the question, with rpm's own comparison
    rules, for the six orphan detectors, the reverse-dependency verbs
    and the install-ordering graph.  The two paths that already held a
    libsolv pool ask it rather than rebuilding an index by hand.
  - Measured on a full pool: 10 652 of 225 628 dependency edges were
    imaginary.  The orphan list of a 3 116-package system is
    unchanged, which is the expected outcome where no two versions of
    a capability coexist.
  - The database-side query is not covered yet: on the media half,
    `urpm rdepends` still misses the requirements that carry a version.

### Improvements

- **A lighter core.**  The container tools (`urpm build`,
  `urpm image`, `urpm mkimage`), the image profiles and the modules
  behind them move to `urpm-ng-build`.  The verbs stay in
  `urpm --help` on a machine without it, and answer with the name of
  the package to install instead of a traceback.
- **`urpm cleanup` was two unrelated jobs behind one verb**, a chroot
  unmount that belongs to the build side and a history migration that
  belongs to core.  They no longer share a code path.  Splitting them
  at the level of the public verb belongs to the history rework.
- **A service restart asked for during a distupgrade is declined**, and
  said so as a notice: the running session is still on the previous
  release, so restarting is a policy decision rather than a failure.
- **First-pass translations in the six languages** for every new
  message.


**Full Changelog**: https://gitweb.mageia.org/software/rpm/urpm-ng/log/?h=release/0.9.x
