"""Render synthetic app screens with Playwright and record ground-truth element boxes.

Synthetic screens keep real desktop content out of API requests and give exact
answers to score Jev against.
"""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "fixtures"

PAGE = """<!doctype html><html><head><style>
body{margin:0;font:14px system-ui,sans-serif;background:#eef1f5}
.bar{height:44px;background:#1f2937;color:#fff;display:flex;align-items:center;padding:0 16px;gap:16px}
.side{position:absolute;top:44px;left:0;bottom:0;width:200px;background:#fff;border-right:1px solid #ddd;padding:12px}
.side div{padding:8px;border-radius:6px}.side .on{background:#dbeafe}
.main{position:absolute;top:44px;left:200px;right:0;bottom:0;padding:24px}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;max-width:720px}
label{display:block;margin:10px 0 4px;color:#555}
input,textarea{width:100%;box-sizing:border-box;padding:8px;border:1px solid #bbb;border-radius:6px;font:inherit}
textarea{height:160px}
button{padding:8px 16px;border-radius:6px;border:1px solid #bbb;background:#fff;font:inherit;margin-top:12px}
#send{background:#2563eb;color:#fff;border-color:#2563eb}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center}
.modal{background:#fff;border-radius:10px;padding:20px;width:360px}
.toast{position:fixed;bottom:20px;right:20px;background:#16a34a;color:#fff;padding:12px 16px;border-radius:8px}
</style></head><body>
<div class="bar"><b>MailDesk</b><span>Inbox</span><span>Calendar</span><span style="margin-left:auto">jamie@example.com</span></div>
<div class="side"><div id="compose-nav" class="on">Compose</div><div>Inbox (12)</div><div>Sent</div><div>Drafts</div><div>Trash</div></div>
<div class="main"><div class="card">
<label>To</label><input id="to" value="{to}">
<label>Subject</label><input id="subject" value="{subject}">
<label>Message</label><textarea id="body">{body}</textarea>
<button id="send">Send</button> <button id="discard">Discard</button> <button id="attach">Attach file</button>
</div></div>
{extra}
</body></html>"""

MODAL = """<div class="overlay"><div class="modal"><b>Discard this draft?</b>
<p>Your message will be lost.</p><button id="cancel">Cancel</button> <button id="confirm">Discard</button></div></div>"""
TOAST = '<div class="toast" id="toast">Message sent</div>'

SCREENS = {
    "compose_empty": dict(to="", subject="", body="", extra=""),
    "compose_filled": dict(
        to="alex@example.com", subject="Q3 report", body="Hi Alex, the Q3 report is attached.", extra=""
    ),
    "compose_modal": dict(
        to="alex@example.com", subject="Q3 report", body="Hi Alex, the Q3 report is attached.", extra=MODAL
    ),
    "compose_sent": dict(to="", subject="", body="", extra=TOAST),
}

TARGETS = ["send", "discard", "attach", "to", "subject", "confirm", "cancel"]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    truth = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for name, fields in SCREENS.items():
            html = PAGE
            for key, value in fields.items():
                html = html.replace("{" + key + "}", value)
            page.set_content(html)
            page.screenshot(path=OUT / f"{name}.png")
            boxes = {}
            for target in TARGETS:
                el = page.query_selector(f"#{target}")
                if el and (b := el.bounding_box()):
                    boxes[target] = {
                        "left": b["x"] / 1280,
                        "top": b["y"] / 800,
                        "right": (b["x"] + b["width"]) / 1280,
                        "bottom": (b["y"] + b["height"]) / 800,
                    }
            truth[name] = boxes
        browser.close()
    (OUT / "truth.json").write_text(json.dumps(truth, indent=2))
    print(f"wrote {len(SCREENS)} screens to {OUT}")


if __name__ == "__main__":
    main()
