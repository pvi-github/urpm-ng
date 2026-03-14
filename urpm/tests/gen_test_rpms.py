#!/usr/bin/python3
from pathlib import Path
import shutil
from subprocess import run
import os, sys

# generate packages for testing urpm

def rpmbuild(spec_file:Path, medium_name:str=None):
    cmd = ["rpmbuild"]
    current_dir = Path.cwd()
    if "i586" in spec_file.name:
        cmd.append("--target")
        cmd.append("i686")
    elif '86_64' in spec_file.name:
        cmd.append("--target")
        cmd.append("x86_64")
    cmd += ["--define", "__os_install_post %nil"]
    # is rpm_version needed
    cmd += ["--quiet", "--define", f'_topdir {current_dir}/tmp', '--define', f"_tmppath {current_dir}/tmp", "-bb", "--clean", "--nodeps", str(spec_file.absolute())]
    print(" ".join(cmd))
    p = run(cmd)
    name = spec_file.stem
    if not medium_name:
        medium_name = name
    (current_dir /"media" / medium_name).mkdir(parents=True, exist_ok=True)
    run("find tmp/RPMS -type f -name '*.rpm' | xargs -I{} mv {} media/" + medium_name + "/", shell=True)
    return medium_name
    
def rpmbuild_srpm(spec_file:Path):
    cmd = ["rpmbuild"]
    current_dir = Path.cwd()
    # is rpm_version needed
    cmd += ["--quiet", "--define", f'_topdir {current_dir}/tmp', '--define', f"_tmppath {current_dir}/tmp", "-bs", "--clean", "--nodeps", "--build-in-place", str(spec_file.absolute())]
    print(" ".join(cmd))
    p = run(cmd)
    name = spec_file.stem
    medium_name = Path("SRPMS-" + name)
    (current_dir /"media" / medium_name).mkdir(parents=True, exist_ok=True)
    os.system(f"mv tmp/SRPMS/*.rpm media/{medium_name}")
    return medium_name.name
    
def main():
    #TODO Test if genhdlist2 is installed
    def genhdlist(dir_test:Path):
        print(dir_test)
        run(["genhdlist2", "--xml-info", "media/" + dir_test])

    if Path.cwd().name != "tests":
        print("Must be run from 'tests' directory")
        sys.exit(0)
    # cleaning previous tests
    for to_delete in ("media", "tmp"):
        shutil.rmtree(Path(to_delete), ignore_errors=True)

    base = Path.cwd()
    for p in ( "BUILD", "RPMS/noarch", "SRPMS"):
        (Path("tmp") / p).mkdir(parents=True, exist_ok=True)

    for spec_dir in base.glob("data/SPECS/*"):
        if spec_dir.is_dir():
            medium_name = Path(spec_dir).name
            for spec_file in spec_dir.glob("*"):
                name = rpmbuild(spec_file, medium_name=medium_name)
                genhdlist(name)

    for spec_file in base.glob("data/SPECS/*.spec"):
        if "rpm-query-in-scriptlet" in spec_file.name:
            continue
        name = rpmbuild(spec_file)
        if name == "various":
            os.system(f"cp -r media/{name} media/{name}_nohdlist");
            os.system(f"cp -r media/{name} media/{name}_no_subdir");
            os.system(f"genhdlist2 media/{name}_no_subdir");
            Path(f"media/{name} nohdlist").symlink_to(f"{name}_nohdlist")
        genhdlist(name)

    for spec_file in base.glob("data/SPECS/srpm*.spec"):
        name = rpmbuild_srpm(spec_file)
        genhdlist(name)

    name = 'rpm-i586-to-i686'
    os.system(f"cp -r data/{name} media")
    genhdlist(name)

    (Path("media") / 'reconfig').mkdir(exist_ok=True)
    os.system("cp -r data/reconfig.urpmi media/reconfig")

    (Path("media") / 'media_info').mkdir(exist_ok=True)
    os.system("cp -r data/media.cfg media/media_info")
    os.system('gendistrib -s .')


if __name__ == '__main__':
    main()
