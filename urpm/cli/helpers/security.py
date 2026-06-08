"""Security-related CLI helpers for urpm.

Centralises the user-visible artefacts of the bug #3 iteration B
blacklist mechanism so they appear identical across ``urpm install``,
``urpm upgrade``, ``urpm media update`` and ``urpm server list``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, Iterable, List

from .. import colors
from ...i18n import _, ngettext


def _format_ts(ts: int) -> str:
    """Render a Unix timestamp as ``YYYY-MM-DD HH:MM`` (local time).

    Wrapped here so the banner format is stable across callers and we
    do not pull ``datetime`` into every consumer.
    """
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def format_blacklist_banner(servers: Iterable[Dict]) -> str:
    """Render the persistent security-alert banner.

    Takes the dict rows returned by
    :meth:`PackageDatabase.list_blacklisted_servers` and produces a
    multi-line, colour-emphasised block.  Designed to be hard to miss
    in a scrollback: thick rule lines, a leading warning symbol and
    the exact CLI invocation needed to reactivate.

    Returns the empty string when ``servers`` is empty so callers can
    do ``if banner: print(banner)`` unconditionally.
    """
    rows = [dict(s) for s in servers if s]
    if not rows:
        return ""

    lines: List[str] = []
    rule = "═" * 66
    sub = "─" * 66
    head = _("SECURITY ALERT — {n} server(s) blacklisted").format(n=len(rows))
    lines.append(colors.error(rule))
    lines.append(colors.error(f"  ⚠ {head} ⚠"))
    lines.append(colors.error(sub))
    lines.append(colors.error(_(
        "  These mirrors served at least one RPM whose signature could"
    )))
    lines.append(colors.error(_(
        "  not be verified — they are potentially compromised and have"
    )))
    lines.append(colors.error(_(
        "  been blacklisted from every mirror pool.  Per-event detail:"
    )))
    lines.append("")
    lines.append(colors.bold("      urpm server status <name>"))
    lines.append("")
    lines.append(colors.error(sub))

    for idx, row in enumerate(rows):
        name = row.get("name") or "?"
        reason = row.get("blacklist_reason") or _("(no reason recorded)")
        ts = _format_ts(row.get("blacklisted_at"))
        host = row.get("host") or ""
        url_hint = (
            f"{row.get('protocol', '')}://{host}{row.get('base_path', '')}"
            if host else ""
        )

        lines.append(colors.error(_("  Server:   ") + name))
        if url_hint:
            lines.append(colors.error(_("  URL:      ") + url_hint))
        lines.append(colors.error(_("  Reason:   ") + reason))
        lines.append(colors.error(_("  When:     ") + ts))
        if idx < len(rows) - 1:
            lines.append(colors.error(sub))

    lines.append(colors.error(sub))
    lines.append(colors.error(_(
        "  Once you have verified the media integrity (GPG keys,"
    )))
    lines.append(colors.error(_(
        "  mirror source) reactivate the server with:"
    )))
    lines.append("")
    lines.append(colors.bold("      urpm server unblacklist <name>"))
    lines.append("")
    lines.append(colors.error(_(
        "  To stop this reminder while you investigate:"
    )))
    lines.append("")
    lines.append(colors.bold("      urpm server ack-blacklist <name>"))
    lines.append(colors.error(rule))

    return "\n".join(lines)


def emit_blacklist_alert_if_any(db, *, header_only: bool = False,
                                unacknowledged_only: bool = True) -> bool:
    """Print the banner whenever the database holds any blacklisted server.

    Used at the start of every privileged urpm command (install,
    upgrade, media update, server list) so a user who scrolled past
    the original alert at install time still gets the reminder on
    the next interaction — until they explicitly acknowledge it with
    ``urpm server ack-blacklist <name>``.

    Args:
        db: ``PackageDatabase`` instance.
        header_only: When True, print just a single-line summary
            without the full multi-line box.
        unacknowledged_only: When True (default), the banner only
            mentions servers the user has not yet acknowledged.
            ``urpm server list`` and ``urpm server status`` pass
            ``False`` to surface the full state regardless of ack.

    Returns:
        True when a banner was printed, False otherwise.
    """
    try:
        servers = db.list_blacklisted_servers(
            unacknowledged_only=unacknowledged_only,
        )
    except Exception:
        # Schema may be older than v30 in degraded environments —
        # never let the banner machinery crash a routine command.
        return False
    if not servers:
        return False

    if header_only:
        n = len(servers)
        names = ", ".join((s.get("name") or "?") for s in servers[:3])
        if n > 3:
            names += _(", … (+{rest} more)").format(rest=n - 3)
        print(colors.error(
            _("⚠ SECURITY ALERT: {n} server(s) potentially compromised "
              "(blacklisted): {names} — run 'urpm server status <name>' "
              "for details.").format(n=n, names=names)
        ))
    else:
        print(format_blacklist_banner(servers))

    return True
