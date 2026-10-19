"""Unit tests for jev (no desktop, no network): parsing, safety, memory, transcripts, and the agent loop
driven by a fake desktop and a scripted planner."""

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "live"))
os.environ.setdefault("TYPESAFE_API_KEY", "test")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://example.invalid/openai/v1")

import agent_output  # noqa: E402
import changes  # noqa: E402
import hybrid_agent as ha  # noqa: E402
import jev_agent as ja  # noqa: E402
import memory  # noqa: E402
import notify  # noqa: E402
import takeover  # noqa: E402
from jev_cli import app_in_prompt, duration_minutes  # noqa: E402


# --- parsing --------------------------------------------------------------------------------------
@pytest.mark.parametrize("prompt,minutes", [
    ("work on my project for 3 hours", 180), ("for 2h keep improving it", 120), ("spend 90 minutes on tests", 90),
    ("run 1.5 hours", 90), ("check my email", None), ("add 2 headers to the doc", None), ("list 5 movies", None),
])
def test_duration(prompt, minutes):
    assert duration_minutes(prompt) == minutes


APPS = {"text editor": "org.gnome.TextEditor", "files": "org.gnome.Nautilus", "libreoffice calc": "libreoffice-calc",
        "zen browser": "zen", "web browser": "firefox", "firefox": "firefox", "videos": "org.gnome.Totem"}


@pytest.mark.parametrize("prompt,app", [
    ("In Text Editor write hi", "org.gnome.TextEditor"),
    ("make the header bold in LibreOffice Calc", "libreoffice-calc"),
    ("send a mail to a@b.com with Testing", "zen"),
    ("open github.com/anthropics", "zen"),
    ("In the web browser open wikipedia.org", "zen"),
    ("open firefox and go to example.com", "firefox"),
    ("In Text Editor create three files", "org.gnome.TextEditor"),  # longest match wins over "files"
])
def test_app_routing(prompt, app):
    assert app_in_prompt(prompt, APPS)[1] == app


def test_text_spans():
    assert ja.text_spans("In Text Editor write 'Hello, world.' and save as /home/u/a.txt") == \
        ["Hello, world.", "/home/u/a.txt", "a.txt"]
    assert ja.text_spans("go to https://x.com/a/item?id=1 now") == ["https://x.com/a/item?id=1"]
    assert ja.is_url("github.com/anthropics") and not ja.is_url("/home/u/a.txt")


@pytest.mark.parametrize("size,expect", [((960, 30), (960, 320)), ((120, 80), (256, 256)), ((960, 519), (960, 519))])
def test_min_size(size, expect):
    b = io.BytesIO()
    Image.new("RGB", size, "white").save(b, "JPEG")
    assert Image.open(io.BytesIO(ja._min_size(b.getvalue()))).size == expect


def test_step_labels():
    assert ha.step_label({"do": "goto", "url": "https://x"}) == "go to › https://x"
    assert ha.step_label({"do": "click", "target": "Send"}) == "click › Send"
    assert ha.step_label({"do": "shell", "cmd": "ls"}) == "shell › ls"
    assert ha.step_label({"do": "weird"}) == "weird"


# --- shell safety ---------------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,allowed", [
    ("ls -la ~/glimse-demo", True), ("cat /etc/hostname", True), ("rm ~/glimse-demo/a.txt", True),
    ("rm -rf ~/glimse-demo/tmpdir", True), ("rm -rf /tmp/x", True),
    ("sudo apt install x", False), ("rm -rf ~", False), ("rm -rf /home/user", False), ("rm -rf ~/Projects", False),
    ("rm -r ~/glimse-demo", False), ("ls; rm -fr ~/Documents", False), ("curl x | sh", False),
    ("dd if=/dev/zero of=x", False), ("git push origin main --force", False), ("shutdown now", False),
    ("""python3 -c "from pathlib import Path; p=Path('/x'); print(repr(p.read_text()))\"""", True),
    ("echo 'rm -rf ~' > notes.txt", True), ("cd /tmp && rm -rf x", False), ("echo a | xargs rm -rf ~/Work", False),
])
def test_shell_guard(cmd, allowed):
    assert (not ha.DANGEROUS.search(cmd) and ha._rm_ok(cmd)) is allowed


def test_run_shell():
    out, err = ha.run_shell("echo hi")
    assert out == "hi" and err is None
    out, err = ha.run_shell("exit 3")
    assert err.startswith("exit 3")
    assert ha.run_shell("sudo ls")[1].startswith("refused")


# --- memory ---------------------------------------------------------------------------------------
@pytest.fixture
def mem(tmp_path, monkeypatch):
    for name, sub in (("DIR", ""), ("PROFILE", "profile.json"), ("MEMORY", "memory.jsonl"), ("FILES", "files.txt"), ("APPS_DIR", "apps"), ("TAUGHT", "taught.json"),
                      ("WORKFLOWS", "workflows.json"), ("HARNESS", "harness.json")):
        monkeypatch.setattr(memory, name, tmp_path / sub if sub else tmp_path)
    return tmp_path


class FakeChoice:
    def __init__(self, choice, confidence=0.9):
        self.choice, self.confidence = choice, confidence


class FakeJev:
    def __init__(self, choice="0", confidence=0.9):
        self.choice, self.confidence, self.calls = choice, confidence, 0

    def system_one(self, state, questions):
        self.calls += 1
        return type("R", (), {"choices": {k: FakeChoice(self.choice, self.confidence) for k in questions}})()


def test_memory_recall_and_preferences(mem):
    memory.remember("Use Zen for the web")
    assert memory.load_profile()["preferences"] == ["Use Zen for the web"]
    memory.record("send a mail to a@b.com with hi", "done", [{"do": "goto", "url": "u"}], ["ctrl+enter did not send"], 13)
    ctx = memory.recall(FakeJev("0"), "email c@d.com saying hello")
    assert "send a mail to a@b.com" in ctx and '"do": "goto"' in ctx and "ctrl+enter did not send" in ctx
    assert memory.recall(FakeJev("none"), "something else") == "Lessons from recent runs:\n- ctrl+enter did not send"
    memory.forget(0)
    assert memory.load_profile()["preferences"] == []


def test_file_index_search(mem):
    memory.FILES.write_text("/h/Projects/Probability_Grade/\n/h/Projects/Probability_Grade/docs/PROBABILITY.md\n/h/Music/song.mp3\n")
    hits = memory.find_files("open my probability grade notes")
    assert hits[0] == "/h/Projects/Probability_Grade/" and "/h/Music/song.mp3" not in hits


def test_app_knowledge(mem):
    memory.learn_app("org.gnome.TextEditor.desktop", "t", [{"role": "button", "name": "Save As…"}],
                     worked=[{"do": "key", "key": "ctrl+s"}], dialog="portal dialog")
    memory.learn_app("org.gnome.TextEditor.desktop", "t", [], worked=[{"do": "key", "key": "ctrl+s"}])  # no duplicate
    k = memory.app_knowledge(["org.gnome.TextEditor.desktop"])
    worked = next(line for line in k.splitlines() if "steps that worked" in line)
    assert "button: Save As…" in k and worked.count("ctrl+s") == 1 and "portal dialog" in k
    assert k.startswith("How-tos for this desktop")  # curated playbook comes first


def test_playbooks(mem):
    assert any('"do": "select"' in t for t in memory.tips_for("libreoffice-calc.desktop"))
    assert any("view=cm" in t for t in memory.tips_for(None, "send a mail to a@b.com"))
    assert any("wait_until" in t for t in memory.tips_for(None, "work with Claude on it"))
    assert memory.tips_for("unknown.app") == []


# --- complete agent replies from transcripts ------------------------------------------------------
def _line(t, content, side=False):
    return json.dumps({"type": t, "isSidechain": side, "message": {"role": t, "content": content}})


