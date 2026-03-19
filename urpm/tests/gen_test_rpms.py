#!/usr/bin/python3
from pathlib import Path
import shutil
from subprocess import run
import os, sys

# generate packages for testing urpm

def rpmbuild(spec_file: Path, base_dir: Path, medium_name:str=None):
    cmd = ["rpmbuild"]
    if "i586" in spec_file.name:
        cmd.append("--target")
        cmd.append("i686")
    elif '86_64' in spec_file.name:
        cmd.append("--target")
        cmd.append("x86_64")
    cmd += ["--define", "__os_install_post %nil"]
    # is rpm_version needed
    cmd += ["--quiet", "--define", f'_topdir {base_dir}/tmp', '--define', f"_tmppath {base_dir}/tmp", "-bb", "--clean", "--nodeps", str(spec_file.absolute())]
    p = run(cmd)
    name = spec_file.stem
    if not medium_name:
        medium_name = name
    (base_dir /"media" / medium_name).mkdir(parents=True, exist_ok=True)
    run("find tmp/RPMS -type f -name '*.rpm' | xargs -I{} mv {} media/" + medium_name + "/", shell=True, cwd=base_dir)
    return medium_name

def rpmbuild_srpm(spec_file:Path, base_dir: Path):
    cmd = ["rpmbuild"]
    # is rpm_version needed
    cmd += ["--quiet", "--define", f'_topdir {base_dir}/tmp', '--define', f"_tmppath {base_dir}/tmp", "-bs", "--clean", "--nodeps", "--build-in-place", str(spec_file.absolute())]
    p = run(cmd)
    name = spec_file.stem
    medium_name = Path("SRPMS-" + name)
    (base_dir /"media" / medium_name).mkdir(parents=True, exist_ok=True)
    run(f"mv tmp/SRPMS/*.rpm media/{medium_name}", shell=True, cwd=base_dir)
    return medium_name.name

def main():
    #TODO Test if genhdlist2 is installed
    def genhdlist(dir_test:Path):
        # ret = run(["genhdlist2", "--xml-info", "media/" + dir_test], cwd=base_dir)
        ret = run(["python3", "upanier.py", "--xml-info", "media/" + dir_test], cwd=base_dir)
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
    print(base_dir.absolute())
    # cleaning previous tests
    for to_delete in ("media", "tmp"):
        shutil.rmtree(base_dir / to_delete, ignore_errors=True)

    for p in ( "BUILD", "RPMS/noarch", "SRPMS"):
        (base_dir / "tmp" / p).mkdir(parents=True, exist_ok=True)

    for spec_dir in base_dir.glob("data/SPECS/*"):
        if spec_dir.is_dir():
            medium_name = Path(spec_dir).name
            for spec_file in spec_dir.glob("*"):
                name = rpmbuild(spec_file, base_dir, medium_name=medium_name)
                genhdlist(name)

    for spec_file in base_dir.glob("data/SPECS/*.spec"):
        if "rpm-query-in-scriptlet" in spec_file.name:
            continue
        name = rpmbuild(spec_file, base_dir)
        if name == "various":
            shutil.copytree(base_dir / f"media/{name}", base_dir / f"media/{name}_nohdlist")
            shutil.copytree(base_dir / f"media/{name}", base_dir / f"media/{name}_no_subdir")
            genhdlist(f"{name}_no_subdir")
            (base_dir / f"media/{name} nohdlist").symlink_to( base_dir / f"{name}_nohdlist")
        genhdlist(name)

    for spec_file in base_dir.glob("data/SPECS/srpm*.spec"):
        name = rpmbuild_srpm(spec_file, base_dir)
        genhdlist(name)

    name = 'rpm-i586-to-i686'
    run( ["cp", "-r", f"data/{name}", "media"], cwd=base_dir)
    genhdlist(name)

    (base_dir / 'media/reconfig').mkdir(exist_ok=True)
    run( ["cp", "-r", "data/reconfig.urpmi", "media/reconfig"], cwd=base_dir)

    (base_dir / 'media/media_info').mkdir(exist_ok=True)
    run( ["cp", "-r", "data/media.cfg", "media/media_info"], cwd=base_dir)
    run([(base_dir / 'gendistrib').absolute(),'-s', base_dir.absolute()])


if __name__ == '__main__':
    main()
