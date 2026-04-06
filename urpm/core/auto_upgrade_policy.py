"""Auto-upgrade policy enforcement for third-party update mechanisms.

When urpm-ng is the system package manager, other tools (gnome-software,
KDE Discover, PackageKit offline updates, dnf-automatic) must not install
updates behind the user's back.  This module provides:

- Detection of which mechanisms are present on the system.
- Per-mechanism enable/disable via GSettings overrides, KConfig overrides,
  or systemctl operations.
- A single ``enforce_all()`` entry point for the RPM ``%post`` scriptlet.
- Individual ``set_*()`` functions for ``urpm config`` subcommands.

Design rules:
- ``dnf-automatic`` timers are **always** masked when urpm-ng is installed
  (no user toggle — it's a direct conflict).
- gnome-software, Discover, and packagekit-offline-update are user-toggleable
  (default: disabled).
- Operations that target absent software are silently skipped.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────

GSCHEMA_DIR = Path("/usr/share/glib-2.0/schemas")
GSCHEMA_OVERRIDE = GSCHEMA_DIR / "10-urpm.gschema.override"

DISCOVER_CONFIG_DIR = Path("/etc/xdg")
DISCOVER_CONFIG = DISCOVER_CONFIG_DIR / "discoverrc"

# ── Helpers ────────────────────────────────────────────────────────────


def _systemctl(*args, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a systemctl command, suppressing errors for missing units."""
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _is_unit_present(unit: str) -> bool:
    """Check whether a systemd unit file exists on the system."""
    result = _systemctl("cat", unit)
    return result.returncode == 0


def _is_unit_enabled(unit: str) -> bool:
    result = _systemctl("is-enabled", unit)
    return result.stdout.strip() == "enabled"


def _is_unit_active(unit: str) -> bool:
    result = _systemctl("is-active", unit)
    return result.stdout.strip() == "active"


