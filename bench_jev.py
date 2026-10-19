"""Measure Jev latency and accuracy on the questions the agent loop depends on.

Cases:
  text_noul     one text-only Noul (network + model floor)
  image_noul    one Noul on a screenshot, at full and half width
  checkpoint    the 4-question checkpoint (step ok, blocked, next target, Box) in one request
  box           a single Box question, scored as a click hit against the DOM box

Run `uv run fixtures.py` first.
"""

import argparse
import asyncio
import io
import json
import statistics
import time
from pathlib import Path

from PIL import Image as PILImage
from typesafe_sdk import AsyncTypeSafeClient, Box, Choice, Image, Noul

FIXTURES = Path(__file__).parent / "fixtures"
IMAGES_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}

# Expected answers per screen. None means the question has no clear answer there.
EXPECTED = {
    "compose_empty": {"filled": False, "blocked": False, "next": None},
    "compose_filled": {"filled": True, "blocked": False, "next": "send"},
    "compose_modal": {"filled": True, "blocked": True, "next": "cancel_dialog"},
    "compose_sent": {"filled": False, "blocked": False, "next": None},
}


def jpeg(path: Path, width: int) -> bytes:
    img = PILImage.open(path).convert("RGB")
    if img.width != width:
        img = img.resize((width, round(img.height * width / img.width)), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def checkpoint_questions(with_box: bool) -> dict:
    questions = {
        "filled": Noul(
            instructions="In `screen`, is the email compose form's To field filled in with an email address?"
        ),
        "blocked": Noul(
            instructions="In `screen`, is a dialog, popup or overlay covering the app and blocking interaction with it?"
        ),
        "next": Choice(
            instructions=(
                "The goal is to send the email drafted in `screen`. Which element should be clicked next "
                "to make progress toward that goal?"
            ),
            criteria={
                "send": "The Send button of the compose form.",
                "discard": "The Discard button of the compose form.",
                "attach": "The Attach file button.",
                "cancel_dialog": "A Cancel button inside an open dialog, to dismiss it.",
                "none": "No element should be clicked: the email is already sent or the form is empty.",
            },
        ),
    }
    if with_box:
        questions["send_box"] = Box(instructions="the blue Send button in `screen`")
    return questions


def hit(pred: dict, truth: dict) -> bool:
    cx = (pred["left"] + pred["right"]) / 2
    cy = (pred["top"] + pred["bottom"]) / 2
    return truth["left"] <= cx <= truth["right"] and truth["top"] <= cy <= truth["bottom"]


async def timed(client: AsyncTypeSafeClient, **kwargs):
    start = time.perf_counter()
    response = await client.system_one(**kwargs)
    return (time.perf_counter() - start) * 1000, response


def summarize(name: str, ms: list[float]) -> dict:
    ms = sorted(ms)
    p90 = ms[min(len(ms) - 1, round(0.9 * (len(ms) - 1)))]
    row = {"case": name, "n": len(ms), "p50_ms": round(statistics.median(ms)), "p90_ms": round(p90), "min_ms": round(ms[0])}
    print(f"{name:<28} n={row['n']:<3} p50={row['p50_ms']:>5} ms  p90={row['p90_ms']:>5} ms  min={row['min_ms']:>5} ms")
    return row


async def main(model: str, repeats: int) -> None:
    truth = json.loads((FIXTURES / "truth.json").read_text())
    screens = list(EXPECTED)
    results, accuracy = [], {"filled": [], "blocked": [], "next": [], "box_hit": [], "box_conf": []}

    async with AsyncTypeSafeClient(model=model, headers=IMAGES_HEADER) as client:
        # Warm the connection so TLS setup isn't counted.
        await client.system_one("warmup", {"q": Noul(instructions="Is this a warmup?")})

        ms = []
        for _ in range(repeats):
            t, _r = await timed(
                client,
                state={"screen_text": "MailDesk compose form. To: alex@example.com. Subject: Q3 report."},
                questions={"filled": Noul(instructions="Is the To field in `screen_text` filled in?")},
            )
            ms.append(t)
        results.append(summarize("text_noul", ms))

        for width in (1280, 640):
            ms = []
            for screen in screens:
                data = jpeg(FIXTURES / f"{screen}.png", width)
                for _ in range(repeats):
                    t, r = await timed(
                        client,
                        state={"screen": Image(data)},
                        questions={"blocked": checkpoint_questions(False)["blocked"]},
                    )
                    ms.append(t)
            results.append(summarize(f"image_noul@{width}px", ms))

        for width in (1280, 640):
            ms = []
            for screen in screens:
                data = jpeg(FIXTURES / f"{screen}.png", width)
                for _ in range(repeats):
                    t, r = await timed(client, state={"screen": Image(data)}, questions=checkpoint_questions(True))
                    ms.append(t)
                    exp = EXPECTED[screen]
                    accuracy["filled"].append((r.nouls["filled"].noul > 0.5) == exp["filled"])
                    accuracy["blocked"].append((r.nouls["blocked"].noul > 0.5) == exp["blocked"])
                    if exp["next"] is not None:
                        accuracy["next"].append(r.choices["next"].choice == exp["next"])
                    box = r.answers["send_box"]
                    accuracy["box_hit"].append(hit(box.box.model_dump(), truth[screen]["send"]))
                    accuracy["box_conf"].append(box.confidence)
            results.append(summarize(f"checkpoint4@{width}px", ms))

        ms, hits = [], []
        targets = {"send": "the blue Send button", "attach": "the Attach file button", "subject": "the Subject text field"}
        data = jpeg(FIXTURES / "compose_filled.png", 1280)
        for key, desc in targets.items():
            for _ in range(repeats):
                t, r = await timed(client, state={"screen": Image(data)}, questions={"b": Box(instructions=f"{desc} in `screen`")})
                ms.append(t)
                hits.append(hit(r.answers["b"].box.model_dump(), truth["compose_filled"][key]))
        results.append(summarize("box@1280px", ms))

    print("\naccuracy")
    for key in ("filled", "blocked", "next"):
        vals = accuracy[key]
        print(f"  {key:<10} {sum(vals)}/{len(vals)}")
    print(f"  box_hit    {sum(accuracy['box_hit'])}/{len(accuracy['box_hit'])} (checkpoint), {sum(hits)}/{len(hits)} (single Box, 3 targets)")
    print(f"  box_conf   mean {statistics.mean(accuracy['box_conf']):.2f}")

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    (out / f"bench_{model}.json").write_text(
        json.dumps({"model": model, "latency": results, "accuracy": {k: sum(v) / len(v) for k, v in accuracy.items() if v}}, indent=2)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="jev-preview")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(main(args.model, args.repeats))
