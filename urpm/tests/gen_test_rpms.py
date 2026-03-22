#!/usr/bin/python3
from pathlib import Path
import shutil
from subprocess import run
import os, sys

# generate packages for testing urpm

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

    if Path.cwd().name == "tests":
        base_dir = Path.cwd()
    elif Path.cwd().name == "urpm":
        base_dir = Path.cwd() / "tests"
    elif Path.cwd().name == "urpm-ng":
        base_dir = Path.cwd() / "urpm/tests"
    else:
        print("Must be run from 'urpm/tests' directory")
        sys.exit(0)

    # Look for gendistrib executable (optional, only needed for media_info tests)
    gendistrib_cmd = ''
    for path in os.environ.get('PATH', '').split(':') + [base_dir]:
        if os.path.isfile(os.path.join(path, 'gendistrib')):
            gendistrib_cmd = os.path.join(path, 'gendistrib')
            break
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


if __name__ == '__main__':
    main()
