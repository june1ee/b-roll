"""타임라인 조립 — 줄별 녹음을 트림해 이어 붙이고, 각 줄에 영상 구간을 배정한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import align, asr, vad
from . import ffmpeg as ff
from .project import Reel

# 녹음이 없는 줄의 길이 추정치. 한국어 낭독 속도 대략값이라 어디까지나 임시.
CHARS_PER_SEC = 5.5
MIN_LINE = 0.8
LEAD_PAD = 0.06   # 발화 시작 직전을 조금 남긴다 (숨소리까지 자르면 부자연스럽다)
TAIL_PAD = 0.10


@dataclass
class Clip:
    index: int
    text: str
    tl_start: float
    tl_end: float
    rec: Path | None = None
    rec_in: float = 0.0
    rec_out: float = 0.0
    score: float = 0.0          # 정렬 신뢰도 0~1
    takes: int = 1              # 녹음 안에서 검출된 테이크 수
    coverage: float = 0.0       # 자막이 들린 말의 몇 %를 덮는가
    tightened: bool = False     # 텍스트로 좁혔는지 (아니면 테이크 통째)
    src: Path | None = None
    src_in: float = 0.0
    src_short: float = 0.0      # 소스가 모자라 프리즈로 때운 시간

    @property
    def duration(self) -> float:
        return self.tl_end - self.tl_start


@dataclass
class Timeline:
    reel: Reel
    clips: list[Clip] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.clips[-1].tl_end if self.clips else 0.0

    def to_dict(self) -> dict:
        return {
            "source": str(self.reel.path),
            "size": [self.reel.width, self.reel.height],
            "fps": self.reel.fps,
            "duration": round(self.duration, 3),
            "clips": [
                {
                    "i": c.index, "text": c.text,
                    "tl": [round(c.tl_start, 3), round(c.tl_end, 3)],
                    "rec": str(c.rec) if c.rec else None,
                    "rec_range": [round(c.rec_in, 3), round(c.rec_out, 3)],
                    "align_score": round(c.score, 3),
                    "takes": c.takes,
                    "coverage": round(c.coverage, 3),
                    "tightened": c.tightened,
                    "src": str(c.src) if c.src else None,
                    "src_in": round(c.src_in, 3),
                    "src_short": round(c.src_short, 3),
                }
                for c in self.clips
            ],
        }


def build(reel: Reel, *, on_progress=None) -> Timeline:
    tl = Timeline(reel=reel)
    cursor = 0.0

    for i, line in enumerate(reel.lines, 1):
        if on_progress:
            on_progress(i, len(reel.lines), line.t)

        takes, coverage, tightened = 1, 0.0, False
        if line.rec:
            # 원본(ADTS AAC 등) 대신 디코드된 wav 를 기준으로 잰다 — 길이가 정확해야 A/V 가 안 밀린다
            wav = ff.cached_wav(line.rec)
            spans = vad.speech_spans(wav)
            chars = asr.transcribe(wav, lang=reel.lang)
            span = align.find_span(chars, line.t, spans)
            rec_len = ff.duration(wav)
            rec_in = max(0.0, span.start - LEAD_PAD)
            rec_out = min(span.end + TAIL_PAD, rec_len)
            score, takes = span.score, span.takes
            coverage, tightened = span.coverage, span.tightened
            dur = max(rec_out - rec_in, MIN_LINE)
        else:
            rec_in = rec_out = score = coverage = 0.0
            tightened = False
            dur = max(len(line.t) / CHARS_PER_SEC, MIN_LINE)

        dur += line.hold
        clip = Clip(
            index=i, text=line.t,
            tl_start=cursor, tl_end=cursor + dur,
            rec=line.rec, rec_in=rec_in, rec_out=rec_out, score=score, takes=takes,
            coverage=coverage, tightened=tightened,
            src=line.src,
        )

        if line.src:
            src_len = ff.duration(line.src)
            want = line.frm if line.frm is not None else 0.0
            # M3에서 컷 선택이 이 값을 대체한다. 지금은 지정값 또는 0에서 시작.
            clip.src_in = max(0.0, min(want, max(src_len - dur, 0.0)))
            clip.src_short = max(0.0, dur - (src_len - clip.src_in))

        tl.clips.append(clip)
        cursor = clip.tl_end + (reel.gap if i < len(reel.lines) else 0.0)

    return tl
