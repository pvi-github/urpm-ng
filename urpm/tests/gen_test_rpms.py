#!/usr/bin/python3
from pathlib import Path
import hashlib
import shutil
from subprocess import run
import os, sys

# generate packages for testing urpm

#: Written into media/ once generation completes, holding the digest of
#: the inputs it was built from.  Its absence means the media predate
#: this mechanism and cannot be vouched for.
STAMP_NAME = ".generated-from"


def find_gendistrib(base_dir: Path) -> str:
    """Locate ``gendistrib`` (package ``rpmtools``), or return ``''``.

    It builds the distribution-level ``media_info/`` from ``media.cfg``,
    which ``genhdlist2`` does not do -- that one works per medium.  It
    is optional : without it the ``media_info`` medium is not built and
    the tests needing it skip.
    """
    for path in os.environ.get('PATH', '').split(':') + [str(base_dir)]:
        candidate = os.path.join(path, 'gendistrib')
        if os.path.isfile(candidate):
            return candidate
    return ''


def input_digest(base_dir: Path) -> str:
    """Digest every input this generator reads.

    Covers ``data/`` (specs and the trees copied verbatim into media)
    plus this module, since new media come from code as much as from
    spec files -- ``rpm-i586-to-i686``, ``reconfig`` and ``media_info``
    are all created by lines below rather than by a spec.

    Hashes *content*, not mtimes : git sets mtimes to checkout time, so
    a fresh clone of unchanged data would otherwise look modified while
    a stale media/ touched by a test run would look current.  Both
    mistakes were observed before this existed.
    """
    h = hashlib.sha256()
    for path in sorted((base_dir / "data").rglob("*")):
        if not path.is_file():
            continue
        # Name and content both, so swapping two specs is not a no-op.
        h.update(str(path.relative_to(base_dir)).encode())
        h.update(path.read_bytes())
    # The generator is an input in its own right, under a fixed label
    # rather than a path : it does not have to live under *base_dir*.
    h.update(b"gen_test_rpms.py")
    h.update(Path(__file__).resolve().read_bytes())
    # Whether gendistrib was reachable belongs in the digest as much as
    # the specs do : without it the media_info medium is simply not
    # built, so two runs over identical data yield different media.
    # Installing rpmtools afterwards must invalidate the stamp, or the
    # missing medium would stay missing while looking current.
    h.update(b"gendistrib:" + (b"yes" if find_gendistrib(base_dir) else b"no"))
    return h.hexdigest()


def media_are_current(base_dir: Path) -> bool:
    """Whether ``media/`` was generated from the inputs present now."""
    stamp = base_dir / "media" / STAMP_NAME
    try:
        return stamp.read_text().strip() == input_digest(base_dir)
    except OSError:
        return False

def rpmbuild(spec_file: Path, base_dir: Path, medium_name: str = None) -> str | None:
    """Build a binary RPM from a spec file and move it to media/<medium_name>.

    Returns the medium name on success, None on failure.
    """
    cmd = ["rpmbuild"]
    if "i586" in spec_file.name:
        cmd += ["--target", "i686"]
    elif '86_64' in spec_file.name:
        cmd += ["--target", "x86_64"]
    cmd += ["--define", "__os_install_post %nil"]
    cmd += ["--quiet", "--define", f'_topdir {base_dir}/tmp',
            '--define', f"_tmppath {base_dir}/tmp",
            "-bb", "--clean", "--nodeps", str(spec_file.absolute())]
    p = run(cmd)
    name = spec_file.stem
    if not medium_name:
        medium_name = name
    if p.returncode != 0:
        print(f"Warning: rpmbuild failed for {spec_file.name} (rc={p.returncode})")
        return None
    (base_dir / "media" / medium_name).mkdir(parents=True, exist_ok=True)
    run("find tmp/RPMS -type f -name '*.rpm' | xargs -I{} mv {} media/"
        + medium_name + "/", shell=True, cwd=base_dir)
    return medium_name

def rpmbuild_srpm(spec_file: Path, base_dir: Path) -> str | None:
    """Build a source RPM from a spec file and move it to media/SRPMS-<name>.

    Returns the medium name on success, None on failure.
    """
    cmd = ["rpmbuild"]
    cmd += ["--quiet", "--define", f'_topdir {base_dir}/tmp',
            '--define', f"_tmppath {base_dir}/tmp",
            "-bs", "--clean", "--nodeps", "--build-in-place",
            str(spec_file.absolute())]
    p = run(cmd)
    name = spec_file.stem
    if p.returncode != 0:
        print(f"Warning: rpmbuild -bs failed for {spec_file.name} (rc={p.returncode})")
        return None
    medium_name = Path("SRPMS-" + name)
    (base_dir / "media" / medium_name).mkdir(parents=True, exist_ok=True)
    run(f"mv tmp/SRPMS/*.rpm media/{medium_name}", shell=True, cwd=base_dir)
    return medium_name.name

