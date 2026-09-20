"""Reading a yes and reading a no are not the same question.

``confirm_yes`` was the only answer reader, so a prompt whose default
is yes had to be written as its negation — or, as three sites did, by
testing for the letters in the clear:

    preserve = (resp == "" or resp[0:1] in ("y", "o"))

Both forms fail the same way.  A German operator answering « j » to
« keep this machine's drivers? » was read as refusing, and so was
anyone who fat-fingered the key.  The prompt could not even advertise
the right letter: printing « [J/n] » would have offered a key that
meant no.

Nothing here asserts on what a catalogue says.  The words are read
*from* the catalogue and fed back through the readers, so the contract
under test is « whatever this locale calls yes is accepted as yes »,
which stays true when a translator reworks the wording.  Pinning « oui »
would turn a translation review into a broken build.
"""

from __future__ import annotations

import gettext
from pathlib import Path

import pytest

import urpm.i18n as i18n
from urpm.i18n import confirm_no, confirm_yes

LOCALE_DIR = Path(__file__).resolve().parents[2] / "po" / "locale"

#: Shipped locales.  The list is the deliverable — ``po/LINGUAS`` — not
#: a property of any translation, so pinning it is safe.
LANGUAGES = ("de", "es", "fr", "it", "nl", "pt")


@pytest.fixture
def in_locale(monkeypatch):
    """Switch the process translation, undoing it afterwards.

    ``conftest`` installs a null translation for the whole suite so
    assertions elsewhere can be written against the English msgid.
    These tests are about the catalogues, so they opt out.
    """
    def _switch(lang: str):
        if not (LOCALE_DIR / lang).is_dir():
            pytest.skip(f"no compiled catalogue for {lang} (run `make mo`)")
        monkeypatch.setattr(
            i18n, "_translation",
            gettext.translation("urpm", localedir=str(LOCALE_DIR),
                                languages=[lang]))
    return _switch


@pytest.mark.parametrize("lang", LANGUAGES)
class TestEveryShippedLocale:

    def test_its_yes_is_read_as_yes(self, lang, in_locale):
        in_locale(lang)
        assert confirm_yes(i18n._("y")) is True
        assert confirm_yes(i18n._("yes")) is True

    def test_its_no_is_read_as_no(self, lang, in_locale):
        in_locale(lang)
        assert confirm_no(i18n._("n")) is True
        assert confirm_no(i18n._("no")) is True

    def test_its_yes_is_not_a_no(self, lang, in_locale):
        in_locale(lang)
        assert confirm_no(i18n._("y")) is False
        assert confirm_no(i18n._("yes")) is False

    def test_its_no_is_not_a_yes(self, lang, in_locale):
        in_locale(lang)
        assert confirm_yes(i18n._("n")) is False
        assert confirm_yes(i18n._("no")) is False

    def test_the_two_do_not_collide(self, lang, in_locale):
        """A catalogue that gave yes and no the same word would make
        every prompt in that locale unanswerable.  This is the one
        translation mistake worth failing a build over, and it does not
        depend on which words were chosen."""
        in_locale(lang)
        yes = {i18n._("y"), i18n._("yes")}
        no = {i18n._("n"), i18n._("no")}
        assert not (yes & no)

    def test_english_keeps_working(self, lang, in_locale):
        """Operators who type « y » out of habit, and scripts written
        before the locale existed."""
        in_locale(lang)
        assert confirm_yes("y") is True
        assert confirm_yes("yes") is True
        assert confirm_no("n") is True
        assert confirm_no("no") is True

    def test_surrounding_space_and_case_are_forgiven(self, lang, in_locale):
        in_locale(lang)
        assert confirm_yes(f"  {i18n._('y').upper()}  ") is True
        assert confirm_no(f"  {i18n._('n').upper()}  ") is True


class TestTheAsymmetryThatMatters:
    """An empty line is the default, and the two defaults differ."""

    def test_empty_is_not_a_yes(self):
        assert confirm_yes("") is False

    def test_empty_is_not_a_no_either(self):
        """So a « [Y/n] » prompt written as ``not confirm_no(...)``
        falls back on yes, which is what the bracket advertises."""
        assert confirm_no("") is False

    def test_a_typo_is_neither(self):
        """The old form read anything that was not a yes as a no.  On
        « keep the drivers? » that turned a slip into a removal."""
        assert confirm_yes("xyz") is False
        assert confirm_no("xyz") is False

    def test_a_protective_prompt_protects_by_default(self):
        """The shape the caller uses: preserve unless told otherwise."""
        for answer in ("", "xyz", "y", "yes"):
            assert not confirm_no(answer), answer
