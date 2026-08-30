"""Install / upgrade urpm-ng-core in image chroots and running containers.

Two entry points share the same decision engine:

- :func:`install_urpm_ng_core` -- called by ``urpm image make`` at
  Phase 1 to install urpm-ng-core into the fresh chroot being built.
- :func:`ensure_urpm_ng_in_container` -- called by ``urpm image update``
  right after ``urpm media update`` inside the running container that
  is about to become the updated image.

Both walk the same 4-rule decision tree (waterfall by default,
overridable via the ``--urpm-ng-source={auto,local,media,github}``
flag or completely bypassed via ``--urpm-ng-core=<path>``):

1. **Local match wins on a standalone target.**  If a RPM in
   ``rpmbuild/RPMS/`` matches ``VERSION+RELEASE+arch+disttag`` AND
   the target (image being built, or existing image) is standalone
   for urpm-ng (no media provides it), install the local RPM.  The
   image stays as configured.
2. **Local match + target already has a media providing urpm-ng-core
   + local newer.**  Prompt the user: use the local build, or stick
   with the media?  Default in ``-y`` / non-interactive mode: stick
   with the media, ``urpm u`` will handle it.
3. **No local match, host has a media providing urpm-ng-core, and
   that media covers target arch + Mageia release.**  Add the media
   to the target (with pubkey), install urpm-ng-core from it.  The
   target now has this media configured for future updates.
4. **No local match, no reachable host media for the target.**
   Download the latest ``urpm-ng-core`` RPM from the project's
   GitHub releases page and install it.

The code is deliberately agnostic to ``mgabiz`` -- rule 3 only sees
"a host media that provides urpm-ng-core and can be transposed to
the target arch + release".  The day urpm-ng ships in core/updates,
rule 3 keeps matching against that media instead, without a single
line of code change.

The ``--urpm-ng-core=<path>`` flag overrides every rule and installs
exactly that file; ``--urpm-ng-source=<value>`` skips the waterfall
and pins to a specific arm.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .container import Container
    from .database import PackageDatabase


GITHUB_API = "https://api.github.com/repos/pvi-github/urpm-ng"

# Valid values for the ``source=`` argument of the public entries.
SOURCE_AUTO = "auto"
SOURCE_LOCAL = "local"
SOURCE_MEDIA = "media"
SOURCE_GITHUB = "github"
SOURCE_VALUES = (SOURCE_AUTO, SOURCE_LOCAL, SOURCE_MEDIA, SOURCE_GITHUB)


__all__ = [
    "install_urpm_ng_core",
    "ensure_urpm_ng_in_container",
    "SOURCE_VALUES",
]


# ══════════════════════════════════════════════════════════════════════
#  Public entry -- image make (fresh chroot on disk)
# ══════════════════════════════════════════════════════════════════════

def install_urpm_ng_core(
    chroot_dir: str,
    chroot_db: "PackageDatabase",
    arch: str,
    mageia_release: str,
    *,
    host_db: Optional["PackageDatabase"] = None,
    source: str = SOURCE_AUTO,
    explicit_rpm: Optional[str] = None,
    allow_disttag_mismatch: bool = False,
    log: callable = print,
) -> int:
    """Install ``urpm-ng-core`` into a fresh chroot at ``chroot_dir``.

    Args:
        chroot_dir: Filesystem path to the chroot being built.
        chroot_db: PackageDatabase pointing at the chroot's
            ``/var/lib/urpm/packages.db`` (already initialised by
            ``cmd_init`` earlier in Phase 1).
        arch: Target architecture (``x86_64``, ``i686``, ``aarch64`` ...).
        mageia_release: Target Mageia release (``9``, ``10``, ``cauldron``).
        host_db: Host PackageDatabase (default DB path).  Loaded lazily
            if not provided.  Used by rule-3 detection.
        source: Waterfall vs. pinned source.  ``auto`` walks the 4
            rules; ``local`` / ``media`` / ``github`` skip the
            waterfall and force that arm (error/prompt as documented
            in the module docstring).
        explicit_rpm: Path to a specific RPM to install.  Overrides
            every waterfall rule -- treated as a hard directive.
        log: Progress printer (defaults to ``print`` so the module
            works standalone).

    Returns:
        0 on success, non-zero on failure.
    """
    if explicit_rpm:
        return _install_explicit_rpm_in_chroot(
            chroot_dir, chroot_db, Path(explicit_rpm), log,
        )

    _check_source(source)
    if host_db is None:
        from .database import PackageDatabase
        host_db = PackageDatabase()

    # Signal detection.
    # ``system-numeric`` was set by ``cmd_init`` (either from an
    # explicit ``cauldron:N`` or a media.cfg probe).  Falling back to
    # the raw release covers the numeric-target case where identity
    # and numeric coincide.
    target_numeric = (chroot_db.get_config('system-numeric')
                      or (mageia_release if mageia_release.isdigit()
                          else None))
    local_rpm = _detect_local_match(
        arch, mageia_release, target_numeric, allow_disttag_mismatch)
    chroot_media_version = _chroot_media_version(chroot_db)
    host_media = _detect_host_source(host_db, mageia_release, arch)
    non_interactive = not sys.stdin.isatty()

    choice = _decide_source(
        requested=source,
        local_rpm=local_rpm,
        target_media_version=chroot_media_version,
        installed_version=None,  # fresh chroot, nothing installed yet
        host_media=host_media,
        non_interactive=non_interactive,
        log=log,
    )

    if choice == SOURCE_LOCAL:
        log(f"  urpm-ng-core: local match -> {local_rpm.name}")
        return _install_local_rpm_in_chroot(chroot_dir, chroot_db, local_rpm)
    if choice == "keep_media":
        # Rule 2 declined the local: chroot media provides urpm-ng-core
        # and user (or -y) chose to stick with it.  No urpm-ng-specific
        # action -- ``urpm u`` upstream will pick the media version.
        log("  urpm-ng-core: taking the chroot-media version")
        return _install_from_chroot_media(chroot_dir, chroot_db)
    if choice == SOURCE_MEDIA:
        log(f"  urpm-ng-core: adding host media '{host_media['name']}' to chroot")
        return _install_from_host_media_in_chroot(
            chroot_dir, chroot_db, host_media, log,
        )
    if choice == SOURCE_GITHUB:
        log("  urpm-ng-core: falling back to GitHub release")
        return _install_from_github_in_chroot(
            chroot_dir, chroot_db, arch, mageia_release,
            target_numeric, allow_disttag_mismatch, log,
        )
    log(f"  ERROR: unexpected source decision {choice!r}")
    return 1


# ══════════════════════════════════════════════════════════════════════
#  Public entry -- image update (running container)
# ══════════════════════════════════════════════════════════════════════

def ensure_urpm_ng_in_container(
    container: "Container",
    cid: str,
    host_db: Optional["PackageDatabase"] = None,
    *,
    source: str = SOURCE_AUTO,
    explicit_rpm: Optional[str] = None,
    allow_disttag_mismatch: bool = False,
    log: callable = print,
) -> int:
    """Install / upgrade urpm-ng-core inside a running image container.

    Called by ``urpm image update`` after ``urpm media update`` on the
    image's own media.  Walks the same 4-rule tree as
    :func:`install_urpm_ng_core` -- see the module docstring.

    Args:
        container: :class:`Container` wrapper.
        cid: Running container id.
        host_db: Host PackageDatabase (default DB).  Loaded lazily.
        source, explicit_rpm, log: See :func:`install_urpm_ng_core`.

    Returns:
        0 on success, non-zero on failure.
    """
    if explicit_rpm:
        return _install_explicit_rpm_in_container(
            container, cid, Path(explicit_rpm), log,
        )

    _check_source(source)

    # Detect target release + arch from inside the container so a mga10
    # host can upgrade an mga9 image without hard-coding release/arch.
    release = _container_mageia_release(container, cid)
    arch = _container_arch(container, cid)
    if not release or not arch:
        log("  Warning: could not detect image release/arch, skipping urpm-ng")
        return 0

    if host_db is None:
        from .database import PackageDatabase
        host_db = PackageDatabase()

    # The image's ``/etc/mageia-release`` was seeded by mkimage phase
    # 1 with the target numeric, so ``release`` here is already the
    # numeric (``'11'`` for a cauldron image, not ``'cauldron'``).
    # That means the plain N-only branch of _accepted_disttags fires
    # and rejects RPMs built on a mga{N-1} host.  Ask the container's
    # ``/etc/os-release`` (seeded by ``cmd_init``) for the symbolic
    # identity when one is set.
    identity = _container_identity(container, cid) or release
    target_numeric = release if release.isdigit() else None
    local_rpm = _detect_local_match(
        arch, identity, target_numeric, allow_disttag_mismatch)
    image_media_version = _container_media_version(container, cid)
    installed_version = _container_installed_version(container, cid)
    host_media = _detect_host_source(host_db, release, arch)
    non_interactive = not sys.stdin.isatty()

    choice = _decide_source(
        requested=source,
        local_rpm=local_rpm,
        target_media_version=image_media_version,
        installed_version=installed_version,
        host_media=host_media,
        non_interactive=non_interactive,
        log=log,
    )

    if choice == SOURCE_LOCAL:
        log(f"  urpm-ng-core: local match -> {local_rpm.name}")
        return _install_local_rpm_in_container(container, cid, local_rpm, log)
    if choice == "keep_media":
        log("  urpm-ng-core: keeping the image-media version, urpm u will handle it")
        return 0
    if choice == SOURCE_MEDIA:
        log(f"  urpm-ng-core: adding host media '{host_media['name']}' to image")
        return _install_from_host_media_in_container(
            container, cid, host_media, log,
        )
    if choice == SOURCE_GITHUB:
        log("  urpm-ng-core: falling back to GitHub release")
        # Pass ``identity`` (may be ``'cauldron'``) rather than the
        # numeric ``release`` so :func:`_accepted_disttags` applies the
        # cauldron relaxation.  ``target_numeric`` still carries the
        # numeric so the disttag set resolves to real values.
        return _install_from_github_in_container(
            container, cid, arch, identity,
            target_numeric, allow_disttag_mismatch, log,
        )
    log(f"  ERROR: unexpected source decision {choice!r}")
    return 1


# ══════════════════════════════════════════════════════════════════════
#  Shared decision engine
# ══════════════════════════════════════════════════════════════════════

def _decide_source(
    *,
    requested: str,
    local_rpm: Optional[Path],
    target_media_version: Optional[str],
    installed_version: Optional[str],
    host_media: Optional[dict],
    non_interactive: bool,
    log: callable,
) -> str:
    """Walk the 4-rule tree and return one of:

    - ``"local"``      -- install the local RPM
    - ``"media"``      -- add the host media, install from it
    - ``"github"``     -- download latest release from GitHub
    - ``"keep_media"`` -- target already has a media providing urpm-ng-core;
                          don't do anything urpm-ng-specific, upstream
                          ``urpm u`` handles it.

    ``requested`` is the CLI flag value.  ``auto`` walks the 4 rules;
    the other three force that arm with the documented fallback.
    """
    target_has_media = target_media_version is not None

    # Explicit ``--urpm-ng-source=<X>`` overrides the waterfall.
    if requested == SOURCE_LOCAL:
        if local_rpm:
            return SOURCE_LOCAL
        raise _NoLocalMatchError(
            "--urpm-ng-source=local but no local RPM matches VERSION/RELEASE/arch/mga"
        )
    if requested == SOURCE_GITHUB:
        return SOURCE_GITHUB
    if requested == SOURCE_MEDIA:
        if host_media is not None:
            return SOURCE_MEDIA
        # Fallback github with confirmation
        if non_interactive or _confirm_fallback_github(log):
            return SOURCE_GITHUB
        raise _NoLocalMatchError(
            "--urpm-ng-source=media but host has no media for target, "
            "and user declined GitHub fallback"
        )

    # ── AUTO waterfall ────────────────────────────────────────────────

    if local_rpm:
        # Rule 1: standalone or creation → local wins.
        if not target_has_media:
            return SOURCE_LOCAL
        # Rule 2: target has media + local newer → PROMPT.
        # ``-y`` / non-interactive keeps the media; only an explicit
        # user confirmation swaps to the local build.
        local_ver = _rpm_file_version(local_rpm)
        media_ver = target_media_version
        if _version_gt(local_ver, media_ver) and not non_interactive:
            if _confirm_local_over_media(local_rpm, media_ver, log):
                return SOURCE_LOCAL
        return "keep_media"

    # No local match.

    # Rule 3: host has a source that actually covers target arch+release.
    if host_media is not None and _target_media_reachable(host_media, log):
        return SOURCE_MEDIA

    # Rule 4: fallback github (also lands here if rule 3's target
    # media.cfg is not reachable -- e.g. mgabiz publishes 10/x86_64
    # but nothing for 9/aarch64).
    return SOURCE_GITHUB


class _NoLocalMatchError(RuntimeError):
    """Raised by ``_decide_source`` when a forced arm can't be satisfied."""


