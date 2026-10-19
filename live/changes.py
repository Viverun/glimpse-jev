"""What changed on screen after an action, and whether the agent is going in circles.

Adapted from jev-browser (pageDiff, insertedText, repeatsBlock) and jev-ultrafast (fingerprints, stuck
detection) for desktop windows: elements come from the AT-SPI tree, pixels from window screenshots.
"""

import hashlib
import io

STATES = ("checked", "selected", "pressed", "expanded", "focused")


def _str(v) -> str:
    if isinstance(v, dict):  # AT-SPI text comes as {"content": ..., "caret": ...}
        v = v.get("content")
    return v if isinstance(v, str) else ""


def ident(e: dict) -> str:
    name = (_str(e.get("name")) or _str(e.get("text")) or _str(e.get("description"))).strip()
    return f"{e.get('role')} {name[:50]!r}"


def state(e: dict) -> str:
    st = [s for s in (e.get("states") or []) if s in STATES]
    v = e.get("value")
    val = f"value={str(v)[:40]!r}" if v not in (None, "", e.get("name")) else ""
    return " ".join(x for x in [val, ",".join(st)] if x)


def snapshot(title: str, els: list[dict], text: str) -> dict:
    """The parts of an observation a diff compares (taken only from a freshly read tree)."""
    return {"title": title or "", "elements": [(ident(e), state(e)) for e in els], "text": text or ""}


def inserted_text(a: str, b: str, max_chars: int = 300) -> str:
    """Word runs in b that are not in a (LCS over words), e.g. "Saved | 3 items"."""
    A, B = a.split()[:600], b.split()[:600]
    n, m = len(A), len(B)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if A[i] == B[j] else max(dp[i + 1][j], dp[i][j + 1])
    runs, cur, i, j = [], [], 0, 0
    while j < m:
        if i < n and A[i] == B[j]:
            if cur:
                runs.append(" ".join(cur))
                cur = []
            i, j = i + 1, j + 1
        elif i < n and dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            cur.append(B[j])
            j += 1
    if cur:
        runs.append(" ".join(cur))
    return " | ".join(runs)[:max_chars]


def diff(a: dict | None, b: dict) -> dict | None:
    """Elements added / removed / changed (value, checked, selected, focused...), title and new text.
    Duplicates count: two "Bold" buttons are two elements. None if there is nothing to compare with."""
    if a is None:
        return None
    left, pairs, d = list(a["elements"]), [], {"added": [], "removed": [], "changed": []}
    for e in b["elements"]:
        k = next((i for i, x in enumerate(left) if x[0] == e[0]), None)
        if k is None:
            d["added"].append(f"{e[0]} {e[1]}".strip())
        else:
            pairs.append((left.pop(k), e))
    for old, new in pairs:
        if old[1] != new[1]:
            d["changed"].append(f"{new[0]}: {old[1] or '(empty)'} -> {new[1] or '(empty)'}")
    d["removed"] = [f"{e[0]} {e[1]}".strip() for e in left]
    d = {k: v[:15] for k, v in d.items() if v}
    if a["title"] != b["title"]:
        d["title"] = f"{a['title']!r} -> {b['title']!r}"
    ins = inserted_text(a["text"], b["text"])
    if ins:
        d["new_text"] = ins
    return d


def describe_diff(d: dict | None) -> str:
    if d is None:
        return ""
    if not d:
        return "nothing visible changed in the window's elements"
    lines = []
    if "title" in d:
        lines.append(f"window title: {d['title']}")
    for k in ("added", "removed", "changed"):
        if d.get(k):
            lines.append(f"{k}: " + "; ".join(d[k]))
    if d.get("new_text"):
        lines.append(f"new text: {d['new_text']}")
    return "\n".join(lines)[:1500]


# --- pixels ----------------------------------------------------------------------------------------
def thumb(jpeg: bytes | None, size: tuple[int, int] = (64, 36)) -> bytes | None:
    """A tiny grayscale version of a screenshot, for cheap 'did anything change' comparisons."""
    if not jpeg:
        return None
    from PIL import Image
    return Image.open(io.BytesIO(jpeg)).convert("L").resize(size, Image.BILINEAR).tobytes()


def similar(a: bytes | None, b: bytes | None, mean_tol: float = 1.5, max_tol: int = 40) -> bool:
    """Same picture, give or take a blinking caret or anti-aliasing noise."""
    if a is None or b is None or len(a) != len(b):
        return False
    diffs = [abs(x - y) for x, y in zip(a, b)]
    return sum(diffs) / len(diffs) < mean_tol and max(diffs) < max_tol


def fingerprint(title: str, els: list[dict], th: bytes | None) -> str:
    """Identity of a screen state: title, elements with their states, and coarse pixels."""
    h = hashlib.sha1((title or "").encode())
    for e in els:
        h.update(f"{ident(e)}|{state(e)}\n".encode())
    if th:
        h.update(bytes(x // 32 for x in th[::4]))  # coarse: ignores noise, keeps layout
    return h.hexdigest()[:16]


# --- loops ----------------------------------------------------------------------------------------
def repeats_block(seq: list[str], k: int, times: int) -> bool:
    """True if the last k*times entries of seq are one k-long block repeated `times` times."""
    if len(seq) < k * times:
        return False
    tail = seq[-k * times:]
    block = tail[:k]
    if k > 1 and len(set(block)) == 1:  # identical single actions are counted by k = 1
        return False
    return all(tail[n * k:(n + 1) * k] == block for n in range(1, times))


def looping(seq: list[str]) -> list[str] | None:
    """The repeated block if the recent actions go in circles (A B A B A B, A B C x3, one action x8), or one
    target clicked 3 times within the last 6 actions (clicking the same thing again and again between other moves)."""
    for k, times in ((2, 3), (3, 3), (1, 8)):
        if repeats_block(seq, k, times):
            return seq[-k:]
    recent = seq[-6:]
    for a in dict.fromkeys(recent):
        if a.startswith("click") and recent.count(a) >= 3:
            return [a]
    return None
