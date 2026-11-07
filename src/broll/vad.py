"""Speech span detection from silence.

whisper.cpp segment timestamps drift 0.5-1 s at the edges (measured), so the jobs are split:
**whisper says what was said; silence detection says where it starts and stops.**
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import ffmpeg as ff

_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


# At -32 dB, quietly trailing sentence endings (common in Korean) get clipped.
# Against a hand-edited reference, -42 dB cut the end-edge error from 0.72 s to 0.11 s.
# Lower (-46 dB) counts breaths as speech and takes stop separating.
DEFAULT_NOISE_DB = -42


def speech_spans(audio: Path, *, noise_db: int = DEFAULT_NOISE_DB, min_silence: float = 0.15,
                 min_speech: float = 0.15) -> list[tuple[float, float]]:
    """Speech spans [(start, end), ...]; the whole file as one span if none are found."""
    total = ff.duration(audio)
    cp = subprocess.run(
        [ff.binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(audio),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    log = cp.stderr or ""
    starts = [float(x) for x in _START.findall(log)]
    ends = [float(x) for x in _END.findall(log)]

    # build the silences, then invert them. A leading silence_end means the file starts silent.
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
