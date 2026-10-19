"""Frame daemon: keeps a screencast open and publishes the newest frame in shared memory, so glimpse reads the
screen in milliseconds instead of a portal screenshot per look (0.3-1 s, with a flash).

Runs in the system Python (GStreamer + PyGObject); glimpse starts it through frames.py and reads the memory with
numpy. Two slots, so a reader never sees a half-written frame. Exits when glimpse (its parent) is gone.

Layout of /dev/shm/glimpse-frames-<uid>:  header (64 bytes) + 2 slots of max_w * max_h * 3 bytes (RGB)
  header: magic "GLFR", current slot u32, seq u64, time f64, width u32, height u32,
          logical width u32, logical height u32, monitor x i32, monitor y i32, daemon pid u32
"""

import mmap
import os
import struct
import sys
import time

HEADER = struct.Struct("<4sIQdIIIIiiI")  # ... monitor x, y, daemon pid
SIZE = 64
MAX_W, MAX_H = 3840, 2400


def path() -> str:
    return f"/dev/shm/glimpse-frames-{os.getuid()}"


def serve(parent: int) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gi
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk

    import screencast
    display = Gdk.Display.get_default()
    if display.get_n_monitors() != 1:  # the stream shows one monitor: crops could come from the wrong one
        return
    geo = display.get_monitor(0).get_geometry()  # logical pixels (GNOME scaling)
    slot_bytes = MAX_W * MAX_H * 3
    fd = os.open(path(), os.O_RDWR | os.O_CREAT, 0o600)
    os.ftruncate(fd, SIZE + 2 * slot_bytes)
    mem = mmap.mmap(fd, SIZE + 2 * slot_bytes)
    seq, slot = 0, 0

    class Cast(screencast.Screencast):
        def _sample(self, sink):
            nonlocal seq, slot
            from gi.repository import Gst
            s = sink.emit("pull-sample")
            caps = s.get_caps().get_structure(0)
            w, h = caps.get_value("width"), caps.get_value("height")
            if w > MAX_W or h > MAX_H:
                return Gst.FlowReturn.OK
            buf = s.get_buffer()
            ok, m = buf.map(Gst.MapFlags.READ)
            if ok:
                stride = len(m.data) // h
                slot ^= 1
                off = SIZE + slot * slot_bytes
                if stride == w * 3:
                    mem[off:off + w * h * 3] = m.data[: w * h * 3]
                else:
                    import numpy as np
                    rows = np.frombuffer(m.data, np.uint8).reshape(h, stride)[:, : w * 3]
                    mem[off:off + w * h * 3] = rows.tobytes()
                buf.unmap(m)
                seq += 1
                mem[:HEADER.size] = HEADER.pack(b"GLFR", slot, seq, time.time(), w, h, geo.width, geo.height, geo.x, geo.y, os.getpid())
                self.frames += 1
                self._frame = True  # Screencast.__enter__ waits for the first frame
            return Gst.FlowReturn.OK

    try:
        with Cast():
            while True:
                time.sleep(1)
                try:
                    os.kill(parent, 0)
                except ProcessLookupError:
                    break
    finally:
        mem.close()
        os.close(fd)
        try:
            os.unlink(path())
        except OSError:
            pass


if __name__ == "__main__":
    serve(int(sys.argv[1]))