def main():
    """Generate all test media (RPMs + synthesis) from spec files in data/SPECS/."""

    def genhdlist(dir_test: str):
        """Generate synthesis/hdlist for a media directory."""
        ret = run(genmedia_cmd + ["--xml-info", "media/" + dir_test], cwd=base_dir)
        if ret.returncode != 0:
            print(ret.stderr)
            sys.exit(1)

    # Anchor on __file__: this script lives in urpm/tests/, so its
    # parent IS the test data directory regardless of where the
    # caller invoked us from (repo root, worktree, urpm/, tests/).
    base_dir = Path(__file__).resolve().parent

    gendistrib_cmd = find_gendistrib(base_dir)
    if gendistrib_cmd == '':
        print("Warning: gendistrib not found (install rpmtools). "
              "media_info tests will be skipped.")

    # Look for upanier
    genmedia_cmd = []
    for path in os.environ.get('PATH', '').split(':') + [base_dir]:
        if os.path.isfile(os.path.join(path, 'upanier.py')):
            genmedia_cmd = ["/usr/bin/python3", os.path.join(path, 'upanier.py')]
            break
    if genmedia_cmd == []:
        # try fallback with genhdlist2
        for path in os.environ.get('PATH', '').split(':') + [base_dir]:
            if os.path.isfile(os.path.join(path, 'genhdlist2')):
                genmedia_cmd = [os.path.join(path, 'genhdlist2')]
                break
    if genmedia_cmd == []:
        print("Executable for generating media data is missing, install upanier or genhdlist2")
        sys.exit(1)

    # cleaning previous tests
    for to_delete in ("media", "tmp"):
        shutil.rmtree(base_dir / to_delete, ignore_errors=True)

    for p in ( "BUILD", "RPMS/noarch", "SRPMS"):
        (base_dir / "tmp" / p).mkdir(parents=True, exist_ok=True)

    # Build specs grouped in sub-directories (one medium per directory)
    for spec_dir in sorted(base_dir.glob("data/SPECS/*")):
        if spec_dir.is_dir():
            medium_name = spec_dir.name
            ok = False
            for spec_file in sorted(spec_dir.glob("*")):
                if rpmbuild(spec_file, base_dir, medium_name=medium_name) is not None:
                    ok = True
            if ok:
                genhdlist(medium_name)

    # Build standalone specs (one medium per spec)
    for spec_file in sorted(base_dir.glob("data/SPECS/*.spec")):
        if "rpm-query-in-scriptlet" in spec_file.name:
            continue
        name = rpmbuild(spec_file, base_dir)
        if name is None:
            continue
        if name == "various":
            shutil.copytree(base_dir / f"media/{name}", base_dir / f"media/{name}_nohdlist")
            shutil.copytree(base_dir / f"media/{name}", base_dir / f"media/{name}_no_subdir")
            genhdlist(f"{name}_no_subdir")
            try:
                (base_dir / f"media/{name} nohdlist").symlink_to(base_dir / f"{name}_nohdlist")
            except OSError:
                # Symlinks not supported (e.g. vboxsf), use a copy instead
                shutil.copytree(base_dir / f"media/{name}_nohdlist",
                                base_dir / f"media/{name} nohdlist")
        genhdlist(name)

    for spec_file in sorted(base_dir.glob("data/SPECS/srpm*.spec")):
        name = rpmbuild_srpm(spec_file, base_dir)
        if name is not None:
            genhdlist(name)

    name = 'rpm-i586-to-i686'
    run( ["cp", "-r", f"data/{name}", "media"], cwd=base_dir, check=True)
    genhdlist(name)

    (base_dir / 'media/reconfig').mkdir(exist_ok=True)
    run( ["cp", "-r", "data/reconfig.urpmi", "media/reconfig"], cwd=base_dir, check=True)

    if gendistrib_cmd:
        (base_dir / 'media/media_info').mkdir(exist_ok=True)
        run( ["cp", "-r", "data/media.cfg", "media/media_info"], cwd=base_dir, check=True)
        run([gendistrib_cmd,'-s', base_dir.absolute()], check=True)

    # Last, so a run that dies partway leaves no stamp and the next
    # caller regenerates rather than trusting half a media set.
    (base_dir / "media" / STAMP_NAME).write_text(input_digest(base_dir) + "\n")


if __name__ == '__main__':
    main()
