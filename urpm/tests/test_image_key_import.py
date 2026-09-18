"""``urpm image make --import-key`` must not ask inside the container.

``media add --import-key`` prints the key's id, fingerprint and uid,
then asks for confirmation.  Run through ``podman exec`` that question
reaches a pseudo-tty with no stdin behind it: it appeared, nobody could
answer, and the media add aborted on end-of-file, failing the image
build.

The consent is ``--import-key`` on the host command line.  The second
question is the defect, so ``--auto`` travels with the flag, and only
with it: without a key to import, this command asks nothing and adding
``--auto`` would be a blanket "yes" nobody asked for.
"""

from __future__ import annotations

from urpm.cli.commands.build import media_add_command


class TestMediaAddCommand:

    def test_import_key_comes_with_auto(self):
        command = media_add_command("https://s/repo", "Extra", True)
        assert "--import-key" in command
        assert "--auto" in command

    def test_no_key_no_auto(self):
        """Nothing to confirm, so nothing to pre-answer."""
        command = media_add_command("https://s/repo", "Extra", False)
        assert "--import-key" not in command
        assert "--auto" not in command

    def test_the_verb_and_url_come_first(self):
        """The argv shape itself is asserted in
        ``test_mkimage_addmedia.py``, which owns that regression; here
        we only pin the prefix the key options are appended to."""
        command = media_add_command("https://s/repo", "Extra", True)
        assert command[:5] == ['urpm', 'media', 'add', '--custom',
                               'https://s/repo']

    def test_a_name_with_spaces_stays_one_argument(self):
        """The argv is handed to the container as a list, never a shell
        string: a display name with spaces must not split."""
        command = media_add_command("https://s/repo", "Core Release", True)
        assert "Core Release" in command
        assert command.count("Core Release") == 2  # --name and --shortname
