"""Names of the repositories that have no media row behind them.

Every package urpm-ng resolves comes from a libsolv repository, but two
of those repositories are not configured media and never appear in the
``media`` table:

* :data:`INSTALLED` holds what the rpmdb already has.  The name is
  libsolv's own convention, shared with every tool built on it, and
  ``pool.add_rpmdb()`` fills it.
* :data:`LOCAL_RPMS` holds packages named as a path on the command
  line (``urpm install ./foo.rpm``).  That one is ours.

Both were written as bare strings at three dozen sites, in comparisons
that decide whether a solvable is installed, whether an action needs a
download, and where its payload lives.  A typo in any of them fails
open: the comparison is simply false, the package is treated as coming
from an ordinary medium, and the mistake surfaces much later as a
missing file or a phantom download.

This module imports nothing, deliberately.  :mod:`urpm.core.
transaction_sizes` is kept free of libsolv and must be able to name the
local repository without pulling the resolver in behind it.

The strings themselves are a wire format of sorts — they are compared
against ``repo.name`` from libsolv and against ``media_name`` carried
through :class:`~urpm.core.resolver.PackageAction`.  The tests pin the
literal values on purpose: asserting against these constants instead
would make a typo in them pass.
"""

#: libsolv's repository for what the rpmdb already holds.
INSTALLED = "@System"

#: Repository for packages named as a path on the command line.
#: Created by :meth:`urpm.core.resolution.pool.SolvPool.add_local_rpms`.
LOCAL_RPMS = "@LocalRPMs"
