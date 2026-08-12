"""whisper.cpp 래퍼 - 녹음 하나를 문자 단위 타임스탬프로 바꾼다.

정렬(align)에는 단어보다 문자 해상도가 편해서, whisper 토큰 구간 안에서
문자별 시각을 선형 보간해 내보낸다. 한국어는 어절 경계가 모호해 문자 기준이 안전하다.
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
    start: float  # 초
    end: float


def model_path() -> Path:
    p = MODEL_DIR / MODEL_NAME
    if not p.exists():
        raise RuntimeError(
            f"whisper 모델 없음: {p}\n"
            f"  curl -L --fail -o {p} {MODEL_URL}"
        )
    return p


_ASR_REV = "2"  # whisper 호출 옵션이 바뀌면 올린다 (캐시 무효화용)


def _fingerprint(audio: Path, lang: str) -> str:
    st = audio.stat()
    key = f"{audio.resolve()}|{st.st_size}|{int(st.st_mtime)}|{MODEL_NAME}|{lang}|{_ASR_REV}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def transcribe(audio: Path, lang: str = "ko") -> list[Char]:
    """녹음 → 문자별 타임스탬프. 내용 해시로 캐시하므로 재실행은 공짜다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{_fingerprint(audio, lang)}.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
    else:
        raw = _run_whisper(audio, lang)
        cache.write_text(json.dumps(raw, ensure_ascii=False))
    return _to_chars(raw)


def _run_whisper(audio: Path, lang: str) -> list[dict]:
    if not shutil.which("whisper-cli"):
        raise RuntimeError("whisper-cli 없음. `brew install whisper-cpp` 후 다시 시도.")
    with tempfile.TemporaryDirectory() as td:
        wav = ff.to_wav16k(audio, Path(td) / "in.wav")
        prefix = Path(td) / "out"
        subprocess.run(
            # -sow 없이 -ml 1 만 주면 토큰이 한글 글자 중간에서 쪼개져 JSON이 깨진 UTF-8이 된다.
            ["whisper-cli", "-m", str(model_path()), "-f", str(wav),
             "-l", lang, "-ml", "1", "-sow", "-oj", "-of", str(prefix),
             "--no-prints", "-np"],
            capture_output=True, text=True, errors="replace", check=False,
        )
        js = prefix.with_suffix(".json")
        if not js.exists():
            raise RuntimeError(f"whisper 출력 없음: {audio.name}")
        data = json.loads(js.read_bytes().decode("utf-8", errors="replace"))
    return [
        {"t0": seg["offsets"]["from"] / 1000.0,
         "t1": seg["offsets"]["to"] / 1000.0,
         "text": seg.get("text", "")}
        for seg in data.get("transcription", [])
    ]


def _to_chars(segments: list[dict]) -> list[Char]:
    """토큰 구간 안에서 문자 시각을 선형 보간. 공백은 버린다."""
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
