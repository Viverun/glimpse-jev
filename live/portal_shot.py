"""Take a screenshot through the XDG desktop portal (works on GNOME Wayland) and print the file path.

Run with the system python (needs PyGObject): /usr/bin/python3 portal_shot.py
"""

import sys
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

bus = Gio.bus_get_sync(Gio.BusType.SESSION)
loop = GLib.MainLoop()
result = {}


def on_response(conn, sender, path, iface, signal, params):
    code, results = params.unpack()
    result["code"], result["uri"] = code, results.get("uri")
    loop.quit()


token = "glimse"
sender = bus.get_unique_name()[1:].replace(".", "_")
handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
bus.signal_subscribe(None, "org.freedesktop.portal.Request", "Response", handle, None, Gio.DBusSignalFlags.NONE, on_response)
bus.call_sync(
    "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Screenshot", "Screenshot",
    GLib.Variant("(sa{sv})", ("", {"handle_token": GLib.Variant("s", token), "interactive": GLib.Variant("b", False)})),
    None, Gio.DBusCallFlags.NONE, -1, None,
)
GLib.timeout_add_seconds(60, loop.quit)
loop.run()
if result.get("code") != 0 or not result.get("uri"):
    sys.exit(f"portal screenshot failed: {result}")
print(unquote(urlparse(result["uri"]).path))
