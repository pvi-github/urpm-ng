"""Internationalization support for urpm.

This module provides gettext-based internationalization for the urpm CLI.

Usage:
    from urpm.i18n import _, ngettext, N_

    # Simple strings
    print(_("Package installed successfully"))

    # Strings with variables (use .format(), not f-strings for xgettext)
    print(_("Installing {count} packages").format(count=5))

    # Plural forms
    print(ngettext(
        "{n} package will be installed",
        "{n} packages will be installed",
        count
    ).format(n=count))

    # Mark for extraction without translation (lazy)
    ERRORS = {
        'not_found': N_("Package not found"),
    }
    # Later: print(_(ERRORS['not_found']))
"""

import gettext
import locale
import os

# Domain name for this project
DOMAIN = "urpm"

# Singleton translation object
_translation: gettext.GNUTranslations | gettext.NullTranslations | None = None


def init(localedir: str | None = None) -> None:
    """Initialize the i18n system.

    Args:
        localedir: Custom locale directory. If None, uses system default
                   or development fallback.
    """
    global _translation

    # Set up locale from environment
    try:
        locale.setlocale(locale.LC_ALL, '')
    except locale.Error:
        pass  # Fall back to C locale

    # Determine locale directory
    if localedir is None:
        # Check for development environment first
        dev_locale = os.path.join(os.path.dirname(__file__), '..', 'po', 'locale')
        if os.path.isdir(dev_locale):
            localedir = dev_locale
        else:
            # System installation
            localedir = "/usr/share/locale"

    # Load translations
    try:
        _translation = gettext.translation(
            DOMAIN,
            localedir=localedir,
            fallback=True
        )
    except Exception:
        _translation = gettext.NullTranslations()


def _(message: str) -> str:
    """Translate a string.

    Args:
        message: The string to translate (in English).

    Returns:
        Translated string, or original if no translation found.
    """
    global _translation
    if _translation is None:
        init()
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a string with plural forms.

    Args:
        singular: Singular form in English.
        plural: Plural form in English.
        n: The count determining which form to use.

    Returns:
        Appropriate translated string for the count.
    """
    global _translation
    if _translation is None:
        init()
    return _translation.ngettext(singular, plural, n)


def confirm_yes(response: str) -> bool:
    """Check if a user response means 'yes' in the current locale.

    Accepts English 'y'/'yes' plus the localized equivalents.
    Translators should translate "y" and "yes" to their locale's
    single-letter and full-word confirmations (e.g., "o"/"oui" in French).

    Args:
        response: Raw user input string.

    Returns:
        True if the response is affirmative.
    """
    r = response.strip().lower()
    # Always accept English, plus whatever the locale defines
    return r in ('y', 'yes', _('y'), _('yes'))


def confirm_no(response: str) -> bool:
    """Check if a user response means 'no' in the current locale.

    The counterpart of :func:`confirm_yes`, for the prompts whose
    default is yes.  Those cannot be phrased as ``not confirm_yes(...)``
    — that would read an empty line, or a typo, as a refusal, and the
    prompts that default to yes are the ones protecting something.

    A prompt asking whether to keep the target machine's drivers is the
    case in point: with the negated form, a fumbled keystroke removed
    them.  Asking the question the other way round makes anything that
    is not an explicit refusal fall back on the safe default.

    Args:
        response: Raw user input string.

    Returns:
        True if the response is an explicit refusal.
    """
    r = response.strip().lower()
    return r in ('n', 'no', _('n'), _('no'))


def pgettext(context: str, message: str) -> str:
    """Translate a string with context disambiguation.

    Use this when the same English string needs different translations
    depending on context (e.g., "Media" as a column header vs. section title).

    Args:
        context: A short context string (e.g., "server", "table header").
        message: The string to translate (in English).

    Returns:
        Translated string, or original if no translation found.
    """
    global _translation
    if _translation is None:
        init()
    return _translation.pgettext(context, message)


def N_(message: str) -> str:
    """Mark a string for extraction without translating.

    Use this for strings that need to be extracted but will be
    translated later (e.g., in data structures).

    Args:
        message: String to mark for extraction.

    Returns:
        The original string unchanged.
    """
    return message
