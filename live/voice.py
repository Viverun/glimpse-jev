"""Voice input: speak a task, glimpse writes it into the prompt (English, offline, faster-whisper).

Fast by design:
- the model loads in the background as soon as voice mode is switched on, and stays loaded;
- the microphone streams raw 16 kHz audio straight into memory (arecord), no files;
- listening ends by itself ~0.8 s after you stop talking (an energy detector calibrated on the room's noise);
- decoding is tuned for short commands: int8 on the CPU (or CUDA when present), greedy, no timestamps, silence
  trimmed by Silero VAD, and glimpse's vocabulary as hotwords so app and site names come out right.

"voice_model" in config.json picks the model: "base.en" (default: fast, accurate on commands), "small.en" (slower,
steadier on names), "medium.en".
"""

import os
import re
import shutil
import subprocess
import threading
import time

import numpy as np

RATE = 16000
CHUNK = 480  # 30 ms
HOTWORDS = ("glimpse Jev LibreOffice Calc Writer Impress Zen Firefox Chrome Brave Nautilus Files Terminal GitHub "
            "x.com YouTube Gmail Discord spreadsheet PDF")

_model = None
_model_lock = threading.Lock()


def available() -> str | None:
    """None when voice works here, else what is missing."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return "faster-whisper is not installed (uv sync)"
    if not shutil.which("arecord"):
        return "arecord is not installed (sudo apt install alsa-utils)"
    return None


def model():
    """The loaded model (loaded once, then kept)."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            try:
                import config
                name = config.get("voice_model") or "base.en"
            except Exception:
                name = "base.en"
            device, compute = "cpu", "int8"
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    device, compute = "cuda", "float16"
            except Exception:
                pass
            _model = WhisperModel(name, device=device, compute_type=compute, cpu_threads=os.cpu_count() or 4)
        return _model


def warm() -> None:
    """Load the model in the background, and run it once so the first real phrase is fast."""
    def go():
        try:
            m = model()
            list(m.transcribe(np.zeros(RATE // 2, np.float32), language="en", beam_size=1)[0])
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def transcribe(audio: np.ndarray) -> str:
    """float32 mono 16 kHz -> text."""
    if audio.size < RATE * 0.2:
        return ""
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio = audio * min(4.0, 0.9 / peak)  # quiet microphones: normalise, but don't blow up the noise
    segs, _ = model().transcribe(audio, language="en", beam_size=1, best_of=1, temperature=0.0,
                                 without_timestamps=True, condition_on_previous_text=False,
                                 vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
                                 hotwords=HOTWORDS)
    text = " ".join(s.text.strip() for s in segs).strip()
    return "" if text.lower().strip(" .!?") in ("", "you", "thank you", "thanks for watching") else text  # silence artefacts


VOICE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "glimpse-voice")


def set_mode(on: bool) -> None:
    """Tell the screen glow that voice mode is on (it turns green)."""
    try:
        if on:
            open(VOICE_FILE, "w").close()
        else:
            os.unlink(VOICE_FILE)
    except OSError:
        pass


class Listener:
    """One phrase from the microphone: start() returns at once; on_state(state, level) reports progress
    ("listening", "hearing", "transcribing", "done", "error"), on_text(text) gets the result."""

    def __init__(self, on_text, on_state=lambda state, level=0.0: None, max_seconds: float = 30, end_silence: float = 0.8):
        self.on_text, self.on_state = on_text, on_state
        self.max_seconds, self.end_silence = max_seconds, end_silence
        self._stop = threading.Event()
        self.proc = None

    def start(self) -> "Listener":
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def stop(self) -> None:
        """Stop listening now (what was heard so far is still transcribed)."""
        self._stop.set()

    def cancel(self) -> None:
        self.on_text = lambda text: None
        self._stop.set()

    def _run(self) -> None:
        try:
            self.proc = subprocess.Popen(["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.on_state("error", 0.0)
            self.on_text(f"(no microphone: {e})")
            return
        chunks, floor, heard, quiet, t0 = [], [], 0.0, 0.0, time.monotonic()
        self.on_state("listening", 0.0)
        try:
            while not self._stop.is_set() and time.monotonic() - t0 < self.max_seconds:
                raw = self.proc.stdout.read(CHUNK * 2)
                if not raw:
                    break
                x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
                if len(floor) < 10:  # the first 0.3 s: the room's noise level
                    floor.append(rms)
                    continue
                gate = max(np.median(floor) * 3.0, 0.006)
                level = min(1.0, rms / (gate * 6))
                if rms > gate:
                    heard += CHUNK / RATE
                    quiet = 0.0
                    self.on_state("hearing", level)
                elif heard:
                    quiet += CHUNK / RATE
                    self.on_state("hearing", level)
                chunks.append(x)
                if not heard:
                    chunks = chunks[-10:]  # keep 0.3 s before speech starts, so the first word isn't cut
                if heard >= 0.25 and quiet >= self.end_silence:
                    break
        finally:
            self.proc.terminate()
        if not heard:
            self.on_state("done", 0.0)
            self.on_text("")
            return
        self.on_state("transcribing", 0.0)
        try:
            text = transcribe(np.concatenate(chunks))
        except Exception:
            self.on_state("error", 0.0)
            self.on_text("")
            return
        self.on_state("done", 0.0)
        self.on_text(text)


CHAT = re.compile(r"^(please )?((switch|go|change|get) (back )?to |back to )?(chat|typing|text|keyboard)( mode)?"
                  r"|^(stop|end|exit|turn off) (listening|voice( mode)?)", re.I)
STOP = re.compile(r"^(stop|cancel|abort|halt)( it| that| the task| now| please)*$", re.I)
GO_ON = re.compile(r"^(continue|resume|go on|carry on|keep going)( please)?$", re.I)


def intent(text: str) -> str:
    """What a spoken phrase means for glimpse itself: "chat" (leave voice mode), "stop", "continue", or "" (a task
    or an instruction)."""
    t = re.sub(r"[^\w\s.]", "", text or "").strip().rstrip(".").strip()
    t = re.sub(r"^(hey |ok |okay )?glimpse,? ", "", t, flags=re.I)
    if CHAT.search(t):
        return "chat"
    if STOP.match(t):
        return "stop"
    if GO_ON.match(t):
        return "continue"
    return ""


# the mic indicator's animation, one frame per tick (0.1 s)
FRAMES = {
    "listening": ["◉ ○ ○", "○ ◉ ○", "○ ○ ◉", "○ ◉ ○"],
    "transcribing": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
}
BARS = "▁▂▃▄▅▆▇█"


def indicator(state: str, level: float, tick: int) -> str:
    """The animated mic label for the prompt."""
    if state == "hearing":
        n = len(BARS) - 1
        wave = [max(0.0, min(1.0, level * (0.6 + 0.4 * np.sin(tick * 0.9 + i)))) for i in range(5)]
        return "🎙 " + "".join(BARS[int(w * n)] for w in wave)
    if state in FRAMES:
        f = FRAMES[state]
        return f"🎙 {f[tick % len(f)]}"
    return "🎙"
