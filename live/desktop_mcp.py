"""Synchronous client for the computer-use-linux MCP server (AT-SPI tree, portal/uinput input)."""

import asyncio
import base64
import json
import re
import threading
import time
from pathlib import Path

import frames



PICTURES = Path.home() / "Pictures"
PORTAL_NAME = re.compile(r"Screenshot-\d+\.png")


def clean_portal_screenshots(since: float) -> int:
    """Delete screenshots the desktop portal saved to ~/Pictures for us.

    Program-requested portal screenshots are saved as Screenshot-<n>.png; only those created since
    `since` are removed, so the user's own "Screenshot From ..." files are never touched.
    """
    removed = 0
    for f in PICTURES.glob("Screenshot-*.png") if PICTURES.exists() else []:
        try:
            if PORTAL_NAME.fullmatch(f.name) and f.stat().st_mtime >= since:
                f.unlink()
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def _plain(v):
    """numpy numbers -> Python numbers (JSON cannot carry numpy types)."""
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except (TypeError, ValueError):
            return v
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


MUTATING = {"click", "type_text", "press_key", "scroll", "drag", "perform_action", "set_value", "activate_window",
            "move_window", "resize_window"}


class DesktopMCP:
    def __init__(self, command: str = "computer-use-linux", args: tuple[str, ...] = ("mcp",)):
        self._wins: dict = {}  # the last window list, for fast screenshots of the focused window
        self._verified, self._mismatches, self._fast_off = False, 0, False  # the fast path's one-time self-check
        threading.Thread(target=frames.source, daemon=True).start()  # the frame daemon warms up meanwhile
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._ready = threading.Event()
        self._stop = None
        from mcp import StdioServerParameters  # loaded on first use: it takes ~0.6 s to import
        import os
        # glimpse draws its own edge glow and cursor: computer-use-linux 0.7.12+ would add a second overlay.
        # Remember the "Allow remote interaction" grant across runs and reboots (GNOME asks once, then reuses it).
        env = {**os.environ, "COMPUTER_USE_LINUX_INDICATOR": "0", "COMPUTER_USE_LINUX_PERSIST_REMOTE_DESKTOP": "1"}
        asyncio.run_coroutine_threadsafe(self._serve(StdioServerParameters(command=command, args=list(args), env=env)), self._loop)
        if not self._ready.wait(30):
            raise RuntimeError("computer-use-linux MCP did not start")

    async def _serve(self, params):
        self._stop = asyncio.Event()
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.session = session
                self._ready.set()
                await self._stop.wait()

    def call(self, tool: str, **args) -> dict:
        args = {k: _plain(v) for k, v in args.items() if v is not None}
        started = time.time()
        if tool in MUTATING:
            frames.mark_action()
        if tool == "get_app_state" and args.get("include_screenshot", True):
            fast = self._fast_state(args)
            if fast is not None:
                return fast
        try:
            out = self._call(tool, args)
            if tool == "list_windows":
                self._wins = {w.get("window_id"): w for w in out.get("windows", [])}
            return out
        finally:
            if tool in ("get_app_state", "screenshot") and args.get("include_screenshot", True):
                clean_portal_screenshots(since=started - 1)

    def _check(self, img: bytes, portal: bytes) -> bool:
        """Do the two pictures of the window match (centre 84%, grayscale)? Margins are left out: the glow tints them."""
        from io import BytesIO

        from PIL import Image
        thumbs = []
        for data in (img, portal):
            im = Image.open(BytesIO(data)).convert("L")
            w, h = im.size
            thumbs.append(im.crop((int(w * .08), int(h * .08), int(w * .92), int(h * .92))).resize((96, 54), Image.BILINEAR).tobytes())
        diffs = [abs(a - b) for a, b in zip(*thumbs)]
        return sum(diffs) / len(diffs) < 6 and sorted(diffs)[int(len(diffs) * .98)] < 60

    def _fast_state(self, args: dict) -> dict | None:
        """get_app_state with the screenshot from the screencast (milliseconds, no flash) instead of the portal.
        Only for the focused window, whose pixels are the ones on screen; None to fall back to the portal."""
        if self._fast_off:
            return None
        w = self._wins.get(args.get("window_id")) if args.get("window_id") is not None else None
        if not w or not w.get("focused") or not w.get("bounds"):
            return None
        fs = frames.source()
        if fs is None:
            return None
        try:
            got = fs.window_shot(w["bounds"], after=frames.last_action(), fmt=args.get("format") or "png",
                                 quality=int(args.get("quality") or 70), max_width=int(args.get("max_width") or 1920))
        except Exception:
            return None
        if got is None:
            return None
        img, meta = got
        if not self._verified:
            # once per session: the fast picture must match the portal's picture of the same window, else the fast
            # path stays off (two tries, in case the screen changed between the two pictures)
            try:
                ref = self._call("get_app_state", {**args, "include_screenshot": True, "max_nodes": 1, "max_depth": 1})
                ok = bool(ref.get("_images")) and self._check(img, base64.b64decode(ref["_images"][0]))
            except Exception:
                ok = False
            if not ok:
                self._mismatches += 1
                if self._mismatches >= 2:
                    self._fast_off = True
                return None
            self._verified = True
        if (args.get("max_nodes") or 600) <= 1:  # only the picture was wanted: no call to computer-use-linux at all
            out = {"accessibility_tree": []}
        else:
            out = self._call("get_app_state", {**args, "include_screenshot": False})
        out["_images"] = [base64.b64encode(img).decode()]
        out["screenshot"] = meta
        return out

    def _call(self, tool: str, args: dict) -> dict:
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(tool, args), self._loop)
        result = fut.result(120)
        text = "".join(getattr(c, "text", "") for c in result.content)
        images = [c.data for c in result.content if getattr(c, "type", "") == "image"]  # base64
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            raise RuntimeError(f"{tool} failed: {text[:300]}")
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            out = {"text": text}
        if images and isinstance(out, dict):
            out["_images"] = images
        return out

    def close(self) -> None:
        if self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