def _check_source(source: str) -> None:
    if source not in SOURCE_VALUES:
        raise ValueError(
            f"invalid source {source!r}, expected one of {SOURCE_VALUES}"
        )


# ══════════════════════════════════════════════════════════════════════
#  Signal detection -- side-agnostic
# ══════════════════════════════════════════════════════════════════════

def _accepted_disttags(
    mageia_release: str,
    target_numeric: Optional[str],
    allow_disttag_mismatch: bool,
) -> Optional[set]:
    """Compute the set of disttag substrings a local RPM name may
    carry to be considered a match for this target.

    * ``allow_disttag_mismatch=True`` — packager assumed responsibility;
      returns ``None``, callers treat that as "accept any ``.mga…``".
    * Numeric release (``10``, ``11``, …) — accepts only ``.mgaN.``;
      an RPM built for a different N is almost always the wrong file
      to inject into an image tree.
    * Cauldron — accepts ``.mgaN.`` for cauldron's current numeric
      *and* ``.mga{N-1}.`` for the previous stable.  Rationale: the
      packager typically rebuilds ``urpm-ng`` on their current stable
      host (``mga{N-1}``) to test its behaviour in a cauldron image
      (``mga{N}``).  urpm-ng-core is noarch pure Python, so the disttag
      is cosmetic; letting the stable RPM in unblocks that workflow
      without opening the door to arbitrary crossings.  When
      ``target_numeric`` is unknown we fall back to accepting any
      ``.mga…`` and note it in the caller so the choice is visible.
    """
    if allow_disttag_mismatch:
        return None
    if mageia_release == 'cauldron':
        if target_numeric and target_numeric.isdigit():
            n = int(target_numeric)
            return {f".mga{n}.", f".mga{n - 1}."}
        # Unknown numeric — behave as if the packager passed
        # ``--allow-disttag-mismatch`` but let the caller print a
        # note (this branch is uncommon: cauldron init failed to
        # probe media.cfg AND no explicit ``cauldron:N`` was passed).
        return None
    return {f".mga{mageia_release}."}


