URPM & Rpmdrake-NG <Testing> 0.9.11:


> ** WARNING - the `distupgrade` feature is still young.**  Only
> use it on a machine you can afford to lose and reinstall, or on a
> machine you can snapshot and restore (VM, Clonezilla, btrfs/LVM
> snapshot…).  Every failure report makes the next version safer.

Three bug fixes, all from beta-tester reports on 0.9.10.  A fresh install on
cauldron could reach no mirror at all, `urpm genmedia -v` printed a traceback
instead of a log line, and the spec carried seventeen rpmlint warnings.

## What's Changed

### Bug Fixes

- **A fresh cauldron install reached no mirror at all.**
  - Every enabled medium was listed as having no server, and
    `urpm media update` failed on all of them.
  - The servers were never the problem: the mirror list was fetched
    correctly and six mirrors were added.  The media were pointing at
    `11/x86_64/media/…`, a tree no mirror serves until release day.
  - The `%post` imports `urpmi.cfg`, whose mirrorlist-based entries carry no
    URL, so an identity has to be derived for them.  It was read from
    `/etc/mageia-release`, whose line says « Mageia release 11 (Cauldron) »
    — and the pattern matched the number before the word.
  - `os-release` cannot tell the two apart either: a cauldron announces the
    version it is *becoming*.  `/etc/version`'s third field is the only thing
    on the system that names the branch (`11 0.0.5 cauldron`).
  - Five sites decided this independently, three of them wrong on a cauldron,
    which is why fixing them one at a time did not work.  There is now a
    single authority, and a guard test refuses any new site that reads those
    files to reach a mirror.
  - Switching `version-mode` now realigns `/etc/version` too, so the system
    stops telling one story while behaving another way.

- **`urpm genmedia -v` printed a traceback after every hdlist write.**
  - The debug call passed the path as a formatting argument to a message
    with no placeholder, so logging raised *not all arguments converted
    during string formatting* and the line was lost.
  - Only the verbose output was affected; the metadata produced was always
    correct.
  - A guard test now walks the tree and refuses any logging call whose
    placeholder count does not match its arguments.

### Packaging & Distribution

- **The spec is rpmlint-clean: 0 errors, 0 warnings, down from 17.**
  - `Group` was `System/Configuration/Packaging`, which rpmlint does not
    know.  `urpmi`, `rpm`, `rpmdrake` and `rpm-helper` all declare
    `System/Packaging`, and that is what urpm-ng is.
  - Four comments spelled a macro with a single `%`, which rpm expands.
  - The hooks drop-in directory is written `%{_exec_prefix}/lib`: same
    expansion, same installed path, but rpmlint matches on the literal text.
    `%{_libdir}` would be wrong — it is `/usr/lib64`, while these rules are
    arch-independent and the code reads `/usr/lib`.


**Full Changelog**: https://gitweb.mageia.org/software/rpm/urpm-ng/log/?h=release/0.9.x
