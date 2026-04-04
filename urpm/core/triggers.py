"""Human-readable descriptions for known RPM file triggers.

When RPM runs post-install scriptlets (file triggers, ``%posttrans``),
it reports the *package name* that owns the trigger (e.g. ``shared-mime-info``).
This module maps those names to user-friendly descriptions so the CLI and
GUI can display meaningful status messages.

Usage::

    from urpm.core.triggers import describe_trigger

    name = describe_trigger("shared-mime-info")
    # → "Rebuilding MIME database" (translated)
"""

from ..i18n import N_, _


# Mapping of trigger package names to translatable descriptions.
# N_() marks strings for extraction by xgettext without translating them
# at import time.  The actual translation happens in describe_trigger().
#
# This covers the most common Mageia file triggers.  Unknown triggers
# fall back to a generic "Running: <package>" message.
_TRIGGER_DESCRIPTIONS: dict[str, str] = {
    'shared-mime-info':    N_("Rebuilding MIME database"),
    'desktop-file-utils':  N_("Updating desktop database"),
    'hicolor-icon-theme':  N_("Updating icon cache"),
    'man-db':              N_("Updating man page index"),
    'desktop-common-data': N_("Updating menus"),
    'glibc':               N_("Updating shared library links"),
    'fontconfig':          N_("Rebuilding font cache"),
    'gtk+3.0':             N_("Updating GTK icon cache"),
    'gdk-pixbuf2.0':       N_("Updating GDK pixbuf loaders"),
    'glib2.0':             N_("Compiling GSettings schemas"),
    'systemd':             N_("Reloading systemd units"),
    'texlive':             N_("Updating TeX file database"),
    'ca-certificates':     N_("Updating CA certificates"),
    'xml-common':          N_("Updating XML catalog"),
    'sgml-common':         N_("Updating SGML catalog"),
    'info-install':        N_("Updating info directory"),
    'gconf2':              N_("Updating GConf schemas"),
    'gio-querymodules':    N_("Updating GIO modules"),
    'ldconfig':            N_("Updating shared library links"),
}


def describe_trigger(package_name: str) -> str:
    """Return a human-readable description of a trigger.

    If the trigger package is known, returns a translated description.
    Otherwise returns a generic ``"Running: <package>"`` message.

    Args:
        package_name: Name of the package whose trigger is running.

    Returns:
        Translated description string.
    """
    desc = _TRIGGER_DESCRIPTIONS.get(package_name)
    if desc:
        return _(desc)
    return _("Running: {package}").format(package=package_name)
