"""Fold a transaction's phases into one monotonic 0-100 progress bar.

A PackageKit client draws a single percentage for a whole operation, but
an upgrade is two unrelated activities back to back: pulling payloads
over the network, then handing them to rpm.  The backend used to give
each half of the bar, and that constant is wrong in both directions.
With 3 GB to fetch over a slow link the bar crawls to 50 and then
finishes in one jump; with everything already cached it reaches 50 at
once and then crawls for minutes.  No constant is right in both cases.

So the split is not a constant.  It comes from the plan the resolver has
just produced: the share of the bar given to the download is the share
of the transaction's bytes that actually have to be fetched.  An upgrade
served entirely from cache yields a download share of zero, and the bar
starts moving from zero with no special case to write for it.  A removal
builds the scale with no bytes at all and gets the whole bar for the rpm
transaction, for the same reason.

One approximation remains, and it is worth stating rather than hiding: a
byte pulled from a mirror and a byte unpacked by rpm do not cost the
same time, and their ratio moves with the link speed.  This is a
heuristic.  What it is not is a figure that ignores the transaction it
claims to describe.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What a percentage field carries when the answer is not known.  Same
#: value as PackageKit's ``PK_BACKEND_PERCENTAGE_INVALID``, so the C
#: backend can forward it untouched.
PERCENTAGE_UNKNOWN = 101


@dataclass(frozen=True)
class ProgressScale:
    """Where the download stops and the rpm transaction starts on the bar.

    Both figures come from the resolved plan: ``download_bytes`` is what
    is really missing from the cache, not what the transaction weighs,
    and ``install_bytes`` is the unpacked footprint rather than the
    compressed payload, because what rpm spends its time on is writing
    the unpacked bytes.

    Build one per operation and keep it for the whole run: the split must
    not move under the bar, or the percentage stops being monotonic.
    """

    download_bytes: int = 0
    install_bytes: int = 0

    @property
    def download_share(self) -> float:
        """Fraction of the bar the download owns, between 0 and 1."""
        total = self.download_bytes + self.install_bytes
        if total <= 0:
            return 0.0
        return self.download_bytes / total

    def downloading(self, done: int, total: int) -> int:
        """Overall percentage while fetching, ``done``/``total`` in bytes."""
        return self._between(0.0, self.download_share, done, total)

    def transacting(self, done: int, total: int) -> int:
        """Overall percentage while rpm works, ``done``/``total`` in packages."""
        return self._between(self.download_share, 1.0, done, total)

    @staticmethod
    def _between(low: float, high: float, done: int, total: int) -> int:
        """Place ``done``/``total`` inside the ``[low, high]`` band.

        An unknown or zero total pins the result at the start of the
        band rather than raising: a mirror that sends no
        ``Content-Length`` must not take the progress bar down with it.
        """
        fraction = 0.0
        if total and total > 0:
            fraction = min(1.0, max(0.0, done / total))
        return round((low + (high - low) * fraction) * 100)


def item_percentage(done: int, total: int) -> int:
    """Progress within the current package, or :data:`PERCENTAGE_UNKNOWN`.

    Feeds PackageKit's per-item progress, which Discover shows next to
    the package it is working on. Separate from the overall bar on
    purpose: it restarts at zero for every package, and folding the two
    together is what would make the main bar jump backwards.
    """
    if not total or total <= 0:
        return PERCENTAGE_UNKNOWN
    return min(100, max(0, round(done * 100 / total)))
