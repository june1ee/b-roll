"""무음 기반 발화 구간 검출.

whisper.cpp 의 세그먼트 타임스탬프는 경계가 0.5~1초씩 밀린다(실측). 그래서 역할을 나눈다:
**무엇을 말했나는 whisper, 어디서 시작하고 끝났나는 무음 검출.**
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import ffmpeg as ff

_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


# -32dB 로는 한국어 어미("~요", "~죠")처럼 조용히 흘리는 문장 끝을 잘라먹는다.
# 0806 정답지에 맞춰 훑어본 결과 -42dB 에서 끝 경계 오차가 0.72s → 0.11s 로 떨어졌다.
# 더 낮추면(-46dB) 숨소리까지 발화로 잡아 테이크 구분이 무너진다.
DEFAULT_NOISE_DB = -42


def speech_spans(audio: Path, *, noise_db: int = DEFAULT_NOISE_DB, min_silence: float = 0.15,
                 min_speech: float = 0.15) -> list[tuple[float, float]]:
    """[(start, end), ...] 발화 구간. 아무것도 못 찾으면 파일 전체 한 덩어리."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg 없음")
    total = ff.duration(audio)
    cp = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    log = cp.stderr or ""
    starts = [float(x) for x in _START.findall(log)]
    ends = [float(x) for x in _END.findall(log)]

    # 무음 구간을 만들어 뒤집는다. silence_end 가 먼저 나오면 파일이 무음으로 시작한 것.
    silences: list[tuple[float, float]] = []
    if len(ends) > len(starts):
        silences.append((0.0, ends[0]))
        ends = ends[1:]
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        silences.append((max(s, 0.0), min(e, total)))

    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s - cursor >= min_speech:
            spans.append((cursor, s))
        cursor = max(cursor, e)
    if total - cursor >= min_speech:
        spans.append((cursor, total))

    return spans or [(0.0, total)]
