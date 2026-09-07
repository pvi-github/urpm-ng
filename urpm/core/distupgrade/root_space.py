"""Will the disks hold the upgrade?

Nothing answered that before this module.  Two space checks existed and
neither covered it: :meth:`urpm.core.download.Downloader.
assert_space_for` sizes the *payload* directory, and
:func:`urpm.core.distupgrade.checks.check_boot_space` sizes ``/boot``.
The filesystem that actually has to absorb a cross-release upgrade —
several gigabytes of new ``/usr`` — was never measured, and rpm only
notices at commit time, thousands of packages and several gigabytes of
download too late to be of any use in deciding whether to start.

Two different things land in two different places, and on a stock
Mageia install they are not on the same partition by accident: the
payload goes to ``/var/cache/urpm`` and the unpacked files to ``/usr``.
Where ``/var`` is its own filesystem, adding the two together and
comparing the sum to either one answers a question nobody asked — it
would refuse an upgrade whose payload fits ``/var`` comfortably because
``/usr`` is tight, and pass one that fills ``/var`` mid-download.  So
each filesystem gets its own verdict, and they collapse back into one
when ``st_dev`` says they are the same.

Every term below is read off the machine or off the plan.  A pre-flight
built on invented constants would be worse than none: it would refuse
upgrades that fit and wave through ones that do not, and an operator
who has been refused once for no reason never trusts the check again.

**Net footprint** — ``installed − freed`` from :func:`urpm.core.
transaction_sizes.compute_sizes`, charged to the filesystem holding
``/usr``.  Installed sizes come from the synthesis for what arrives and
from ``RPMTAG_SIZE`` for what leaves.

**Deferred erases** — the peak sits above the net precisely because
Tx B holds some erases back: what :mod:`live_system` ranks
``SACRIFICIAL`` goes in the penultimate batch and ``CRITICAL`` in the
last, so their bytes come back only at the very end, while everything
has already been installed.  Summing exactly that set is why this
figure is machine-specific: it depends on what happens to be running.
An unreadable ``/proc`` yields an empty snapshot, which defers nothing
— so a zero here is the right answer, not a degraded one.

**Payload** — charged to whichever filesystem the payload directory
sits on.  Only what has yet to arrive counts: whatever is already
cached is already missing from the free figure, and ``--resume``
re-enters Stage 2 with the whole payload on disk.

The payload and the footprint are *not* added together, which is the
other half of what made this check demand 14 GB where under 5 were
needed.  Tx B unlinks each batch's ``.rpm`` as soon as it commits
(:func:`stage3._purge_installed_batch_rpms`), so the cache drains at
the rate the footprint arrives.  At the start of Tx B the disk holds
the payload and nothing else; at the end it holds the footprint and no
payload.  The peak along that path is the larger of the two, not their
sum.

**Transient** — inside one batch, rpm writes the new files before its
commit frees the replaced ones, so a batch briefly costs its full
uncompressed size.  ``stage3.TX_B_BATCH_BYTES`` bounds a batch at 200
MB of *compressed* payload; multiplying by the plan's own
installed-to-download ratio gives what that batch weighs unpacked.
Both numbers are read, not chosen.

**Margin** — the filesystem publishes its own.  ``statvfs`` reports
``f_bfree`` (free) and ``f_bavail`` (free to a non-root process); the
difference is the reserved-block pool that ``mkfs`` set aside, 5% by
default on ext4.  A distupgrade runs as root and could eat into it; the
margin is deciding not to.  The number follows the machine — one
formatted with ``-m 0`` has no reserve, which is exactly what its
administrator asked for.

**Headroom** — the one figure here that is chosen rather than read,
so it is named, displayed on its own line, and kept proportional:
``HEADROOM_RATIO`` of the gross installed footprint.  It is advice
about a margin, never part of the estimated need.  It stands for
what the plan genuinely cannot describe — files scriptlets create (a
regenerated initramfs, font and icon caches, ``ldconfig`` output, none
of them owned by any RPM), rpmdb growth, and how much of the deferred
erase set a given machine really holds back.  Proportional rather than
flat so a point release gets a small allowance and a cross-release
migration a real one.  It is displayed rather than folded into the
total because a visible allowance stays arguable and a hidden one
becomes dogma.

One mga9 → mga10 migration has been measured against it end to end, on
a 17 GB root with 5.98 GB free and an 11 GB gross footprint:

===========================  =========  ========
term                         predicted  measured
===========================  =========  ========
net footprint                  4.42 GB   4.88 GB
one batch unpacked             0.64 GB   0.45 GB
estimated need                 5.06 GB   5.33 GB
===========================  =========  ========

So the terms read off the plan land within a few hundred MB, and the
estimate under-calls the peak by 0.27 GB — under-calling is the
direction that fills a disk mid-migration, which is exactly what the
headroom is advice about.  10% is roughly four times what this machine
needed, deliberately: it is one measurement, on one desktop, and what
it stands for (initramfs regeneration, font caches) scales with how
many kernels and how much of KDE a machine carries.  Shrinking it to
fit this run would be fitting the noise.

**Nothing here refuses.**  An earlier cut did, and stopped a tester
169 MB short on a machine that had completed the same migration three
times.  The asymmetry is what settles it: a false refusal is strictly
worse than no check at all — it aborts a migration that would have
worked, and since the pre-flight cannot run before Stage 1 has swapped
the media, every refusal costs a switchover, an eight-medium sync, a
solve and a rollback.  A false pass costs what the status quo costs,
which is nothing extra.  A model calibrated on one machine, on one
run, does not earn a veto against those odds.

Nor does the *display* refuse, which took a second pass to get right.
Charging the headroom into the estimated need read as « 6.15 GB needed,
5.98 GB available » on a machine whose real peak was 5.33 — a figure
no operator would start a two-hour migration against.  The veto had
left the code and stayed in the number.  So the estimate is the plan's
own arithmetic, the headroom sits beside it as advice, and there are
three things to say : nothing when the margin is comfortable, that the
margin is thin and more would be safer when it fits without it, and
that the filesystem is too small when the plan's own figures do not
fit.  The operator reads it next to the plan they are confirming.

One thing is knowingly not counted: package metadata carries no file
list, so a plan's footprint cannot be split across filesystems.
Packages installing under ``/var`` or ``/opt`` are charged to ``/usr``
when those are separate mounts.  That over-states ``/usr`` (the safe
direction) and under-states the others, which is why they are named in
the report.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from ...i18n import _
from ..cache import format_size
from ..transaction_sizes import compute_sizes

logger = logging.getLogger(__name__)


#: Headroom worth having beyond the estimate, as a share of the gross
#: installed footprint.  It covers what the plan cannot describe :
#: scriptlet output, rpmdb growth, and how much of the deferred erase
#: set a given machine really holds back.
#:
#: It is a recommendation, not a requirement, and it is kept out of the
#: estimated need for that reason.  Folded in, it turned a measurement
#: into a verdict — « 6.15 GB needed, 5.98 GB available » on a machine
#: whose real peak was 5.33, which no operator would take a two-hour
#: migration on.  Removing the veto from the code and leaving it in the
#: figure would have refused just the same, only without saying so.
HEADROOM_RATIO = 0.10


@dataclass(frozen=True)
class FilesystemNeed:
    """One filesystem, what it must absorb, and what it has.

    All figures in bytes.  A machine with ``/usr`` and ``/var`` on the
    same partition produces exactly one of these; splitting them
    produces two, each measured against its own free space.
    """

    mountpoint: Path
    #: Net footprint of the unpacked files : installed minus freed.
    unpacked: int = 0
    #: Erases Tx B holds back to the last batches, so their bytes are
    #: not available while the installs are landing.
    deferred: int = 0
    #: RPMs still to download.  Tx B unlinks each batch's payload as it
    #: commits, so this drains as :attr:`unpacked` accrues.
    payload: int = 0
    #: One batch, unpacked : what rpm writes before its commit frees
    #: the versions it replaces.
    transient: int = 0
    #: Room worth having on top.  A recommendation, deliberately kept
    #: out of :attr:`required` — see :data:`HEADROOM_RATIO`.
    headroom: int = 0
    available: int = 0
    reserved: int = 0

    @property
    def required(self) -> int:
        """What the plan says this filesystem has to hold, at its peak.

        Every term here is read off the plan or the machine, which is
        what makes it worth putting in front of an operator.

        The payload and the unpacked footprint never coexist at full
        size — Tx B unlinks each batch's ``.rpm`` as soon as it commits,
        so the cache drains at the rate the footprint arrives.  The disk
        holds the payload at the start and the footprint at the end;
        along that path the peak is the larger of the two, and adding
        them is what made this check demand three times what a
        migration needs.

        On top of that sit the terms that really are simultaneous : one
        batch caught between its writes and its commit, and the erases
        held back to the last batches.

        :attr:`headroom` is *not* part of this.  It is advice about a
        margin, not a quantity the migration will consume, and mixing
        the two made an estimate read as a refusal.

        Floored at zero: a filesystem that gives back more than it takes
        needs no extra room, and a negative peak is not a quantity
        anyone can act on.
        """
        return max(0, max(self.payload, self.unpacked)
                   + self.deferred + self.transient)

    @property
    def fits(self) -> bool:
        """The plan's own figures fit in what is free."""
        return self.required <= self.available

    @property
    def comfortable(self) -> bool:
        """Fits, and leaves the headroom worth having on top."""
        return self.required + self.headroom <= self.available

    @property
    def remaining(self) -> int:
        """What would be left free once the upgrade has landed."""
        return self.available - self.required

    @property
    def shortfall(self) -> int:
        return max(0, self.required - self.available)

    @property
    def holds_payload_only(self) -> bool:
        """True for a payload directory on its own filesystem.

        Its report has no footprint line to show — only what the
        download will park there.  Nothing is unpacked on it, so it
        carries no transient and needs no headroom either.
        """
        return (bool(self.payload)
                and not self.unpacked
                and not self.deferred
                and not self.transient
                and not self.headroom)


