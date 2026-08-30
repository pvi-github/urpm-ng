"""Container runtime abstraction for Docker/Podman.

Provides a unified interface for container operations regardless of
whether Docker or Podman is being used.
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ContainerRuntime:
    """Detected container runtime."""
    name: str           # 'docker' or 'podman'
    path: str           # /usr/bin/docker
    version: str        # 24.0.1


def detect_runtime(preferred: str = None) -> ContainerRuntime:
    """Detect available container runtime.

    Args:
        preferred: 'docker', 'podman', or None (auto-detect, prefers podman)

    Returns:
        ContainerRuntime with detected info

    Raises:
        RuntimeError if no runtime found
    """
    if preferred:
        runtimes = [preferred]
    else:
        # Prefer podman (rootless, daemonless)
        runtimes = ['podman', 'docker']

    for rt in runtimes:
        path = shutil.which(rt)
        if path:
            # Get version
            try:
                result = subprocess.run(
                    [path, '--version'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    # Parse version from output like "podman version 4.5.0" or "Docker version 24.0.1"
                    version = result.stdout.strip().split()[-1]
                else:
                    version = 'unknown'
            except (subprocess.TimeoutExpired, OSError):
                version = 'unknown'

            return ContainerRuntime(name=rt, path=path, version=version)

    raise RuntimeError(
        "No container runtime found. Install docker or podman.\n"
        "  Mageia: urpmi podman\n"
        "  Or: urpmi docker"
    )


class Container:
    """Wrapper for container operations."""

    # Per-cid record of the user-space architecture, set by
    # ``probe_arch()`` and consulted by ``_personality_wrap()``.
    # Used to prepend ``linux32`` to every exec into a 32-bit
    # container running on a 64-bit kernel, so that ``uname -m``
    # (and everything downstream — rpm-build's CFLAGS, Python's
    # ``sysconfig.get_platform()``, …) reports the user-space arch
    # rather than the kernel arch.
    _ARCH_NEEDS_LINUX32 = frozenset({'i386', 'i486', 'i586', 'i686'})

    def __init__(self, runtime: ContainerRuntime):
        self.runtime = runtime
        self.cmd = runtime.path
        self._arch_by_cid: dict[str, str] = {}
        # Cache "does this container have C.UTF-8 compiled?" per cid.
        # Mageia 9 predates glibc 2.35 (where C.UTF-8 became builtin)
        # and the -minimal images ship no locales, so a naive
        # LC_ALL=C.UTF-8 there produces the "Setting locale failed"
        # spam.  The probe is one podman-exec locale -a; the cache
        # keeps it out of the hot path.
        self._has_c_utf8_by_cid: dict[str, bool] = {}

    def run(
        self,
        image: str,
        command: List[str] = None,
        detach: bool = False,
        rm: bool = True,
        volumes: List[Tuple[str, str]] = None,
        name: str = None,
        network: str = None,
        workdir: str = None,
        env: dict = None,
        memory: Optional[str] = None,
        memory_swap: Optional[str] = None,
        cpus: Optional[int] = None,
    ) -> str:
        """Run a container.

        Args:
            image: Image name/tag to run
            command: Command to execute in container
            detach: Run in background
            rm: Remove container when it exits
            volumes: List of (host_path, container_path) tuples
            name: Container name
            network: Network mode ('host', 'bridge', etc.)
            workdir: Working directory in container
            env: Environment variables dict
            memory: Container RAM ceiling (e.g. ``"8g"``, ``"12000m"``,
                ``"16G"``).  Passed to ``--memory``.  ``None`` leaves
                the runtime default (no cap).
            memory_swap: Total ``memory + swap`` ceiling.  Passed to
                ``--memory-swap`` verbatim, so podman semantics apply:
                the string ``"-1"`` means "unlimited swap" (the
                container can spill onto host swap without a cgroup
                cap), any positive size caps combined RAM+swap, and
                the same value as ``memory`` means "no swap headroom".
                ``None`` skips the flag entirely, in which case podman
                falls back to its own default (usually twice
                ``memory``).  Ignored when ``memory`` is ``None``.
            cpus: Maximum CPU cores the container may use.  Passed to
                ``--cpus``.  ``None`` leaves the runtime default (all
                host cores).  Note that ``--cpus`` caps *cumulative*
                CPU time and does not by itself lower rpmbuild's ``-j``
                — callers that want fewer parallel compile jobs must
                also inject the corresponding ``%_smp_mflags`` override.

        Returns:
            Container ID if detached, else stdout
        """
        args = [self.cmd, 'run']

        if detach:
            args.append('-d')
        if rm:
            args.append('--rm')
        if name:
            args.extend(['--name', name])
        if network:
            args.extend(['--network', network])
        if workdir:
            args.extend(['-w', workdir])
        if volumes:
            for host_path, container_path in volumes:
                args.extend(['-v', f'{host_path}:{container_path}'])
        if env:
            for key, value in env.items():
                args.extend(['-e', f'{key}={value}'])
        if memory:
            args.extend(['--memory', memory])
            if memory_swap is not None:
                args.extend(['--memory-swap', memory_swap])
        if cpus:
            args.extend(['--cpus', str(cpus)])

        args.append(image)

        if command:
            args.extend(command)

        logger.debug(f"Running: {' '.join(args)}")
        result = subprocess.run(args, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"Container run failed: {result.stderr}")

        return result.stdout.strip()

    def probe_arch(self, container_id: str) -> Optional[str]:
        """Detect and remember the user-space arch of a running container.

        Queries the ``ARCH`` header of the always-installed
        ``filesystem`` package — the same approach as the host's
        :func:`urpm.cli.helpers.package.system_arch`.  Caches the
        result so subsequent :meth:`exec` / :meth:`exec_stream`
        calls can prepend ``linux32`` automatically when the
        container's arch is 32-bit (otherwise ``uname -m`` returns
        the kernel's ``x86_64`` and confuses rpm-build, gcc, Python
        ``sysconfig``, …).

        Safe to call multiple times; subsequent calls re-probe and
        overwrite the cached value.

        Args:
            container_id: A running container's id.

        Returns:
            The detected arch (``'i686'``, ``'x86_64'``, …), or
            ``None`` if probing failed (``filesystem`` absent,
            container stopped, …).  When ``None``, no personality
            wrap is applied — same behaviour as before the helper
            existed.
        """
        result = subprocess.run(
            [self.cmd, 'exec', container_id,
             'rpm', '-q', '--qf', '%{ARCH}\n', 'filesystem'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        arch = (result.stdout or '').strip().splitlines()
        if not arch:
            return None
        arch = arch[0].strip()
        if not arch or arch == 'noarch':
            return None
        self._arch_by_cid[container_id] = arch
        return arch

    def _personality_wrap(self, container_id: str) -> List[str]:
        """Return the personality wrapper (e.g. ``['linux32']``) to
        prepend to commands sent into ``container_id``."""
        arch = self._arch_by_cid.get(container_id)
        if arch in self._ARCH_NEEDS_LINUX32:
            return ['linux32']
        return []

    def exec(
        self,
        container_id: str,
        command: List[str],
        workdir: str = None,
        env: dict = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute command in running container.

        Args:
            container_id: Container ID or name
            command: Command to execute
            workdir: Working directory
            env: Environment variables
            capture_output: Capture stdout/stderr

        Returns:
            CompletedProcess with return code and output
        """
        args = [self.cmd, 'exec']

        if workdir:
            args.extend(['-w', workdir])
        if env:
            for key, value in env.items():
                args.extend(['-e', f'{key}={value}'])

        args.append(container_id)
        args.extend(self._personality_wrap(container_id))
        args.extend(command)

        logger.debug(f"Exec: {' '.join(args)}")
        return subprocess.run(args, capture_output=capture_output, text=True)

    def _container_has_c_utf8(self, container_id: str) -> bool:
        """Return True when ``C.UTF-8`` is a valid locale in the container.

        Runs ``locale -a`` once per container id and caches the result.
        Failures (timeout, missing ``locale`` binary, non-zero exit)
        conservatively map to ``False`` so the caller falls back to
        the always-defined ``C`` locale instead of leaking warnings.
        """
        cached = self._has_c_utf8_by_cid.get(container_id)
        if cached is not None:
            return cached
        try:
            probe = subprocess.run(
                [self.cmd, 'exec', container_id, 'locale', '-a'],
                capture_output=True, text=True, timeout=10, check=False,
            )
            has_it = probe.returncode == 0 and any(
                line.strip() in ('C.UTF-8', 'C.utf8')
                for line in probe.stdout.splitlines()
            )
        except (subprocess.TimeoutExpired, OSError):
            has_it = False
        self._has_c_utf8_by_cid[container_id] = has_it
        return has_it

    def exec_stream(
        self,
        container_id: str,
        command: List[str],
        workdir: str = None,
    ) -> int:
        """Execute command with output streaming to terminal.

        Args:
            container_id: Container ID or name
            command: Command to execute
            workdir: Working directory

        Returns:
            Exit code
        """
        args = [self.cmd, 'exec']

        if workdir:
            args.extend(['-w', workdir])

        # Allocate a pseudo-TTY inside the container when our own stdout
        # is a terminal, so programs like ``urpm install`` inherit the
        # host terminal's width via TIOCGWINSZ instead of falling back
        # to the runtime's default (typically 80 columns) and drawing
        # truncated progress bars.  Skip ``-t`` when stdout is piped or
        # redirected -- ``podman exec -t`` errors out with "cannot
        # enable tty mode" if no real tty is attached.
        import sys, os as _os
        if sys.stdout.isatty():
            args.append('-t')

        # Propagate locale so the urpm inside the container prints
        # translated messages when a matching langpack is present.
        # ``podman exec`` scrubs environment by default; only variables
        # passed via ``-e`` reach the child.
        #
        # There are two orthogonal decisions here:
        #
        # 1. Which language(s) should the container's gettext consult?
        #    GNU gettext reads ``LANGUAGE`` as a colon-separated
        #    priority list (``"en_US:fr_FR:fr"`` means "try en_US,
        #    then fr_FR, then fr"), independently of ``LANG``.  We
        #    forward ``URPM_HOST_LANGUAGE`` (saved by ``cmd_mkimage``
        #    before it clamps its own env to C for the stderr
        #    silencer) if set, else the caller's current ``LANGUAGE``
        #    if it is not itself C/POSIX.  Only as a last resort do
        #    we derive a single code from ``LANG`` — that loses the
        #    fallback list a bilingual operator relies on.
        #
        # 2. What locale can the container's shells/perl actually
        #    setlocale() to?  Mageia 9 predates glibc 2.35 so
        #    ``C.UTF-8`` is not builtin, and the -minimal images ship
        #    no compiled locales — forcing ``LC_ALL=C.UTF-8`` there
        #    produces "Setting locale failed" spam from every perl
        #    spec-helper called during rpmbuild.  We probe
        #    ``locale -a`` inside the container (cached) and fall back
        #    to ``LC_ALL=C`` when ``C.UTF-8`` is missing.  ``LC_ALL=C``
        #    makes gettext ignore ``LANGUAGE`` too, so container-side
        #    urpm prints stay in English in that fallback path —
        #    acceptable in a build container.
        _host_language = (_os.environ.get('URPM_HOST_LANGUAGE')
                          or _os.environ.get('LANGUAGE', ''))
        if _host_language and _host_language not in ('C', 'POSIX'):
            language_to_send = _host_language
        else:
            _host_lang = (_os.environ.get('URPM_HOST_LANG')
                          or _os.environ.get('LANG', ''))
            language_to_send = None
            if _host_lang:
                _code = _host_lang.split('.')[0].split('_')[0]
                if _code and _code not in ('C', 'POSIX'):
                    language_to_send = _code

        if language_to_send:
            if self._container_has_c_utf8(container_id):
                args.extend(['-e', 'LC_ALL=C.UTF-8'])
                args.extend(['-e', f'LANGUAGE={language_to_send}'])
            else:
                args.extend(['-e', 'LC_ALL=C'])

        args.append(container_id)
        args.extend(self._personality_wrap(container_id))
        args.extend(command)

        logger.debug(f"Exec (streaming): {' '.join(args)}")
        result = subprocess.run(args)
        return result.returncode

    def cp(self, src: str, dst: str) -> bool:
        """Copy files to/from container.

        Args:
            src: Source path (container_id:/path or /local/path)
            dst: Destination path (container_id:/path or /local/path)

        Returns:
            True if successful
        """
        logger.debug(f"Copy: {src} -> {dst}")
        result = subprocess.run(
            [self.cmd, 'cp', src, dst],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logger.warning(f"Copy failed: {result.stderr}")
        return result.returncode == 0

    def rm(self, container_id: str, force: bool = True) -> bool:
        """Remove container.

        Args:
            container_id: Container ID or name
            force: Force removal even if running

        Returns:
            True if successful
        """
        args = [self.cmd, 'rm']
        if force:
            args.append('-f')
        args.append(container_id)

        logger.debug(f"Remove: {container_id}")
        result = subprocess.run(args, capture_output=True, text=True)
        # Drop the cached arch for this cid so the dict does not grow
        # unbounded in long-running processes (urpmd, the build loop
        # spinning many containers).
        self._arch_by_cid.pop(container_id, None)
        return result.returncode == 0

    def stop(self, container_id: str, timeout: int = 10) -> bool:
        """Stop a running container.

        Args:
            container_id: Container ID or name
            timeout: Seconds to wait before killing

        Returns:
            True if successful
        """
        args = [self.cmd, 'stop', '-t', str(timeout), container_id]
        result = subprocess.run(args, capture_output=True, text=True)
        return result.returncode == 0

    def ps(self, all_containers: bool = False, filter_name: str = None) -> List[dict]:
        """List containers.

        Args:
            all_containers: Include stopped containers
            filter_name: Filter by name pattern

        Returns:
            List of container info dicts
        """
        args = [
            self.cmd, 'ps',
            '--format', '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'
        ]
        if all_containers:
            args.append('-a')
        if filter_name:
            args.extend(['--filter', f'name={filter_name}'])

        result = subprocess.run(args, capture_output=True, text=True)
        containers = []

        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('\t')
                containers.append({
                    'id': parts[0],
                    'name': parts[1] if len(parts) > 1 else '',
                    'image': parts[2] if len(parts) > 2 else '',
                    'status': parts[3] if len(parts) > 3 else '',
                })

        return containers

    def images(self, filter_name: str = None) -> List[dict]:
        """List images.

        Args:
            filter_name: Filter by reference pattern

        Returns:
            List of image info dicts
        """
        args = [
            self.cmd, 'images',
            '--format', '{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}'
        ]
        if filter_name:
            args.extend(['--filter', f'reference={filter_name}'])

        result = subprocess.run(args, capture_output=True, text=True)
        images = []

        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('\t')
                images.append({
                    'tag': parts[0],
                    'id': parts[1] if len(parts) > 1 else '',
                    'size': parts[2] if len(parts) > 2 else '',
                })

        return images

    def image_exists(self, tag: str) -> bool:
        """Check if an image exists locally.

        Args:
            tag: Image tag to check

        Returns:
            True if image exists
        """
        result = subprocess.run(
            [self.cmd, 'image', 'inspect', tag],
            capture_output=True,
            text=True
        )
        return result.returncode == 0

    def import_tar(self, tar_path: str, tag: str) -> bool:
        """Import tarball as image.

        Args:
            tar_path: Path to tarball
            tag: Tag for the new image

        Returns:
            True if successful
        """
        logger.info(f"Importing {tar_path} as {tag}")
        with open(tar_path, 'rb') as f:
            result = subprocess.run(
                [self.cmd, 'import', '-', tag],
                stdin=f,
                capture_output=True,
                text=True
            )
        if result.returncode != 0:
            logger.error(f"Import failed: {result.stderr}")
        return result.returncode == 0

    def import_from_dir(self, directory: str, tag: str, tmpdir: str = None, use_userns: bool = False) -> bool:
        """Create image from directory (tar + import).

        Args:
            directory: Directory to import
            tag: Tag for the new image
            tmpdir: Temporary directory for podman (avoids /tmp space issues)
            use_userns: Run under 'podman unshare' (user namespace) for
                proper UID/GID mapping.  Required when the chroot was
                built under a user namespace so file ownership matches.

        Returns:
            True if successful
        """
        logger.info(f"Creating image {tag} from {directory}")

        if use_userns and self.runtime.name == 'podman':
            # Two processes bridged by a plain OS pipe, only ONE of
            # them wrapped in ``podman unshare`` :
            #
            #   podman unshare tar -C <dir> -c .   |   podman import - <tag>
            #   └── UID mapping active (root=0)        └── operator context,
            #       so chroot files tar as root:root       XDG_/HOME point at
            #       instead of the caller's real UID       the rootless storage
            #                                              podman actually uses
            #
            # Wrapping ``podman import`` inside the same ``unshare``
            # subshell (the previous form) put it under nested
            # user-namespace territory : image storage lookup
            # silently landed on whatever ``$HOME/.local/share/
            # containers`` resolved to inside the userns, so the
            # tag got registered in an ephemeral store the caller
            # could not see and Phase 2's ``podman run`` failed
            # with "did not resolve to an alias".
            env_tar = os.environ.copy()
            env_tar['TMPDIR'] = tmpdir if tmpdir else str(Path(directory).parent)
            env_import = os.environ.copy()
            env_import['TMPDIR'] = env_tar['TMPDIR']

            tar_proc = subprocess.Popen(
                ['podman', 'unshare', 'tar', '-C', directory, '-c', '.'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_tar,
            )
            imp_proc = subprocess.Popen(
                [self.cmd, 'import', '-', tag],
                stdin=tar_proc.stdout, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env_import,
            )
            # Close our copy so tar sees EPIPE if import dies.
            tar_proc.stdout.close()
            imp_out, imp_err = imp_proc.communicate()
            tar_proc.wait()
            tar_err = tar_proc.stderr.read().decode(errors='replace')
            tar_proc.stderr.close()

            if tar_proc.returncode != 0:
                logger.error(f"tar under unshare failed: {tar_err}")
                if tar_err:
                    print(tar_err)
                return False
            if imp_proc.returncode != 0:
                err = imp_err.decode(errors='replace')
                logger.error(f"podman import failed: {err}")
                print(err)
                return False
            return True

        # Standard import without unshare — running as root, no
        # userns wrapper.  Keep the operator's env (this path is
        # already privileged, hermetic scrubbing would only strip
        # legitimate proxy / cache config).  ``TMPDIR`` still needs
        # to be pinned so podman's own scratch space doesn't fill
        # ``/tmp``.
        env = os.environ.copy()
        env['TMPDIR'] = tmpdir if tmpdir else str(Path(directory).parent)
        # tar -C dir -c . | docker/podman import - tag
        # Let tar stderr go to terminal so user sees warnings
        tar_proc = subprocess.Popen(
            ['tar', '-C', directory, '-c', '.'],
            stdout=subprocess.PIPE
        )
        import_proc = subprocess.Popen(
            [self.cmd, 'import', '-', tag],
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )

        # Allow tar_proc to receive SIGPIPE if import_proc exits early
        tar_proc.stdout.close()

        import_stdout, import_stderr = import_proc.communicate()
        tar_proc.wait()

        if import_proc.returncode != 0:
            logger.error(f"Import failed: {import_stderr}")
            return False

        return True

    def commit(self, container_id: str, tag: str,
               squash: bool = False, tmpdir: str = None) -> bool:
        """Commit a container's changes as a new image.

        Args:
            container_id: Running or stopped container ID/name
            tag: Image tag for the committed image
            squash: On podman, ask ``commit`` to fold the new diff into a
                single layer via ``--squash``.  This keeps the number of
                layers from growing on every ``image update`` cycle.  To
                truly flatten the whole history (base + N updates) into
                one layer, use ``export | import`` -- but that loses
                image metadata (entrypoint, labels), so we stay with
                ``--squash`` here.  Ignored on docker (``docker commit``
                has no equivalent).
            tmpdir: Directory podman should use as ``TMPDIR`` while
                staging the commit's blob layers.  When ``None``,
                podman falls through to the system default (``/tmp``),
                which overflows small tmpfs mounts on commits that copy
                multi-hundred-MB layers.  Symmetric with
                :meth:`import_from_dir`, which has always accepted this
                knob.

        Returns:
            True if successful
        """
        cmd = [self.cmd, 'commit']
        if squash and self.runtime.name == 'podman':
            cmd.append('--squash')
        cmd += [container_id, tag]
        env = os.environ.copy()
        if tmpdir:
            env['TMPDIR'] = tmpdir
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            logger.error(f"Commit failed: {result.stderr.strip()}")
        return result.returncode == 0

    def rmi(self, image: str, force: bool = False) -> bool:
        """Remove an image.

        Args:
            image: Image tag or ID
            force: Force removal

        Returns:
            True if successful
        """
        args = [self.cmd, 'rmi']
        if force:
            args.append('-f')
        args.append(image)

        result = subprocess.run(args, capture_output=True, text=True)
        return result.returncode == 0
