"""Internationalization support for rpmdrake-ng.

This module provides gettext-based internationalization for the Qt6 GUI.
It integrates with Qt's translation system while using gettext as the backend.

Usage:
    from rpmdrake.i18n import _, ngettext, init_qt_translation

    # Initialize in main():
    app = QApplication(sys.argv)
    init_qt_translation(app)

    # Use in widgets:
    button.setText(_("Install"))
    label.setText(_("Found {count} packages").format(count=n))
"""

import gettext
import locale
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

# Domain name for this project
DOMAIN = "rpmdrake"

# Translation object
_translation: gettext.GNUTranslations | gettext.NullTranslations | None = None


def init(localedir: str | None = None) -> None:
    """Initialize the i18n system.

    Args:
        localedir: Custom locale directory. If None, uses system default
                   or development fallback.
    """
    global _translation

    try:
        locale.setlocale(locale.LC_ALL, '')
    except locale.Error:
        pass

    if localedir is None:
        dev_locale = os.path.join(os.path.dirname(__file__), '..', 'po', 'locale')
        if os.path.isdir(dev_locale):
            localedir = dev_locale
        else:
            localedir = "/usr/share/locale"

    try:
        _translation = gettext.translation(
            DOMAIN,
            localedir=localedir,
            fallback=True
        )
    except Exception:
        _translation = gettext.NullTranslations()


def init_qt_translation(app: 'QApplication') -> None:
    """Initialize Qt application with translations.

    Call this after creating QApplication but before creating widgets.

    Args:
        app: The QApplication instance.
    """
    from PySide6.QtCore import QLocale, QTranslator

    init()

    # Also load Qt's own translations for standard dialogs
    qt_translator = QTranslator(app)
    locale_name = QLocale.system().name()

    # Try Qt translations from system
    qt_locale_paths = [
        "/usr/share/qt6/translations",
        "/usr/share/qt/translations",
    ]
    for path in qt_locale_paths:
        if qt_translator.load(f"qtbase_{locale_name}", path):
            app.installTranslator(qt_translator)
            break


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
