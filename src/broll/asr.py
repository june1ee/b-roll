"""whisper.cpp wrapper: one recording to per-character timestamps.

Alignment works better at character than word resolution, so character times are
interpolated inside each whisper token. Korean word boundaries are ambiguous, so characters are safer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg as ff

MODEL_DIR = Path(os.environ.get("BROLL_MODEL_DIR", Path.home() / ".cache/broll/models"))
MODEL_NAME = os.environ.get("BROLL_WHISPER_MODEL", "ggml-large-v3-turbo.bin")
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"
CACHE_DIR = Path(os.environ.get("BROLL_CACHE_DIR", Path.home() / ".cache/broll/asr"))


@dataclass(frozen=True)
class Char:
    ch: str
    start: float  # seconds
    end: float


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


def model_path() -> Path:
    p = MODEL_DIR / MODEL_NAME
    if not p.exists():
        raise RuntimeError(
            f"whisper model not found: {p}\n"
            f"  curl -L --fail -o {p} {MODEL_URL}"
        )
    return p


_ASR_REV = "2"  # bump when the whisper options change, to invalidate the cache


def _fingerprint(audio: Path, lang: str) -> str:
    st = audio.stat()
    key = f"{audio.resolve()}|{st.st_size}|{int(st.st_mtime)}|{MODEL_NAME}|{lang}|{_ASR_REV}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _segments(audio: Path, lang: str) -> list[dict]:
    """Cached by content hash, so reruns are free."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{_fingerprint(audio, lang)}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    raw = _run_whisper(audio, lang)
    cache.write_text(json.dumps(raw, ensure_ascii=False))
    return raw


def transcribe(audio: Path, lang: str = "ko") -> list[Char]:
    """Recording to per-character timestamps."""
    return _to_chars(_segments(audio, lang))


def words(audio: Path, lang: str = "ko") -> list[Word]:
    """Recording to per-word timestamps. With -ml 1 -sow each whisper segment is one word."""
    return [Word(seg["text"].strip(), seg["t0"], seg["t1"])
            for seg in _segments(audio, lang) if (seg["text"] or "").strip()]


def _run_whisper(audio: Path, lang: str) -> list[dict]:
    if not shutil.which("whisper-cli"):
        raise RuntimeError("whisper-cli not found. Install it with `brew install whisper-cpp`.")
    with tempfile.TemporaryDirectory() as td:
        wav = ff.to_wav16k(audio, Path(td) / "in.wav")
        prefix = Path(td) / "out"
        subprocess.run(
            # -ml 1 without -sow splits tokens inside Hangul syllables and the JSON gets broken UTF-8.
            ["whisper-cli", "-m", str(model_path()), "-f", str(wav),
             "-l", lang, "-ml", "1", "-sow", "-oj", "-of", str(prefix),
             "--no-prints", "-np"],
            capture_output=True, text=True, errors="replace", check=False,
        )
        js = prefix.with_suffix(".json")
        if not js.exists():
            raise RuntimeError(f"whisper produced no output for {audio.name}")
        data = json.loads(js.read_bytes().decode("utf-8", errors="replace"))
    return [
        {"t0": seg["offsets"]["from"] / 1000.0,
         "t1": seg["offsets"]["to"] / 1000.0,
         "text": seg.get("text", "")}
        for seg in data.get("transcription", [])
    ]


def _to_chars(segments: list[dict]) -> list[Char]:
    """Interpolate character times linearly inside a token. Spaces are dropped."""
    out: list[Char] = []
    for seg in segments:
        text = (seg["text"] or "").strip()
        if not text:
            continue
        t0, t1 = seg["t0"], seg["t1"]
        span = max(t1 - t0, 1e-4)
        step = span / len(text)
        for i, ch in enumerate(text):
            if ch.isspace():
                continue
            out.append(Char(ch, t0 + i * step, t0 + (i + 1) * step))
    return out
