URPM & Rpmdrake-NG <Testing> 0.9.12:


> ** WARNING - the `distupgrade` feature is still young.**  Only
> use it on a machine you can afford to lose and reinstall, or on a
> machine you can snapshot and restore (VM, Clonezilla, btrfs/LVM
> snapshot…).  Every failure report makes the next version safer.

`urpm install` now takes a URL.  And building got unblocked: `urpm build`
could not compile python, whose `%prep` died restoring file ownership and
whose test suite could not run rootless, while `urpm image make` stalled on
a key confirmation nobody could answer.

## What's Changed

### Major Features

- **`urpm install` accepts a URL.**
  - `urpm i https://host/path/pkg.rpm`, mixed freely with package names
    and local files.
  - The package is fetched first, then treated exactly like a file
    already on disk: header read, signature verified unless
    `--nosignature`, dependencies resolved from the configured media.
  - `file:///path/pkg.rpm` works too; it used to fail the same way,
    the scheme never being stripped.
  - The file lands in `/var/lib/urpm/downloads/`, beside `medias/` and
    never inside it: that tree mirrors a remote one medium by medium,
    and a package named by a URL belongs to no medium.
  - It is cache all the same, so `urpm cache flush` sweeps it and
    `urpm cache clean` counts it as an orphan.

### Bug Fixes

- **Network isolation made non-root file ownership unrestorable.**
  - Every `rpmbuild` ran under `unshare --user --map-root-user --net`,
    a detour taken because `unshare --net` needs `CAP_SYS_ADMIN` and
    the container had none.
  - `--map-root-user` writes a one-line `uid_map`, so uid 0 became the
    only translatable identity.  Any `chown` towards another uid
    answered `EINVAL`.
  - python's `%prep` died unpacking its documentation tarball, whose
    files belong to uid 1000, with `tar: Cannot change ownership to
    uid 1000, gid 1000: Invalid argument`.  Every spec restoring
    archive ownership, or chowning to a system user in `%install`, hit
    the same wall.
  - The capabilities now come from the container itself, `SYS_ADMIN`
    and `NET_ADMIN` for the namespace, `NET_RAW` in every mode so a
    `%check` can still ping the loopback.  No user namespace is
    created and the identity map stays whole.
  - `--net-isolation auto|strict|off` exposes the choice;
    `--with-network` is kept as an alias for `off`.  On a runtime that
    cannot isolate, `auto` builds with the network open and says so,
    `strict` refuses.

- **`urpm image make --import-key` asked a question nobody could answer.**
  - The confirmation ran inside the container, through `podman exec`,
    which gets a pseudo-terminal for its output and no stdin.
  - So the key's id and fingerprint appeared, the prompt waited, and
    the media add aborted on end-of-file, failing the image build.
  - `--import-key` on the host command line is the consent; the second
    question is gone.  The key's identity is still printed.

### Improvements

- **`urpm build` checks the id delegation before it starts.**
  - A rootless container can only name the uids its user namespace
    translates, and `/etc/subuid` delegates 65536 by default.  Plenty
    for system users, short for a `%check`: python's `test_posix`
    chowns to 2³¹ on purpose, to exercise large values.
  - Past that range the kernel answers `EINVAL` and the build dies
    eleven thousand log lines in, on an error that never mentions
    delegation.
  - The check names the exact `/etc/subuid` line to replace, gives the
    commands for a root shell, and asks whether to build anyway.
    `--auto` prints the same recommendation without asking, and so
    does a run with no terminal.
  - Widening keeps the range start, so images already on disk stay
    valid.  The change takes effect through `podman system migrate`,
    which needs no container running: until then the pause process
    holds the old map alive, which is why the edit can look ignored.
  - With the delegation widened, python 2.7.18 builds with its full
    test suite passing, spec untouched.

- **`--nocheck` skips a spec's `%check` section.**
  - Handed to `rpmbuild`, so it is all or nothing: rpm has no notion
    of an individual test.
  - For a suite that cannot pass in a rootless container, or simply to
    shorten a development iteration.
  - The packages produced are untested, and the build line says so.

### Packaging & Distribution

- **The spec review is applied**, seven points of eight, each checked
  against the packages on a Mageia 10 machine rather than taken on
  trust.
  - `Requires: python3` is redundant in both specs: rpm generates
    `python(abi) = 3.13` from the `.py` files and only python3
    satisfies it.
  - rpmdrake-ng's `%post` and `%postun` are gone entirely.
    desktop-file-utils and hicolor-icon-theme ship file triggers that
    already do the work, which also drops two generated `Requires` on
    `/bin/sh`.
  - `%setup` gives way to `%autosetup -p1`, and to `-a 1` for urpm-ng's
    second source.
  - `python3-setuptools` and `python3-wheel` are hard requirements of
    pyproject-rpm-macros, so they go.
  - Two points were not applied: pyproject-rpm-macros stays, since on
    Mageia nothing else pulls it in, and `python3dist()` fits only
    four dependencies out of seven.


**Full Changelog**: https://gitweb.mageia.org/software/rpm/urpm-ng/log/?h=release/0.9.x
