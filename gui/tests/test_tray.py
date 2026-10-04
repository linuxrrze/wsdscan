"""Status bar icon over a private D-Bus, with a fake StatusNotifierWatcher.

Needs PyGObject and dbus-daemon (Gio.TestDBus starts a private bus).
"""

import os
import shutil
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE))]

try:
    from gi.repository import Gio, GLib
    HAVE_GIO = shutil.which("dbus-daemon") is not None
except ImportError:
    HAVE_GIO = False

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
</interface></node>
"""


def spin(check, timeout=5.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.005)


@unittest.skipUnless(HAVE_GIO, "needs PyGObject and dbus-daemon")
class TrayTest(unittest.TestCase):
    def setUp(self):
        import tray
        self.tray_module = tray
        self.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        self.bus.up()
        self.addCleanup(self.bus.down)
        address = self.bus.get_bus_address()
        assert address is not None
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        self.host = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.app_bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.registered = []
        self.actions = []
        self.availability = []

    def start_watcher(self):
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

        def call(conn, sender, path, iface, method, params, invocation):
            self.registered.append(params.unpack()[0])
            invocation.return_value(None)

        self.host.register_object("/StatusNotifierWatcher", info, call, None, None)
        Gio.bus_own_name_on_connection(self.host, "org.kde.StatusNotifierWatcher",
                                       Gio.BusNameOwnerFlags.NONE, None, None)

    def make_tray(self):
        icon = self.tray_module.TrayIcon(self.app_bus, self.actions.append,
                                         self.availability.append)
        icon.start()
        self.addCleanup(lambda: icon.ids and icon.stop())
        return icon

    def call(self, path, iface, method, params, reply_type):
        """D-Bus call to the icon. Asynchronous: the icon answers from this
        thread's main loop (in real use the status bar is another process)."""
        result = []
        self.host.call(self.registered[0], path, iface, method, params,
                       GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE, 2000, None,
                       lambda conn, res: result.append(conn.call_finish(res)))
        spin(lambda: result)
        return result[0].unpack()

    def test_registers_and_serves_item_and_menu(self):
        self.start_watcher()
        icon = self.make_tray()
        spin(lambda: icon.available)
        self.assertEqual(self.registered, [icon.bus_name])
        self.assertEqual(self.availability, [True])

        props = self.call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "GetAll",
                          GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)), "(a{sv})")[0]
        self.assertEqual(props["Id"], "io.github.wsdscan.ScanToPdf")
        self.assertEqual(props["Menu"], "/MenuBar")
        icon.set_tooltip("Scanning: page 2")
        spin(lambda: True, 0.05)
        tooltip = self.call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "Get",
                            GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "ToolTip")),
                            "(v)")[0]
        self.assertEqual(tooltip[3], "Scanning: page 2")

        revision, layout = self.call("/MenuBar", "com.canonical.dbusmenu", "GetLayout",
                                     GLib.Variant("(iias)", (0, -1, [])), "(u(ia{sv}av))")
        labels = [child[1].get("label") for child in layout[2]]
        self.assertEqual(labels, ["Open Scan to PDF", "Scan", None, "Quit"])

        icon.set_enabled("scan", False)
        _rev, layout = self.call("/MenuBar", "com.canonical.dbusmenu", "GetLayout",
                                 GLib.Variant("(iias)", (0, -1, [])), "(u(ia{sv}av))")
        self.assertFalse(layout[2][1][1]["enabled"])

        self.call("/MenuBar", "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (4, "clicked", GLib.Variant("s", ""), 0)), "()")
        self.call("/MenuBar", "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (2, "clicked", GLib.Variant("s", ""), 0)), "()")
        self.call("/StatusNotifierItem", "org.kde.StatusNotifierItem", "Activate",
                  GLib.Variant("(ii)", (0, 0)), "()")
        spin(lambda: len(self.actions) >= 2)
        # Menu clicks run deferred (they may open windows or quit), a click on
        # the icon itself immediately: compare without order.
        self.assertEqual(sorted(self.actions), ["open", "quit"], "disabled Scan ignored")

    def test_no_watcher_means_unavailable(self):
        icon = self.make_tray()
        spin(lambda: True, 0.3)
        self.assertFalse(icon.available)
        self.start_watcher()  # a status bar appears later (e.g. extension enabled)
        spin(lambda: icon.available)


if __name__ == "__main__":
    unittest.main()