def test_claude_code_reply(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "other.jsonl").write_text(_line("user", "unrelated question") + "\n" + _line("assistant", [{"type": "text", "text": "nope"}]))
    (proj / "s.jsonl").write_text("\n".join([
        _line("user", "Please add tests for memory.py and run them"),
        _line("assistant", [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Adding tests."},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]),
        _line("user", [{"type": "tool_result", "content": "3 passed"}]),
        _line("assistant", [{"type": "text", "text": "All 3 tests pass."}]),
        _line("user", "next instruction"),
        _line("assistant", [{"type": "text", "text": "should not be included"}]),
    ]))
    monkeypatch.setattr(agent_output, "TRANSCRIPTS", tmp_path)
    r = agent_output.claude_code_reply("Please add tests for memory.py and run them")
    assert r == "Adding tests.\n\n[tool: Bash pytest -q]\n\nAll 3 tests pass."
    assert agent_output.claude_code_reply("never sent this instruction") is None


def test_notify_plain():
    assert notify._plain("**Done**\n\n- **A**: b\n* c") == "Done\n• A: b\n• c"


# --- the agent loop ------------------------------------------------------------------------------
class FakeMCP:
    def call(self, tool, **a):
        return {"windows": []}


class FakeDesktop:
    mcp = FakeMCP()
    last_text = ""

    def windows(self):
        return [{"window_id": 1, "title": "Doc", "app_id": "", "pid": 1, "focused": True}]


def make_agent(plans, step_results=None, checks=None, **kw):
    """HybridAgent with a scripted planner, scripted step outcomes and scripted checks."""
    a = ha.HybridAgent(FakeDesktop(), on_step=lambda i: None, **kw)
    a.task_window = FakeDesktop().windows()[0]
    plans, steps, chk = list(plans), list(step_results or []), list(checks or [])
    a.plan = lambda *x, **k: plans.pop(0) if plans else {"status": "done", "report": "end", "steps": []}
    a.observe = lambda need_tree=True: ([], None)
    a.step = lambda s, els, shot: (steps.pop(0) if steps else None)
    a.check = lambda expect, shot: (chk.pop(0) if chk else 1.0)
    a.settle = lambda *x, **k: True
    return a


def test_loop_done():
    r = make_agent([{"status": "working", "steps": [{"do": "key", "key": "a"}]}, {"status": "done", "report": "**ok**", "steps": []}]).run("t")
    assert r["status"] == "done" and r["report"] == "**ok**" and r["steps"] == 1


def test_loop_recovers_from_failure_and_exception():
    plans = [{"status": "working", "steps": [{"do": "click", "target": "x"}]},
             {"status": "working", "steps": [{"do": "goto"}]},  # malformed: missing url
             {"status": "working", "steps": [{"do": "key", "key": "enter"}]},
             {"status": "done", "report": "r", "steps": []}]
    a = make_agent(plans, step_results=["could not find 'x'"])
    real_step = a.step
    calls = {"n": 0}

    def step(s, els, shot):
        calls["n"] += 1
        if s.get("do") == "goto":
            raise KeyError("url")
        return real_step(s, els, shot)
    a.step = step
    r = a.run("t")
    assert r["status"] == "done" and len(a.failures) == 2 and any("KeyError" in f for f in a.failures)


def test_loop_impossible_needs_two_votes():
    a = make_agent([{"status": "impossible", "reason": "no app"}, {"status": "working", "steps": [{"do": "key", "key": "a"}]},
                    {"status": "impossible", "reason": "no app"}, {"status": "impossible", "reason": "really no app"}])
    r = a.run("t")
    assert r["status"] == "impossible: really no app"


def test_loop_stuck_after_ten_failed_rounds():
    plans = [{"status": "working", "steps": [{"do": "click", "target": "x"}]} for _ in range(12)]
    r = make_agent(plans, step_results=["nope"] * 12).run("t")
    assert r["status"].startswith("stuck")


def test_loop_timeboxed_keeps_working(monkeypatch):
    """Goal met with time left: the run continues with more work, and only ends when the time is used up."""
    clock = {"t": 0.0}
    monkeypatch.setattr(ha.time, "perf_counter", lambda: clock.__setitem__("t", clock["t"] + 20) or clock["t"])
    monkeypatch.setattr(ha.time, "sleep", lambda s: None)
    plans = [{"status": "done", "report": "early", "steps": []},
             {"status": "working", "steps": [{"do": "key", "key": "a"}]}] + [{"status": "done", "report": "late", "steps": []}] * 50
    a = make_agent(plans, max_minutes=10, timeboxed=True)
    tasks = []
    orig = a.plan
    a.plan = lambda task, *x: (tasks.append(task), orig(task, *x))[1]
    r = a.run("goal")
    assert "[time-boxed]" in tasks[1]          # told to keep working after the early "done"
    assert r["steps"] >= 1                      # and actually did more work
    assert r["status"] == "done" and clock["t"] >= 8 * 60   # ended only when < 2 min were left


def test_check_failure_retries_then_fails(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(ha.time, "perf_counter", lambda: clock.__setitem__("t", clock["t"] + 0.5) or clock["t"])
    a = make_agent([{"status": "working", "steps": [{"do": "click", "target": "x", "expect": ["it opened"]}]},
                    {"status": "done", "report": "r", "steps": []}], checks=[0.1, 0.2, 0.1, 0.1, 0.1])
    r = a.run("t")
    assert r["status"] == "done" and a.failures and "not seen" in a.failures[0]


def test_done_needs_passing_verification():
    """A 'done' whose verify command fails is rejected; work continues until a verified done."""
    plans = [{"status": "done", "report": "lied", "verify": "false", "steps": []},
             {"status": "working", "steps": [{"do": "key", "key": "a"}]},
             {"status": "done", "report": "real", "verify": "true", "steps": []}]
    a = make_agent(plans)
    r = a.run("t")
    assert r["status"] == "done" and r["report"] == "real" and r["steps"] == 1


def test_final_plan_with_failing_verify_continues():
    plans = [{"status": "working", "final": True, "verify": "exit 1", "report": "x", "steps": [{"do": "key", "key": "a"}]},
             {"status": "done", "report": "ok", "verify": "true", "steps": []}]
    r = make_agent(plans).run("t")
    assert r["status"] == "done" and r["report"] == "ok"


@pytest.mark.parametrize("cmd,ro", [
    ("ls -la ~/glimse-demo/bench", True), ("cat a.txt 2>&1", True), ("test -f a.txt && grep -qx hi a.txt", True),
    ("unzip -p F.ods content.xml | grep -c 'fo:font-weight=\"bold\"'", True),
    ("python3 -c \"import pandas as pd; print(pd.read_excel('F.xlsx'))\"", True),
    ("python3 - <<'PY'\nprint(open('a.txt').read())\nPY", True), ("find . -type f >/dev/null", True),
    ("echo hi > a.txt", False), ("mv a b", False), ("cp a b", False), ("mkdir x", False), ("sed -i s/a/b/ f", False),
    ("python3 - <<'PY'\nfrom openpyxl import Workbook\nwb=Workbook(); wb.save('x.xlsx')\nPY", False),
    ("python3 -c \"open('a','w').write('x')\"", False), ("python3 -c \"import shutil; shutil.move('a','b')\"", False),
    ("soffice --headless --convert-to ods a.csv", False), ("cat a | tee b", False),
    ("python3 -c \"import pandas as pd; d=pd.read_excel('s.xlsx'); assert len(d)>0\"", True),   # > inside code
    ("awk '$2 > 3' a.csv", True), ("python3 - <<'PY'\nif 2 > 1: print(open('a').read())\nPY", True),
    ("ls &>/dev/null", True), ("cat <<EOF > f.txt\nx\nEOF", False), ("echo \"a > b\" > out.txt", False),
])
def test_read_only_shell(cmd, ro):
    assert ha.is_read_only(cmd) is ro


def test_read_only_refusal_message():
    out, err = ha.run_shell("echo hi > /tmp/jev_ro_test.txt", read_only=True)
    assert err.startswith("refused: the task names an app")


def test_taught_lessons(mem):
    memory.teach("org.gnome.Nautilus.desktop", "Select by pattern: ctrl+s, type *.txt, enter")
    memory.teach("org.gnome.Nautilus.desktop", "Select by pattern: ctrl+s, type *.txt, enter")  # no duplicate
    memory.teach("general", "Prefer keyboard shortcuts")
    tips = memory.tips_for("org.gnome.Nautilus.desktop")
    assert tips.count("Select by pattern: ctrl+s, type *.txt, enter") == 1 and "Prefer keyboard shortcuts" in tips
    memory.untaught()
    assert "Prefer keyboard shortcuts" not in memory.tips_for("org.gnome.Nautilus.desktop")


def test_two_stage_selection_keeps_far_target():
    """With hundreds of elements, the target must still be offered even if it is far down the list."""
    a = make_agent([])
    els = [{"role": "menu item", "name": f"Item {i}", "actions": [{"name": "Click"}], "abs": {"x": 0, "y": 0, "width": 1, "height": 1}}
           for i in range(400)]
    els.append({"role": "menu item", "name": "Sort Descending", "actions": [{"name": "Click"}], "abs": {"x": 0, "y": 0, "width": 1, "height": 1}})
    offered = {}

    class Ask:
        def __call__(self, state, questions):
            offered.update(state["elements"])
            return type("R", (), {"choices": {"el": FakeChoice("none", 0.9)}})()
    a.ask = Ask()
    a.find("the Sort Descending menu item", els, None)
    assert any("Sort Descending" in v for v in offered.values()) and len(offered) <= 121


# --- what changed, loops, confirmation (from jev-browser / jev-ultrafast / Agent-S / AWM) ----------
def _el(role, name, value=None, states=()):
    return {"role": role, "name": name, "value": value, "states": list(states)}


def test_tree_diff():
    a = changes.snapshot("Untitled 1", [_el("push button", "Bold"), _el("text", "To"), _el("push button", "Send")], "Hello there")
    b = changes.snapshot("sales.ods", [_el("push button", "Bold", states=["pressed"]), _el("text", "To", "a@b.com"),
                                       _el("dialog", "Save")], "Hello there Saved 3 items")
    d = changes.diff(a, b)
    assert d["title"] == "'Untitled 1' -> 'sales.ods'"
    assert d["added"] == ["dialog 'Save'"] and d["removed"] == ["push button 'Send'"]
    assert "push button 'Bold': (empty) -> pressed" in d["changed"] and any("a@b.com" in c for c in d["changed"])
    assert d["new_text"] == "Saved 3 items"
    assert changes.diff(a, a) == {} and changes.describe_diff({}).startswith("nothing visible changed")
    assert changes.diff(None, b) is None and changes.describe_diff(None) == ""
    odd = {"role": "text", "name": None, "text": {"content": "A1", "caret": 0}, "value": {"x": 1}}  # AT-SPI shapes
    assert changes.ident(odd) == "text 'A1'" and changes.fingerprint("t", [odd], None)


def test_duplicate_elements_count():
    a = changes.snapshot("t", [_el("check box", "Todo"), _el("check box", "Todo")], "")
    b = changes.snapshot("t", [_el("check box", "Todo"), _el("check box", "Todo"), _el("check box", "Todo")], "")
    assert changes.diff(a, b)["added"] == ["check box 'Todo'"]


def test_inserted_text():
    assert changes.inserted_text("a b c", "a x b c y") == "x | y"
    assert changes.inserted_text("same words", "same words") == ""


def test_loop_detection():
    assert changes.looping(["a", "b", "a", "b", "a", "b"]) == ["a", "b"]
    assert changes.looping(["x", "a", "b", "c", "a", "b", "c", "a", "b", "c"]) == ["a", "b", "c"]
    assert changes.looping(["a"] * 8) == ["a"] and changes.looping(["a"] * 7) is None
    assert changes.looping(["a", "b", "c", "d"]) is None


def _jpeg(color, mark=None):
    img = Image.new("RGB", (960, 540), color)
    if mark:
        img.paste((0, 0, 0), mark)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def test_thumbs_ignore_a_caret_but_see_a_dialog():
    base = changes.thumb(_jpeg((240, 240, 240)))
    assert changes.similar(base, changes.thumb(_jpeg((240, 240, 240), (400, 200, 402, 218))))       # blinking caret
    assert not changes.similar(base, changes.thumb(_jpeg((240, 240, 240), (300, 150, 660, 390))))  # a dialog opened
    assert changes.fingerprint("t", [], base) == changes.fingerprint("t", [], base)


class FakeNoul:
    def __init__(self, p):
        self.noul = p


def test_check_combines_wordings_and_reads_errors():
    a = make_agent([])
    del a.check  # the real check
    a.last_change = "added: dialog 'Save'"
    asked = {}

    def ask(state, qs):
        asked.update(qs)
        vals = {"f0": 0.3, "c0": 0.8, "f1": 0.9, "c1": 0.2, "error": 0.75}
        return type("R", (), {"nouls": {k: FakeNoul(vals[k]) for k in qs}})()
    a.ask = ask
    assert a.check(["a dialog is open", "the name field is empty"], None) == pytest.approx(0.8)  # min over facts of max
    assert set(asked) == {"f0", "c0", "f1", "c1", "error"} and a.last_error == 0.75


def test_error_message_stops_waiting():
    plans = [{"status": "working", "steps": [{"do": "click", "target": "x", "expect": ["it opened"]}]},
             {"status": "done", "report": "r", "steps": []}]
    a = make_agent(plans, checks=[0.1])
    settled = []
    a.settle = lambda *x, **k: settled.append(1) or True

    def check(expect, shot):
        a.last_error = 0.9
        return 0.1
    a.check = check
    a.run("t")
    assert not settled and "an error message is showing" in a.failures[0]


def test_same_plan_on_same_screen_is_not_run_a_third_time():
    plans = [{"status": "working", "steps": [{"do": "click", "target": "x"}]}] * 3 + [{"status": "done", "report": "r", "steps": []}]
    a = make_agent(plans, step_results=["nope", "nope"])
    tasks, ran = [], []
    orig_plan, orig_step = a.plan, a.step
    a.plan = lambda task, *x: (tasks.append(task), orig_plan(task, *x))[1]
    a.step = lambda s, els, shot: (ran.append(s), orig_step(s, els, shot))[1]
    a.run("t")
    assert len(ran) == 2 and "REFLECTION" in tasks[3]


def test_three_actions_without_effect_fail_the_chunk():
    plans = [{"status": "working", "steps": [{"do": "click", "target": f"b{i}"} for i in range(5)]},
             {"status": "done", "report": "r", "steps": []}]
    a = make_agent(plans)
    a.unchanged = lambda: True
    a.run("t")
    # The first click that changes nothing already stops the chunk (the next steps would act on nothing).
    assert a.failures == ["click › b0: the click had no visible effect (the element may not be clickable this way): try another way"]


@pytest.mark.parametrize("score,confirm,done", [(0.7, 0.0, True), (0.5, 0.7, True), (0.5, 0.5, False), (0.2, 0.9, False)])
def test_final_confirmation_bands(score, confirm, done):
    plans = [{"status": "working", "final": True, "done_when": ["the sheet is sorted"], "report": "ok",
              "steps": [{"do": "key", "key": "a"}]},
             {"status": "working", "steps": [{"do": "key", "key": "b"}]}, {"status": "done", "report": "later", "steps": []}]
    a = make_agent(plans, checks=[score])
    a.confirm_done = lambda shot: confirm
    r = a.run("t")
    assert (r["report"] == "ok") is done


def test_missing_target_reports_closest_elements():
    a = make_agent([])
    del a.step  # the real step
    els = [{"role": "push button", "name": n, "abs": {"x": 0, "y": 0, "width": 1, "height": 1}} for n in ("Save", "Save As…", "Close")]

    class Ask:
        def __call__(self, state, questions):
            c = type("C", (), {"choice": "none", "confidence": 0.8, "probabilities": {"none": 0.5, "1": 0.3, "0": 0.15, "2": 0.05}})()
            return type("R", (), {"choices": {"el": c}})()
    a.ask = Ask()
    a.visual_click = lambda *x: False
    err = a.step({"do": "click", "target": "the Export button"}, els, None)
    assert err.startswith("could not find 'the Export button'; closest elements: push button") and err.index("Save As") < err.index("'Save'")


def test_workflow_induction_and_recall(mem):
    wf = {"name": "Save the open sheet in another format", "when": "save a spreadsheet as xlsx/csv", "app": "libreoffice-calc",
          "variables": {"path": "target file"}, "steps": [{"do": "open_file", "path": "{source}"}, {"do": "save_as", "path": "{path}"}]}
    seen = {}

    def luna_json(system, text):
        seen["text"] = text
        return dict(wf)
    steps = [{"do": "open_file", "path": "/a.csv"}, {"do": "save_as", "path": "/a.xlsx"}]
    assert memory.induce_workflow(luna_json, "convert a.csv to xlsx in calc", steps, "libreoffice-calc.desktop")
    assert "Known workflows:\n(none)" in seen["text"]
    assert memory.induce_workflow(luna_json, "convert b.csv to xlsx", steps, None) is None  # duplicate
    assert memory.induce_workflow(lambda *x: {"skip": True}, "t", steps, None) is None
    assert memory.induce_workflow(luna_json, "t", [{"do": "key", "key": "a"}], None) is None  # too short to reuse
    assert len(memory.load_workflows()) == 1
    ctx = memory.workflows_for(FakeJev("0"), "turn report.csv into an excel file")
    assert "Save the open sheet in another format" in ctx and "{path}" in ctx
    assert memory.workflows_for(FakeJev("none"), "check the weather") == ""
    assert memory.workflows_for(FakeJev("0", 0.3), "unsure") == ""


def test_save_as_follows_focus_into_an_open_portal_dialog(tmp_path):
    """ctrl+s in an earlier step opened the GNOME portal dialog: save_as must use the dialog, not type the path."""
    dialog = {"window_id": 2, "title": "Save a File", "app_id": "org.gnome.TextEditor.desktop", "wm_class": "org.gnome.Nautilus",
              "pid": 2, "focused": True}
    a = make_agent([])
    del a.step  # the real step
    a.d.windows = lambda: [dialog]
    a.observe = lambda need_tree=True: (setattr(a, "task_window", dialog), ([], None))[1]
    path = tmp_path / "out.txt"
    a.gtk.save_as = lambda win, p: (Path(p).write_text("x"), None)[1] if win is dialog else "wrong window"
    typed = []
    a.act = lambda c: typed.append(c)
    assert a.step({"do": "save_as", "path": str(path)}, [], None) is None
    assert path.exists() and not typed


# --- you take over: real input stops jev ----------------------------------------------------------
LISTING = """I: Bus=0011
N: Name="AT Translated Set 2 keyboard"
H: Handlers=sysrq kbd event3 leds

N: Name="ELAN0B00:00 04F3:3261 Touchpad"
H: Handlers=mouse1 event5

N: Name="Power Button"
H: Handlers=kbd event1

N: Name="ydotoold virtual device"
H: Handlers=sysrq rfkill kbd mouse2 event13

N: Name="sof-hda-dsp Headphone"
H: Handlers=event9
"""


def test_only_physical_input_devices_are_watched():
    assert takeover.physical_devices(LISTING) == [("AT Translated Set 2 keyboard", "/dev/input/event3"),
                                                  ("ELAN0B00:00 04F3:3261 Touchpad", "/dev/input/event5")]


def _watch(monkeypatch, events, combo="ctrl+alt+esc"):
    kills = []
    monkeypatch.setattr(takeover.os, "kill", lambda pid, sig: kills.append(sig))
    r, w = os.pipe()
    t = takeover.Takeover(combo, grace=0)
    t._fds, t._t0 = {r: "fake"}, time.monotonic()
    th = __import__("threading").Thread(target=t._watch)
    th.start()
    os.write(w, b"".join(takeover.EVENT.pack(0, 0, *e) for e in events))
    time.sleep(0.4)
    t._stop.set()
    th.join(2)
    os.close(w)
    os.close(r)
    return t, kills


CTRL, ALT, ESC, A = 29, 56, 1, 30


@pytest.mark.parametrize("events,stopped", [
    ([(1, CTRL, 1), (1, ALT, 1), (1, ESC, 1)], True),                    # the combination held
    ([(1, 97, 1), (1, 100, 1), (1, ESC, 1)], True),                      # right-hand modifiers count too
    ([(1, ESC, 1), (1, CTRL, 1), (1, ALT, 1)], True),                    # in any order
    ([(1, A, 1), (1, A, 0), (1, A, 2)], False),                          # ordinary typing never stops
    ([(1, CTRL, 1), (1, ESC, 1)], False),                                # a partial combination
    ([(1, CTRL, 1), (1, ALT, 1), (1, CTRL, 0), (1, ESC, 1)], False),    # a modifier released before the last key
    ([(1, 0x14A, 1), (2, 0, 3), (2, 1, -2), (2, 0, 1)], False),         # touch, click and pointer motion are ignored
])
def test_takeover_only_stops_on_the_hotkey(monkeypatch, events, stopped):
    t, kills = _watch(monkeypatch, events)
    assert bool(t.reason) == stopped and (kills == [takeover.signal.SIGINT]) == stopped
    assert t.reason in ("", "you pressed ctrl+alt+esc")


def test_stop_combo_parsing():
    assert takeover.parse_combo("Ctrl + Alt+F9") == [(29, 97), (56, 100), (67,)]
    with pytest.raises(ValueError):
        takeover.parse_combo("ctrl+banana")
    with pytest.raises(ValueError):
        takeover.parse_combo("")


def test_stop_combo_config_order(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "PATH", tmp_path / "config.json")
    monkeypatch.delenv("JEV_STOP_KEYS", raising=False)
    assert takeover.stop_combo() == takeover.DEFAULT_STOP
    config.put("stop_keys", "ctrl+alt+x")
    assert takeover.stop_combo() == "ctrl+alt+x"
    monkeypatch.setenv("JEV_STOP_KEYS", "ctrl+shift+f9")
    assert takeover.stop_combo() == "ctrl+shift+f9"


def test_sessions_roundtrip_and_context(tmp_path, monkeypatch):
    import sessions
    monkeypatch.setattr(sessions, "DIR", tmp_path)
    s = sessions.new()
    sessions.save(s)
    assert not list(tmp_path.glob("*.json"))  # empty sessions leave nothing behind
    sessions.add_turn(s, "open the calculator", {"status": "done", "report": "Calculator is open"}, 3.2)
    sessions.add_turn(s, "close it", {"status": "stopped", "error": "you pressed ctrl+alt+esc"}, 1.0)
    assert s["title"] == "open the calculator"
    back = sessions.find("1")
    assert back["id"] == s["id"] and sessions.find("calculator")["id"] == s["id"] and sessions.find(s["id"][:8])["id"] == s["id"]
    ctx = sessions.context(back)
    assert "open the calculator" in ctx and "Calculator is open" in ctx and "stopped" in ctx
    assert "# open the calculator" in sessions.export_markdown(back)
    f = sessions.fork(back)
    assert f["id"] != s["id"] and len(f["turns"]) == 2
    assert sessions.delete(f["id"]) and not sessions.delete(f["id"])


def test_notes_memory(mem, monkeypatch):
    monkeypatch.setattr(memory, "NOTES", mem / "JEV.md")
    assert memory.notes_text() == ""
    memory.add_note("Always use Zen")
    memory.add_note("Save files in ~/notes")
    assert "- Always use Zen" in memory.notes_text() and "~/notes" in memory.notes_text()


def test_slash_commands_all_have_handlers_and_legacy_words_route():
    import repl
    for name in repl.COMMANDS:
        assert hasattr(repl.Repl, f"cmd_{name}"), name
    for alias, target in repl.ALIASES.items():
        assert target in repl.COMMANDS, alias
    comps = list(repl.SlashCompleter().get_completions(__import__("prompt_toolkit.document", fromlist=["Document"]).Document("/res"), None))
    assert [c.text for c in comps] == ["resume"]


@pytest.mark.parametrize("role,actions,expect", [
    ("button", [{"name": ""}], ("perform_action", "button", None)),             # LibreOffice toolbar button: its unnamed action
    ("push button", [{"name": "Click"}], ("perform_action", "push button", "Click")),
    ("menu item", [{"name": ""}], ("pointer", "left", 1)),                      # ghost menu item: never its unnamed action
    ("button", [], ("pointer", "left", 1)),                                     # no action: the real pointer clicks
])
def test_click_uses_working_accessibility_actions(monkeypatch, role, actions, expect):
    import a11y_agent
    calls = []
    import cursor
    monkeypatch.setattr(a11y_agent.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cursor, "glide", lambda x, y: None)
    monkeypatch.setattr(cursor, "click", lambda x, y, button="left", count=1: calls.append(("pointer", button, count)))
    d = a11y_agent.Desktop.__new__(a11y_agent.Desktop)
    d.mcp = type("M", (), {"call": lambda self, tool, **a: calls.append((tool, a.get("role"), a.get("action")))})()
    el = {"role": role, "name": "Sort Descending", "actions": actions, "abs": {"x": 10, "y": 10, "width": 20, "height": 20}}
    d.click(el, "click", [el])
    assert calls == [expect]


def test_wait_after_a_self_waiting_step_is_skipped(monkeypatch):
    slept = []
    monkeypatch.setattr(ha.time, "sleep", lambda s: slept.append(s))
    a = make_agent([])
    del a.step  # the real step
    a._prev_do = "open_file"
    assert a.step({"do": "wait", "seconds": 3}, [], None) is None and slept == []  # open_file already waited
    assert a.step({"do": "wait", "seconds": 3}, [], None) is None and slept == [3]  # a wait after a wait is honoured


def test_context_window_and_auto_compact(tmp_path, monkeypatch):
    import config
    import context
    import sessions
    monkeypatch.setattr(sessions, "DIR", tmp_path / "s")
    monkeypatch.setattr(config, "PATH", tmp_path / "config.json")
    assert context.window() == 256_000 and context.compact_at() == 0.98
    assert context.parse_size("256k") == 256_000 and context.parse_size("max") == context.LIMIT and context.parse_size("1m") == 1_000_000
    assert context.parse_size("3") is None
    config.put("window", 10_000)
    s = sessions.new()
    for i in range(6):
        sessions.add_turn(s, f"task {i}", {"status": "done", "report": "x" * 4000}, 1)
    assert context.needs_compact(s)  # ~7k tokens of turns + 15k overhead > 98% of 10k
    seen = []
    before, after = context.compact(s, lambda system, text: seen.append(text) or {"summary": "folded"})
    assert after < before and s["compacted"] == 4 and s["summary"] == "folded"
    ctx = sessions.context(s)
    assert "folded" in ctx and "task 5" in ctx and "task 0" not in ctx and "Summary so far" not in seen[0]
    context.note_usage(s, 9_000)
    assert context.overhead(s) == 2_500  # learned overhead is capped at a quarter of this tiny window
    config.put("window", 256_000)
    context.note_usage(s, 40_000)
    assert context.overhead(s) == 40_000 - context.tokens(sessions.context(s))

    def boom(system, text):
        raise RuntimeError("no network")
    sessions.add_turn(s, "task 6", {"status": "done", "report": "y"}, 1)
    sessions.add_turn(s, "task 7", {"status": "done", "report": "z"}, 1)
    context.compact(s, boom)  # falls back to one line per turn
    assert "task 4" in s["summary"] and "folded" in s["summary"]


def test_finished_card_shows_the_outcome(monkeypatch):
    cards = []
    monkeypatch.setattr(notify, "send", lambda title, body="", **kw: cards.append((title, body, kw.get("state"))))
    report = "**Your JITS dataset has 45 downloads**\n- Dataset: [Viverun/jits](https://huggingface.co/datasets/Viverun/jits)\n- Likes: 3"
    notify.finished({"status": "done", "report": report}, 45)
    assert cards[-1] == ("Your JITS dataset has 45 downloads", "• Dataset: Viverun/jits\n• Likes: 3\n✔ done in 45 s", "done")
    notify.finished({"status": "stopped", "error": "you pressed ctrl+alt+esc"}, 8)
    assert cards[-1][0] == "Stopped · 8 s" and "ctrl+alt+esc" in cards[-1][1] and cards[-1][2] == "stopped"
    notify.finished({"status": "done"}, 3)
    assert cards[-1][0] == "Task complete"


def test_bring_back_raises_the_terminal_only_if_it_still_exists():
    import jev_cli
    calls = []
    mcp = type("M", (), {"call": lambda self, name, **kw: calls.append((name, kw)) or {}})()
    desk = type("D", (), {"mcp": mcp, "windows": lambda self: [{"window_id": 7}]})()
    jev_cli.bring_back(desk, {"window_id": 7})
    jev_cli.bring_back(desk, {"window_id": 8})
    jev_cli.bring_back(desk, None)
    assert calls == [("activate_window", {"window_id": 7})]


@pytest.mark.parametrize("text,note", [
    ("you need to do some scrolling if you don't find out something and check the entire page, get this into your memory",
     "you need to do some scrolling if you don't find out something and check the entire page"),
    ("remember that I use Zen for email", "I use Zen for email"),
    ("add this to your memory: always save reports in ~/notes", None),  # too little left once the request itself is removed? kept below
    ("open the calculator", None),
    ("what is in my memory settings page on the website", None),
])
def test_memory_requests_are_saved_not_run(text, note):
    import repl
    got = repl.memory_note(text)
    if text.startswith("add this"):
        assert got is None or "save reports" in got
    else:
        assert got == note


def test_not_found_answers_are_detected():
    ha = pytest.importorskip("hybrid_agent")
    for bad in ("I couldn't find an overall download total", "The count is not shown on the page", "Unable to locate the field"):
        assert ha.NOT_FOUND.search(bad), bad
    for ok in ("Your dataset has 45 downloads", "Found the setting under Billing"):
        assert not ha.NOT_FOUND.search(ok), ok


def test_harness_overlay_and_adoption(mem, tmp_path, monkeypatch):
    memory.adopt_harness(["Read file values with shell before typing them"])
    ov = tmp_path / "cand.json"
    ov.write_text(json.dumps({"notes": ["Never click New Tab before typing"], "lessons": {"zen": ["Use the GitHub API"]}}))
    monkeypatch.setenv("JEV_HARNESS_OVERLAY", str(ov))
    assert memory.harness_notes() == ["Read file values with shell before typing them", "Never click New Tab before typing"]
    assert "Use the GitHub API" in memory.tips_for("zen.desktop")
    a = make_agent([])
    assert "Never click New Tab before typing" in a.system_prompt()
    monkeypatch.setenv("JEV_NO_HARNESS", "1")
    assert memory.harness_notes() == ["Never click New Tab before typing"]


def test_final_plan_that_reads_a_page_gets_another_round():
    """The report must come from the page text, not be written before reading it."""
    plans = [{"status": "working", "final": True, "report": "I'll identify the heading", "done_when": [],
              "steps": [{"do": "read_page", "url": "https://example.com"}]},
             {"status": "done", "report": "**Example Domain**", "steps": []}]
    r = make_agent(plans).run("t")
    assert r["report"] == "**Example Domain**"


def test_select_range_undoes_a_misfire_and_retries(monkeypatch):
    """If the range text lands in a cell (the Name Box did not have focus yet), undo and try again."""
    monkeypatch.setattr(ha.time, "sleep", lambda s: None)
    a = make_agent([])
    a.task_window = {"window_id": 1, "title": "s.csv — LibreOffice Calc", "app_id": "libreoffice-calc.desktop", "pid": 1}
    keys, reads = [], iter([
        [{"role": "table cell", "name": "A1", "text": {"content": "A1:C1"}, "states": ["focused"]}],   # typed into A1
        [{"role": "table cell", "name": "A1", "text": {"content": "Product"}, "states": ["selected"]},
         {"role": "label", "text": {"content": "Selected: 1 row, 3 columns"}}],
        [{"role": "label", "text": {"content": "Selected: 5 rows, 1 column"}}],     # later selects: status bar only
    ])
    a.act = lambda c: keys.append(c.get("key") or c.get("text"))
    a.d.mcp = type("M", (), {"call": lambda self, tool, **k: {"accessibility_tree": next(reads)}})()
    assert a.select_range("a1:c1") is None
    assert keys == ["ctrl+shift+F5", "A1:C1", "enter", "ctrl+z", "ctrl+shift+F5", "A1:C1", "enter"]
    assert a.select_range("B2:B6") is None and keys[-3:] == ["ctrl+shift+F5", "B2:B6", "enter"]
    assert a.select_range("not a range").startswith("not a cell range")


def test_settings_env_beats_file_and_file_is_private(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "PATH", tmp_path / "config.json")
    for envs in config.SETTINGS.values():
        for e in envs:
            monkeypatch.delenv(e, raising=False)
    config.save_settings({"planner_base_url": "https://file/v1", "typesafe_api_key": "ts-file"})
    assert (tmp_path / "config.json").stat().st_mode & 0o077 == 0          # only the owner can read keys
    assert config.setting("planner_base_url") == "https://file/v1" and config.setting("planner_model") == "gpt-6-luna"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env/v1")
    assert config.setting("planner_base_url") == "https://env/v1"           # the environment wins
    config.apply()
    assert __import__("os").environ["TYPESAFE_API_KEY"] == "ts-file"


def test_learning_labels_improvement_and_learnability(mem):
    first = memory.record("check telegram for unread messages", "done", [{"do": "click"}] * 55, ["x", "y", "z"], 370,
                          category="agent", app="telegram", plans=34)
    assert first["label"] == "first time" and first["attempt"] == 1 and first["learnability"] < 60
    again = memory.record("check telegram for unread messages", "done", [{"do": "open_app"}, {"do": "click"}], [], 40,
                          category="agent", app="telegram", plans=1, workflow="Check Telegram for unread messages")
    assert again["label"] == "best yet" and again["faster"] == 89 and again["fewer_plans"] == 33
    assert again["attempt"] == 2 and again["learnability"] == 100
    failed = memory.record("check telegram for unread messages", "error", [], ["boom"], 5, category="agent", app="telegram")
    assert failed["label"].startswith("not learned") and failed["learnability"] == 0
    stats = {c["category"]: c for c in memory.learning_stats()}
    assert stats["agent"]["runs"] == 3 and stats["agent"]["done"] == 2 and stats["agent"]["trend"] == 89


def test_similar_tasks_compare_within_their_category(mem):
    memory.record("sort sales.csv by price in calc", "done", [{"do": "select"}], [], 60, category="spreadsheet", app="libreoffice-calc", plans=2)
    memory.record("send an email to bob", "done", [{"do": "goto"}], [], 10, category="email", app="zen", plans=1)
    rep = memory.learning({"task": "sort report.csv by price in calc", "status": "done", "seconds": 30, "plans": 1,
                           "category": "spreadsheet", "app": "libreoffice-calc", "steps": [{}], "failures": []}, memory.runs())
    assert rep["attempt"] == 2 and rep["faster"] == 50


def test_repeated_click_between_other_moves_is_a_loop():
    assert changes.looping(["click › Archived chats", "go to › x", "click › Archived chats", "focus › y", "click › Archived chats"])


def test_your_input_pauses_then_glimpse_looks_again(monkeypatch):
    t = takeover.Takeover()
    assert t.paused_for() == 0
    t.last_input = time.monotonic()
    assert 9 < t.paused_for() <= takeover.PAUSE_SECONDS
    t.resume()
    assert t.paused_for() == 0
    a = make_agent([])
    clock = {"t": 100.0}
    monkeypatch.setattr(ja.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(takeover.time, "monotonic", lambda: clock["t"])
    a.you = t
    t.last_input = 100.0
    with pytest.raises(ja.UserPaused):
        a.act({"type": "key", "key": "enter", "label": "x"})
    assert clock["t"] >= 100.0 + takeover.PAUSE_SECONDS     # it waited out the pause, then asked to look again


def test_pause_replans_without_counting_a_failure():
    plans = [{"status": "working", "steps": [{"do": "key", "key": "a"}, {"do": "key", "key": "b"}]},
             {"status": "done", "report": "r", "steps": []}]
    a = make_agent(plans)
    a.step = lambda s, els, shot: (_ for _ in ()).throw(ja.UserPaused("you used the computer"))
    r = a.run("t")
    assert r["status"] == "done" and not a.failures


def test_added_instructions_reach_the_planner():
    a = make_agent([])
    del a.plan  # the real planner call
    a.extra = ["also save a copy as PDF"]
    sent = {}

    class Completions:
        def create(self, **k):
            sent.update(k)
            msg = type("M", (), {"content": "{}"})()
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": usage})()
    a.luna = type("L", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    a.plan("t", [], [], None)
    assert "also save a copy as PDF" in sent["messages"][1]["content"][0]["text"]


def test_clear_starts_a_fresh_session(tmp_path, monkeypatch):
    import repl
    import sessions
    monkeypatch.setattr(sessions, "save", lambda s: None)
    r = repl.Repl.__new__(repl.Repl)
    r.session = {"id": "old", "title": "trip", "turns": [{"prompt": "x"}]}
    r.last = ("x", {})
    import io
    out = io.StringIO()
    r.console = type("C", (), {"file": out, "print": lambda self, *a, **k: None})()
    r.cmd_clear("")
    assert r.session["id"] != "old" and r.session["turns"] == [] and r.last is None
    assert "\033[3J" in out.getvalue()  # the scrollback is erased too


def _h2(monkeypatch, mem, proposals, probe_rows):
    sys.path.insert(0, str(ROOT / "bench"))
    import harness2
    monkeypatch.setattr(harness2, "WORK", mem / "h2")
    monkeypatch.setattr(harness2, "LIBRARY", mem / "lib.jsonl")
    props, rows = list(proposals), dict(probe_rows)
    monkeypatch.setattr(harness2, "propose", lambda dim, name, base, earlier=(): props.pop(0) if props else {"keep_base": True})
    monkeypatch.setattr(harness2, "probe", lambda name, cand, label: rows[label])
    monkeypatch.setattr(harness2, "luna_json", lambda system, text: {})
    monkeypatch.setattr(harness2, "evidence", lambda name, base: "")
    return harness2


def test_harness2_parallel_budget_and_tool_guidance(mem, monkeypatch):
    base = {"ok": True, "plans": 4, "seconds": 60, "history": []}
    good = {"ok": True, "plans": 1, "seconds": 20, "history": []}
    meh = {"ok": True, "plans": 4, "seconds": 59, "history": []}
    h2 = _h2(monkeypatch, mem,
             [{"notes": ["n1"]}, {"tools": {"select": ["check the status bar"]}},   # INV1, INV2
              {"lessons": {"zen": ["l1"]}}, {"keep_base": True}],                    # DOM1, then DOM stops
             {"INV1": meh, "INV2": good, "DOM1": meh})
    r = h2.improve("calc_sort", base, k=2)
    assert set(r["probes"]) == {"INV1", "INV2", "DOM1"} and r["adopted"] == "INV2"
    assert memory.harness_tools() == {"select": ["check the status bar"]}
    a = make_agent([])
    assert "select: check the status bar" in a.system_prompt()


def test_harness2_sequential_stops_without_evidence(mem, monkeypatch):
    base = {"ok": True, "plans": 4, "seconds": 60, "history": []}
    h2 = _h2(monkeypatch, mem, [{"notes": ["n1"]}], {"INV": {"ok": True, "plans": 1, "seconds": 20, "history": []}})
    calls = []
    real = h2.improve
    monkeypatch.setattr(h2, "improve", lambda name, b, k=1: (calls.append(b), real(name, b, k))[1])
    monkeypatch.setattr(h2.live_bench, "run", lambda name, **kw: base)
    monkeypatch.setattr(h2.live_bench, "cleanup_windows", lambda: None)
    monkeypatch.setattr(h2.time, "sleep", lambda s: None)
    h2.main(["calc_sort"], k=3, mode="sequential")
    assert len(calls) == 2 and calls[1]["plans"] == 1  # step 2 starts from step 1's winner; nothing more to try: stop


def _pointer_agent(monkeypatch):
    import cursor
    done = []
    monkeypatch.setattr(cursor, "click", lambda x, y, button="left", count=1: done.append(("click", round(x), round(y), button, count)))
    monkeypatch.setattr(cursor, "stroke", lambda pts, step=4.0: done.append(("stroke", [(round(x), round(y)) for x, y in pts])))
    monkeypatch.setattr(cursor, "scroll", lambda n, at=None: done.append(("scroll", n, tuple(round(v) for v in at))))
    a = make_agent([])
    del a.step  # the real step
    a.act = lambda c: c["label"]
    a.task_window = {"window_id": 1, "title": "paint", "bounds": {"x": 100, "y": 50, "width": 1000, "height": 600}}
    a._shot_meta = None
    return a, done


def test_pointer_steps_map_screenshot_fractions_to_the_screen(monkeypatch):
    a, done = _pointer_agent(monkeypatch)
    assert a.step({"do": "click_at", "x": 0.5, "y": 0.5, "button": "right"}, [], None) is None
    assert a.step({"do": "drag", "from": [0, 0], "to": [1, 1]}, [], None) is None
    assert done == [("click", 600, 350, "right", 1), ("stroke", [(100, 50), (1100, 650)])]
    with pytest.raises(ValueError):
        a.pointer_step({"do": "click_at", "x": 1.5, "y": 0.2}, None)


def test_draw_strokes_are_fractions_of_the_found_area(monkeypatch):
    a, done = _pointer_agent(monkeypatch)
    a.locate = lambda target, shot: (200, 100, 600, 500)  # the canvas Jev found, in screen px
    r = a.step({"do": "draw", "on": "the white canvas", "strokes": [[[0, 0], [0.5, 0.5], [1, 0]]]}, [], b"shot")
    assert r is None and done == [("stroke", [(200, 100), (400, 300), (600, 100)])]
    assert a.step({"do": "draw", "on": "canvas", "strokes": [[[0, 0]]]}, [], b"shot").startswith("draw needs strokes")
    a.locate = lambda target, shot: None
    assert "could not find the drawing area" in a.step({"do": "draw", "on": "canvas", "strokes": [[[0, 0], [1, 1]]]}, [], b"shot")


def test_not_found_is_pushed_back_only_once_and_only_early():
    """An early give-up is sent back once; an answer given after acting and looking stands."""
    plans = [{"status": "working", "steps": [{"do": "click", "target": "x"}]},
             {"status": "done", "report": "I could not find the setting", "steps": []},
             {"status": "done", "report": "I could not find the setting anywhere", "steps": []}]
    a = make_agent(plans)
    r = a.run("find the setting")
    assert r["status"] == "done" and r["report"] == "I could not find the setting anywhere" and not a.failures
    honest = make_agent([{"status": "done", "report": "You didn't get any new messages", "steps": []}])
    assert honest.run("check messages")["report"] == "You didn't get any new messages"  # no action taken: no pushback


@pytest.mark.parametrize("app_id,fam", [("zen.desktop", "firefox"), ("firefox_firefox.desktop", "firefox"), ("org.mozilla.firefox", "firefox"),
                                        ("brave-browser", "chromium"), ("google-chrome", "chromium"), ("chromium", "chromium"),
                                        ("microsoft-edge", "chromium"), ("org.gnome.TextEditor", None)])
def test_browser_families(app_id, fam):
    import browsers
    assert browsers.family(app_id) == fam


def test_browsers_start_with_accessibility(tmp_path, monkeypatch):
    import browsers
    (tmp_path / "brave-browser.desktop").write_text("[Desktop Entry]\nType=Application\nName=Brave\nExec=/usr/bin/brave-browser-stable %U\n")
    monkeypatch.setattr(browsers, "APP_DIRS", [tmp_path])
    assert browsers.launch_command("brave-browser") == ["/usr/bin/brave-browser-stable", "--force-renderer-accessibility"]
    assert browsers.launch_command("zen") == ["gtk-launch", "zen"]
    e = browsers.env()
    assert e["GNOME_ACCESSIBILITY"] == "1" and e["ACCESSIBILITY_ENABLED"] == "1"


def test_a_click_that_changes_nothing_stops_the_chunk():
    plans = [{"status": "working", "steps": [{"do": "click", "target": "notes.txt"}, {"do": "key", "key": "ctrl+x"}]},
             {"status": "done", "report": "r", "steps": []}]
    a = make_agent(plans)
    a.unchanged = lambda: True
    ran = []
    orig = a.step
    a.step = lambda s, els, shot: (ran.append(s["do"]), orig(s, els, shot))[1]
    a.run("t")
    assert ran == ["click"] and "no visible effect" in a.failures[0]  # ctrl+x never ran on nothing


def test_setup_knows_when_remote_interaction_is_saved(tmp_path, monkeypatch):
    import setup_wizard
    monkeypatch.setattr(setup_wizard, "GRANT_DIR", tmp_path)
    assert not setup_wizard.remote_grant_saved()
    (tmp_path / "remote-desktop-keyboard.token").write_text("t")
    assert setup_wizard.remote_grant_saved()
    printed = []
    con = type("C", (), {"print": lambda self, *a, **k: printed.append(a[0]), "input": lambda self, *a, **k: "y"})()
    setup_wizard.grant_remote_interaction(con)
    assert "already allowed" in printed[0]          # never asks again once saved


def test_setup_skips_browser_restart_when_all_ready(monkeypatch):
    import browsers
    import setup_wizard
    monkeypatch.setattr(browsers, "running", lambda: [("zen", 1, "firefox")])
    monkeypatch.setattr(browsers, "process_ready", lambda pid, fam: True)
    asked = []
    con = type("C", (), {"print": lambda self, *a, **k: None, "input": lambda self, *a, **k: asked.append(1) or "y"})()
    setup_wizard.restart_unready_browsers(con)
    assert not asked


def test_fileop_guards_and_operations(tmp_path, monkeypatch):
    import fileops
    monkeypatch.setattr(fileops.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(fileops, "show", lambda paths: None)
    monkeypatch.setattr(fileops, "_gio", lambda *a: (Path(a[-1]).write_text("x") if a[0] == "copy" else
                                                      Path(a[-2]).rename(a[-1]) if a[0] == "move" else None) and None)
    (tmp_path / "a.txt").write_text("hi")
    assert fileops.run({"op": "mkdir", "dst": str(tmp_path / "docs")}) is None and (tmp_path / "docs").is_dir()
    assert fileops.run({"op": "copy", "src": str(tmp_path / "a.txt"), "dst": str(tmp_path / "docs")}) is None
    assert "already exists" in fileops.run({"op": "copy", "src": str(tmp_path / "a.txt"), "dst": str(tmp_path / "docs")})
    assert fileops.run({"op": "rename", "src": str(tmp_path / "a.txt"), "dst": "b.txt"}) is None and (tmp_path / "b.txt").exists()
    assert "outside your home" in fileops.run({"op": "copy", "src": str(tmp_path / "b.txt"), "dst": "/etc/x"})
    assert "unknown fileop" in fileops.run({"op": "delete", "src": str(tmp_path / "b.txt")})


def test_fileop_is_off_when_the_task_names_an_app():
    a = make_agent([])
    del a.step
    a.read_only_shell = True
    assert a.step({"do": "fileop", "op": "mkdir", "dst": "~/x"}, [], None).startswith("fileop is off")


def test_identical_clean_task_is_replayed_without_the_planner(mem):
    memory.record("sort sales.csv by price", "done", [{"do": "open_file", "path": "s.csv"}, {"do": "wait"}, {"do": "select", "range": "C2"}],
                  [], 20, plans=1)
    assert memory.exact_repeat("Sort  sales.csv by PRICE") == [{"do": "open_file", "path": "s.csv"}, {"do": "select", "range": "C2"}]
    memory.record("flaky task", "done", [{"do": "click"}], ["x failed"], 50, plans=5)
    assert memory.exact_repeat("flaky task") == []          # never clean: the planner decides
    memory.record("sort sales.csv by price", "done", [{"do": "click"}], ["oops"], 90, plans=6)
    assert memory.exact_repeat("sort sales.csv by price")[0]["do"] == "open_file"  # a messy run keeps the good recipe
    plans_called = []
    a = make_agent([{"status": "done", "report": "confirmed", "steps": []}])
    orig = a.plan
    a.plan = lambda *x, **k: (plans_called.append(1), orig(*x, **k))[1]
    a.replay = [{"do": "key", "key": "a"}]
    r = a.run("t")
    assert r["status"] == "done" and r["steps"] == 1 and len(plans_called) == 1  # replay ran, then one planner call


def test_ocr_click_chooses_a_text_and_falls_back_for_icons(monkeypatch):
    import ocr
    monkeypatch.setattr(ocr, "enabled", lambda: True)
    monkeypatch.setattr(ocr, "texts", lambda shot: [("Cancel", (0.1, 0.1, 0.2, 0.2)), ("Save", (0.8, 0.8, 0.9, 0.9))])
    a, done = _pointer_agent(monkeypatch)
    picks = iter(["0", "none"])  # "Save" is ranked first: it shares a word with the target
    a.ask = lambda state, q: type("R", (), {"choices": {"t": FakeChoice(next(picks), 0.9)}})()
    shot = _jpeg((240, 240, 240))
    assert a.ocr_click("the Save button", shot) and done == [("click", 950, 560, "left", 1)]  # centre of (0.85, 0.85)
    assert not a.ocr_click("the close icon", shot)  # no text fits: the box takes over
    assert [t for t, _ in ocr.candidates("save it", ocr.texts(b""))][0] == "Save"


OSWORLD_XML = """<desktop-frame xmlns:st="https://accessibility.ubuntu.example.org/ns/state"
 xmlns:cp="https://accessibility.ubuntu.example.org/ns/component" xmlns:act="https://accessibility.ubuntu.example.org/ns/action">
 <application name="soffice">
  <frame name="sales.csv - LibreOffice Calc" st:showing="true" st:visible="true" st:active="true"
         cp:screencoord="(0, 37)" cp:size="(1920, 1043)">
   <push-button name="Sort Descending" st:showing="true" st:visible="true" cp:screencoord="(775, 80)" cp:size="(33, 35)" act:click_desc="Click"/>
   <table-cell name="A1" st:showing="true" cp:screencoord="(45, 200)" cp:size="(66, 17)">Product</table-cell>
  </frame>
 </application>
 <application name="gnome-shell"><frame name="hidden" cp:screencoord="(0, 0)" cp:size="(10, 10)"/></application>
</desktop-frame>"""


def test_osworld_tree_becomes_windows_and_elements(monkeypatch):
    import osworld_backend as ob
    nodes = ob.parse_tree(OSWORLD_XML)
    btn = next(n for n in nodes if n["name"] == "Sort Descending")
    assert btn["role"] == "push button" and btn["bounds"] == {"x": 775, "y": 80, "width": 33, "height": 35}
    assert btn["actions"] == [{"name": "click"}] and "showing" in btn["states"]
    cell = next(n for n in nodes if n["name"] == "A1")
    assert cell["text"] == {"content": "Product"}
    server = type("S", (), {"accessibility": lambda self: OSWORLD_XML})()
    mcp = ob.OSWorldMCP(server)
    wins = mcp.call("list_windows")["windows"]
    assert [w["title"] for w in wins] == ["sales.csv - LibreOffice Calc"] and wins[0]["focused"]   # hidden frames skipped
    st = mcp.call("get_app_state", window_id=wins[0]["window_id"], include_screenshot=False)
    assert [n["name"] for n in st["accessibility_tree"]] == ["sales.csv - LibreOffice Calc", "Sort Descending", "A1"]
    assert st["accessibility_tree"][1]["parent_index"] == 0 and st["accessibility_tree"][0]["depth"] == 1


def test_superpowers_only_when_stuck(monkeypatch):
    import config
    import hybrid_agent
    monkeypatch.setattr(config, "get", lambda k, d=None: d)
    assert hybrid_agent.superpowers() == ["realtime"]
    agent = hybrid_agent.HybridAgent.__new__(hybrid_agent.HybridAgent)
    agent.stuck = False
    assert '"do": "play"' not in agent.system_prompt()   # dormant: not shown, no cost
    agent.stuck = True
    assert '"do": "play"' in agent.system_prompt()
    monkeypatch.setattr(config, "get", lambda k, d=None: [] if k == "superpowers" else d)
    assert hybrid_agent.superpowers() == []


def test_realtime_snake_reads_and_steers():
    import numpy as np
    realtime = pytest.importorskip("realtime")
    f = np.full((600, 800, 3), (160, 190, 90), np.uint8)       # light green page
    f[100:102, 100:524] = f[402:404, 100:524] = (43, 48, 24)  # board frame: 21 x 15 cells of 20 px inside
    f[100:404, 100:102] = f[100:404, 522:524] = (43, 48, 24)
    l, t = 102, 102
    for cx, cy in ((10, 7), (10, 8), (10, 9)):                # snake, head at (10, 7) moving up
        f[t + cy * 20 + 1:t + cy * 20 + 19, l + cx * 20 + 1:l + cx * 20 + 19] = (43, 48, 24)
    fx, fy = 4, 7                                             # food with a light centre
    f[t + fy * 20 + 2:t + fy * 20 + 18, l + fx * 20 + 2:l + fx * 20 + 18] = (43, 48, 24)
    f[t + fy * 20 + 8:t + fy * 20 + 12, l + fx * 20 + 8:l + fx * 20 + 12] = (160, 190, 90)
    box = realtime.find_board(f)
    g = realtime.Grid.fit(box, 20.0)
    assert (g.cols, g.rows) == (21, 15)
    snake, food = g.read(f)
    assert snake == {(10, 7), (10, 8), (10, 9)} and food == {(4, 7)}
    assert realtime.choose(g, (10, 7), snake - {(10, 7)}, food, "up") == "left"


def test_osworld_shell_uses_execute(monkeypatch):
    import osworld_backend
    sent = {}

    class R:
        ok = True

        def json(self):
            return {"output": "hello\n", "error": "", "returncode": 3}

    def post(url, headers=None, data=None, timeout=None):
        sent.update(url=url, body=json.loads(data))
        return R()
    monkeypatch.setattr(osworld_backend.requests, "post", post)
    out, rc = osworld_backend.OSWorldServer("http://vm:5000").bash("echo hello; exit 3", timeout=9)
    assert (out, rc) == ("hello\n", 3)
    assert sent["url"].endswith("/execute") and sent["body"]["command"][:4] == ["timeout", "9", "bash", "-c"]


def test_osworld_windows_from_wmctrl_and_indexes_kept():
    import osworld_backend as ob
    wm = ("0x01e00003 -1 1200 0 0 1920 32 gnome-shell.Gnome-shell host top bar\n"
          "0x03a00007  0 2210 70 27 1850 1053 libreoffice.libreoffice-calc host sales.csv - LibreOffice Calc\n"
          "ACTIVE 0x3a00007\n")   # xprop drops the leading zeros

    class S:
        def accessibility(self):
            return OSWORLD_XML

        def bash(self, script, timeout=60, keep_tree=False):
            return wm, 0
    mcp = ob.OSWorldMCP(S())
    wins = mcp.call("list_windows")["windows"]
    assert [(w["title"], w["focused"], w["bounds"]["width"]) for w in wins] == [("sales.csv - LibreOffice Calc", True, 1850)]
    st = mcp.call("get_app_state", window_id=wins[0]["window_id"], include_screenshot=False)
    assert len(st["accessibility_tree"]) == 3
    mcp.call("get_app_state", window_id=wins[0]["window_id"], include_screenshot=False, max_nodes=1)
    assert len(mcp._nodes) == 3   # a screenshot-only read keeps the element indexes for perform_action


def test_shell_guide_hints_and_syntax_check():
    import hybrid_agent
    import shell_guide
    assert shell_guide.relevant("rename the files in ~/Downloads") and not shell_guide.relevant("open x.com")
    assert "not found" in shell_guide.explain(127, "bash: foo: command not found")
    assert "path does not exist" in shell_guide.explain(2, "ls: cannot access 'x': No such file or directory")
    assert shell_guide.help_command("tar; rm -rf ~").startswith("{ type tarrm-rf")   # only a command name survives
    out, err = hybrid_agent.run_shell("echo 'unclosed")
    assert out == "" and err.startswith("bash syntax error (nothing ran)")
    out, err = hybrid_agent.run_shell("bash -c 'exit 126'")
    assert err.startswith("exit 126 (found but not executable")


def test_bash_guide_joins_prompt_only_when_relevant(monkeypatch):
    import config
    import hybrid_agent
    monkeypatch.setattr(config, "get", lambda k, d=None: d)
    agent = hybrid_agent.HybridAgent.__new__(hybrid_agent.HybridAgent)
    agent.stuck, agent.shell_aware = False, False
    assert "BASH GUIDE" not in agent.system_prompt()
    agent.shell_aware = True
    assert "BASH GUIDE" in agent.system_prompt()


def test_voice_listener_stops_after_speech_and_keeps_first_word(monkeypatch):
    import io
    import threading
    import numpy as np
    import voice
    rate = voice.RATE
    t = np.arange(int(rate * 1.0)) / rate
    speech = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    quiet = np.zeros(int(rate * 0.6), np.float32) + 0.0005
    tail = np.zeros(int(rate * 3), np.float32) + 0.0005
    pcm = (np.concatenate([quiet, speech, tail]) * 32767).astype(np.int16).tobytes()

    class P:
        def __init__(self, *a, **k):
            self.stdout = io.BytesIO(pcm)

        def terminate(self):
            pass
    monkeypatch.setattr(voice.subprocess, "Popen", P)
    got, states, done = {}, [], threading.Event()
    monkeypatch.setattr(voice, "transcribe", lambda a: got.setdefault("seconds", a.size / rate) and "open x dot com")
    voice.Listener(lambda text: (got.update(text=text), done.set()), lambda s, level=0.0: states.append(s)).start()
    assert done.wait(5)
    assert got["text"] == "open x dot com"
    assert 1.5 < got["seconds"] < 2.4      # 0.3 s before speech + 1 s speech + ~0.8 s silence, not the whole 3 s tail
    assert states[0] == "listening" and "hearing" in states and states[-1] == "done"


def test_voice_indicator_animates():
    import voice
    assert voice.indicator("listening", 0, 0) != voice.indicator("listening", 0, 1)
    assert voice.indicator("transcribing", 0, 0) != voice.indicator("transcribing", 0, 1)
    assert voice.indicator("hearing", 0.9, 3).startswith("🎙 ") and len(voice.indicator("hearing", 0.9, 3)) == 7


def test_voice_intents():
    import voice
    for said in ("Chat mode.", "switch to chat mode", "Hey glimpse, back to typing", "stop listening", "go to text mode"):
        assert voice.intent(said) == "chat", said
    for said in ("Stop.", "stop it", "cancel that please", "Abort"):
        assert voice.intent(said) == "stop", said
    assert voice.intent("Continue.") == "continue" and voice.intent("keep going") == "continue"
    for said in ("stop the music in Spotify", "open the chat in Discord", "add a row for March"):
        assert voice.intent(said) == "", said   # tasks and instructions, not commands to glimpse


def test_voice_mode_turns_glow_green(tmp_path, monkeypatch):
    import glow
    import voice
    f = tmp_path / "glimpse-voice"
    monkeypatch.setattr(voice, "VOICE_FILE", str(f))
    monkeypatch.setattr(glow, "VOICE_FILE", str(f))
    voice.set_mode(True)
    assert glow.voice_now()
    voice.set_mode(False)
    assert not glow.voice_now()


def test_background_process_lifecycle():
    import procs
    p = procs.Procs()
    name = p.spawn("echo started; sleep 30", "worker")
    time.sleep(0.5)
    st = p.status(name)
    assert st.startswith("[worker] running") and "started" in st
    p.spawn("echo short-lived", "once")
    time.sleep(0.5)
    assert "ended (exit 0)" in p.status("once") and "short-lived" in p.status("once")
    kept = p.spawn("sleep 30", "server", keep=True)
    assert p.close() == ["worker"]                 # the task ended: unkept processes stop, finished ones are left
    assert not p.alive("worker") and p.alive(kept)
    p.stop(kept)
    assert not p.alive(kept)
    assert "no process named" in p.status("nope")


def test_spawn_uses_shell_guards():
    import hybrid_agent
    assert hybrid_agent.shell_refusal("rm -rf ~") .startswith("refused (destructive)")
    assert hybrid_agent.shell_refusal("python3 -m http.server 8000") is None
    assert hybrid_agent.shell_refusal("touch ~/x", read_only=True).startswith("refused: the task names an app")


def _fake_frames(tmp_path, pid=None):
    import mmap
    import numpy as np
    import frames
    shm = tmp_path / "frames"
    w, h = 400, 200                                   # physical pixels; the logical screen is 200 x 100 (scale 2)
    arr = np.zeros((h, w, 3), np.uint8)
    arr[40:80, 100:200] = (255, 0, 0)                 # a red "window" at logical (50, 20) 50 x 20
    with open(shm, "wb") as f:
        f.write(b"\0" * (frames.SIZE + 2 * frames.SLOT))
    with open(shm, "r+b") as f:
        m = mmap.mmap(f.fileno(), 0)
        m[frames.SIZE + frames.SLOT:frames.SIZE + frames.SLOT + arr.nbytes] = arr.tobytes()
        m[:frames.HEADER.size] = frames.HEADER.pack(b"GLFR", 1, 5, time.time(), w, h, 200, 100, 0, 0, pid or os.getpid())
        m.close()
    return str(shm)


def test_fast_frames_crop_the_window(tmp_path):
    import io as _io
    import frames
    fs = frames.Frames()
    assert fs._attach(_fake_frames(tmp_path))
    t0 = time.perf_counter()
    img, meta = fs.window_shot({"x": 50, "y": 20, "width": 50, "height": 20}, fmt="png", max_width=960)
    assert time.perf_counter() - t0 < 0.1
    pic = Image.open(_io.BytesIO(img)).convert("RGB")
    assert pic.size == (100, 40) and pic.getpixel((50, 20)) == (255, 0, 0)   # logical bounds x2 = physical crop
    assert meta["coordinate_width"] == 100 and meta["source"] == "screencast"
    assert fs.window_shot({"x": 500, "y": 500, "width": 50, "height": 20}) is None   # off the streamed monitor


def test_fast_frames_ignore_a_dead_daemon(tmp_path):
    import frames
    assert not frames.Frames()._attach(_fake_frames(tmp_path, pid=4194000))   # no such process: stale frames


def test_screenshot_only_reads_skip_the_mcp(monkeypatch):
    import desktop_mcp
    import frames
    calls = []
    mcp = desktop_mcp.DesktopMCP.__new__(desktop_mcp.DesktopMCP)
    mcp._wins = {7: {"window_id": 7, "focused": True, "bounds": {"x": 0, "y": 0, "width": 10, "height": 10}}}
    mcp._verified, mcp._mismatches, mcp._fast_off = True, 0, False
    mcp._call = lambda tool, args: calls.append((tool, args)) or {"accessibility_tree": [1, 2]}
    fake = type("F", (), {"window_shot": lambda self, b, **k: (b"img", {"source": "screencast"})})()
    monkeypatch.setattr(frames, "source", lambda: fake)
    st = mcp.call("get_app_state", window_id=7, include_screenshot=True, max_nodes=1)
    assert calls == [] and st["screenshot"]["source"] == "screencast"
    st = mcp.call("get_app_state", window_id=7, include_screenshot=True, max_nodes=600)
    assert calls == [("get_app_state", {"window_id": 7, "include_screenshot": False, "max_nodes": 600})]
    assert st["accessibility_tree"] == [1, 2] and st["_images"]
    calls.clear()
    monkeypatch.setattr(frames, "source", lambda: None)              # no screencast: the portal path as before
    mcp.call("get_app_state", window_id=7, include_screenshot=True)
    assert calls[0][1]["include_screenshot"] is True


def _png(color, size=(200, 100)):
    import io as _io
    b = _io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def test_fast_frames_self_check(monkeypatch):
    import base64
    import desktop_mcp
    import frames
    mcp = desktop_mcp.DesktopMCP.__new__(desktop_mcp.DesktopMCP)
    mcp._wins = {7: {"window_id": 7, "focused": True, "bounds": {"x": 0, "y": 0, "width": 10, "height": 10}}}
    mcp._verified, mcp._mismatches, mcp._fast_off = False, 0, False
    portal = {"img": _png((30, 30, 30))}
    mcp._call = lambda tool, args: {"accessibility_tree": [], "_images": [base64.b64encode(portal["img"]).decode()]}
    fast = {"img": _png((30, 30, 30))}
    monkeypatch.setattr(frames, "source", lambda: type("F", (), {"window_shot": lambda s, b, **k: (fast["img"], {"source": "screencast"})})())
    st = mcp.call("get_app_state", window_id=7, max_nodes=1)
    assert mcp._verified and st["screenshot"]["source"] == "screencast"     # same picture: the fast path is trusted
    mcp._verified = False
    fast["img"] = _png((230, 230, 230))                                         # a different picture
    assert "source" not in (mcp.call("get_app_state", window_id=7, max_nodes=1).get("screenshot") or {})
    mcp.call("get_app_state", window_id=7, max_nodes=1)
    assert mcp._fast_off                                                         # two mismatches: off for the session


def test_fast_frames_avoid_notifications(tmp_path, monkeypatch):
    import frames
    area = tmp_path / "area"
    monkeypatch.setattr(frames, "TOAST_AREA", str(area))
    area.write_text(f"1500 40 400 120 {time.time() + 2:.2f}")
    assert frames.covered({"x": 0, "y": 0, "width": 1920, "height": 1000})
    assert not frames.covered({"x": 0, "y": 0, "width": 1000, "height": 1000})
    area.write_text(f"1500 40 400 120 {time.time() - 1:.2f}")                 # gone: no longer in the way
    assert not frames.covered({"x": 0, "y": 0, "width": 1920, "height": 1000})


def test_unfinished_reports_are_not_done():
    import hybrid_agent
    assert hybrid_agent.unfinished_report("Deleted the data, but its confirmation dialog is still open.") == "still open"
    assert hybrid_agent.unfinished_report("**Amazon browsing data cleanup started**")
    assert hybrid_agent.unfinished_report("Saved report.pdf in ~/Documents (2 pages).") is None
    agent = hybrid_agent.HybridAgent.__new__(hybrid_agent.HybridAgent)
    assert "not finished" in agent.verify_done({"report": "The setting is not yet applied."})


def test_search_urls_become_default_engine_searches():
    import hybrid_agent
    assert hybrid_agent.search_query("https://search.brave.com/search?q=Ilya+Sutskever+SSI") == "Ilya Sutskever SSI"
    assert hybrid_agent.search_query("https://www.google.com/search?hl=en&q=gnome%2050") == "gnome 50"
    assert hybrid_agent.search_query("https://duckduckgo.com/?q=zen+browser") == "zen browser"
    assert hybrid_agent.search_query("https://github.com/search?q=glimpse") is None      # a site's own search stays
    assert hybrid_agent.search_query("https://x.com/home") is None


def test_pointer_accepts_numpy_coordinates(tmp_path, monkeypatch):
    import numpy as np
    import cursor
    monkeypatch.setattr(cursor, "STATE", tmp_path / "pointer.json")
    monkeypatch.setattr(cursor.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cursor.time, "sleep", lambda s: None)
    cursor.click(np.float32(412.5), np.float32(230.25))     # OCR boxes are numpy: this used to crash before clicking
    assert json.loads((tmp_path / "pointer.json").read_text()) == {"x": 412.5, "y": 230.25}


def test_ocr_boxes_are_plain_floats():
    import ocr
    if not ocr.enabled():
        pytest.skip("rapidocr not installed")
    img = Image.new("RGB", (400, 80), "white")
    from PIL import ImageDraw
    ImageDraw.Draw(img).text((20, 30), "Settings", fill="black")
    b = io.BytesIO(); img.save(b, "PNG")
    for _, box in ocr.texts(b.getvalue()):
        assert all(type(v) is float for v in box)
