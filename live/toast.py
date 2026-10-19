"""jev's own pop-ups: small see-through cards in the bottom-left corner, instead of GNOME notifications.

GNOME Wayland has no layer-shell, so the cards are override-redirect GTK3 windows on XWayland: they can
sit at exact screen coordinates, stay on top, ignore clicks and be translucent. A tiny daemon (system
python, which has PyGObject) owns the stack of cards; `show()` hands it one JSON line over a unix socket
and starts it on first use. The daemon exits after a minute with nothing to show.

  python toast.py "Done in 12 s" "saved notes.txt" done      # try it
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "jev-toast.sock"
ACCENTS = {"jev": (0.49, 0.36, 1.0), "working": (0.38, 0.65, 1.0), "done": (0.30, 0.85, 0.50),
           "stopped": (1.0, 0.42, 0.42), "info": (0.49, 0.36, 1.0)}
GLYPHS = {"working": "●", "done": "✔", "stopped": "■", "jev": "◆", "info": "◆"}
WIDTH, MARGIN, GAP, PAD, BORDER = 360, 24, 10, 16, 3
MAX_MS = 2650  # on screen at most ~3 s in total: 200 ms fade-in + this + 350 ms fade-out
FONT = "JetBrainsMono Nerd Font, JetBrains Mono"


def show(title: str, body: str = "", state: str = "jev", ms: int = MAX_MS) -> bool:
    """Ask the daemon to show a card (starting it if needed). False if no card could be shown."""
    msg = (json.dumps({"title": title, "body": body, "state": state, "ms": min(ms, MAX_MS)}) + "\n").encode()
    for attempt in range(2):
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(1)
                s.connect(str(SOCK))
                s.sendall(msg)
            return True
        except OSError:
            if attempt:
                return False
            SOCK.unlink(missing_ok=True)
            env = {**os.environ, "GDK_BACKEND": "x11"}
            subprocess.Popen(["/usr/bin/python3", __file__, "--daemon"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            for _ in range(30):
                time.sleep(0.1)
                if SOCK.exists():
                    break
    return False


def daemon() -> None:
    import cairo
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

    cards: list = []

    def layout(cr, text, size, weight, width):
        lay = PangoCairo.create_layout(cr)
        lay.set_font_description(Pango.FontDescription(f"{FONT} {weight} {size}"))
        lay.set_width(width * Pango.SCALE)
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_ellipsize(Pango.EllipsizeMode.END)
        lay.set_height(-4 if text is not None else 0)  # at most 4 lines
        lay.set_text(text or "", -1)
        return lay

    class Card(Gtk.Window):
        def __init__(self, title, body, state):
            super().__init__(type=Gtk.WindowType.POPUP)
            self.title_s, self.body_s, self.state = title, body, state
            self.set_app_paintable(True)
            self.set_decorated(False)
            self.set_keep_above(True)
            self.set_accept_focus(False)
            self.set_skip_taskbar_hint(True)
            self.set_visual(self.get_screen().get_rgba_visual())
            probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
            inner = WIDTH - 2 * PAD - 22
            self.t_lay = layout(probe, title, 10, "Bold", inner)
            self.b_lay = layout(probe, body, 9, "", inner) if body else None
            h = 2 * PAD + self.t_lay.get_pixel_size()[1] + (self.b_lay.get_pixel_size()[1] + 4 if self.b_lay else 0)
            self.card_h = max(h, 52)
            self.set_default_size(WIDTH, self.card_h)
            self.connect("draw", self.on_draw)
            self.connect("realize", lambda w: w.get_window().input_shape_combine_region(cairo.Region(), 0, 0))  # clicks pass through
            self.set_opacity(0.0)
            self.born = time.monotonic()

        def on_draw(self, _w, cr):
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            r, bw = 3, BORDER
            x0, y0, x1, y1 = bw / 2, bw / 2, WIDTH - bw / 2, self.card_h - bw / 2  # stroke stays inside the window
            cr.new_sub_path()
            for cx, cy, a in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
                cr.arc(cx, cy, r, a * 3.14159 / 180, (a + 90) * 3.14159 / 180)
            cr.close_path()
            cr.set_source_rgba(0.06, 0.06, 0.09, 0.42)  # see-through dark glass
            cr.fill_preserve()
            ar, ag, ab = ACCENTS.get(self.state, ACCENTS["jev"])
            cr.set_source_rgba(ar, ag, ab, 0.6)
            cr.set_line_width(bw)
            cr.stroke()
            cr.move_to(PAD + 6, PAD)
            cr.set_source_rgba(ar, ag, ab, 1)
            glyph = PangoCairo.create_layout(cr)
            glyph.set_font_description(Pango.FontDescription(f"{FONT} Bold 10"))
            glyph.set_text(GLYPHS.get(self.state, "◆"), -1)
            PangoCairo.show_layout(cr, glyph)
            cr.move_to(PAD + 28, PAD)
            cr.set_source_rgba(1, 1, 1, 0.97)
            PangoCairo.show_layout(cr, self.t_lay)
            if self.b_lay:
                cr.move_to(PAD + 28, PAD + self.t_lay.get_pixel_size()[1] + 4)
                cr.set_source_rgba(1, 1, 1, 0.74)
                PangoCairo.show_layout(cr, self.b_lay)
            return False

    def place():
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        geo = mon.get_geometry()
        y = geo.y + geo.height - MARGIN
        for c in cards:  # newest at the bottom, older ones pushed up
            y -= c.card_h
            c.move(geo.x + MARGIN, y)
            y -= GAP

    def add(msg):
        c = Card(msg.get("title", ""), msg.get("body", ""), msg.get("state", "jev"))
        c.ms = min(int(msg.get("ms", MAX_MS)), MAX_MS)
        cards.insert(0, c)
        c.show_all()
        place()
        # keep them stacked: show() may reposition after the window maps
        GLib.timeout_add(60, lambda: place() or False)
        GLib.timeout_add(16, tick, c)
        return False

    def tick(c):
        age = (time.monotonic() - c.born) * 1000
        if age < 200:
            c.set_opacity(age / 200)
        elif age > c.ms:
            fade = 1 - (age - c.ms) / 350
            if fade <= 0:
                cards.remove(c)
                c.destroy()
                place()
                return False
            c.set_opacity(fade)
        else:
            c.set_opacity(1.0)
        return True

    srv = socket.socket(socket.AF_UNIX)
    SOCK.unlink(missing_ok=True)
    srv.bind(str(SOCK))
    srv.listen(8)
    srv.setblocking(False)
    idle = [time.monotonic()]

    def accept(*_):
        try:
            while True:
                conn, _ = srv.accept()
                conn.settimeout(0.5)
                data = b""
                try:
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                conn.close()
                for line in data.splitlines():
                    try:
                        GLib.idle_add(add, json.loads(line))
                    except ValueError:
                        pass
        except BlockingIOError:
            pass
        return True

    def watchdog():
        if cards:
            idle[0] = time.monotonic()
        elif time.monotonic() - idle[0] > 60:
            SOCK.unlink(missing_ok=True)
            Gtk.main_quit()
        return True

    GLib.io_add_watch(srv.fileno(), GLib.IO_IN, accept)
    GLib.timeout_add_seconds(5, watchdog)
    Gtk.main()


if __name__ == "__main__":
    if sys.argv[1:] == ["--daemon"]:
        daemon()
    else:
        a = sys.argv[1:] + ["jev", "", "jev"][len(sys.argv[1:]):]
        print(show(a[0], a[1], a[2]))
