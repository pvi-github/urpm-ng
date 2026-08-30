"""Hermetic environment for chroot-bootstrap sub-processes.

``urpm image make`` runs its first phase under ``podman unshare`` — a
rootless wrapper that remaps UIDs so rpm's ``chown`` calls succeed,
but does NOT create a mount / PID / network namespace and does NOT
substitute the operator's environment.  Every sub-process spawned
inside inherits the caller's env verbatim ; any host-side user config
(``~/.rpmmacros``, ``~/.gnupg``, ``XDG_CONFIG_HOME``, ``TMPDIR``…)
reaches rpm and its scriptlets.

Left uncontrolled that turns into cross-contamination : the classic
symptom is a packager whose ``~/.rpmmacros`` sets
``%_topdir %(echo $HOME)/rpmbuild`` — rpm scriptlets inside the
bootstrap then try to open ``/home/<user>/rpmbuild/tmp/rpm-tmp.XXX``,
which doesn't exist inside the wrapped view, and the entire
``filesystem`` / ``shadow-utils`` / ``dbus`` / ``systemd`` cascade
fails.

The two helpers in this module fix that once, for every rpm-touching
sub-process the bootstrap spawns :

* :func:`bootstrap_env` returns an explicit, whitelisted env dict.
  Nothing from ``os.environ`` is copied wholesale — every variable
  is either pinned to a deterministic value, taken from the caller's
  explicit ``passthrough`` list, or (for the proxy family) forwarded
  only when ``forward_proxy=True``.

* :data:`PROXY_VARS` documents the exact set of proxy variables the
  helper is willing to forward, so callers wanting to preserve them
  don't have to remember all ten case variants.

The pattern is deliberately paranoid : if a future scriptlet needs
a variable that isn't in the whitelist, the fix is to add it here
with a comment saying why — not to fall back on inheriting the whole
parent environment.  See ``doc/`` (SPEC or CHANGELOG entry) for the
rationale trace.
"""

from __future__ import annotations

import os
from typing import Mapping, Sequence

# Every proxy variable the outside world might have set, in both
# ``lower`` and ``UPPER`` variants.  Curl reads ``lower``, wget /
# python-requests / most Java clients read ``UPPER``, so forwarding
# one form and not the other misses half the tools rpm scriptlets
# might invoke.
PROXY_VARS: tuple[str, ...] = (
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "ftp_proxy", "FTP_PROXY",
    "no_proxy", "NO_PROXY",
    "all_proxy", "ALL_PROXY",
)


def bootstrap_env(
    chroot_root: str,
    *,
    forward_proxy: bool = False,
    passthrough: Sequence[str] = (),
) -> dict[str, str]:
    """Return a hermetic environment dict for a chroot-bootstrap child.

    Args:
        chroot_root: Filesystem root the bootstrap installs into.
            Used to pin ``TMPDIR`` inside the chroot so rpm scriptlets
            write to ``<chroot_root>/tmp`` (created by ``cmd_mkimage``
            before the first install) instead of leaking to
            ``/tmp`` on the operator's session.
        forward_proxy: Copy the ``PROXY_VARS`` set from the parent
            environment when they are set there.  Off by default —
            turn on when the operator's site can only reach Mageia
            mirrors through an outbound HTTP proxy.  Controlled at
            the CLI level by ``urpm image make
            --forward-proxy`` / ``--no-forward-proxy`` and at the
            system level by the ``[image] forward_proxy = true``
            config key.
        passthrough: Names of urpm-set signalling variables the
            caller wants preserved when they are set in the parent
            (typically ``URPM_MKIMAGE_SILENCE``,
            ``URPM_HOST_LANG``, ``URPM_HOST_LANGUAGE`` — the
            ``cmd_mkimage`` boundary sets some of these itself so the
            child can pick them up).  Silently ignored when the
            variable is absent from the parent.

    Returns:
        A fresh dict — nothing shared with ``os.environ``.  Suitable
        as the ``env=`` argument for :func:`subprocess.Popen` /
        :func:`subprocess.run`.
    """
    env: dict[str, str] = {
        # Deterministic ``$PATH`` matching the target chroot's own
        # layout post-UsrMove — no ``/home/<user>/.local/bin`` first
        # entry that would pick up an operator-side binary.
        "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
        # Rpm needs *some* HOME — the operator's leaked one is what
        # made ``%_topdir %(echo $HOME)/rpmbuild`` explode.  We point
        # it at ``chroot_root`` itself : always exists (that's where
        # we install into), always writable, and any ``~/...`` macro
        # an rpmmacros file expands stays *inside* the chroot even
        # before ``/root`` has been populated by the ``filesystem``
        # package.
        "HOME": chroot_root,
        # Force rpm scriptlets to use the chroot's own ``/tmp``
        # rather than fall back on any leaked ``_tmppath`` or on the
        # operator's ``$TMPDIR``.  ``cmd_mkimage`` already ``mkdir``s
        # this before the first install.
        "TMPDIR": os.path.join(chroot_root, "tmp"),
        # Deterministic locale — script output stays parseable and
        # rpm's own error messages don't switch language depending
        # on who invoked the build.
        "LC_ALL": "C",
        "LANG": "C",
        "LANGUAGE": "C",
        # Deterministic timezone — timestamps in the freshly-created
        # rpmdb match across operators and hosts.
        "TZ": "UTC",
        # ``systemctl`` inside the chroot has no live pid 1 to talk
        # to ; the ``systemd`` scriptlet helpers short-circuit when
        # this is set.  Also set on the parent side by
        # ``cmd_mkimage`` so keep it in sync at the child boundary.
        "SYSTEMD_OFFLINE": "1",
    }

    # ``TERM`` : only forward when we actually have a tty ; the
    # progress widget renders ANSI when it detects one.
    parent_term = os.environ.get("TERM")
    if parent_term and _stdout_is_tty():
        env["TERM"] = parent_term
    else:
        env["TERM"] = "dumb"

    # Signal variables the caller explicitly opts into keeping (see
    # docstring).  Absent-in-parent → silently skipped.
    for name in passthrough:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val

    # Proxy family : opt-in.  When on, copy every set variant so the
    # matching tool (curl vs wget vs python-requests) finds the one
    # it expects.
    if forward_proxy:
        for name in PROXY_VARS:
            val = os.environ.get(name)
            if val is not None:
                env[name] = val

    return env


def _stdout_is_tty() -> bool:
    """Split from :func:`bootstrap_env` so tests can monkey-patch it."""
    import sys
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
