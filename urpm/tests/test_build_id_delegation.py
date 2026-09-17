"""``urpm build`` checks the id delegation before it starts.

A build in a rootless container can only name the uids its user
namespace translates.  With the 65536 ids shadow-utils delegates by
default, a ``%check`` that goes further gets EINVAL from the kernel,
and the build dies deep inside a test suite on an error that says
nothing about identifier delegation.  python's ``test_posix`` chowns
to 2**31 on purpose and hit exactly that.

The check is a warning and a question, never a refusal: most specs
never approach the limit, and the packager is the one who knows
whether this one does.
"""

from __future__ import annotations

import pytest

from urpm.cli.commands.build import _confirm_id_delegation
from urpm.core import userns
from urpm.core.userns import (
    BUILD_RANGE_MIN,
    RECOMMENDED_RANGE,
    UserNamespaceCapability,
    format_build_range_warning,
)

NARROW = 65536


def _capability(count: int = NARROW, is_root: bool = False,
                gid_count: int | None = None,
                entries=((524288, NARROW),)) -> UserNamespaceCapability:
    gid_count = count if gid_count is None else gid_count
    return UserNamespaceCapability(
        username="packager",
        subuid_count=count,
        subgid_count=gid_count,
        is_root=is_root,
        subuid_entries=entries,
        subgid_entries=entries,
    )


class TestWidthVerdict:

    def test_default_delegation_is_too_narrow(self):
        assert not _capability().wide_enough_for_build

    def test_widened_delegation_passes(self):
        wide = _capability(RECOMMENDED_RANGE,
                           entries=((524288, RECOMMENDED_RANGE),))
        assert wide.wide_enough_for_build

    def test_the_threshold_is_the_uid_the_build_must_reach(self):
        """2**31 exactly: podman maps container uids 1..N, so uid 2**31
        is reachable when N reaches it and not one id sooner."""
        assert not _capability(BUILD_RANGE_MIN - 1).wide_enough_for_build
        assert _capability(BUILD_RANGE_MIN).wide_enough_for_build

    def test_a_chown_names_both_so_the_narrower_file_decides(self):
        """A wide /etc/subuid with a default /etc/subgid still fails:
        ``chown uid:gid`` needs both translated."""
        lopsided = _capability(RECOMMENDED_RANGE, gid_count=NARROW)
        assert not lopsided.wide_enough_for_build

    def test_root_needs_no_delegation(self):
        """Rootful podman has no user namespace to translate through."""
        assert _capability(0, is_root=True).wide_enough_for_build


class TestRecommendation:

    def test_names_the_exact_line_to_replace(self):
        text = format_build_range_warning(_capability())
        assert f"packager:524288:{NARROW}" in text
        assert f"packager:524288:{RECOMMENDED_RANGE}" in text

    def test_never_hands_out_a_sudo_command(self):
        """The commands are for a root shell. ``podman system migrate``
        has to run back as the packager, and a copy-paste block mixing
        the two migrates root's containers instead of the user's.

        The message may well spell out « not sudo » ; what must not
        appear is a command line starting with it.
        """
        text = format_build_range_warning(_capability())
        offenders = [line for line in text.splitlines()
                     if line.strip().startswith("sudo ")]
        assert not offenders, offenders
        assert "podman system migrate" in text

    def test_says_why_the_change_can_look_ignored(self):
        """The pause process keeps the old map alive while a container
        runs. Losing an hour to that once was enough."""
        text = format_build_range_warning(_capability())
        assert "pause" in text

    def test_unusual_layouts_get_prose_not_a_wrong_sed(self):
        """Several ranges, or uid and gid delegated differently: we
        cannot name one line to replace, and a wrong sed on
        /etc/subuid locks the user out of rootless containers."""
        odd = UserNamespaceCapability(
            username="packager", subuid_count=NARROW * 2,
            subgid_count=NARROW, is_root=False,
            subuid_entries=((100000, NARROW), (300000, NARROW)),
            subgid_entries=((100000, NARROW),))
        text = format_build_range_warning(odd)
        assert "sed -i" not in text
        assert str(BUILD_RANGE_MIN) in text


class TestBuildGate:
    """What ``urpm build`` does with the verdict."""

    @pytest.fixture
    def narrow(self, monkeypatch):
        monkeypatch.setattr(userns, "probe_current_user",
                            lambda: _capability())

    @pytest.fixture
    def wide(self, monkeypatch):
        monkeypatch.setattr(
            userns, "probe_current_user",
            lambda: _capability(RECOMMENDED_RANGE,
                                entries=((524288, RECOMMENDED_RANGE),)))

    @pytest.fixture
    def interactive(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def test_wide_delegation_says_nothing(self, wide, capsys):
        assert _confirm_id_delegation(auto=False)
        assert capsys.readouterr().out == ""

    def test_narrow_asks_and_yes_proceeds(self, narrow, interactive,
                                          monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        assert _confirm_id_delegation(auto=False)
        assert "too narrow" in capsys.readouterr().out

    def test_narrow_defaults_to_not_building(self, narrow, interactive,
                                             monkeypatch):
        """Bare Enter answers no: the packager was asked for a reason."""
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        assert not _confirm_id_delegation(auto=False)

    def test_auto_still_shows_the_recommendation(self, narrow, monkeypatch,
                                                 capsys):
        """Skipping the question must not skip the information."""
        def refuse(_prompt):
            raise AssertionError("--auto must not prompt")

        monkeypatch.setattr("builtins.input", refuse)
        assert _confirm_id_delegation(auto=True)
        out = capsys.readouterr().out
        assert "too narrow" in out
        assert "podman system migrate" in out

    def test_a_scripted_build_does_not_hang_on_the_prompt(
            self, narrow, monkeypatch, capsys):
        """No terminal, nobody to answer. Warn and carry on, otherwise
        every CI build blocks forever on an invisible question."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        def refuse(_prompt):
            raise AssertionError("must not prompt without a terminal")

        monkeypatch.setattr("builtins.input", refuse)
        assert _confirm_id_delegation(auto=False)
        assert "too narrow" in capsys.readouterr().out
