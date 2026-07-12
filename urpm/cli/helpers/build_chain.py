"""Multi-spec builds inside a shared container.

Called by ``urpm build`` when ``--parallel`` is not requested (the
default).  A single container is instantiated for the whole run;
shared phases (``urpm upgrade``, ``rpm-build`` install, host CA
trust bootstrap) execute once, and each spec then compiles in its
own ``/root/<pkg-name>`` topdir so the ``BUILD`` / ``BUILDROOT`` /
``SOURCES`` trees never collide.

Design notes
------------

- **Topdir per package.**  We set ``%_topdir /root/<pkg-name>`` on
  every ``rpmbuild`` and ``rpmspec`` invocation.  The rest of the
  urpm plumbing (``urpm install --buildrequires``) does not care
  about ``_topdir``; it only reads the spec file.

- **Output paths preserved.**  Even though the container-side
  topdir moves, the host-side destination for RPMs/SRPMs and the
  build log follows exactly what ``_build_single_package`` did:
  workspace-relative for spec builds, ``output_dir/<name>/`` for
  SRPM builds.  Existing packagers see no change.

- **Produced RPMs re-injected as a local media.**  After each
  successful spec, its RPMs are copied to ``/root/produced-rpms/``
  and the media metadata is regenerated on the fly, so a later
  spec whose BuildRequires depends on one of the newly-built
  packages picks it up naturally via the resolver.  The
  ``--rollback-between-builds`` flag rewinds the container to the
  post-setup baseline between specs, which does NOT touch the
  ``produced-rpms`` media — the artefacts remain available.

- **``--parallel`` untouched.**  Isolated multi-container builds
  (one spec per container, N in flight) still run through
  ``_build_single_package``; that path stays useful for
  build-system-of-the-poor experiments.  See
  ``doc/TODO_BUILD_MULTI_IMAGE_DASHBOARD.md`` for the long-term
  vision.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple, TYPE_CHECKING

from ...i18n import _, ngettext

if TYPE_CHECKING:
    from ...core.container import Container


# The Mageia ``produced-rpms`` local media lives here inside the
# container; the resolver picks it up as soon as ``urpm media add``
# is called during the shared setup.  Kept as a constant so tests
# can assert against it and future callers do not invent variants.
PRODUCED_MEDIA_DIR = "/root/produced-rpms"
PRODUCED_MEDIA_NAME = "chain-produced"


# rpm's documented "more BuildRequires needed" exit code.
# Mirrors the value used in ``_build_single_package`` — kept local
# so this module has no build.py dependency.
RPMBUILD_MISSING_BR = 11
MAX_DYNBR_PASSES = 16
_VER_RE = re.compile(r'\s*(?:>=|<=|=>|=<|[><=!])\s*\S+')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_pkg_name(container: "Container", cid: str, spec_path: str) -> str:
    """Return the source-package name declared inside ``spec_path``.

    Uses ``rpmspec -q --srpm --qf '%{name}'`` inside the container
    so macros defined in ``.rpmmacros`` are expanded.  Falls back to
    the spec file stem when ``rpmspec`` fails so we still get a
    stable, unique-per-spec topdir name.
    """
    result = container.exec(
        cid, ['rpmspec', '-q', '--srpm', '--qf', '%{name}', spec_path])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return Path(spec_path).stem


def _topdir_for(pkg_name: str) -> str:
    """Container-side ``_topdir`` for the given package."""
    return f"/root/{pkg_name}"


def _get_last_txn_id(container: "Container", cid: str) -> Optional[int]:
    """Return the id of the newest transaction in the container's history.

    ``urpm history`` prints newest first; we grab the first numeric
    token of the first non-header line.  ``None`` when the history
    is empty or unreadable.
    """
    result = container.exec(cid, ['urpm', 'history'])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        # Header lines start with the ``#`` glyph or with spaces;
        # transaction rows start with a bare integer.
        stripped = line.strip()
        if not stripped:
            continue
        # Some formats show ``#123`` — accept both.
        token = stripped.lstrip('#').split()[0]
        try:
            return int(token)
        except ValueError:
            continue
    return None


def _rollback_to(container: "Container", cid: str, baseline_id: int) -> bool:
    """Run ``urpm rollback to <baseline_id>`` inside the container.

    Returns True on success.  Failures are logged by the caller so
    it can decide whether to abort the chain or fall through.
    """
    result = container.exec(
        cid, ['urpm', 'rollback', '--auto', 'to', str(baseline_id)])
    return result.returncode == 0


def _add_produced_media_if_missing(container: "Container", cid: str) -> None:
    """Ensure the local ``produced-rpms`` media exists in the container.

    Idempotent — safe to call every time we publish a new artefact.
    ``urpm media add`` on an already-registered media is a no-op
    that returns a non-zero exit code but does not disturb the DB,
    which is why we don't return the result.
    """
    container.exec(cid, ['mkdir', '-p', PRODUCED_MEDIA_DIR])
    # ``--custom`` because ``file:///…`` is not an official Mageia
    # URL; ``--allow-unsigned`` because the RPMs we just built are
    # not signed (this is a local scratchpad, not a distribution
    # channel).
    container.exec(cid, [
        'urpm', 'media', 'add', '--custom',
        PRODUCED_MEDIA_NAME, PRODUCED_MEDIA_NAME,
        f'file://{PRODUCED_MEDIA_DIR}',
        '--allow-unsigned',
    ])


def _publish_produced_rpms(container: "Container", cid: str,
                           produced_rpms_in_container: List[str]) -> None:
    """Copy freshly built RPMs into the local media and refresh it.

    ``produced_rpms_in_container`` is a list of container-side paths
    (typically ``/root/<pkg>/RPMS/<arch>/foo-*.rpm``).  Empty list
    → nothing to do (a spec that produced only debuginfo, for
    example).
    """
    if not produced_rpms_in_container:
        return
    _add_produced_media_if_missing(container, cid)
    # Batch-copy under the media dir.  ``cp -f`` keeps the target
    # writable across successive publishes for the same package name
    # (rebuild after a source tweak).
    for rpm_path in produced_rpms_in_container:
        container.exec(cid, ['cp', '-f', rpm_path, PRODUCED_MEDIA_DIR])
    # Regenerate synthesis so the resolver sees the new packages.
    container.exec(
        cid, ['urpm', 'media', 'update', PRODUCED_MEDIA_NAME])


# ---------------------------------------------------------------------------
# Shared-container setup + per-spec build
# ---------------------------------------------------------------------------


def _setup_shared_container(
    container: "Container",
    cid: str,
    with_rpms: List[Path],
    no_update: bool,
    rpmmacros_path: Optional[Path],
    subrel: Optional[str],
) -> Optional[int]:
    """Run the phases every spec shares against an already-created cid.

    The caller instantiates the container so that a setup failure
    still leaves a reachable ``cid`` for teardown.  Returns the
    baseline transaction id used by ``--rollback-between-builds`` as
    its rewind target, or ``None`` when the container has no
    readable history (which should not occur after ``rpm-build``
    install, but the caller tolerates it).
    """
    from .. import colors  # local import to avoid cycles at module load

    container.probe_arch(cid)

    if rpmmacros_path or subrel:
        macros_content = ''
        if rpmmacros_path:
            print(_("  rpmmacros: {path}").format(path=rpmmacros_path))
            macros_content = rpmmacros_path.read_text()
            if macros_content and not macros_content.endswith('\n'):
                macros_content += '\n'
        if subrel:
            print(_("  Sub-release tag: {tag}").format(tag=subrel))
            macros_content += f'%subrel {subrel}\n'
        with tempfile.NamedTemporaryFile(
                mode='w', suffix='.rpmmacros',
                prefix='urpm-build-', delete=False,
        ) as macros_tmp:
            macros_tmp.write(macros_content)
            macros_tmp_path = macros_tmp.name
        try:
            container.cp(macros_tmp_path, f'{cid}:/root/.rpmmacros')
        finally:
            Path(macros_tmp_path).unlink(missing_ok=True)

    # Bootstrap CA trust so signed downloads through pip/curl inside
    # rpmbuild resolve properly.
    container.exec(cid, ['/bin/update-ca-trust', 'extract'])

    if not no_update:
        print(_("  Updating media..."))
        ret = container.exec_stream(cid, ['urpm', 'media', 'update'])
        if ret != 0:
            print(colors.warning(
                _("  Warning: media update failed, continuing...")))
        print(_("  Updating packages..."))
        ret = container.exec_stream(cid, ['urpm', 'upgrade', '--auto'])
        if ret != 0:
            print(colors.warning(
                _("  Warning: package update failed, continuing...")))

    print(_("  Installing rpm-build..."))
    ret = container.exec_stream(cid, [
        'urpm', 'install', '--auto', '--without-recommends', '--sync',
        'rpm-build',
    ])
    if ret != 0:
        raise RuntimeError(_("Failed to install rpm-build in shared container"))

    if with_rpms:
        print(ngettext(
            "  Pre-installing {count} local RPM...",
            "  Pre-installing {count} local RPMs...",
            len(with_rpms)).format(count=len(with_rpms)))
        container.exec(cid, ['mkdir', '-p', '/tmp/local-rpms'])
        rpm_paths_in_container = []
        for rpm_path in with_rpms:
            if not container.cp(str(rpm_path), f"{cid}:/tmp/local-rpms/"):
                raise RuntimeError(
                    _("Failed to copy {name} into shared container").format(
                        name=rpm_path.name))
            rpm_paths_in_container.append(
                f"/tmp/local-rpms/{rpm_path.name}")
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--without-recommends', '--sync',
            '--nosignature',
        ] + rpm_paths_in_container)
        if ret != 0:
            raise RuntimeError(
                _("Failed to install --with-rpms into shared container"))

    # Prepare the local media directory now so its ``urpm media add``
    # is idempotent even if the very first spec produces nothing.
    _add_produced_media_if_missing(container, cid)

    return _get_last_txn_id(container, cid)


def _build_one_spec_in_container(
    container: "Container",
    cid: str,
    source_path: Path,
    output_dir: Path,
    _find_workspace_fn: Callable,
    _diagnose_fn: Callable,
) -> Tuple[Path, bool, str, List[str]]:
    """Build one spec inside a container that has already been set up.

    ``_find_workspace_fn`` and ``_diagnose_fn`` are injected so this
    module does not import ``build.py`` and risk a cycle.  The
    caller (``run_shared_container_chain``) passes them from
    ``build.py`` when it dispatches.

    Returns ``(source, success, message, produced_rpm_paths)`` where
    ``produced_rpm_paths`` is the container-side list of the RPMs
    that were just built — the caller feeds them to
    :func:`_publish_produced_rpms` so the next spec can consume them.
    """
    from .. import colors

    is_spec_build = source_path.suffix == '.spec'
    workspace = None
    sources_dir = None

    # Copy spec / sources into a package-scoped topdir.  The
    # per-package topdir is what keeps parallel-inside-container
    # BUILD trees from collide, and also what makes cleanup
    # trivially safe: we never wipe another package's WIP.
    if source_path.suffix == '.rpm' and '.src.' in source_path.name:
        # SRPM install path — use its own top-level dir named after
        # the SRPM stem (minus ``.src``) to keep the same layout as
        # spec builds.
        stem = source_path.stem.replace('.src', '')
        # The name field is embedded in the SRPM but we don't need
        # to know it upfront: rpm's own ``rpm -ivh`` will drop the
        # spec into whatever ``_topdir`` we pass.  Guess a name for
        # the topdir first (SRPM stem), then reconcile once the spec
        # is on disk.
        pkg_topdir = _topdir_for(stem)
        container.exec(cid, [
            'mkdir', '-p',
            f'{pkg_topdir}/SPECS', f'{pkg_topdir}/SOURCES',
            f'{pkg_topdir}/BUILD', f'{pkg_topdir}/BUILDROOT',
            f'{pkg_topdir}/RPMS',  f'{pkg_topdir}/SRPMS',
        ])
        if not container.cp(str(source_path), f"{cid}:{pkg_topdir}/SRPMS/"):
            return (source_path, False, "Failed to copy SRPM", [])
        print(_("  Installing SRPM..."))
        result = container.exec(cid, [
            'rpm', '--define', f'_topdir {pkg_topdir}',
            '-ivh', f'{pkg_topdir}/SRPMS/{source_path.name}',
        ])
        if result.returncode != 0:
            return (source_path, False,
                    f"SRPM install failed: {result.stderr}", [])
        name_parts = source_path.stem.replace('.src', '').rsplit('-', 2)
        spec_name = name_parts[0] + '.spec'
        spec_path = f'{pkg_topdir}/SPECS/{spec_name}'
        pkg_name = name_parts[0]

    elif is_spec_build:
        workspace, sources_dir, _is_rpmbuild_layout = _find_workspace_fn(
            source_path)
        # Provisional topdir name from the spec file — refined once
        # rpmspec has told us the real ``%{name}``.  Two-pass rename
        # is not worth it: build layouts always use the file's stem
        # as the package identity in practice.
        pkg_topdir = _topdir_for(source_path.stem)
        container.exec(cid, [
            'mkdir', '-p',
            f'{pkg_topdir}/SPECS', f'{pkg_topdir}/SOURCES',
            f'{pkg_topdir}/BUILD', f'{pkg_topdir}/BUILDROOT',
            f'{pkg_topdir}/RPMS',  f'{pkg_topdir}/SRPMS',
        ])
        if not container.cp(str(source_path), f"{cid}:{pkg_topdir}/SPECS/"):
            return (source_path, False, "Failed to copy spec file", [])
        spec_path = f'{pkg_topdir}/SPECS/{source_path.name}'
        if sources_dir and sources_dir.exists():
            source_files = [f for f in sources_dir.iterdir() if f.is_file()]
            print(ngettext(
                "  Copying {count} source file from {path}...",
                "  Copying {count} source files from {path}...",
                len(source_files)).format(
                count=len(source_files), path=sources_dir))
            container.cp(f"{sources_dir}/.",
                         f"{cid}:{pkg_topdir}/SOURCES/")
        else:
            print(colors.warning(_("  Warning: No SOURCES directory found")))
        pkg_name = _spec_pkg_name(container, cid, spec_path)
        # Reconcile if the real name differs from the stem — most
        # of the time it won't, but Mageia has a handful of specs
        # whose file basename does not match ``%{name}``.
        if pkg_name != source_path.stem:
            new_topdir = _topdir_for(pkg_name)
            container.exec(cid, ['mkdir', '-p', new_topdir])
            container.exec(
                cid, ['sh', '-c',
                      f'mv {pkg_topdir}/* {new_topdir}/ 2>/dev/null; '
                      f'rmdir {pkg_topdir} 2>/dev/null; true'])
            pkg_topdir = new_topdir
            spec_path = f'{pkg_topdir}/SPECS/{source_path.name}'

    else:
        return (source_path, False,
                f"Unsupported source type: {source_path.suffix}", [])

    container_log = f'/tmp/log.{pkg_name}'

    # Static BuildRequires — resolves via all enabled media,
    # including the produced-rpms one from earlier chain steps.
    print(_("  Installing BuildRequires..."))
    ret = container.exec_stream(cid, [
        'urpm', 'install', '--auto', '--without-recommends', '--sync',
        '--buildrequires', spec_path,
    ])
    if ret != 0:
        return (source_path, False, "BuildRequires install failed", [])

    # Dynamic BuildRequires convergence loop.  Identical logic to
    # ``_build_single_package`` — kept inline because factoring it
    # out would require an even bigger surface to move, and the
    # subtle interactions with the container log and the retry cap
    # are easier to reason about locally.
    for dynbr_pass in range(MAX_DYNBR_PASSES):
        result = container.exec(cid, [
            'bash', '-c',
            f'set -o pipefail; '
            f'rpmbuild --define "_topdir {pkg_topdir}" -br {spec_path} '
            f'2>&1 | tee -a {container_log}',
        ])
        rc = result.returncode
        if rc == 0:
            break
        if rc != RPMBUILD_MISSING_BR:
            if result.stdout:
                print(result.stdout, end='')
            return (source_path, False,
                    _("rpmbuild -br failed before %build (rc={rc}, see log)"
                      ).format(rc=rc), [])

        result = container.exec(cid, [
            'bash', '-c',
            f'rpm -qp --requires {pkg_topdir}/SRPMS/*.buildreqs.nosrc.rpm '
            f'2>/dev/null',
        ])
        if result.returncode != 0:
            return (source_path, False,
                    _("rpmbuild reported missing BRs but "
                      ".buildreqs.nosrc.rpm could not be read"), [])
        new_deps = []
        for line in result.stdout.splitlines():
            dep = line.strip()
            if dep and not dep.startswith('rpmlib('):
                new_deps.append(dep)
        new_deps = sorted(set(new_deps))
        if not new_deps:
            return (source_path, False,
                    _("rpmbuild requested more BRs but emitted no "
                      "installable requirements"), [])
        print(colors.warning(
            _("Getting dynamic buildrequires (round {n}), found :").format(
                n=dynbr_pass + 1)))
        for dep in new_deps:
            if '(' in dep:
                print("  " + colors.dim(colors.light_purple(dep)))
            else:
                print("  " + colors.light_purple(dep))
        ret = container.exec_stream(cid, [
            'urpm', 'install', '--auto', '--without-recommends', '--sync',
        ] + new_deps)
        if ret != 0:
            _diagnose_fn(cid, container, new_deps)
            return (source_path, False,
                    _("Dynamic BuildRequires install failed"), [])
    else:
        return (source_path, False,
                _("Dynamic BuildRequires did not converge in {n} passes"
                  ).format(n=MAX_DYNBR_PASSES), [])

    # Actual build.
    print(_("  Building..."))
    result = container.exec_stream(cid, [
        'bash', '-c',
        f'set -o pipefail; '
        f'rpmbuild --define "_topdir {pkg_topdir}" -ba {spec_path} '
        f'2>&1 | tee -a {container_log}',
    ])
    build_failed = result != 0

    # Determine host-side destinations exactly as
    # ``_build_single_package`` did.
    if is_spec_build and workspace:
        rpms_dir = workspace / 'RPMS'
        srpms_dir = workspace / 'SRPMS'
        log_dir = workspace / 'SPECS'
    else:
        pkg_output = output_dir / source_path.stem.replace('.src', '')
        pkg_output.mkdir(parents=True, exist_ok=True)
        rpms_dir = pkg_output / 'RPMS'
        srpms_dir = pkg_output / 'SRPMS'
        log_dir = pkg_output
    rpms_dir.mkdir(parents=True, exist_ok=True)
    srpms_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"log.{pkg_name}"
    if container.cp(f"{cid}:{container_log}", str(log_file)):
        print(_("  Build log: {path}").format(path=log_file))

    if build_failed:
        return (source_path, False,
                f"rpmbuild failed (see {log_file})", [])

    print(_("  Copying RPMs to {path}/").format(path=rpms_dir))
    container.cp(f"{cid}:{pkg_topdir}/RPMS/.", str(rpms_dir))
    print(_("  Copying SRPMs to {path}/").format(path=srpms_dir))
    container.cp(f"{cid}:{pkg_topdir}/SRPMS/.", str(srpms_dir))
    rpm_count = len(list(rpms_dir.rglob('*.rpm')))
    srpm_count = len(list(srpms_dir.rglob('*.rpm')))

    # Enumerate the produced RPMs container-side so the caller can
    # publish them into the local media without a round-trip
    # through the host.
    result = container.exec(cid, [
        'bash', '-c', f'ls {pkg_topdir}/RPMS/*/*.rpm 2>/dev/null || true'])
    produced = [line.strip() for line in result.stdout.splitlines()
                if line.strip()]

    return (source_path, True,
            f"{rpm_count} RPMs, {srpm_count} SRPMs", produced)


def run_shared_container_chain(
    container: "Container",
    image: str,
    valid_sources: List[Path],
    output_dir: Path,
    keep_container: bool,
    with_rpms: List[Path],
    no_update: bool,
    subrel: Optional[str],
    rpmmacros_path: Optional[Path],
    stop_on_fail: bool,
    rollback_between_builds: bool,
    _find_workspace_fn: Callable,
    _diagnose_fn: Callable,
) -> List[Tuple[Path, bool, str]]:
    """Compile each spec in ``valid_sources`` in a single container.

    Returns a list of ``(source_path, success, message)`` tuples
    parallel to ``valid_sources``.  When ``stop_on_fail`` is set, the
    remaining entries after the first failure are added with a
    "skipped" message so the caller can still report a full status
    table.  Never raises: any exception during setup fails the
    whole chain gracefully.
    """
    from .. import colors

    cid: Optional[str] = None
    baseline_id: Optional[int] = None
    results: List[Tuple[Path, bool, str]] = []
    try:
        # Create the container first so a setup failure still leaves
        # a reachable ``cid`` for the ``finally`` block to reap.
        cid = container.run(
            image,
            ['sleep', 'infinity'],
            detach=True,
            rm=False,
            network='host',
        )
        print(_("  Container: {cid}").format(cid=cid[:12]))
        try:
            baseline_id = _setup_shared_container(
                container, cid,
                with_rpms=with_rpms, no_update=no_update,
                rpmmacros_path=rpmmacros_path, subrel=subrel,
            )
        except RuntimeError as e:
            # Setup failure — nothing built, mark every source as
            # failed with the same reason so the caller's summary
            # is correct.
            for src in valid_sources:
                results.append((src, False, str(e)))
            return results

        for idx, source_path in enumerate(valid_sources):
            print(f"\n{'=' * 60}")
            print(_("Building: {name}").format(name=source_path.name))
            print(f"{'=' * 60}")
            src, ok, msg, produced = _build_one_spec_in_container(
                container, cid, source_path, output_dir,
                _find_workspace_fn=_find_workspace_fn,
                _diagnose_fn=_diagnose_fn,
            )
            results.append((src, ok, msg))
            if ok:
                _publish_produced_rpms(container, cid, produced)
            elif stop_on_fail:
                # Skip remaining specs but include them in the
                # summary so the caller can print a complete table.
                for rest in valid_sources[idx + 1:]:
                    results.append(
                        (rest, False,
                         _("skipped (--stop-on-fail)")))
                break

            # Roll back per-spec BuildRequires between specs, if
            # requested.  Only fires when the last build succeeded
            # AND we have a baseline id AND there's another spec
            # to come — after the final spec the container will be
            # reaped anyway, so the rollback would be wasted work.
            has_next = idx + 1 < len(valid_sources)
            if (rollback_between_builds and ok
                    and baseline_id is not None
                    and has_next):
                if not _rollback_to(container, cid, baseline_id):
                    print(colors.warning(_(
                        "  Warning: rollback to baseline #{n} failed; "
                        "next spec starts from the current state."
                    ).format(n=baseline_id)))
        return results
    finally:
        if cid and not keep_container:
            container.rm(cid)
