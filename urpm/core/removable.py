"""Locating a removable medium that is already mounted.

A DVD is not a kind of media, it is a *source* for media we already
know.  Measured on a real Mageia 10 x86_64 disc : its ``core`` tree
holds 4488 package names, every one of them at a NEVRA identical to
the mirror's ``Core Release``, with nothing diverging and nothing
exclusive to the disc.  So a pressing never competes on content, it
only offers a cheaper origin for part of a medium — which is why a
disc is a row in ``server`` linked through ``server_media``, and not
a medium of its own.

The protocol on that row is :data:`PROTOCOL`, and it is an *identity*
rather than something libcurl could fetch.  A disc has no stable
location : the same pressing is ``/run/media/<user>/Mageia-10-x86_64``
under a graphical session and ``/mnt/cdrom`` when mounted by hand, and
either path is stale the moment the tray opens.  So nothing is stored
about where the disc lives.  Instead, every operation that needs it
calls :func:`resolve`, which looks at what is mounted *now* and hands
back a server dict pointed at ``file://<mount>`` for the duration of
that operation only.

We never mount anything ourselves.  udisks is ruled out by the
project's zero-systemd-dependency rule, and mounting a guessed device
as root, with filesystem detection, on a mount point we invent, is
hard to undo and easy to get wrong : busy mount points, orphan mounts
after a crash.  So we do what :mod:`urpm.core.userns` does for
delegated uid ranges — detect, report, and let the human act.

The identification method is urpmi's, deliberately : read the mount
table, keep the filesystems a pressing can carry, and for each
candidate test whether *the medium we are after* has its synthesis
readable underneath.  See ``urpm::cdrom::_look_for_mounted_cdrom_in_mtab``
and ``_find_blist_matching`` in urpmi.  It means a disc is recognised
by what it contains, not by where it sits, so both the
session-mounted and the hand-mounted case travel the same code path
and no mount point is ever hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .sync import SYNTHESIS_PATH


#: Protocol stored on a ``server`` row that stands for a removable
#: disc.  Not a scheme libcurl knows : :func:`resolve` must turn it
#: into ``file://`` before any transfer is attempted.
PROTOCOL = "cdrom"

#: Filesystems a pressed Mageia disc can carry.  ``iso9660`` is what
#: the udisks mount of a Mageia DVD actually reports (verified on a
#: 10 x86_64 disc) ; ``udf`` covers the hybrid pressings and anything
#: burnt from a UDF image.  Same pair as urpmi's.
REMOVABLE_FILESYSTEMS = frozenset({"iso9660", "udf"})

#: The kernel's mount table.  ``/etc/mtab`` is a symlink to it on any
#: current system, and reading the kernel's own view avoids trusting a
#: file a chroot may have left behind.
MOUNT_TABLE = Path("/proc/mounts")


@dataclass(frozen=True)
class MountPoint:
    """One mounted filesystem that could be a pressing.

    Attributes:
        path: Where it is mounted, escapes already decoded.
        device: The backing device, for the message that asks the
            operator to mount or swap a disc.
        filesystem: ``iso9660`` or ``udf``.
    """

    path: Path
    device: str
    filesystem: str


def is_removable_server(server: Optional[Dict]) -> bool:
    """Whether *server* stands for a removable disc.

    The counterpart of :func:`urpm.core.config.is_local_server`.  Note
    that the two are mutually exclusive on a stored row and both true
    on nothing : a disc only becomes local once :func:`resolve` has
    found it, and what it returns is a ``file`` server.
    """
    return bool(server) and server.get("protocol") == PROTOCOL


def was_resolved_from_disc(server: Optional[Dict]) -> bool:
    """Whether *server* is a disc that :func:`resolve` has located.

    What ``resolve`` hands back is a ``file`` server, deliberately so
    that the transfer code treats a pressing as the local tree it is.
    The price is that :func:`is_removable_server` no longer recognises
    it, so a caller that needs to *report* where a file came from asks
    this instead.

    Keyed on the mount ``resolve`` recorded, which is always a real
    path, and not on the identity, which is the empty string when the
    row carries no label.
    """
    return bool(server) and "removable_mount" in server


def mirrors_only(servers: Sequence[Dict]) -> List[Dict]:
    """Drop the removable sources from a list meant for mirrors.

    Some operations are about mirrors as such rather than about
    getting at a file, and a disc has no business in them : comparing
    a candidate mirror's synthesis against a reference (a pressing
    carries a *subset*, so its checksum can never match a full
    mirror's), probing latency, or a daemon refreshing metadata in the
    background, which must not depend on what is in the tray.

    Spelling it this way keeps the intent at the call site readable,
    where a bare ``protocol != 'cdrom'`` would look like an oversight.
    """
    return [s for s in servers if not is_removable_server(s)]


def _unescape(field: str) -> str:
    """Decode the octal escapes the kernel writes in the mount table.

    Space, tab, newline and backslash appear as ``\\040``, ``\\011``,
    ``\\012`` and ``\\134`` because the table is whitespace-separated
    (see ``fstab(5)``).  A disc labelled ``Mageia 10`` mounts under a
    path containing a space, so skipping this step would hand back a
    path that cannot be opened.
    """
    out: List[str] = []
    i = 0
    while i < len(field):
        digits = field[i + 1:i + 4]
        if field[i] == "\\" and len(digits) == 3:
            try:
                out.append(chr(int(digits, 8)))
            except ValueError:
                # Not octal after all (``\\089``) : keep it verbatim.
                out.append(field[i])
            else:
                i += 4
                continue
        else:
            out.append(field[i])
        i += 1
    return "".join(out)


def mounted_removable(table: Optional[Path] = None) -> List[MountPoint]:
    """The mounted filesystems that could carry a pressing.

    Args:
        table: Mount table to read.  Defaults to :data:`MOUNT_TABLE`,
            resolved at call time rather than captured as a default
            argument so that the module constant stays the single
            source of truth — binding it in the signature would let a
            test aim this at a fixture and still read the kernel's
            table, which is a lie that passes.

    Returns:
        One entry per mount whose filesystem is in
        :data:`REMOVABLE_FILESYSTEMS`, in mount-table order.  Empty
        when nothing is mounted, which is the normal case and costs
        one small read.
    """
    try:
        raw = (table or MOUNT_TABLE).read_text()
    except OSError:
        # No mount table is not an error here : it means no disc, and
        # the caller's job is to say so, not to crash.
        return []

    found: List[MountPoint] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mount_point, filesystem = fields[0], fields[1], fields[2]
        if filesystem not in REMOVABLE_FILESYSTEMS:
            continue
        found.append(MountPoint(
            path=Path(_unescape(mount_point)),
            device=_unescape(device),
            filesystem=filesystem,
        ))
    return found


def locate(relative_path: str,
           mounts: Optional[Sequence[MountPoint]] = None
           ) -> Optional[MountPoint]:
    """Find the mounted disc that carries the medium at *relative_path*.

    Identity is containment, not location : a candidate qualifies when
    the medium's own synthesis is readable underneath it.  That is
    urpmi's test, and it is what makes the mount point irrelevant.

    Args:
        relative_path: Where the medium sits inside the disc, relative
            to the mount point, e.g. ``x86_64/media/core``.  This is
            the per-pair path stored on ``server_media``, *not* the
            mirror layout on ``media.relative_path``.
        mounts: Candidates to consider.  Defaults to whatever
            :func:`mounted_removable` finds.  Passing a list lets a
            caller probe several media against one mount-table read.

    Returns:
        The matching mount, or ``None`` when no mounted disc carries
        that medium — which covers both « no disc at all » and « the
        wrong disc is in the drive ».  The caller decides whether that
        is fatal : it is only fatal when the disc is the sole source.
    """
    if mounts is None:
        mounts = mounted_removable()
    wanted = relative_path.strip("/")
    for mount in mounts:
        if (mount.path / wanted / SYNTHESIS_PATH).is_file():
            return mount
    return None


def resolve(server: Dict, relative_path: str,
            mounts: Optional[Sequence[MountPoint]] = None
            ) -> Optional[Dict]:
    """Turn a ``cdrom`` server row into a usable ``file`` server.

    This is the resolution step the whole design rests on : the stored
    row is an identity, and here it becomes a location, for the
    duration of one operation.  Everything downstream — URL building,
    the local-copy branch of the downloader, freshness checks against
    mtime — then works on a disc without knowing a disc is involved,
    because what it gets is an ordinary ``file://`` server.

    The returned dict is a copy and **must not be written back** to
    the database : its ``base_path`` is true only while the disc stays
    mounted where it is. The row's ``id`` is preserved, so reputation
    and statistics still key correctly.

    Args:
        server: A ``server`` row.  A row that is not removable is
            returned unchanged, so callers can funnel every server
            through here without branching first.
        relative_path: The medium's path inside the disc.
        mounts: Candidates, as for :func:`locate`.

    Returns:
        A resolved copy, or ``None`` when the disc is not available.
    """
    if not is_removable_server(server):
        return server

    mount = locate(relative_path, mounts)
    if mount is None:
        return None

    resolved = dict(server)
    resolved["protocol"] = "file"
    resolved["base_path"] = str(mount.path)
    # Keep the per-pair layout on the dict so ``build_media_url`` uses
    # the disc's own tree rather than the mirror's.
    resolved["media_relative_path"] = relative_path.strip("/")
    # Kept for diagnostics and for the messages that name the disc :
    # once ``protocol`` says ``file``, nothing else would remember
    # that this source is a pressing that can be ejected.
    resolved["removable_identity"] = server.get("host") or ""
    resolved["removable_mount"] = str(mount.path)
    resolved["removable_device"] = mount.device
    return resolved
