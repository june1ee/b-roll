"""타임라인 조립 — 줄별 녹음을 트림해 이어 붙이고, 각 줄에 영상 구간을 배정한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import align, asr, capcut, vad
from . import ffmpeg as ff
from .project import Reel

# 녹음이 없는 줄의 길이 추정치. 한국어 낭독 속도 대략값이라 어디까지나 임시.
CHARS_PER_SEC = 5.5
MIN_LINE = 0.8
# 말이 씹히는 것보다 조금 긴 게 낫다 — 여백은 나중에 줄일 수 있지만 잘린 음절은 못 되살린다
LEAD_PAD = 0.12
TAIL_PAD = 0.25


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
    speed: float = 1.0          # 목소리 배속
    src: Path | None = None
    src_in: float = 0.0
    src_len: float = 0.0        # 러프컷 원본 길이
    src_short: float = 0.0      # 소스가 모자라 프리즈로 때운 시간
    clock: str | None = None    # 시각 레이어
    aside: str | None = None    # 괄호 부연 레이어

    @property
    def duration(self) -> float:
        return self.tl_end - self.tl_start


@dataclass
class Timeline:
    reel: Reel
    clips: list[Clip] = field(default_factory=list)
    speed: float = 1.0
    voice_source: str | None = None   # 목소리를 가져온 캡컷 프로젝트 (역방향 모드)

    @property
    def duration(self) -> float:
        return self.clips[-1].tl_end if self.clips else 0.0

    def to_dict(self) -> dict:
        return {
            "source": str(self.reel.path),
            "size": [self.reel.width, self.reel.height],
            "fps": self.reel.fps,
            "speed": round(self.speed, 3),
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
                    "speed": round(c.speed, 3),
                    "src": str(c.src) if c.src else None,
                    "src_in": round(c.src_in, 3),
                    "src_len": round(c.src_len, 3),
                    "src_short": round(c.src_short, 3),
                    "clock": c.clock,
                    "aside": c.aside,
                }
                for c in self.clips
            ],
        }


def from_capcut(reel: Reel, source: str) -> Timeline:
    """캡컷에서 이미 녹음·속도 조절까지 끝낸 프로젝트를 받아 타임라인을 만든다.

    타이밍은 추정하지 않는다 — June 이 캡컷에서 확정한 값을 그대로 쓴다.
    정렬(whisper)도 배속 추론도 필요 없고, TTS 로 만든 경우에도 똑같이 동작한다.
    n번째 목소리 세그먼트 ↔ n번째 줄로 짝짓는다.
    """
    voices = capcut.voice_segments(source)
    if not voices:
        raise ValueError(f"'{source}' 에 녹음/TTS 세그먼트가 없다")
    if len(voices) != len(reel.lines):
        raise ValueError(
            f"줄 수가 안 맞는다 — yml {len(reel.lines)}줄 vs '{source}' 목소리 {len(voices)}개.\n"
            f"  한 줄에 목소리 하나씩 1:1 이어야 한다."
        )

    tl = Timeline(reel=reel, voice_source=source)
    tl.speed = round(sum(v.speed for v in voices) / len(voices), 3)
    for i, (line, v) in enumerate(zip(reel.lines, voices), 1):
        clip = Clip(
            index=i, text=line.t,
            tl_start=v.start, tl_end=v.end,
            rec=v.src, rec_in=v.src_in, rec_out=v.src_in + v.src_dur,
            score=1.0, coverage=1.0, tightened=False, speed=v.speed,
            src=line.src, clock=line.clock, aside=line.aside,
        )
        if line.src:
            src_len = ff.duration(line.src)
            slack = max(src_len - clip.duration, 0.0)
            start = (line.frm if line.frm is not None
                     else 0.0 if line.fit == "start"
                     else slack if line.fit == "end" else slack / 2)
            clip.src_in = max(0.0, min(start, slack))
            clip.src_len = src_len
            clip.src_short = max(0.0, clip.duration - (src_len - clip.src_in))
        tl.clips.append(clip)
    return tl


def build(reel: Reel, *, on_progress=None) -> Timeline:
    tl = Timeline(reel=reel)
    cursor = 0.0
    # 목소리 배속은 포맷마다 다르다. yml 에 없으면 템플릿이 쓰던 값을 그대로 쓴다.
    default_speed = reel.speed or (capcut.voice_speed(reel.template) if reel.template else 1.0)
    tl.speed = default_speed

    for i, line in enumerate(reel.lines, 1):
        if on_progress:
            on_progress(i, len(reel.lines), line.t)

        takes, coverage, tightened = 1, 0.0, False
        speed = line.speed if line.speed else default_speed
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
            dur = max((rec_out - rec_in) / speed, MIN_LINE)
        else:
            rec_in = rec_out = score = coverage = 0.0
            tightened = False
            dur = max(len(line.t) / (CHARS_PER_SEC * speed), MIN_LINE)

        dur += line.hold
        clip = Clip(
            index=i, text=line.t,
            tl_start=cursor, tl_end=cursor + dur,
            rec=line.rec, rec_in=rec_in, rec_out=rec_out, score=score, takes=takes,
            coverage=coverage, tightened=tightened, speed=speed,
            src=line.src, clock=line.clock, aside=line.aside,
        )

        if line.src:
            # 러프컷 다듬기: June 이 넉넉하게 잘라 준 클립에서 필요한 길이만큼만 남긴다.
            # 러프컷은 보통 앞뒤에 여유를 두므로 기본은 가운데 정렬.
            src_len = ff.duration(line.src)
            slack = max(src_len - dur, 0.0)
            if line.frm is not None:
                start = line.frm
            elif line.fit == "start":
                start = 0.0
            elif line.fit == "end":
                start = slack
            else:
                start = slack / 2
            clip.src_in = max(0.0, min(start, slack))
            clip.src_len = src_len
            clip.src_short = max(0.0, dur - (src_len - clip.src_in))

        tl.clips.append(clip)
        cursor = clip.tl_end + (reel.gap if i < len(reel.lines) else 0.0)

    return tl
