# TODO: Multi-source builddep tracking (Approach B)

## Current state (Approach A)

Each builddep package is tracked with a single source (the first spec/srpm
that caused its installation). `urpm autoremove --buildrequires` removes all
builddep packages at once, regardless of source.

CLI flag: `--buildrequires` (aliases: `--builddeps`, `--br`, `-b`).

## Goal: per-source cleanup

Allow `urpm autoremove --buildrequires foo.spec` to remove only the builddeps
that are exclusive to `foo.spec`, keeping those shared with other specs.

### Format change

Store comma-separated sources:

```
cmake	foo.spec,bar.spec
gcc-c++	foo.spec
```

### Behavioral changes

- `urpm install --buildrequires bar.spec`: if `cmake` is already a builddep of
  `foo.spec`, append `bar.spec` to its source list (even if cmake was not in
  `result.actions` because it was already installed).
- `urpm autoremove --buildrequires foo.spec`: only remove builddeps whose source
  list contains `foo.spec` AND that are not shared with another source. For
  shared packages, remove `foo.spec` from the source list but keep the package.
- `urpm autoremove --buildrequires` (no argument): remove all builddeps (current
  behavior).

### Implementation notes

- `mark_as_builddep()` must also mark packages already installed (not just
  those in `result.actions`). Iterate over all resolved requirements.
- `_get_builddep_packages()` returns `dict[str, list[str]]` instead of
  `dict[str, str]`.
- Add `--buildrequires [SPEC]` with `nargs='?'` to argparse so it accepts an
  optional argument.
