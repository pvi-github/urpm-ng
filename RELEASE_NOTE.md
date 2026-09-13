URPM & Rpmdrake-NG <Testing> 0.9.10:


> ** WARNING - the `distupgrade` feature is still young.**  Only
> use it on a machine you can afford to lose and reinstall, or on a
> machine you can snapshot and restore (VM, Clonezilla, btrfs/LVM
> snapshot…).  Every failure report makes the next version safer.

Mostly a Discover release.  Updates through the graphical centre now show a
progress bar from the first byte, report their failures instead of claiming
success, and no longer kill the window they run in.  Packages can also ask
for an action once the transaction is over, which rpm's own scriptlets
cannot do.

## What's Changed

### Major Features

- **Post-operation hooks: a package can ask for an action once the operation
  is really over.**
  - None of rpm's own hooks can do this.  `%post`, `%posttrans` and file
    triggers all run *inside* the transaction, so a scriptlet can neither act
    on the process driving it nor rely on the transaction having finished.
  - The model is APT's `DPkg::Post-Invoke`: actions run by the package
    manager once rpm has returned.  We diverge on one point, deliberately: no
    command lines, only a closed vocabulary of verbs, because a drop-in
    directory running as root is a privilege surface.
  - Rules live in `/usr/lib/urpm/hooks.d/*.cfg` (shipped by packages) and
    `/etc/urpm/hooks.d/*.cfg` (administrator), the second masking the first by
    file name, the way tmpfiles and sysusers already work on Mageia.
  - A rule watches a capability and takes the subject of its action from that
    capability's value, so one generic rule covers every package: write
    `Provides: restart-on-completion = my-service` in your spec and nothing
    else is needed.  Without a rule the capability does nothing at all, which
    is the line between declaring a need and instructing an action.
  - Reporting is the default, as `needrestart` does.  Restarting sshd or a
    display manager underneath a live session is a proven way to break a
    machine, so an administrator has to ask for `action = restart-service`
    explicitly.  Restarts are try-restart: a service that was not running is
    never started.
  - Services are driven through Mageia's own `service` wrapper rather than
    `systemctl`, so the mechanism works whatever the init system is.

### Improvements

- **Discover finally draws a progress bar for updates and removals.**
  - Only the install path ever subscribed to progress signals; update and
    remove called their D-Bus method synchronously, which cannot dispatch a
    signal at all.
  - The per-package bars Discover draws come from `ItemProgress` alone, which
    needs a full package id.  The download callback only had a bare package
    name, so nothing appeared on screen for the whole download.
  - Scriptlets froze the package counter, so the bar looked hung exactly when
    the operation took longest.  They get their own phase now.
  - The overall percentage is computed from the resolved plan rather than a
    hardcoded half-and-half split, so an operation served from cache starts at
    zero and a removal gets the whole bar.
  - Speed, bytes remaining and per-package progress now reach PackageKit; the
    download callback already received them and threw them away.

- **Searching a full media set is no longer unusable.**
  - SQLite has no cost estimate for a virtual table, so the planner re-ran the
    full-text query once per package of the enabled media: 61863 lookups for a
    query that asked for one.
  - `search('lib', 500)` never completed and was killed at 45 s; it now
    answers in 0.089 s.  `search('firefox', 100)` goes from 1.69 s to 0.011 s.

- **Discover shows the version you actually have.**
  - "Is this installed?" was answered by package name, then handed back a
    *media* row's version under that flag, so an arbitrary old row was
    displayed as the installed one: `firefox 140.12.0 -> 153.2.0` on a machine
    running 153.1.0.
  - Resolving 3141 names goes from 1.6 s to 0.06 s along the way, one cached
    `rpm -qa` replacing a batch query per call.

- **Discover's Sources page is no longer empty.**  `GetRepoList` was
  implemented on neither side, so the daemon answered "not supported by
  backend" while urpm had the media in its database all along.

- **Thousands of redundant progress signals removed.**  rpm calls back once
  per cpio block: a one-package upgrade put 9800 signals on the bus in sixteen
  seconds, peaking at 3308 in a single second, nearly all carrying the value
  before them.

### Bug Fixes

- The `%post` of the PackageKit backend restarted `urpm-dbus`, which is the
  very service executing the transaction when the update comes from Discover.
  The package decapitated itself: rpm was cut off halfway and the packages
  ordered after it were never installed.
- A failed upgrade through D-Bus reported success.  The result of the
  transaction was discarded, and a plan whose downloads had all failed fell
  into the "nothing to upgrade" branch.  It also could not go on to erase the
  obsoleted packages on its own, leaving a system without what was meant to
  replace them.
- The service ended its main loop without giving up its bus name, so the
  request Discover sends right after an upgrade reached a process on its way
  out and never got an answer.
- Installing urpm-ng killed the running Discover and stopped the PackageKit
  daemon, to make them re-read their settings.  Both made sense when urpm-ng
  was only installed from a terminal.
- The progress bar went backwards during header verification.  PackageKit
  refuses a decreasing percentage and discards every later one, so the bar
  froze where it had peaked.
- Cache invalidation signals were emitted from a worker thread, where the API
  asserts and returns without emitting, so Discover kept redrawing stale
  lists.
- A double unreference on the install preview reply took a GLib object's
  refcount below zero, on every single install.
- Network isolation during builds left the loopback interface down, so every
  `%check` binding a local socket failed with an error that never mentions the
  network.
- `urpm image make --addmedia` built a command against a signature that had
  since changed, and a `file://` medium was invisible from inside the
  container.

### Packaging & Distribution

- `urpm/dbus` and `urpm/auth/polkit.py` now ship with
  `urpm-ng-packagekit-backend`, which owns the unit, the launcher and the bus
  activation file, instead of with `urpm-ng-daemon`, which imports neither.
- `ProtectHome=yes` removed from the D-Bus unit: a package manager runs rpm
  scriptlets as root, so sandboxing it is decorative, and this one made any
  `file://` medium under a home directory invisible to the service.
- New user-facing strings translated into the six supported languages (de, es,
  fr, it, nl, pt).


**Full Changelog**: https://gitweb.mageia.org/software/rpm/urpm-ng/log/?h=release/0.9.x