def _detect_local_match(
    arch: str,
    mageia_release: str,
    target_numeric: Optional[str] = None,
    allow_disttag_mismatch: bool = False,
) -> Optional[Path]:
    """Return the fresh urpm-ng-core RPM matching VERSION+RELEASE+arch+disttag.

    Looks first at ``$URPM_NG_SOURCE_DIR``, then walks up from CWD to
    find a checkout that carries ``VERSION``, ``RELEASE`` and
    ``rpmbuild/SPECS/urpm-ng.spec``.  Returns the RPM matching the
    checkout's current NVR + the target arch + an acceptable disttag
    (see :func:`_accepted_disttags`).  Returns ``None`` if any of
    those doesn't line up.
    """
    candidates: list[Path] = []
    env_src = os.environ.get("URPM_NG_SOURCE_DIR")
    if env_src:
        candidates.append(Path(env_src))
    cwd = Path.cwd()
    for p in (cwd, *cwd.parents):
        if (
            (p / "VERSION").exists()
            and (p / "RELEASE").exists()
            and (p / "rpmbuild" / "SPECS" / "urpm-ng.spec").exists()
        ):
            candidates.append(p)
            break

    accepted = _accepted_disttags(
        mageia_release, target_numeric, allow_disttag_mismatch)
    for src in candidates:
        try:
            version = (src / "VERSION").read_text().strip()
            release = (src / "RELEASE").read_text().strip()
        except OSError:
            continue
        for suffix in (f".{arch}.rpm", ".noarch.rpm"):
            for rpm in src.glob(
                f"rpmbuild/RPMS/**/urpm-ng-core-{version}-{release}*{suffix}"
            ):
                if "-debuginfo-" in rpm.name or "-debugsource-" in rpm.name:
                    continue
                if accepted is not None and not any(
                        tag in rpm.name for tag in accepted):
                    continue
                return rpm
    return None


