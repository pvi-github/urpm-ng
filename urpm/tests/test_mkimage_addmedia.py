"""`--addmedia` on `urpm image make`, for remote and local media alike.

Two defects, reported together on the same command line.

The call built for the container was ``urpm media add --custom NAME
NAME URL``, written against a signature that has since changed: `media
add` takes the URL as its only positional, the display and short names
being options.  Three positionals for one, so argparse refused before
anything else could happen — identically for `http://` and `file://`.

And a `file://` medium names a directory on the *host*.  The container
saw nothing of it, so fixing the syntax alone would have moved the
failure from the parser to the sync, blaming a path that exists.

Mounting at the same path on both sides is what keeps the mounted
ISO/DVD case free: once mounted, `/run/media/<user>/Mageia-10-x86_64/`
is a directory like any other, and needs no code of its own here.  A
rewritten internal mount point would work today and would have to be
undone then.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest import mock

import pytest

from urpm.cli.commands import build as build_cmd
from urpm.cli.commands.build import _local_media_mounts
from urpm.core.container import Container


class TestTheMediaAddCommandLine:

    def test_the_url_is_the_only_positional(self):
        """What `urpm media add` actually accepts."""
        from urpm.cli.main import create_parser
        import argparse

        parser = create_parser()
        media = next(a for a in parser._actions
                     if isinstance(a, argparse._SubParsersAction)).choices["media"]
        add = next(a for a in media._actions
                   if isinstance(a, argparse._SubParsersAction)).choices["add"]
        positionals = [a.dest for a in add._actions
                       if not a.option_strings and a.dest != "help"]
        assert positionals == ["url"], positionals

    def test_the_builder_passes_the_name_as_an_option(self):
        """The regression itself: names passed positionally left
        argparse with three positionals for one."""
        source = inspect.getsource(build_cmd._phase2_container_promote)
        line = next(l for l in source.splitlines() if "'media', 'add'" in l
                    or '"media", "add"' in l)
        assert "--name" in source[source.index(line):source.index(line) + 300]
        assert "'--custom', name, name" not in source

    def test_the_built_command_parses(self):
        """Fed to the real parser, it must resolve."""
        from urpm.cli.main import create_parser

        argv = ["media", "add", "--custom",
                "https://ftp.blogdrake.org/mageia/mageia10/free/x86_64/",
                "--name", "BDK-Free", "--shortname", "BDK-Free"]
        args = create_parser().parse_args(argv)
        assert args.url.endswith("/free/x86_64/")
        assert args.name == "BDK-Free"


class TestLocalMediaReachTheContainer:

    def test_a_remote_medium_mounts_nothing(self):
        assert _local_media_mounts(
            [("BDK", "https://ftp.blogdrake.org/mageia/")]) == []

    def test_a_local_directory_is_mounted_at_the_same_path(self, tmp_path):
        mounts = _local_media_mounts([("BDK", f"file://{tmp_path}")])
        assert mounts == [(str(tmp_path), str(tmp_path), "ro")], (
            "same path both sides keeps the URL usable unchanged"
        )

    def test_it_is_read_only(self, tmp_path):
        """An image build has no business writing to the operator's
        mirror, and an ISO mount refuses it anyway."""
        assert _local_media_mounts(
            [("BDK", f"file://{tmp_path}")])[0][2] == "ro"

    def test_a_missing_path_is_refused_by_name(self):
        """Better than booting a container that cannot succeed."""
        with pytest.raises(FileNotFoundError) as excinfo:
            _local_media_mounts([("BDK", "file:///nowhere/mirror")])
        assert "/nowhere/mirror" in str(excinfo.value)
        assert "BDK" in str(excinfo.value)

    def test_percent_escapes_are_decoded(self, tmp_path):
        """A URL escapes what a path does not."""
        spaced = tmp_path / "mon dossier"
        spaced.mkdir()
        url = "file://" + str(spaced).replace(" ", "%20")
        assert _local_media_mounts([("X", url)])[0][0] == str(spaced)

    def test_a_mounted_iso_needs_no_special_case(self, tmp_path):
        """The whole point of mounting at the same path: once mounted,
        an installer DVD is a directory.  This is the shape a
        bandwidth-constrained user will pass, and nothing here knows
        about it."""
        iso = tmp_path / "run" / "media" / "qa" / "Mageia-10-x86_64"
        iso.mkdir(parents=True)
        mounts = _local_media_mounts([("Core Release (Installer)",
                                       f"file://{iso}")])
        assert mounts == [(str(iso), str(iso), "ro")]

    def test_mixed_media_mount_only_the_local_ones(self, tmp_path):
        mounts = _local_media_mounts([
            ("Remote", "https://mirror.example/mageia/"),
            ("Local", f"file://{tmp_path}"),
        ])
        assert mounts == [(str(tmp_path), str(tmp_path), "ro")]


class TestTheEngineCommandLine:

    def _argv(self, volumes):
        c = Container.__new__(Container)
        c.cmd = "podman"
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="cid\n",
                                         stderr="")
            c.run("img", ["sleep"], detach=True, volumes=volumes)
            return run.call_args[0][0]

    def test_a_read_only_mount_reaches_podman(self):
        argv = self._argv([("/srv/mirror", "/srv/mirror", "ro")])
        assert "-v" in argv
        assert "/srv/mirror:/srv/mirror:ro" in argv

    def test_a_plain_pair_still_works(self):
        """The two-element form predates this and has other callers in
        principle; it must keep meaning read-write."""
        argv = self._argv([("/a", "/b")])
        assert "/a:/b" in argv
