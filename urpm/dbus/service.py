"""D-Bus service for urpm package management.

Exposes urpm PackageOperations over the system bus at:
    Bus name:    org.mageia.Urpm.v1
    Object path: /org/mageia/Urpm/v1

Authorization is handled via PolicyKit for all privileged operations.
Read-only operations (search, info, list updates) require no auth.

Usage:
    urpm-dbus-service          # Run as D-Bus activated service
    urpm-dbus-service --debug  # Run with debug logging
"""

import logging
import os
import signal
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# D-Bus names
BUS_NAME = "org.mageia.Urpm.v1"
OBJECT_PATH = "/org/mageia/Urpm/v1"
INTERFACE_NAME = "org.mageia.Urpm.v1"


class UrpmDBusService:
    """D-Bus service exposing urpm operations.

    Each method:
    1. Identifies the caller (pid/uid via D-Bus credentials)
    2. Checks PolicyKit authorization
    3. Calls PackageOperations
    4. Emits progress signals
    5. Returns result
    """

    def __init__(self):
        self._ops = None
        self._db = None
        self._polkit = None
        self._audit = None
        self._loop = None
        self._active_operations = {}

    def _init_core(self):
        """Lazy-init core components."""
        if self._db is not None:
            return

        from ..core.database import PackageDatabase
        from ..core.operations import PackageOperations
        from ..auth.polkit import PolicyKitBackend
        from ..auth.audit import AuditLogger

        self._db = PackageDatabase()
        self._audit = AuditLogger()
        self._ops = PackageOperations(self._db, audit_logger=self._audit)
        self._polkit = PolicyKitBackend()

    def _get_caller_credentials(self, bus, sender):
        """Get caller PID and UID from D-Bus sender."""
        try:
            import gi
            gi.require_version('Gio', '2.0')
            from gi.repository import Gio, GLib

            # Use the bus connection to get credentials
            result = bus.call_sync(
                'org.freedesktop.DBus',
                '/org/freedesktop/DBus',
                'org.freedesktop.DBus',
                'GetConnectionUnixProcessID',
                GLib.Variant('(s)', (sender,)),
                GLib.VariantType.new('(u)'),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            pid = result.unpack()[0]

            result = bus.call_sync(
                'org.freedesktop.DBus',
                '/org/freedesktop/DBus',
                'org.freedesktop.DBus',
                'GetConnectionUnixUser',
                GLib.Variant('(s)', (sender,)),
                GLib.VariantType.new('(u)'),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            uid = result.unpack()[0]

            return pid, uid
        except Exception as e:
            logger.error(f"Cannot get caller credentials: {e}")
            return None, None

    def _authorize(self, bus, sender, permission):
        """Authorize a caller for a permission. Returns AuthContext or None."""
        from ..auth.context import Permission

        pid, uid = self._get_caller_credentials(bus, sender)
        if pid is None:
            return None

        try:
            context, denied = self._polkit.create_auth_context(
                pid, uid, permission
            )
            if denied:
                logger.info(f"Denied {denied} for pid={pid} uid={uid}")
                return None
            return context
        except Exception as e:
            logger.error(f"Authorization failed: {e}")
            return None

    # =====================================================================
    # D-Bus method handlers
    # =====================================================================

    def handle_search_packages(self, bus, sender, pattern, search_provides):
        """SearchPackages(pattern: s, search_provides: b) -> aa{sv}"""
        self._init_core()
        results = self._ops.search_packages(
            pattern, search_provides=search_provides, limit=200
        )
        return results

    def handle_get_package_info(self, bus, sender, identifier):
        """GetPackageInfo(identifier: s) -> a{sv}"""
        self._init_core()
        return self._ops.get_package_info(identifier)

    def handle_get_updates(self, bus, sender):
        """GetUpdates() -> (b, aa{sv}, as)"""
        self._init_core()
        success, upgrades, problems = self._ops.get_updates()

        upgrade_dicts = []
        for u in upgrades:
            upgrade_dicts.append({
                'name': u.name,
                'nevra': u.nevra,
                'evr': u.evr,
                'arch': u.arch,
                'size': u.size or 0,
            })

        return success, upgrade_dicts, problems

    def handle_install_packages(self, bus, sender, package_names, options):
        """InstallPackages(packages: as, options: a{sv}) -> (bs)

        Returns (success, error_message).
        """
        from ..auth.context import Permission

        self._init_core()

        context = self._authorize(bus, sender, Permission.INSTALL)
        if context is None:
            return False, "Authorization denied"

        # TODO: resolve packages, download, install via self._ops
        # For now, return a placeholder indicating the service works
        return False, "Not yet implemented - use CLI"

    def handle_remove_packages(self, bus, sender, package_names, options):
        """RemovePackages(packages: as, options: a{sv}) -> (bs)"""
        from ..auth.context import Permission

        self._init_core()

        context = self._authorize(bus, sender, Permission.REMOVE)
        if context is None:
            return False, "Authorization denied"

        return False, "Not yet implemented - use CLI"

    def handle_upgrade_packages(self, bus, sender, options):
        """UpgradePackages(options: a{sv}) -> (bs)"""
        from ..auth.context import Permission

        self._init_core()

        context = self._authorize(bus, sender, Permission.UPGRADE)
        if context is None:
            return False, "Authorization denied"

        return False, "Not yet implemented - use CLI"

    def handle_refresh_metadata(self, bus, sender):
        """RefreshMetadata() -> (bs)"""
        from ..auth.context import Permission

        self._init_core()

        context = self._authorize(bus, sender, Permission.REFRESH)
        if context is None:
            return False, "Authorization denied"

        return False, "Not yet implemented - use CLI"

    # =====================================================================
    # D-Bus registration (GLib/Gio)
    # =====================================================================

    def _build_introspection_xml(self):
        """Build D-Bus introspection XML for the interface."""
        return f"""
<node>
  <interface name="{INTERFACE_NAME}">
    <method name="SearchPackages">
      <arg name="pattern" type="s" direction="in"/>
      <arg name="search_provides" type="b" direction="in"/>
      <arg name="results" type="s" direction="out"/>
    </method>
    <method name="GetPackageInfo">
      <arg name="identifier" type="s" direction="in"/>
      <arg name="info" type="s" direction="out"/>
    </method>
    <method name="GetUpdates">
      <arg name="result" type="s" direction="out"/>
    </method>
    <method name="InstallPackages">
      <arg name="packages" type="as" direction="in"/>
      <arg name="options" type="a{{sv}}" direction="in"/>
      <arg name="success" type="b" direction="out"/>
      <arg name="error" type="s" direction="out"/>
    </method>
    <method name="RemovePackages">
      <arg name="packages" type="as" direction="in"/>
      <arg name="options" type="a{{sv}}" direction="in"/>
      <arg name="success" type="b" direction="out"/>
      <arg name="error" type="s" direction="out"/>
    </method>
    <method name="UpgradePackages">
      <arg name="options" type="a{{sv}}" direction="in"/>
      <arg name="success" type="b" direction="out"/>
      <arg name="error" type="s" direction="out"/>
    </method>
    <method name="RefreshMetadata">
      <arg name="success" type="b" direction="out"/>
      <arg name="error" type="s" direction="out"/>
    </method>
    <signal name="OperationProgress">
      <arg name="operation_id" type="s"/>
      <arg name="phase" type="s"/>
      <arg name="package" type="s"/>
      <arg name="current" type="u"/>
      <arg name="total" type="u"/>
      <arg name="message" type="s"/>
    </signal>
    <signal name="OperationComplete">
      <arg name="operation_id" type="s"/>
      <arg name="success" type="b"/>
      <arg name="message" type="s"/>
    </signal>
  </interface>
</node>
"""

    def _on_method_call(self, connection, sender, object_path, interface_name,
                        method_name, parameters, invocation):
        """Handle incoming D-Bus method calls."""
        import json

        try:
            import gi
            gi.require_version('Gio', '2.0')
            from gi.repository import GLib

            if method_name == "SearchPackages":
                pattern, search_provides = parameters.unpack()
                results = self.handle_search_packages(
                    connection, sender, pattern, search_provides
                )
                invocation.return_value(
                    GLib.Variant('(s)', (json.dumps(results),))
                )

            elif method_name == "GetPackageInfo":
                identifier = parameters.unpack()[0]
                info = self.handle_get_package_info(
                    connection, sender, identifier
                )
                invocation.return_value(
                    GLib.Variant('(s)', (json.dumps(info),))
                )

            elif method_name == "GetUpdates":
                success, upgrades, problems = self.handle_get_updates(
                    connection, sender
                )
                result = {
                    'success': success,
                    'upgrades': upgrades,
                    'problems': problems,
                }
                invocation.return_value(
                    GLib.Variant('(s)', (json.dumps(result),))
                )

            elif method_name == "InstallPackages":
                packages, options = parameters.unpack()
                success, error = self.handle_install_packages(
                    connection, sender, packages, options
                )
                invocation.return_value(
                    GLib.Variant('(bs)', (success, error))
                )

            elif method_name == "RemovePackages":
                packages, options = parameters.unpack()
                success, error = self.handle_remove_packages(
                    connection, sender, packages, options
                )
                invocation.return_value(
                    GLib.Variant('(bs)', (success, error))
                )

            elif method_name == "UpgradePackages":
                options = parameters.unpack()[0]
                success, error = self.handle_upgrade_packages(
                    connection, sender, options
                )
                invocation.return_value(
                    GLib.Variant('(bs)', (success, error))
                )

            elif method_name == "RefreshMetadata":
                success, error = self.handle_refresh_metadata(
                    connection, sender
                )
                invocation.return_value(
                    GLib.Variant('(bs)', (success, error))
                )

            else:
                invocation.return_dbus_error(
                    'org.freedesktop.DBus.Error.UnknownMethod',
                    f'Unknown method: {method_name}'
                )

        except Exception as e:
            logger.exception(f"Error handling {method_name}")
            invocation.return_dbus_error(
                'org.mageia.Urpm.v1.Error',
                str(e)
            )

    def run(self, debug: bool = False):
        """Run the D-Bus service (main loop)."""
        import gi
        gi.require_version('Gio', '2.0')
        from gi.repository import Gio, GLib

        if debug:
            logging.basicConfig(level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.INFO)

        logger.info(f"Starting urpm D-Bus service ({BUS_NAME})")

        node_info = Gio.DBusNodeInfo.new_for_xml(
            self._build_introspection_xml()
        )
        interface_info = node_info.interfaces[0]

        def on_bus_acquired(connection, name):
            logger.info(f"Bus acquired: {name}")
            connection.register_object(
                OBJECT_PATH,
                interface_info,
                self._on_method_call,
                None,  # get_property
                None,  # set_property
            )

        def on_name_acquired(connection, name):
            logger.info(f"Name acquired: {name}")

        def on_name_lost(connection, name):
            logger.error(f"Name lost: {name}")
            self._loop.quit()

        Gio.bus_own_name(
            Gio.BusType.SYSTEM,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            on_bus_acquired,
            on_name_acquired,
            on_name_lost,
        )

        self._loop = GLib.MainLoop()

        # Handle SIGTERM/SIGINT gracefully
        def _quit(signum, frame):
            logger.info("Received signal, shutting down")
            self._loop.quit()

        signal.signal(signal.SIGTERM, _quit)
        signal.signal(signal.SIGINT, _quit)

        try:
            self._loop.run()
        finally:
            if self._audit:
                self._audit.close()
            if self._db:
                self._db.close()
            logger.info("Service stopped")


def main():
    """Entry point for urpm-dbus-service."""
    debug = '--debug' in sys.argv
    service = UrpmDBusService()
    service.run(debug=debug)


if __name__ == '__main__':
    main()