def _detect_host_source(
    host_db: "PackageDatabase",
    target_release: str,
    target_arch: str,
) -> Optional[dict]:
    """Return the host media that provides urpm-ng-core, enriched with
    a ``discover_url`` built for the *target* release + arch (so a
    mga10 host can build a mga9 image).  Returns None when no media
    provides urpm-ng-core or when the URL cannot be reconstructed.
    """
    pkg = host_db.get_package("urpm-ng-core")
    if not pkg:
        return None
    media_id = pkg.get("media_id")
    if not media_id:
        return None
    media = host_db.get_media_by_id(media_id)
    if not media:
        return None
    servers = host_db.get_servers_for_media(media_id, enabled_only=True)
    if not servers:
        return None
    srv = servers[0]
    protocol = srv.get("protocol") or "https"
    host = srv.get("host")
    base_path = (srv.get("base_path") or "").rstrip("/")
    if not host:
        return None

    # The URL that ``cmd_media_discover`` needs is the *discovery
    # point* (``<mirror>/<url_segment>/<arch>/media/media_info/media.cfg``),
    # not the sub-repo the host happens to have configured.  Mageia
    # layouts pack the sub-repo tail after ``media/`` (e.g.
    # ``10/x86_64/media/urpm/release``); a naive reconstruction that
    # preserved that tail sent ``cmd_media_discover`` looking for
    # ``.../media/urpm/release/media_info/media.cfg`` -- a 404, because
    # the real manifest lives one level up at
    # ``.../media/media_info/media.cfg``.  Rebuild from scratch using
    # the target release + arch; the host's own ``relative_path``
    # sub-repo suffix is irrelevant here.
    #
    # The URL segment is the mirror-specific ``url_version`` when
    # known (e.g. ``cauldron`` when the mirror was configured for a
    # release that is still baking in cauldron).  Fallback to
    # ``target_release`` handles pre-v32 rows and custom servers
    # with no detected Mageia layout.  See
    # :func:`choose_target_url_segment` for the 3-case rule that
    # avoids using a stale numeric pin (mga10 host building a mga9
    # image would otherwise fetch under ``/10/``).
    from .distupgrade.version import choose_target_url_segment
    from .config import get_system_version
    host_release = get_system_version() or target_release
    url_segment, _ = choose_target_url_segment(
        srv.get("url_version"), host_release, target_release,
    )
    media = dict(media)
    media["discover_url"] = (
        f"{protocol}://{host}{base_path}/{url_segment}/{target_arch}/media/"
    )
    return media


