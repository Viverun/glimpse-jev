"""Real-time controllers: glimpse's "play" step hands a fast game to a small loop that reads a live screencast many
times a second and presses keys on its own (a planner that sees one screenshot every few seconds cannot steer it).

glimpse runs this only when a plan asks for it, in the system Python (GStreamer + PyGObject):

  /usr/bin/python3 live/realtime.py snake --seconds 120     # prints one JSON line: {"ok", "eaten", "seconds", ...}

snake: a grid snake game (dark snake and food on a light board inside a dark frame, arrow keys), such as
playsnake.org. The game must already be on screen and focused, at its countdown or running.
"""

import argparse
import json
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import screencast  # noqa: E402

KEYS = {"up": 103, "down": 108, "left": 105, "right": 106}
STEP = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
BACK = {"up": "down", "down": "up", "left": "right", "right": "left"}


def press(direction: str) -> None:
    code = KEYS[direction]
    subprocess.run(["ydotool", "key", f"{code}:1", f"{code}:0"], env=screencast_env(), capture_output=True)


def screencast_env() -> dict:
    import os
    return {**os.environ, "YDOTOOL_SOCKET": f"/run/user/{os.getuid()}/.ydotool_socket"}


def dark_mask(f: np.ndarray) -> np.ndarray:
    f = f.astype(np.int32)
    lum = f.sum(2)
    # darker than the board and tinted like it (olive on green): not the grey or black window frame
    return (lum < 0.45 * np.median(lum)) & (f[..., 1] - f[..., 2] > 12)


def find_board(f: np.ndarray) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) inside the board's dark frame lines."""
    d = dark_mask(f)
    h, w = d.shape
    rows = np.nonzero(d.sum(1) > 0.15 * w)[0]
    cols = np.nonzero(d.sum(0) > 0.15 * h)[0]
    if len(rows) < 2 or len(cols) < 2:
        return None
    top, bottom, left, right = rows[0], rows[-1], cols[0], cols[-1]
    while top + 1 < h and d[top + 1, left:right].mean() > 0.5:
        top += 1
    while bottom - 1 > 0 and d[bottom - 1, left:right].mean() > 0.5:
        bottom -= 1
    while left + 1 < w and d[top:bottom, left + 1].mean() > 0.5:
        left += 1
    while right - 1 > 0 and d[top:bottom, right - 1].mean() > 0.5:
        right -= 1
    if right - left < 100 or bottom - top < 100:
        return None
    return left + 1, top + 1, right, bottom


def find_pitch(d: np.ndarray) -> float | None:
    """Grid pitch: the distance between the starts of neighbouring snake segments (a segment plus its small gap).
    None while the board shows text (a countdown, GAME OVER), which has many more dark pixels."""
    if d.mean() > 0.04:
        return None
    gaps = []
    for m in (d, d.T):
        for row in m:
            x = np.flatnonzero(np.diff(row.astype(np.int8)) == 1)
            gaps += [b - a for a, b in zip(x, x[1:]) if 12 < b - a < 120]
    return float(np.median(gaps)) if len(gaps) >= 10 else None


