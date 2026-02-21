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
from typing import List, Tuple

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

    def __init__(self, runtime: ContainerRuntime):
        self.runtime = runtime
        self.cmd = runtime.path

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

        args.append(image)

        if command:
            args.extend(command)

        logger.debug(f"Running: {' '.join(args)}")
        result = subprocess.run(args, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"Container run failed: {result.stderr}")

        return result.stdout.strip()

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
        args.extend(command)

        logger.debug(f"Exec: {' '.join(args)}")
        return subprocess.run(args, capture_output=capture_output, text=True)

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

        args.append(container_id)
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

    def import_from_dir(self, directory: str, tag: str, tmpdir: str = None, use_unshare: bool = False) -> bool:
        """Create image from directory (tar + import).

        Args:
            directory: Directory to import
            tag: Tag for the new image
            tmpdir: Temporary directory for podman (avoids /tmp space issues)
            use_unshare: Run under 'podman unshare' for UID/GID mapping

        Returns:
            True if successful
        """
        logger.info(f"Creating image {tag} from {directory}")

        # Set TMPDIR for podman to avoid /tmp quota issues
        # Use parent of source directory if not specified
        env = os.environ.copy()
        if tmpdir:
            env['TMPDIR'] = tmpdir
        else:
            env['TMPDIR'] = str(Path(directory).parent)

        if use_unshare and self.runtime.name == 'podman':
            # Run tar + import under podman unshare for proper UID/GID mapping
            # This is needed when the chroot was built under podman unshare
            cmd = f'tar -C {directory} -c . | {self.cmd} import - {tag}'
            result = subprocess.run(
                ['podman', 'unshare', 'sh', '-c', cmd],
                capture_output=True,
                text=True,
                env=env
            )
            if result.returncode != 0:
                logger.error(f"Import failed: {result.stderr}")
                print(result.stderr)
                return False
            return True

        # Standard import without unshare
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