def _compile_gschemas() -> bool:
    """Recompile GSettings schemas.  Returns True on success."""
    if not GSCHEMA_DIR.is_dir():
        return False
    try:
        subprocess.run(
            ["glib-compile-schemas", str(GSCHEMA_DIR)],
            capture_output=True, timeout=30,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _has_gnome_software() -> bool:
    """Check if gnome-software's GSettings schema is installed."""
    if not GSCHEMA_DIR.is_dir():
        return False
    # The compiled schema database must contain org.gnome.software
    try:
        result = subprocess.run(
            ["gsettings", "list-keys", "org.gnome.software"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _has_discover() -> bool:
    """Check if KDE Discover is installed."""
    return Path("/usr/bin/plasma-discover").exists()


# ── dnf-automatic (always killed) ─────────────────────────────────────


def kill_dnf_automatic() -> None:
    """Unconditionally mask and stop dnf-automatic timers.

    dnf-automatic is a direct conflict with urpm-ng — there is no user
    toggle, it is always disabled when urpm-ng is installed.
    """
    timers = [
        "dnf-automatic.timer",
        "dnf-automatic-install.timer",
        "dnf-automatic-download.timer",
        "dnf-automatic-notifyonly.timer",
    ]
    for timer in timers:
        try:
            if not _is_unit_present(timer):
                continue
            if _is_unit_active(timer):
                _systemctl("stop", timer)
                logger.info("Stopped %s", timer)
            if _is_unit_enabled(timer):
                _systemctl("disable", timer)
                logger.info("Disabled %s", timer)
            _systemctl("mask", timer)
            logger.info("Masked %s", timer)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass


# ── gnome-software auto-upgrades ──────────────────────────────────────


def get_gnome_auto_upgrades() -> Optional[bool]:
    """Return current gnome-software auto-upgrade state, or None if N/A."""
    if not _has_gnome_software():
        return None
    if not GSCHEMA_OVERRIDE.exists():
        return True  # No override = gnome-software defaults (auto on)
    try:
        text = GSCHEMA_OVERRIDE.read_text()
        for line in text.splitlines():
            if line.strip().startswith("download-updates="):
                return line.split("=", 1)[1].strip().lower() == "true"
    except OSError:
        pass
    return True  # No relevant key found = default on


def set_gnome_auto_upgrades(enabled: bool) -> bool:
    """Enable or disable gnome-software automatic downloads.

    Writes (or updates) a GSettings override file and recompiles schemas.
    Returns True on success, False if gnome-software is not installed.
    """
    if not _has_gnome_software():
        return False

    value = "true" if enabled else "false"

    # Read existing override (may contain other sections)
    existing_sections: dict[str, dict[str, str]] = {}
    if GSCHEMA_OVERRIDE.exists():
        try:
            import configparser
            cp = configparser.ConfigParser()
            cp.optionxform = str  # Preserve key casing
            cp.read_string(GSCHEMA_OVERRIDE.read_text())
            for section in cp.sections():
                existing_sections[section] = dict(cp.items(section))
        except Exception:
            existing_sections = {}

    # Update the gnome-software section
    gs_section = existing_sections.setdefault("org.gnome.software", {})
    gs_section["download-updates"] = value
    gs_section["download-updates-notify"] = value

    # Write back
    lines = [
        "# Auto-generated by urpm-ng — do not edit manually.",
        "# Use 'urpm config gnome-auto-upgrades yes|no' to change.",
        "",
    ]
    for section, keys in sorted(existing_sections.items()):
        lines.append(f"[{section}]")
        for k, v in sorted(keys.items()):
            lines.append(f"{k}={v}")
        lines.append("")

    try:
        GSCHEMA_OVERRIDE.write_text("\n".join(lines))
    except OSError as e:
        logger.error("Failed to write %s: %s", GSCHEMA_OVERRIDE, e)
        return False

    _compile_gschemas()
    logger.info("gnome-software auto-upgrades set to %s", value)
    return True


# ── KDE Discover auto-upgrades ────────────────────────────────────────


def get_discover_auto_upgrades() -> Optional[bool]:
    """Return current Discover auto-upgrade state, or None if N/A."""
    if not _has_discover():
        return None
    if not DISCOVER_CONFIG.exists():
        return True  # No config = Discover defaults
    try:
        text = DISCOVER_CONFIG.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("UseOfflineUpdates="):
                return stripped.split("=", 1)[1].strip().lower() == "true"
    except OSError:
        pass
    return True


def set_discover_auto_upgrades(enabled: bool) -> bool:
    """Enable or disable KDE Discover automatic updates.

    Writes a system-wide KConfig override at ``/etc/xdg/discoverrc``.
    Returns True on success, False if Discover is not installed.
    """
    if not _has_discover():
        return False

    value = "true" if enabled else "false"

    # Read existing config (preserve other sections)
    lines_out: list[str] = [
        "# Auto-generated by urpm-ng — do not edit manually.",
        "# Use 'urpm config discover-auto-upgrades yes|no' to change.",
        "",
    ]

    in_updates_section = False
    wrote_key = False
    existing_lines: list[str] = []

    if DISCOVER_CONFIG.exists():
        try:
            existing_lines = DISCOVER_CONFIG.read_text().splitlines()
        except OSError:
            existing_lines = []

    # Filter out old auto-generated header
    filtered: list[str] = []
    for line in existing_lines:
        if line.startswith("# Auto-generated by urpm-ng"):
            continue
        if line.startswith("# Use 'urpm config discover"):
            continue
        filtered.append(line)
    existing_lines = filtered

    found_section = False
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_updates_section and not wrote_key:
                lines_out.append(f"UseOfflineUpdates={value}")
                wrote_key = True
            in_updates_section = stripped == "[Software]"
            if in_updates_section:
                found_section = True
            lines_out.append(line)
        elif in_updates_section and stripped.startswith("UseOfflineUpdates="):
            lines_out.append(f"UseOfflineUpdates={value}")
            wrote_key = True
        else:
            lines_out.append(line)

    if in_updates_section and not wrote_key:
        lines_out.append(f"UseOfflineUpdates={value}")
        wrote_key = True

    if not found_section:
        lines_out.append("[Software]")
        lines_out.append(f"UseOfflineUpdates={value}")

    try:
        DISCOVER_CONFIG.write_text("\n".join(lines_out) + "\n")
    except OSError as e:
        logger.error("Failed to write %s: %s", DISCOVER_CONFIG, e)
        return False

    logger.info("Discover auto-upgrades set to %s", value)
    return True


# ── PackageKit offline updates ────────────────────────────────────────

_PK_OFFLINE_SERVICE = "packagekit-offline-update.service"


def get_packagekit_auto_upgrades() -> Optional[bool]:
    """Return current packagekit-offline-update state, or None if N/A."""
    if not _is_unit_present(_PK_OFFLINE_SERVICE):
        return None
    return _is_unit_enabled(_PK_OFFLINE_SERVICE)


def set_packagekit_auto_upgrades(enabled: bool) -> bool:
    """Enable or disable packagekit-offline-update.service.

    Returns True on success, False if the service is not present.
    """
    if not _is_unit_present(_PK_OFFLINE_SERVICE):
        return False

    try:
        if enabled:
            _systemctl("unmask", _PK_OFFLINE_SERVICE)
            _systemctl("enable", _PK_OFFLINE_SERVICE)
            logger.info("Enabled %s", _PK_OFFLINE_SERVICE)
        else:
            if _is_unit_active(_PK_OFFLINE_SERVICE):
                _systemctl("stop", _PK_OFFLINE_SERVICE)
                logger.info("Stopped %s", _PK_OFFLINE_SERVICE)
            if _is_unit_enabled(_PK_OFFLINE_SERVICE):
                _systemctl("disable", _PK_OFFLINE_SERVICE)
                logger.info("Disabled %s", _PK_OFFLINE_SERVICE)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ── Stop PackageKit daemon (cancel in-progress transactions) ──────────


def stop_packagekit_daemon() -> None:
    """Stop the PackageKit daemon to cancel any in-progress transaction.

    The daemon is socket-activated and will restart on the next request
    from a GUI front-end (Discover, GNOME Software), so this is safe.
    """
    try:
        if _is_unit_active("packagekit.service"):
            _systemctl("stop", "packagekit.service")
            logger.info("Stopped packagekit.service (cancel in-progress transaction)")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


# ── Master entry point (for %post and urpmd) ──────────────────────────


def _kill_gui_updaters() -> None:
    """Kill running GUI update managers so they pick up new settings.

    gnome-software and Discover cache GSettings/KConfig in memory.
    After writing overrides they must be restarted to apply them.
    Both are D-Bus activated and will restart on next user interaction.
    """
    for process_name in ("gnome-software", "plasma-discover"):
        try:
            subprocess.run(
                ["pkill", "-f", process_name],
                capture_output=True, timeout=5,
            )
            logger.info("Killed %s (will restart with new settings)", process_name)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass


def enforce_all() -> None:
    """Apply all default auto-upgrade policies.

    Called from the RPM ``%post`` scriptlet at install time:
    1. Kill dnf-automatic (always, unconditional).
    2. Disable gnome-software auto-downloads (if present).
    3. Disable Discover auto-updates (if present).
    4. Disable packagekit-offline-update (if present).
    5. Kill GUI updaters so they reload settings.
    6. Stop PackageKit daemon (cancel any in-progress auto-upgrade).
    """
    kill_dnf_automatic()

    if _has_gnome_software():
        set_gnome_auto_upgrades(False)
    if _has_discover():
        set_discover_auto_upgrades(False)
    if _is_unit_present(_PK_OFFLINE_SERVICE):
        set_packagekit_auto_upgrades(False)

    _kill_gui_updaters()
    stop_packagekit_daemon()