class Grid:
    def __init__(self, box, cols, rows):
        l, t, r, b = box
        self.box, self.cols, self.rows = box, cols, rows
        self.cw, self.ch = (r - l) / cols, (b - t) / rows

    @classmethod
    def fit(cls, box, pitch: float) -> "Grid":
        """The column count closest to the pitch whose rows also come out whole (cells are square)."""
        l, t, r, b = box
        w, h = r - l, b - t

        def cost(cols):
            rows = h * cols / w
            return abs(w / cols - pitch) / pitch + 2 * abs(rows - round(rows))
        cols = min(range(4, int(w / 8)), key=cost)
        return cls(box, cols, round(h * cols / w))

    def fractions(self, f: np.ndarray, inset: float = 0.2) -> np.ndarray:
        """Dark share of each cell's inner square (an integral image: a few ms for the whole board)."""
        l, t, r, b = self.box
        d = dark_mask(f[t:b, l:r]).astype(np.int32)
        ii = np.pad(d.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        xs, ys = np.arange(self.cols) * self.cw, np.arange(self.rows) * self.ch
        x0, x1 = (xs + self.cw * inset).astype(int), (xs + self.cw * (1 - inset)).astype(int)
        y0, y1 = (ys + self.ch * inset).astype(int), (ys + self.ch * (1 - inset)).astype(int)
        area = np.maximum(1, np.outer(y1 - y0, x1 - x0))
        return (ii[np.ix_(y1, x1)] - ii[np.ix_(y0, x1)] - ii[np.ix_(y1, x0)] + ii[np.ix_(y0, x0)]) / area

    def read(self, f: np.ndarray) -> tuple[set, set]:
        """(snake cells, food cells): a solid dark cell is snake, a half-dark one (light centre) is food."""
        fr, mid = self.fractions(f), self.fractions(f, inset=0.42)
        snake = {(int(x), int(y)) for y, x in zip(*np.nonzero((fr > 0.75) & (mid > 0.5)))}
        food = {(int(x), int(y)) for y, x in zip(*np.nonzero((fr > 0.2) & ~((fr > 0.75) & (mid > 0.5))))}
        return snake, food  # the food (a flower) has a light centre; a snake segment is solid


def choose(grid: Grid, head, snake: set, food: set, heading: str | None) -> str:
    """Shortest safe path to the food; with none, the move that keeps the most room."""
    def free(c):
        return 0 <= c[0] < grid.cols and 0 <= c[1] < grid.rows and c not in snake

    moves = [m for m in STEP if m != BACK.get(heading)]
    if food:
        seen, q = {head: None}, deque([head])
        while q:
            c = q.popleft()
            if c in food:
                while seen[c] and seen[c][0] != head:
                    c = seen[c][0]
                return seen[c][1]
            for m in (moves if c == head else STEP):
                n = (c[0] + STEP[m][0], c[1] + STEP[m][1])
                if n not in seen and free(n):
                    seen[n] = (c, m)
                    q.append(n)

    def room(start):
        seen, q = {start}, deque([start])
        while q and len(seen) < 400:
            c = q.popleft()
            for dx, dy in STEP.values():
                n = (c[0] + dx, c[1] + dy)
                if n not in seen and free(n):
                    seen.add(n)
                    q.append(n)
        return len(seen)
    options = [(room(n), m) for m in moves if free(n := (head[0] + STEP[m][0], head[1] + STEP[m][1]))]
    return max(options)[1] if options else (heading or "up")


def snake(seconds: float = 120) -> dict:
    t0 = time.monotonic()
    with screencast.Screencast() as cast:
        grid = None
        while time.monotonic() - t0 < 15 and grid is None:  # wait out a countdown until the snake is on the board
            f = cast.frame()
            box = find_board(f)
            if box:
                l, t, r, b = box
                pitch = find_pitch(dark_mask(f)[t:b, l:r])
                if pitch:
                    grid = Grid.fit(box, pitch)
            time.sleep(0.05)
        if grid is None:
            return {"ok": False, "error": "no snake board on screen"}
        prev, head, heading, pressed, start_len, best, last_move = set(), None, None, None, 0, 0, time.monotonic()
        while time.monotonic() - t0 < seconds:
            s, food = grid.read(cast.frame())
            if s and s != prev:
                new = s - prev
                if new and prev:
                    old = head or min(prev, key=lambda p: min(abs(p[0] - c[0]) + abs(p[1] - c[1]) for c in new))
                    # the head moved away from where it was: with a skipped frame, the farthest new cell
                    head = max(new, key=lambda c: abs(c[0] - old[0]) + abs(c[1] - old[1]))
                    dx, dy = np.sign(head[0] - old[0]), np.sign(head[1] - old[1])
                    heading = next((m for m, v in STEP.items() if v == (dx, dy)), heading)
                start_len = start_len or len(s)
                best = max(best, len(s))
                prev, last_move = s, time.monotonic()
                if head:
                    m = choose(grid, head, s - {head}, food, heading)
                    if m != heading and m != pressed:
                        press(m)
                        pressed = m
                    elif m == heading:
                        pressed = None
            elif time.monotonic() - last_move > 3:
                break  # nothing moved for 3 s: game over
            time.sleep(0.005)
    return {"ok": True, "eaten": max(0, best - start_len), "length": best, "grid": f"{grid.cols}x{grid.rows}",
            "seconds": round(time.monotonic() - t0, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("game", choices=["snake"])
    ap.add_argument("--seconds", type=float, default=120)
    a = ap.parse_args()
    print(json.dumps(snake(a.seconds)), flush=True)
