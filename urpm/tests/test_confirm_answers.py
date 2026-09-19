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

These tests pin both readers against every shipped locale, and the
asymmetry that matters: an empty line means the default, and the
default of a protective prompt is to protect.
"""

from __future__ import annotations

import gettext
from pathlib import Path

import pytest

import urpm.i18n as i18n
from urpm.i18n import confirm_no, confirm_yes

LOCALE_DIR = Path(__file__).resolve().parents[2] / "po" / "locale"

#: The shipped locales, with their affirmative and negative words.
ANSWERS = {
    "fr": ("o", "oui", "n", "non"),
    "de": ("j", "ja", "n", "nein"),
    "es": ("s", "sí", "n", "no"),
    "it": ("s", "sì", "n", "no"),
    "nl": ("j", "ja", "n", "nee"),
    "pt": ("s", "sim", "n", "não"),
}


@pytest.fixture
def in_locale(monkeypatch):
    """Switch the process translation, undoing it afterwards.

    ``conftest`` installs a null translation for the whole suite so
    assertions can be written against the English msgid.  These tests
    are precisely about what the catalogues say, so they opt out.
    """
    def _switch(lang: str):
        if not (LOCALE_DIR / lang).is_dir():
            pytest.skip(f"no compiled catalogue for {lang} (run `make mo`)")
        monkeypatch.setattr(
            i18n, "_translation",
            gettext.translation("urpm", localedir=str(LOCALE_DIR),
                                languages=[lang]))
    return _switch


@pytest.mark.parametrize("lang", sorted(ANSWERS))
class TestEveryShippedLocale:

    def test_its_yes_is_read_as_yes(self, lang, in_locale):
        in_locale(lang)
        letter, word, _n, _no = ANSWERS[lang]
        assert confirm_yes(letter) is True
        assert confirm_yes(word) is True

    def test_its_no_is_read_as_no(self, lang, in_locale):
        in_locale(lang)
        _y, _yes, letter, word = ANSWERS[lang]
        assert confirm_no(letter) is True
        assert confirm_no(word) is True

    def test_english_keeps_working(self, lang, in_locale):
        """Operators who type « y » out of habit, and scripts that
        were written before the locale existed."""
        in_locale(lang)
        assert confirm_yes("y") is True
        assert confirm_yes("yes") is True
        assert confirm_no("n") is True
        assert confirm_no("no") is True

    def test_a_yes_is_not_a_no(self, lang, in_locale):
        in_locale(lang)
        letter, word, _n, _no = ANSWERS[lang]
        assert confirm_no(letter) is False
        assert confirm_no(word) is False

    def test_surrounding_space_and_case_are_forgiven(self, lang, in_locale):
        in_locale(lang)
        letter, _word, no_letter, _no = ANSWERS[lang]
        assert confirm_yes(f"  {letter.upper()}  ") is True
        assert confirm_no(f"  {no_letter.upper()}  ") is True


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