def _target_media_reachable(host_media: dict, log) -> bool:
    """Return True when the target-arch/release media URL actually
    serves a ``media.cfg`` (HEAD probe on the discovery point).

    ``_detect_host_source`` rebuilds the URL for the target arch and
    release from the host server row -- but a third-party repo (say
    mgabiz) can perfectly publish mga10/x86_64 while having nothing
    at all for mga9/aarch64.  Probing ``media.cfg`` is the honest
    test: it's exactly the file ``cmd_media_discover`` will fetch
    next, so a 200 here guarantees the discover step won't 404.
    """
    base = host_media.get("discover_url", "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        return False
    url = f"{base}/media_info/media.cfg"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        log(f"  media probe: {url} → HTTP {e.code}")
        return False
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"  media probe: {url} → {e}")
        return False


def _rpm_file_version(rpm: Path) -> str:
    """Return ``version-release`` for a local RPM file via ``rpm -qp``."""
    r = subprocess.run(
        ["rpm", "-qp", "--nosignature", "--qf", "%{V}-%{R}", str(rpm)],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _version_gt(a: str, b: str) -> bool:
    """Return True if RPM version-release string ``a`` is greater than ``b``.

    Uses ``rpm.labelCompare`` for correct RPM version semantics
    (handles ``.``, ``-``, tilde etc. per rpm's own rules).
    """
    if not a or not b:
        return bool(a) and not b
    try:
        import rpm
    except ImportError:
        return a > b  # crude lexical fallback
    def split(vr: str):
        if "-" in vr:
            v, r = vr.rsplit("-", 1)
        else:
            v, r = vr, ""
        return ("", v, r)
    return rpm.labelCompare(split(a), split(b)) > 0


# ══════════════════════════════════════════════════════════════════════
#  Signal detection -- chroot side (image make)
# ══════════════════════════════════════════════════════════════════════

def _chroot_media_version(chroot_db: "PackageDatabase") -> Optional[str]:
    """Return ``version-release`` of urpm-ng-core available in the chroot's
    configured media, or None if no media provides it.
    """
    pkg = chroot_db.get_package("urpm-ng-core")
    if not pkg:
        return None
    ver = pkg.get("version")
    rel = pkg.get("release")
    if not ver:
        return None
    return f"{ver}-{rel}" if rel else ver


# ══════════════════════════════════════════════════════════════════════
#  Signal detection -- container side (image update)
# ══════════════════════════════════════════════════════════════════════

def _container_mageia_release(container: "Container", cid: str) -> Optional[str]:
    """Read ``Mageia release N`` from ``/etc/mageia-release`` inside a container."""
    result = container.exec(cid, ["cat", "/etc/mageia-release"])
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    parts = text.split()
    if len(parts) >= 3 and parts[0] == "Mageia" and parts[1] == "release":
        return parts[2]
    return None


def _container_identity(container: "Container", cid: str) -> Optional[str]:
    """Read the release identity from a container's ``/etc/os-release``.

    Returns the symbolic release identity (``'cauldron'``) when the
    image was built from a cauldron chroot, ``'10'`` / ``'11'`` when
    it was built from a numeric release, ``None`` when the file is
    unreadable.  ``cmd_init`` seeds a stub ``os-release`` in every
    fresh chroot so this call works from the very first bootstrap,
    before ``mageia-release-common`` lands.
    """
    result = container.exec(
        cid, ["sh", "-c", "grep '^VERSION_ID=' /etc/os-release || true"],
    )
    if result.returncode != 0:
        return None
    line = (result.stdout or "").strip()
    if not line.startswith("VERSION_ID="):
        return None
    return line.split("=", 1)[1].strip().strip('"') or None


def _container_arch(container: "Container", cid: str) -> Optional[str]:
    """Return ``uname -m`` output from inside a container."""
    result = container.exec(cid, ["uname", "-m"])
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _container_installed_version(container: "Container", cid: str) -> Optional[str]:
    """Return ``version-release`` of urpm-ng-core installed in the container, or None."""
    result = container.exec(
        cid, ["rpm", "-q", "--qf", "%{V}-%{R}", "urpm-ng-core"],
    )
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return text or None


def _container_media_version(container: "Container", cid: str) -> Optional[str]:
    """Return ``version-release`` of urpm-ng-core visible in the container's
    configured media, or None if no media provides it.  Uses ``urpm q``
    inside the container -- no direct DB access.
    """
    result = container.exec(
        cid, ["urpm", "q", "--qf", "%{V}-%{R}", "urpm-ng-core"],
    )
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return text or None


# ══════════════════════════════════════════════════════════════════════
#  Install helpers -- chroot side (image make)
# ══════════════════════════════════════════════════════════════════════

def _install_explicit_rpm_in_chroot(
    chroot_dir: str,
    chroot_db: "PackageDatabase",
    rpm: Path,
    log: callable,
) -> int:
    if not rpm.exists():
        log(f"  ERROR: --urpm-ng-core file not found: {rpm}")
        return 1
    log(f"  urpm-ng-core: --urpm-ng-core -> {rpm.name}")
    return _install_local_rpm_in_chroot(chroot_dir, chroot_db, rpm)


def _install_local_rpm_in_chroot(
    chroot_dir: str,
    chroot_db: "PackageDatabase",
    rpm: Path,
) -> int:
    from ..cli.commands.install import cmd_install
    ns = _chroot_install_args(chroot_dir, [str(rpm.resolve())], nosignature=True)
    return cmd_install(ns, chroot_db)


def _install_from_chroot_media(
    chroot_dir: str,
    chroot_db: "PackageDatabase",
) -> int:
    from ..cli.commands.install import cmd_install
    ns = _chroot_install_args(chroot_dir, ["urpm-ng-core"], nosignature=False)
    return cmd_install(ns, chroot_db)


def _install_from_host_media_in_chroot(
    chroot_dir: str,
    chroot_db: "PackageDatabase",
    media: dict,
    log: callable,
) -> int:
    """Add the host's media to the chroot (with pubkey), then install."""
    from ..cli.commands.media import cmd_media_discover

    discover_url = media.get("discover_url")
    if not discover_url:
        log("  ERROR: reconstructed media has no discover_url")
        return 1

    _import_pubkey_best_effort_chroot(
        chroot_dir, f"{discover_url.rstrip('/')}/media_info/pubkey", log,
    )
    ns = argparse.Namespace(
        urpm_root=chroot_dir,
        root=None,
        url=discover_url,
        with_categories=None,
        without_categories=None,
        sources=False,
        debug=False,
        dry_run=False,
        # Skip the host-root privilege check: we are operating on a
        # chroot the caller already owns.
        allow_no_root=True,
    )
    rc = cmd_media_discover(ns, chroot_db)
    if rc != 0:
        log(f"  ERROR: 'urpm media discover' returned {rc}")
        return rc

    # Discover only creates the media rows; their synthesis is not on
    # disk yet, so the chroot DB doesn't know about the packages they
    # provide.  Sync the freshly-added media before letting the solver
    # look for urpm-ng-core.
    log("  Syncing freshly-added media metadata...")
    from ..cli.commands.media import cmd_media_update
    upd_ns = argparse.Namespace(
        urpm_root=chroot_dir,
        root=None,
        name=None,                # sync every enabled media
        force=False,
        no_appstream=True,        # skip appstream sync in chroot
        allow_no_root=True,
    )
    rc = cmd_media_update(upd_ns, chroot_db)
    if rc != 0:
        log(f"  ERROR: 'urpm media update' returned {rc}")
        return rc

    return _install_from_chroot_media(chroot_dir, chroot_db)


def _install_from_github_in_chroot(
    chroot_dir: str,
    chroot_db: "PackageDatabase",
    arch: str,
    mageia_release: str,
    target_numeric: Optional[str],
    allow_disttag_mismatch: bool,
    log: callable,
) -> int:
    rpm = _download_urpm_ng_from_github(
        arch, mageia_release, target_numeric, allow_disttag_mismatch, log)
    if rpm is None:
        return 1
    try:
        return _install_local_rpm_in_chroot(chroot_dir, chroot_db, rpm)
    finally:
        try:
            rpm.unlink()
        except OSError:
            pass


def _chroot_install_args(
    chroot_dir: str,
    packages: list,
    nosignature: bool,
) -> argparse.Namespace:
    return argparse.Namespace(
        urpm_root=chroot_dir,
        root=chroot_dir,
        packages=packages,
        auto=True,
        without_recommends=True,
        with_suggests=False,
        download_only=False,
        nodeps=False,
        nosignature=nosignature,
        noscripts=True,
        force=False,
        reinstall=False,
        debug=None,
        watched=None,
        prefer=None,
        all=False,
        test=False,
        sync=True,
        allow_no_root=True,
        config_policy="replace",
        no_readme=True,
        arch=None,
    )


def _import_pubkey_best_effort_chroot(
    chroot_dir: str,
    pubkey_url: str,
    log: callable,
) -> None:
    pubkey_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pubkey", delete=False) as f:
            pubkey_path = f.name
        try:
            urllib.request.urlretrieve(pubkey_url, pubkey_path)
        except (urllib.error.URLError, OSError) as e:
            log(f"    Warning: pubkey fetch from {pubkey_url} failed: {e}")
            return
        # Hermetic env — keeps the operator's ``~/.rpmmacros`` /
        # XDG dirs out of the chroot rpm.  See
        # :mod:`urpm.core.userns_env`.
        from .userns_env import bootstrap_env
        result = subprocess.run(
            ["rpm", "--root", chroot_dir, "--import", pubkey_path],
            capture_output=True, text=True,
            env=bootstrap_env(chroot_dir),
        )
        if result.returncode != 0:
            log(f"    Warning: pubkey import failed: {result.stderr.strip()}")
    finally:
        if pubkey_path:
            try:
                os.unlink(pubkey_path)
            except OSError:
                pass


# ══════════════════════════════════════════════════════════════════════
#  Install helpers -- container side (image update)
# ══════════════════════════════════════════════════════════════════════

def _install_explicit_rpm_in_container(
    container: "Container",
    cid: str,
    rpm: Path,
    log: callable,
) -> int:
    if not rpm.exists():
        log(f"  ERROR: --urpm-ng-core file not found: {rpm}")
        return 1
    log(f"  urpm-ng-core: --urpm-ng-core -> {rpm.name}")
    return _install_local_rpm_in_container(container, cid, rpm, log)


def _install_local_rpm_in_container(
    container: "Container",
    cid: str,
    rpm: Path,
    log: callable,
) -> int:
    dst = f"{cid}:/tmp/{rpm.name}"
    if not container.cp(str(rpm.resolve()), dst):
        log(f"  ERROR: failed to copy {rpm.name} into container")
        return 1
    rc = container.exec_stream(cid, [
        "urpm", "i", "--auto", "--reinstall", "--nosignature",
        f"/tmp/{rpm.name}",
    ])
    container.exec(cid, ["rm", "-f", f"/tmp/{rpm.name}"])
    if rc != 0:
        log(f"  ERROR: install of {rpm.name} in container returned {rc}")
    return rc


def _install_from_host_media_in_container(
    container: "Container",
    cid: str,
    media: dict,
    log: callable,
) -> int:
    """Add the host media to the container (with pubkey), then install urpm-ng-core.

    Per rule 3: the media is added to the image so future
    ``urpm image update`` runs pick up urpm-ng-core naturally via
    ``urpm u`` without going through this code path again.
    """
    discover_url = media.get("discover_url")
    if not discover_url:
        log("  ERROR: reconstructed media has no discover_url")
        return 1

    pubkey_url = f"{discover_url.rstrip('/')}/media_info/pubkey"
    rc = container.exec_stream(cid, [
        "sh", "-c",
        f"curl -fsSL -o /tmp/urpm-ng-pubkey {pubkey_url} "
        f"&& rpm --import /tmp/urpm-ng-pubkey; "
        f"rm -f /tmp/urpm-ng-pubkey",
    ])
    if rc != 0:
        log(f"  Warning: pubkey fetch/import returned {rc}")

    rc = container.exec_stream(cid, [
        "urpm", "media", "discover", discover_url,
    ])
    if rc != 0:
        log(f"  ERROR: 'urpm media discover' returned {rc}")
        return rc

    rc = container.exec_stream(cid, ["urpm", "media", "update"])
    if rc != 0:
        log(f"  Warning: post-discover media update returned {rc}")

    rc = container.exec_stream(cid, [
        "urpm", "i", "--auto", "urpm-ng-core",
    ])
    if rc != 0:
        log(f"  ERROR: 'urpm i urpm-ng-core' returned {rc}")
        return rc
    return 0


def _install_from_github_in_container(
    container: "Container",
    cid: str,
    arch: str,
    mageia_release: str,
    target_numeric: Optional[str],
    allow_disttag_mismatch: bool,
    log: callable,
) -> int:
    rpm = _download_urpm_ng_from_github(
        arch, mageia_release, target_numeric, allow_disttag_mismatch, log)
    if rpm is None:
        return 1
    try:
        return _install_local_rpm_in_container(container, cid, rpm, log)
    finally:
        try:
            rpm.unlink()
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════
#  User prompts
# ══════════════════════════════════════════════════════════════════════

def _confirm_local_over_media(
    local_rpm: Path,
    media_version: str,
    log: callable,
) -> bool:
    local_ver = _rpm_file_version(local_rpm)
    print(
        f"  Local urpm-ng-core is newer ({local_ver}) than the media "
        f"version ({media_version}).\n"
        f"  Use the local build?  [y/N] ",
        end="",
    )
    try:
        reply = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes", "o", "oui")


def _confirm_fallback_github(log: callable) -> bool:
    print(
        "  Host has no media providing urpm-ng-core for this target.\n"
        "  Fall back to GitHub latest release?  [y/N] ",
        end="",
    )
    try:
        reply = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes", "o", "oui")


# ══════════════════════════════════════════════════════════════════════
#  GitHub helpers (shared between chroot + container arms)
# ══════════════════════════════════════════════════════════════════════

def _download_urpm_ng_from_github(
    arch: str,
    mageia_release: str,
    target_numeric: Optional[str],
    allow_disttag_mismatch: bool,
    log: callable,
) -> Optional[Path]:
    """Fetch the latest urpm-ng-core RPM matching arch + Mageia release
    from the project's GitHub releases and return its local path.
    Caller owns the file and must delete it after use.

    ``target_numeric`` and ``allow_disttag_mismatch`` are forwarded to
    :func:`_github_pick_asset` so Rule 4's disttag filter follows the
    same relaxation as Rule 1 (see :func:`_accepted_disttags`).
    """
    tag = _github_latest_tag(log)
    if not tag:
        log("  ERROR: could not resolve latest GitHub release tag")
        return None
    log(f"    latest tag: {tag}")

    rpm_url = _github_pick_asset(
        tag, arch, mageia_release, target_numeric, allow_disttag_mismatch, log)
    if not rpm_url:
        log(f"  ERROR: no urpm-ng-core RPM at {tag} for mga{mageia_release}/{arch}")
        return None

    with tempfile.NamedTemporaryFile(suffix=".rpm", delete=False) as f:
        rpm_path = Path(f.name)
    try:
        log(f"    downloading {rpm_path.name} from GitHub...")
        urllib.request.urlretrieve(rpm_url, str(rpm_path))
    except (urllib.error.URLError, OSError) as e:
        log(f"  ERROR: GitHub download failed: {e}")
        try:
            rpm_path.unlink()
        except OSError:
            pass
        return None
    return rpm_path


def _github_latest_tag(log: callable) -> Optional[str]:
    try:
        with urllib.request.urlopen(f"{GITHUB_API}/releases/latest") as resp:
            data = json.loads(resp.read())
        return data.get("tag_name")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log(f"  ERROR: GitHub /releases/latest returned HTTP {e.code}")
            return None
    except (urllib.error.URLError, OSError) as e:
        log(f"  ERROR: could not reach GitHub API: {e}")
        return None

    try:
        with urllib.request.urlopen(f"{GITHUB_API}/releases") as resp:
            releases = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        log(f"  ERROR: could not reach GitHub API: {e}")
        return None
    return releases[0].get("tag_name") if releases else None


def _github_pick_asset(
    tag: str,
    arch: str,
    mageia_release: str,
    target_numeric: Optional[str],
    allow_disttag_mismatch: bool,
    log: callable,
) -> Optional[str]:
    """Pick the ``urpm-ng-core`` asset URL for ``tag`` that matches the
    target arch + Mageia release.

    Disttag matching goes through :func:`_accepted_disttags` — the same
    relaxation Rule 1 (local match) already applies.  Chief consequence
    for cauldron : the raw filter ``".mga{cauldron}."`` never matches
    anything (cauldron RPMs carry the current numeric disttag, e.g.
    ``.mga11.``, never ``.mgacauldron.``).  Delegating to
    :func:`_accepted_disttags` translates the identity into the right
    set of numeric disttags — ``{.mga11., .mga10.}`` for cauldron at
    numeric=11 — and unblocks the fallback path.
    """
    try:
        with urllib.request.urlopen(f"{GITHUB_API}/releases/tags/{tag}") as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        log(f"  ERROR: could not fetch release {tag} details: {e}")
        return None
    accepted = _accepted_disttags(
        mageia_release, target_numeric, allow_disttag_mismatch)
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if not name.startswith("urpm-ng-core-"):
            continue
        if "-debuginfo-" in name or "-debugsource-" in name:
            continue
        if not (name.endswith(f".{arch}.rpm") or name.endswith(".noarch.rpm")):
            continue
        if accepted is not None and not any(dt in name for dt in accepted):
            continue
        return asset.get("browser_download_url")
    return None
