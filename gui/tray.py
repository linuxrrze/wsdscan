"""Status bar icon via StatusNotifierItem (SNI) + com.canonical.dbusmenu.

SNI is the D-Bus protocol behind status bar icons on KDE Plasma, Ubuntu (its
AppIndicator extension) and GNOME with the "AppIndicator and
KStatusNotifierItem Support" extension. It is implemented here with Gio only:
libappindicator is GTK 3 and cannot be used in a GTK 4 process.
"""

import os

from gi.repository import Gio, GLib

import scanform

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/>
    </signal>
  </interface>
</node>
"""


def to_variant(value):
    """Python value of a menu property -> GLib.Variant."""
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("i", value)
    return GLib.Variant("s", str(value))


def layout_variant(node):
    item_id, props, children = node
    return GLib.Variant("(ia{sv}av)", (item_id, {k: to_variant(v) for k, v in props.items()},
                                       [layout_variant(child) for child in children]))


class TrayIcon:
    """Status bar icon. on_action(name) is called with "open", "scan", "quit".

    on_available(bool) reports whether a status bar host shows the icon (a
    StatusNotifierWatcher exists and accepted it).
    """

    def __init__(self, connection, on_action, on_available):
        self.connection = connection
        self.on_action = on_action
        self.on_available = on_available
        self.tooltip = scanform.APP_NAME
        self.enabled = {}
        self.revision = 1
        self.available = False
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.ids = []

    # --- lifecycle -----------------------------------------------------------

    def start(self):
        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.ids = [
            self.connection.register_object(ITEM_PATH, item_info, self._item_call,
                                            self._item_property, None),
            self.connection.register_object(MENU_PATH, menu_info, self._menu_call,
                                            self._menu_property, None),
        ]
        self.own_id = Gio.bus_own_name_on_connection(
            self.connection, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        self.watch_id = Gio.bus_watch_name_on_connection(
            self.connection, WATCHER, Gio.BusNameWatcherFlags.NONE,
            self._watcher_appeared, self._watcher_vanished)

    def stop(self):
        Gio.bus_unwatch_name(self.watch_id)
        Gio.bus_unown_name(self.own_id)
        for reg_id in self.ids:
            self.connection.unregister_object(reg_id)
        self.ids = []
        self._set_available(False)

    def _watcher_appeared(self, connection, name, owner):
        connection.call(WATCHER, "/StatusNotifierWatcher", WATCHER,
                        "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.bus_name,)),
                        None, Gio.DBusCallFlags.NONE, -1, None, self._registered)

    def _registered(self, connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error:
            self._set_available(False)
            return
        self._set_available(True)

    def _watcher_vanished(self, connection, name):
        self._set_available(False)

    def _set_available(self, available):
        if available != self.available:
            self.available = available
            self.on_available(available)

    # --- updates -------------------------------------------------------------

    def set_tooltip(self, text):
        self.tooltip = text
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)

    def set_enabled(self, action, enabled):
        if self.enabled.get(action, True) != enabled:
            self.enabled[action] = enabled
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                       GLib.Variant("(ui)", (self.revision, 0)))

    def _emit(self, path, interface, signal, params):
        if self.ids:
            self.connection.emit_signal(None, path, interface, signal, params)

    # --- org.kde.StatusNotifierItem -------------------------------------------

    def _item_property(self, connection, sender, path, interface, name):
        icon = f"{scanform.APP_ID}-symbolic"
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", scanform.APP_ID),
            "Title": GLib.Variant("s", scanform.APP_NAME),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", icon),
            "IconThemePath": GLib.Variant("s", ""),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (icon, [], scanform.APP_NAME, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _item_call(self, connection, sender, path, interface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.on_action("open")
        invocation.return_value(None)  # ContextMenu/Scroll: the host shows the menu itself

    # --- com.canonical.dbusmenu -----------------------------------------------

    def _menu_property(self, connection, sender, path, interface, name):
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return values.get(name)

    def _menu_call(self, connection, sender, path, interface, method, params, invocation):
        layout = scanform.tray_menu_layout(self.enabled)
        items = {item[0]: item[1] for item in layout[2]}
        items[0] = layout[1]
        if method == "GetLayout":
            parent = params.unpack()[0]
            item_id, props, children = layout if parent == 0 else next(
                (c for c in layout[2] if c[0] == parent), (parent, {}, []))
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (
                self.revision,
                (item_id, {k: to_variant(v) for k, v in props.items()},
                 [layout_variant(child) for child in children]))))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or list(items)
            result = [(i, {k: to_variant(v) for k, v in items[i].items()})
                      for i in ids if i in items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = items.get(item_id, {}).get(name, "")
            invocation.return_value(GLib.Variant("(v)", (to_variant(value),)))
        elif method == "Event":
            item_id, event_id = params.unpack()[:2]
            if event_id == "clicked":
                self._activate(item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _time in params.unpack()[0]:
                if event_id == "clicked":
                    self._activate(item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _activate(self, item_id):
        action = scanform.tray_action(item_id)
        if action and self.enabled.get(action, True):
            # Not inside the D-Bus handler: the action may open windows or quit.
            def run():
                self.on_action(action)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(run)
