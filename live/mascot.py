"""glimpse's mascot: a round pixel face, light on the left and darker on the right, like ◐.

The same pixels draw it in the terminal (half-block characters: one row of text holds two rows of pixels)
and as assets/logo.svg.
"""

from rich.text import Text

# L light half, D dark half, e eye, . empty
PIXELS = [
    "...LLDDD...",
    ".LLLLDDDDD.",
    "LLLeLLDDeDD",
    "LLLeLLDDeDD",
    "LLLLLDDDDDD",
    ".LLLLDDDDD.",
    "...LLDDD...",
    "...........",
]
COLORS = {"L": "#d4d7dd", "D": "#8b9099", "e": "#26282d"}


def text() -> list[Text]:
    """Four lines of text that draw the face."""
    lines = []
    for top, bottom in zip(PIXELS[0::2], PIXELS[1::2]):
        t = Text()
        for a, b in zip(top, bottom):
            ca, cb = COLORS.get(a), COLORS.get(b)
            if ca and cb:
                t.append("▀", style=f"{ca} on {cb}")
            elif ca:
                t.append("▀", style=ca)
            elif cb:
                t.append("▄", style=cb)
            else:
                t.append(" ")
        lines.append(t)
    return lines


def svg(px: int = 16) -> str:
    rows = [r for r in PIXELS if r.strip(".")]
    w, h = len(rows[0]) * px, len(rows) * px
    rects = "".join(f'<rect x="{x * px}" y="{y * px}" width="{px}" height="{px}" fill="{COLORS[c]}"/>'
                    for y, row in enumerate(rows) for x, c in enumerate(row) if c in COLORS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'shape-rendering="crispEdges">{rects}</svg>\n')


if __name__ == "__main__":
    from rich.console import Console
    for line in text():
        Console().print(line)
