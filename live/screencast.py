"""A live view of the screen (XDG ScreenCast portal + PipeWire), for real-time steps that need many frames a second.

Screenshots through the portal take 0.3-1 s each and flash the screen; a screencast streams frames with no flash.
GNOME asks once to allow screen sharing; the approval is kept (restore token in ~/.local/state/glimpse), so later
runs start without a dialog.

Needs the system Python (python3-gi, GStreamer with pipewiresrc, python3-dbus, numpy).

  with Screencast() as cast:
      frame = cast.frame()   # numpy RGB array (height, width, 3) of the whole screen, the newest frame
"""

import os
import threading
import time
from pathlib import Path

TOKEN = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "glimpse" / "screencast.token"


def _portal_stream() -> tuple[int, int]:
    """Start a ScreenCast portal session; returns (PipeWire fd, node id)."""
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    portal = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
    cast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")
    sender = bus.get_unique_name()[1:].replace(".", "_")
    loop, state, n = GLib.MainLoop(), {}, [0]

    def request(method, *args, opts):
        n[0] += 1
        token = f"glimpse{os.getpid()}_{n[0]}"
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        done = {}

        def on_response(code, results):
            done.update(code=int(code), results=results)
            loop.quit()
        bus.add_signal_receiver(on_response, "Response", "org.freedesktop.portal.Request", path=path)
        getattr(cast, method)(*args, {**opts, "handle_token": token})
        loop.run()
        if done.get("code") != 0:
            raise RuntimeError(f"screen sharing was not allowed ({method})")
        return done["results"]

    r = request("CreateSession", opts={"session_handle_token": f"glimpse{os.getpid()}"})
    session = r["session_handle"]
    opts = {"types": dbus.UInt32(1), "multiple": False, "cursor_mode": dbus.UInt32(1), "persist_mode": dbus.UInt32(2)}
    if TOKEN.exists():
        opts["restore_token"] = TOKEN.read_text().strip()
    request("SelectSources", session, opts=opts)
    r = request("Start", session, "", opts={})
    if r.get("restore_token"):
        TOKEN.parent.mkdir(parents=True, exist_ok=True)
        TOKEN.write_text(str(r["restore_token"]))
        TOKEN.chmod(0o600)
    node = int(r["streams"][0][0])
    fd = cast.OpenPipeWireRemote(session, {}).take()
    state["session"] = session
    return fd, node


class Screencast:
    def __init__(self):
        self._frame = None
        self._lock = threading.Lock()
        self.frames = 0

    def __enter__(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        fd, node = _portal_stream()
        self.pipe = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={node} always-copy=true ! videoconvert ! video/x-raw,format=RGB "
            "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
        self.pipe.get_by_name("sink").connect("new-sample", self._sample)
        self.pipe.set_state(Gst.State.PLAYING)
        t0 = time.monotonic()
        while self._frame is None and time.monotonic() - t0 < 10:
            time.sleep(0.02)
        if self._frame is None:
            raise RuntimeError("the screencast sent no frames")
        return self

    def _sample(self, sink):
        import numpy as np
        from gi.repository import Gst
        s = sink.emit("pull-sample")
        caps = s.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = s.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if ok:
            stride = len(m.data) // h
            arr = np.frombuffer(m.data, np.uint8).reshape(h, stride)[:, : w * 3].reshape(h, w, 3).copy()
            buf.unmap(m)
            with self._lock:
                self._frame = arr
                self.frames += 1
        return Gst.FlowReturn.OK

    def frame(self):
        with self._lock:
            return self._frame

    def __exit__(self, *exc):
        from gi.repository import Gst
        self.pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    with Screencast() as c:
        n0, t0 = c.frames, time.monotonic()
        time.sleep(3)
        f = c.frame()
        print(f"{f.shape[1]}x{f.shape[0]}, {(c.frames - n0) / (time.monotonic() - t0):.0f} frames/s")
