"""Synchronous client for the computer-use-linux MCP server (AT-SPI tree, portal/uinput input)."""

import asyncio
import json
import re
import threading
import time
from pathlib import Path



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


class DesktopMCP:
    def __init__(self, command: str = "computer-use-linux", args: tuple[str, ...] = ("mcp",)):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._ready = threading.Event()
        self._stop = None
        from mcp import StdioServerParameters  # loaded on first use: it takes ~0.6 s to import
        asyncio.run_coroutine_threadsafe(self._serve(StdioServerParameters(command=command, args=list(args))), self._loop)
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
        args = {k: v for k, v in args.items() if v is not None}
        started = time.time()
        try:
            return self._call(tool, args)
        finally:
            if tool in ("get_app_state", "screenshot") and args.get("include_screenshot", True):
                clean_portal_screenshots(since=started - 1)

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
