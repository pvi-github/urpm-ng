# Migrating from urpmi to urpm-ng

A one-page reference for Mageia users familiar with the classic
``urpmi`` tooling.  ``urpm-ng`` replaces the ``urpmi`` / ``urpme`` /
``urpmq`` / ``urpmf`` / ``urpmi.addmedia`` / ``urpmi.removemedia`` /
``urpmi.update`` set with a single ``urpm`` binary and subcommands.

Every subcommand has a short single-letter alias — this cheat sheet
uses the short forms because that is what daily use looks like;
long forms (``install``, ``erase``, ``upgrade``, …) work identically
and read better in scripts.

Read this once; keep it around when you help someone else migrate.

Placeholders in ``<angle brackets>`` are values you provide.

## Package operations

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Notes :
- ``--auto`` and ``-y`` are interchangeable everywhere in ``urpm-ng``.
- ``urpm remove`` is accepted as a courtesy for users coming from
  apt / dnf — the canonical verb is ``e`` (``erase``).

## Media management

| ``urpmi.*`` / ``urpmq``              | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi.update -a``                  | ``urpm m u``                 |
| ``urpmi.update <medianame>``         | ``urpm m u <medianame>``     |
| ``urpmi.addmedia <url>``             | ``urpm m a <url>``           |
| ``urpmi.addmedia --distrib <url>``   | ``urpm m disc <url>``        |
| ``urpmi.removemedia <medianame>``    | ``urpm m r <medianame>``     |
| ``urpmi.removemedia -a``             | ``urpm m r --all``           |
| ``urpmq --list-media``               | ``urpm m l``                 |

Notes :
- ``m`` is the short alias for ``media``.  ``m u`` = ``media
  update``, ``m a`` = ``media add``, ``m r`` = ``media remove``,
  ``m l`` = ``media list``, ``m disc`` = ``media discover``.
  Writing the full ``urpm media update`` etc. works exactly the
  same way.

## Queries

| ``urpmq`` / ``urpmf``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmq <pkg>``                      | ``urpm q <pkg>``             |
| ``urpmq -i <pkg>``                   | ``urpm sh <pkg>``            |
| ``urpmq -d <pkg>``                   | ``urpm d <pkg>``             |
| ``urpmq -R <pkg>``                   | ``urpm rd <pkg>``            |
| ``urpmf --provides <pkg>``           | ``urpm wp <pkg>``            |
| ``urpmf --whatrequires <pkg>``       | ``urpm wr <pkg>``            |
| ``urpmf --files <path>``             | ``urpm f <path>``            |
| ``urpmq --list-orphans``             | ``urpm l --orphans``         |

Notes :
- Short aliases : ``q`` = ``query`` (also ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (also ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Build / distribution

| classic Mageia                       | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Behaviour differences worth knowing

- **One binary, subcommands.**  All operations live under ``urpm``.
  Bash completion is installed by default.
- **``urpm.cfg`` replaces ``urpmi.cfg``** at ``/etc/urpm/urpm.cfg``.
  On first run, ``urpm m import`` reads the legacy
  ``/etc/urpmi/urpmi.cfg`` and migrates every entry, including
  ``MIRRORLIST``-based ones — no manual editing needed.
- **Native rollback.**  ``urpm h`` (history) and ``urpm rollback``
  cover every transaction — no need for third-party snapshot
  tooling.
- **P2P LAN cache.**  If ``urpmd`` runs on multiple machines on the
  same LAN, they share downloaded packages automatically.  No
  configuration needed.
- **Container / build image support.**  ``urpm image make`` builds
  a minimal Mageia chroot / container image ready for
  ``urpm build`` — no ``mach`` / ``mock`` hackery needed any more.
- **Exit codes are structured** — see ``urpm(1)`` ``EXIT CODES``.
  The common ones match urpmi (0 = success, non-zero = something
  to look at).

## Quick start after installation (if not installed as a RPM)

```sh
# Import the media you already had under urpmi
sudo urpm m import

# Attach mirrors to the mirrorlist-based media that just came in
sudo urpm srv autoconfig

# Refresh package lists
sudo urpm m u

# You are ready
urpm q firefox
sudo urpm i firefox
```

## Full documentation

- ``urpm --help`` (also ``urpm <subcommand> --help``)
- ``man urpm``
- [README.md](README.md) — installation and features overview
- [CHANGELOG.md](CHANGELOG.md) — release-by-release history