@dataclass(frozen=True)
class RootSpaceEstimate:
    """What Stage 3 needs, filesystem by filesystem."""

    filesystems: Tuple[FilesystemNeed, ...] = ()
    #: Mountpoints that receive package files and are not measured —
    #: a separate ``/var`` or ``/opt``.  Package metadata gives no file
    #: list, so the footprint cannot be split across filesystems.
    unmodelled: Tuple[Path, ...] = ()

    @property
    def fits(self) -> bool:
        return all(need.fits for need in self.filesystems)

    @property
    def short(self) -> Tuple[FilesystemNeed, ...]:
        """The filesystems that cannot hold their share."""
        return tuple(need for need in self.filesystems if not need.fits)

    @property
    def tight(self) -> Tuple[FilesystemNeed, ...]:
        """Those that fit, but with less than the headroom worth having.

        Not a problem to report as one — the plan's own figures say it
        goes through.  Worth saying anyway: those figures do not count
        what scriptlets regenerate, and the operator is the one who
        knows whether their machine has room to be surprised.
        """
        return tuple(need for need in self.filesystems
                     if need.fits and not need.comfortable)


def _device(path: Path) -> Optional[int]:
    """``st_dev`` of *path*, or ``None`` when it cannot be stat'ed."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _mountpoint(path: Path) -> Path:
    """The mount point *path* lives on.

    Walks up while ``st_dev`` is unchanged, which is what defines a
    mount boundary.  ``os.path.ismount`` would do too, but it stats
    twice per level and this runs on a path that may be deep.
    """
    path = path.resolve()
    device = _device(path)
    while path.parent != path:
        if _device(path.parent) != device:
            return path
        path = path.parent
    return path


def _existing(path: Path) -> Path:
    """*path* or its nearest existing ancestor.

    A ``payload_dir`` configured to a directory nobody has created yet
    is the normal state on a first run — the downloader makes it, and
    that happens after this check.  ``statvfs`` on the parent names the
    same filesystem, so the check runs instead of quietly disappearing.
    """
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _free_and_reserved(path: Path) -> Tuple[int, int]:
    """``(available, reserved)`` for the filesystem holding *path*.

    ``available`` is ``f_bavail``, what a non-root process may use.
    ``reserved`` is the gap up to ``f_bfree`` — the blocks ``mkfs`` set
    aside, which root could spend and this check will not.
    """
    st = os.statvfs(_existing(path))
    return (st.f_bavail * st.f_frsize,
            (st.f_bfree - st.f_bavail) * st.f_frsize)


def _footprint_target(root: Path) -> Path:
    """Where the volume lands: ``/usr`` when it exists, else *root*.

    Better than 90% of a cross-release upgrade unpacks under ``/usr``,
    so that is the filesystem worth measuring.  Falling back to *root*
    keeps this usable against a chroot or an image tree being built.
    """
    candidate = root / "usr"
    return candidate if candidate.is_dir() else root


def batch_transient_bytes(sizes) -> int:
    """What one Tx B batch weighs once unpacked.

    A batch is capped at ``stage3.TX_B_BATCH_BYTES`` of *compressed*
    payload.  Between rpm writing its files and the commit that frees
    the versions they replace, the batch costs its uncompressed size —
    so the cap has to be converted, and the plan states its own
    conversion rate: gross installed footprint over gross download.

    Zero when the plan downloads nothing (everything cached, or a
    removal-only plan): there is no batch to be caught mid-commit.
    """
    from .stage3 import TX_B_BATCH_BYTES

    if not sizes.download:
        return 0
    ratio = sizes.installed / sizes.download
    return int(min(TX_B_BATCH_BYTES * ratio, sizes.installed))


def deferred_erase_bytes(actions: Iterable, *, root: Path = Path("/")) -> int:
    """Installed size of the erases Tx B will hold back to the end.

    Exactly the removals :func:`live_system.snapshot` does not rank
    ``TIER_INTERLEAVED`` — the ones whose bytes arrive too late to
    relieve the peak.  Their ``size`` is the ``RPMTAG_SIZE`` the
    resolver read from the rpmdb.
    """
    from .live_system import TIER_INTERLEAVED, snapshot

    removals = [a for a in actions
                if getattr(a.action, "value", a.action) == "remove"]
    if not removals:
        return 0

    live = snapshot(str(root))
    return sum(getattr(a, "size", 0) or 0
               for a in removals
               if live.tier(a.name) != TIER_INTERLEAVED)


def estimate(actions: Sequence,
             *,
             root: Path = Path("/"),
             payload_dir: Optional[Path] = None,
             payload_bytes: Optional[int] = None) -> RootSpaceEstimate:
    """Measure the plan against every filesystem that has to hold it.

    ``payload_bytes`` is what still has to be fetched.  The caller
    knows which packages are already cached; this module would have to
    rebuild the download item list to find out, and getting it wrong
    means charging a resumed migration for gigabytes that are already
    on the disk it is measuring.  Left out, the whole download total is
    charged — an upper bound, right before a first download and wrong
    on ``--resume``.
    """
    root = Path(root)
    target = _footprint_target(root)
    target_device = _device(target)
    sizes = compute_sizes(actions)
    pending = sizes.download if payload_bytes is None else payload_bytes

    payload_device = None
    if payload_dir is not None:
        payload_device = _device(_existing(payload_dir))

    footprint_free, footprint_reserved = _free_and_reserved(target)
    filesystems: List[FilesystemNeed] = [FilesystemNeed(
        mountpoint=_mountpoint(target),
        unpacked=sizes.net,
        deferred=deferred_erase_bytes(actions, root=root),
        payload=pending if payload_device == target_device else 0,
        transient=batch_transient_bytes(sizes),
        headroom=int(sizes.installed * HEADROOM_RATIO),
        available=footprint_free,
        reserved=footprint_reserved,
    )]

    if pending and payload_dir is not None and payload_device != target_device:
        # Its own partition : its own verdict.  Summing it into the
        # footprint would refuse an upgrade whose payload fits there
        # comfortably, and pass one that fills it mid-download.  Listed
        # only when something is still to be fetched — on ``--resume``
        # the payload is already there and nothing more will land.
        try:
            payload_free, payload_reserved = _free_and_reserved(payload_dir)
        except OSError as exc:
            logger.debug("cannot measure the payload directory %s: %s",
                         payload_dir, exc)
        else:
            filesystems.append(FilesystemNeed(
                mountpoint=_mountpoint(_existing(payload_dir)),
                payload=pending,
                available=payload_free,
                reserved=payload_reserved,
            ))

    return RootSpaceEstimate(filesystems=tuple(filesystems),
                             unmodelled=_unmodelled(root, target_device,
                                                    filesystems))


def _unmodelled(root: Path,
                target_device: Optional[int],
                filesystems: Sequence[FilesystemNeed]) -> Tuple[Path, ...]:
    """Mounts that receive package files and get no verdict of their own.

    Package metadata carries no file list, so a plan's footprint cannot
    be split across filesystems : whatever it installs under a separate
    ``/var`` or ``/opt`` is charged to the one holding ``/usr``.  That
    over-states ``/usr`` — the safe direction — and under-states the
    others, which is worth naming rather than leaving as a figure that
    quietly means something else.

    A mount already measured on its own (the payload partition) is not
    listed : saying it is uncovered would contradict the block printed
    right above it.
    """
    measured = {need.mountpoint for need in filesystems}
    found = []
    for candidate in (root, root / "var", root / "opt"):
        if not candidate.is_dir() or _device(candidate) == target_device:
            continue
        mountpoint = _mountpoint(candidate)
        if mountpoint not in measured and mountpoint not in found:
            found.append(mountpoint)
    return tuple(found)


def describe(est: RootSpaceEstimate) -> str:
    """The figures, in the order they answer the operator's question.

    Shown whether or not the plan fits: someone about to commit to a
    two-hour migration is entitled to see how much room is left, not
    only to be told when there is none.

    Labels are padded here rather than inside the translatable strings.
    A column laid out with spaces in the ``msgid`` looks aligned in
    English and ragged in every other language, and no translator can
    be expected to count them.  The width spans every filesystem so the
    blocks line up with each other, not just internally.
    """
    blocks = [(need, _rows(need)) for need in est.filesystems]
    if not blocks:
        return ""
    width = max(len(label) for _need, rows in blocks
                for label, _value, _note in rows)

    lines: List[str] = []
    for need, rows in blocks:
        if lines:
            lines.append("")
        lines.append(_("Disk space on {mountpoint} :").format(
            mountpoint=need.mountpoint))
        for label, value, note in rows:
            line = f"  {label:<{width}}  {value}"
            if note:
                line += f"   ({note})"
            lines.append(line)
        if need.reserved:
            lines.append("  " + _(
                "({value} of reserved blocks left untouched)").format(
                    value=format_size(need.reserved)))

    for mountpoint in est.unmodelled:
        lines.append("  " + _(
            "note : {mountpoint} is a separate filesystem ; whatever the "
            "plan installs there is counted above instead").format(
                mountpoint=mountpoint))
    return "\n".join(lines)


def _rows(need: FilesystemNeed) -> List[Tuple[str, str, str]]:
    """``(label, value, note)`` triples for one filesystem."""
    rows = [(_("free space"), format_size(need.available), "")]
    if not need.holds_payload_only:
        rows.append((_("net footprint"), _signed(need.unpacked), ""))
    if need.payload:
        rows.append((_("downloaded RPMs"), format_size(need.payload), ""))
    if need.deferred:
        rows.append((_("held to the end"), format_size(need.deferred),
                     _("packages in use, freed by the last batches")))
    if need.transient:
        rows.append((_("one batch unpacked"), format_size(need.transient),
                     _("written before its commit frees what it replaces")))
    rows.append((_("estimated need"), format_size(need.required), ""))
    if need.headroom:
        rows.append((_("headroom advised"), format_size(need.headroom),
                     _("for what the system regenerates while installing")))
    return rows


def _signed(value: int) -> str:
    """Signed, because a cross-release upgrade can be net negative."""
    return ("-" if value < 0 else "+") + format_size(abs(value))


def _recovery_commands(needs: Sequence[FilesystemNeed]) -> List[str]:
    """The commands that free space where it is actually missing.

    Tailored to which filesystem came up short : telling someone to run
    ``autoremove`` when what is full is the partition holding the
    download cache sends them after the wrong gigabytes.
    """
    commands: List[str] = []
    if any(need.payload for need in needs):
        commands.append("urpm cache flush")
    if any(not need.holds_payload_only for need in needs):
        commands += ["urpm autoremove --oldkernels", "urpm autoremove"]
    return commands


def shortfall_warning(est: RootSpaceEstimate) -> str:
    """What to tell an operator the figures put on the wrong side.

    Empty when everything fits.  Two registers, because being 170 MB
    short and being 8 GB short are not the same news : inside the band
    the estimate cannot tell fit from no-fit and says so, outside it
    the gap is larger than anything the model's own noise explains.

    Neither stops anything.  The operator has the plan in front of them
    and the prompt right below; this gives them the figures to answer
    it with.
    """
    lines = [
        _("{mountpoint} : {required} estimated, {available} available, so "
          "about {remaining} would be left. That is a thin margin — "
          "{headroom} would be safer. Freeing some space first is "
          "worth it.").format(
              mountpoint=need.mountpoint,
              required=format_size(need.required),
              available=format_size(need.available),
              remaining=format_size(max(0, need.remaining)),
              headroom=format_size(need.headroom))
        for need in est.tight
    ]
    lines += [
        _("{mountpoint} is too small : {required} estimated, {available} "
          "available. Going ahead is likely to run out of space part-way "
          "through the upgrade.").format(
              mountpoint=need.mountpoint,
              required=format_size(need.required),
              available=format_size(need.available))
        for need in est.short
    ]
    if not lines:
        return ""

    commands = _recovery_commands(est.short + est.tight)
    if commands:
        lines += ["", _("Freeing space :"), ""]
        lines += ["  " + command for command in commands]

    if any(need.holds_payload_only for need in est.short + est.tight):
        lines += ["", _(
            "The download cache can be moved to another filesystem for "
            "good : set `payload_dir` in the [download] section of the "
            "configuration. Only the payload moves ; the database and "
            "the media metadata stay put.")]

    lines += ["", _(
        "These figures do not count what the system regenerates while "
        "installing — the initramfs, the font and icon caches. Allow for "
        "more than they show.")]
    return "\n".join(lines)


def assess_root_space(actions: Sequence,
                      *,
                      root: Path = Path("/"),
                      payload_dir: Optional[Path] = None,
                      payload_bytes: Optional[int] = None
                      ) -> RootSpaceEstimate:
    """Measure and report.  Never refuses.

    This used to raise, and it was wrong to.  A false refusal is
    strictly worse than no check at all — it stops a migration that
    would have worked, after Stage 1 has already swapped the media, so
    every one of them costs a full switchover, an eight-medium sync, a
    solve and a rollback.  A false pass costs exactly what the status
    quo costs: nothing extra.  With that asymmetry, and a model
    calibrated against one machine on one run, the estimate has no
    business holding a veto.

    So it reports.  The operator is already looking at the plan and a
    confirmation prompt; the figures belong there, and the decision is
    theirs.  ``--yes`` is that decision taken in advance, and is not
    second-guessed either.

    The pre-flight cannot move any earlier, whatever its verdict: it
    needs the plan, the plan needs a pool built on the target-release
    synthesis, and those exist only once Stage 1 has swapped the media
    and they have been synced.
    """
    est = estimate(actions, root=root, payload_dir=payload_dir,
                   payload_bytes=payload_bytes)
    for need in est.filesystems:
        logger.info("space on %s : need %s, have %s", need.mountpoint,
                    format_size(need.required), format_size(need.available))
    for need in est.short:
        # ``info``, not ``warning`` : the console already gets this
        # through :func:`shortfall_warning`, translated and in context.
        # A second copy in English, unlocalised, above the block it
        # duplicates is noise at the exact moment the operator needs to
        # read carefully.
        logger.info("space on %s : %s short of the estimate",
                    need.mountpoint, format_size(need.shortfall))
    return est
