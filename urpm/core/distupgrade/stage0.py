"""Stage 0 pre-checks (SPEC_DISTUPGRADE §4.0).

The subset shipped here : clock sanity gate.  Later tickets extend
this module with Phase A (media refresh + upgrade), Phase B checks
(target version, rpmdb integrity, MIN_KERNEL, disk with ``/boot``
dracut margin, GPG keys).
"""

from __future__ import annotations

import os
import time
from pathlib import Path


MAGEIA_RELEASE_PATH = Path("/etc/mageia-release")


class ClockGateError(Exception):
    """Raised when ``time.time()`` is impossibly earlier than the
    installed Mageia release (SPEC §4.0 clock gate).

    ``message`` carries a copy-pasteable ``chrony`` recovery command
    for the user.
    """


def check_clock_sanity(release_path: Path = None) -> None:
    """Refuse to run distupgrade when the wall clock is nonsensical.

    §6.4.a's release selection filters candidates by
    ``release_date ≤ today AND desktop-update-end > today``.  A dead
    CMOS battery (or a VM long-suspended) leaves the clock at some
    absurd past year — the filter rejects every release and the
    auto-detection surface up a cryptic « aucune version cible
    détectée ».  We catch it upstream with a lower bound derived
    from the installed release's own metadata : the wall clock
    cannot legitimately be earlier than the mtime of
    ``/etc/mageia-release`` (that file was at the very least
    touched when the release was installed).

    No upper bound : a RTC pinned in the future is not a documented
    failure mode on Mageia hardware, and picking an arbitrary
    ceiling (« now + 10 years ») has no principled basis (see the
    session log where the earlier F11(a) upper-bound was retired).

    Raises :class:`ClockGateError` with a message pointing to
    ``chrony`` — one apt-style rescue command that installs +
    steps in one go.
    """
    if release_path is None:
        release_path = MAGEIA_RELEASE_PATH
    now = time.time()
    try:
        mtime = os.path.getmtime(release_path)
    except OSError:
        # No release file → nothing to gate against.  Downstream
        # checks (§6.4.a version discovery) will surface the real
        # problem.
        return

    if now < mtime:
        raise ClockGateError(
            "L'horloge système ({now}) est antérieure à la release "
            "Mageia installée ({rel} mtime {rel_time}) — incohérence "
            "évidente.  Le distupgrade s'appuie sur l'horloge pour "
            "sélectionner la version cible ; corrigez avant de relancer :\n\n"
            "  urpm install chrony\n"
            "  chronyc makestep    # step immédiat depuis un serveur NTP\n\n"
            "puis relancez `urpm distupgrade`.".format(
                now=time.ctime(now),
                rel=release_path,
                rel_time=time.ctime(mtime),
            )
        )
