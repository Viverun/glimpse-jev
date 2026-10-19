"""A soft grey glow around the screen edges while glimpse works (like Siri's edge light, but grey and quiet).

Same technique as toast.py: override-redirect GTK3 windows on XWayland, one per monitor, transparent, on top,
and clicks pass through them. They never take focus, are not in the window list, and are not in glimpse's
window screenshots, so Jev never sees them. The glow breathes slowly by changing the window opacity (the
picture is drawn once). It fades out on SIGTERM, and it exits by itself if glimpse ends without stopping it.

  python glow.py            # try it for 6 seconds
"""

import os
import signal
import subprocess
import sys
import time

DEPTH = 34          # glow depth in logical pixels
COLOR = (0.78, 0.80, 0.84)  # cool grey: glimpse is working
PAUSED = (0.92, 0.38, 0.38)  # soft red: glimpse is paused because you are using the computer
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "glimpse-paused-until")


def set_paused_until(t: float) -> None:
    """Tell the glow until when glimpse is paused (wall-clock time; 0 = not paused)."""
    try:
        with open(STATE, "w") as f:
            f.write(f"{t:.2f}")
    except OSError:
        pass


def paused_now() -> bool:
    try:
        with open(STATE) as f:
            return time.time() < float(f.read() or 0)
    except (OSError, ValueError):
        return False
PEAK = 0.42         # alpha at the very edge
BREATH = 3.2        # seconds per breath
LOW, HIGH = 0.55, 1.0  # opacity range of a breath


def start() -> subprocess.Popen | None:
    """Show the glow until stop(). None if it cannot be shown (no XWayland display, GLIMPSE_NO_GLOW set...)."""
    if os.environ.get("GLIMPSE_NO_GLOW") or not os.environ.get("DISPLAY"):
        return None
    restore = ""
    if not os.environ.get("GLIMPSE_NO_CURSOR"):
        try:  # glimpse's own cursor while it works; the daemon puts yours back when the glow ends
            import cursor
            restore = cursor.current_theme()
            cursor.set_theme(cursor.install_theme(restore))
        except Exception:
            restore = ""
    try:
        return subprocess.Popen(["/usr/bin/python3", __file__, "--daemon", str(os.getpid()), restore],
                                env={**os.environ, "GDK_BACKEND": "x11"}, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()  # fades out in ~0.4 s, then exits


def daemon(parent: int, restore_cursor: str = "") -> None:
    import math

    import cairo
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    def edge(cr, w, h):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        r, g, b = PAUSED if paused[0] else COLOR
        for x0, y0, x1, y1 in ((0, 0, 0, DEPTH), (0, h, 0, h - DEPTH), (0, 0, DEPTH, 0), (w, 0, w - DEPTH, 0)):
            grad = cairo.LinearGradient(x0, y0, x1, y1)
            grad.add_color_stop_rgba(0.0, r, g, b, PEAK)
            grad.add_color_stop_rgba(0.35, r, g, b, PEAK * 0.45)
            grad.add_color_stop_rgba(1.0, r, g, b, 0.0)
            cr.set_source(grad)
            if y0 == y1:  # left or right edge
                cr.rectangle(min(x0, x1), 0, DEPTH, h)
            else:
                cr.rectangle(0, min(y0, y1), w, DEPTH)
            cr.fill()
        cr.set_source_rgba(r, g, b, PEAK * 0.9)  # a thin bright rim
        cr.set_line_width(1.5)
        cr.rectangle(0.75, 0.75, w - 1.5, h - 1.5)
        cr.stroke()
        return False

    wins, paused = [], [False]
    disp = Gdk.Display.get_default()
    for i in range(disp.get_n_monitors()):
        geo = disp.get_monitor(i).get_geometry()
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_title("glimpse-glow")
        win.set_app_paintable(True)
        win.set_decorated(False)
        win.set_keep_above(True)
        win.set_accept_focus(False)
        win.set_skip_taskbar_hint(True)
        win.set_visual(win.get_screen().get_rgba_visual())
        win.move(geo.x, geo.y)
        win.set_default_size(geo.width, geo.height)
        win.connect("draw", lambda w, cr, g=geo: edge(cr, g.width, g.height))
        win.connect("realize", lambda w: w.get_window().input_shape_combine_region(cairo.Region(), 0, 0))
        win.set_opacity(0.0)
        win.show_all()
        wins.append(win)

    born, ending = time.monotonic(), [None]

    def watch_pause():
        now = paused_now()
        if now != paused[0]:
            paused[0] = now
            for w in wins:
                w.queue_draw()  # repaint in red (paused) or grey (working)
        return True

    def tick():
        now = time.monotonic()
        breath = LOW + (HIGH - LOW) * (0.5 - 0.5 * math.cos(2 * math.pi * (now - born) / BREATH))
        level = min(1.0, (now - born) / 0.5) * breath  # 0.5 s fade-in
        if ending[0] is not None:
            left = 1 - (now - ending[0]) / 0.4
            if left <= 0:
                Gtk.main_quit()
                return False
            level *= left
        for w in wins:
            w.set_opacity(level)
        return True

    def alive():
        try:
            os.kill(parent, 0)  # glimpse still running?
        except OSError:
            ending[0] = ending[0] or time.monotonic()
        return True

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: ending.__setitem__(0, ending[0] or time.monotonic()) or True)
    GLib.timeout_add(33, tick)
    GLib.timeout_add(150, watch_pause)
    GLib.timeout_add(1000, alive)
    try:
        Gtk.main()
    finally:
        if restore_cursor and restore_cursor != "glimpse-cursor":  # your own cursor back
            subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "cursor-theme", restore_cursor], capture_output=True)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--daemon":
        daemon(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
    else:
        p = start()
        time.sleep(6)
        stop(p)
