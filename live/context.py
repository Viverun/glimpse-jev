"""Session context budget: how full the planner's window is, and compacting a session when it nears the limit.

GPT-6-luna on Azure accepts about 1.05M tokens of context (~922K of input). jev defaults to a 256K working
window (cheaper and faster per planner call); `/window` raises it up to LIMIT. Once a session's context passes
`compact_at` (98%) of the window, the older turns are folded into a summary by Luna and only the last few
turns stay verbatim. Token counts are estimated (~3.5 characters per token) and corrected by the usage
the API reports on each planner call.
"""

import math

import config
import sessions

LIMIT = 922_000
DEFAULT_WINDOW = 256_000
DEFAULT_COMPACT_AT = 0.98
DEFAULT_OVERHEAD = 15_000  # system prompt, desktop profile, accessibility tree and screenshot of one planner call
KEEP_TURNS = 2
CHUNK_TOKENS = 120_000

SUMMARIZE = """You compact the history of a computer-use session on a GNOME desktop so work can continue in a smaller context.
Write a faithful summary that keeps: what the user asked for and why, every file path, URL, account, name and number that
may be needed again, what was finished and verified, what failed and the lesson, the state apps were left in, and anything
still open. Drop chatter and step-by-step detail. Be concrete, no more than 700 words.
Reply as JSON: {"summary": "..."}"""


def tokens(text: str) -> int:
    return math.ceil(len(text) / 3.5)


def window() -> int:
    return max(8_000, min(int(config.get("window") or DEFAULT_WINDOW), LIMIT))


def compact_at() -> float:
    return min(max(float(config.get("compact_at") or DEFAULT_COMPACT_AT), 0.5), 1.0)


def parse_size(text: str) -> int | None:
    """'256k', '1m', 'max', '300000' -> tokens (None if not understood)."""
    t = text.strip().lower().replace(",", "").replace("_", "")
    if t == "max":
        return LIMIT
    try:
        n = float(t[:-1]) * (1_000_000 if t.endswith("m") else 1_000) if t[-1:] in ("k", "m") else float(t)
    except ValueError:
        return None
    return int(n) if n >= 8_000 else None


def overhead(s: dict) -> int:
    """Tokens a planner call adds on top of the session context, learned from the last call's reported usage."""
    seen = s.get("usage")
    if not seen:
        return DEFAULT_OVERHEAD
    return min(max(seen["prompt_tokens"] - seen["context_tokens"], 6_000), window() // 4)


def used(s: dict) -> int:
    return tokens(sessions.context(s)) + overhead(s)


def fill(s: dict) -> float:
    return used(s) / window()


def needs_compact(s: dict) -> bool:
    return len(s["turns"]) - s.get("compacted", 0) > KEEP_TURNS and fill(s) >= compact_at()


def note_usage(s: dict, prompt_tokens: int) -> None:
    """Remember what the planner's last call really cost, so the estimate corrects itself."""
    if prompt_tokens:
        s["usage"] = {"prompt_tokens": prompt_tokens, "context_tokens": tokens(sessions.context(s))}


def _render(turns: list[dict]) -> str:
    return "\n\n".join(f"User asked: {t['prompt']}\nResult ({t['status']}): {t['report'] or 'no report'}" for t in turns)


def compact(s: dict, luna_json, focus: str = "", keep: int = KEEP_TURNS) -> tuple[int, int]:
    """Fold all but the last `keep` turns (and any earlier summary) into one summary. Returns (tokens before, after).
    If Luna is unreachable, falls back to one trimmed line per turn, so a session never gets stuck full."""
    before = tokens(sessions.context(s))
    start, end = s.get("compacted", 0), max(len(s["turns"]) - keep, s.get("compacted", 0))
    old = s["turns"][start:end]
    if not old:
        return before, before
    summary, chunk, size = s.get("summary", ""), [], 0
    try:
        for t in old + [None]:
            if t is not None:
                chunk.append(t)
                size += tokens(t["prompt"] + t["report"])
            if chunk and (t is None or size >= CHUNK_TOKENS):
                text = (f"Summary so far:\n{summary}\n\n" if summary else "") + "New turns to fold in:\n" + _render(chunk)
                if focus:
                    text += f"\n\nPay special attention to: {focus}"
                summary = (luna_json(SUMMARIZE, text).get("summary") or "").strip() or summary
                chunk, size = [], 0
    except Exception:
        summary = (summary + "\n" if summary else "") + "\n".join(
            f"- {t['prompt'][:120]} → {t['status']}: {' '.join(t['report'].split())[:160]}" for t in old)
    s["summary"], s["compacted"] = summary, end
    s.pop("usage", None)
    sessions.save(s)
    return before, tokens(sessions.context(s))
