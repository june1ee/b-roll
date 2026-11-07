"""Timeline assembly: trim and join the per-line recordings and give each line a video span."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import align, asr, capcut, vad
from . import ffmpeg as ff
from .project import Reel

# Length of a line with no recording, from a rough Korean reading speed. A placeholder.
CHARS_PER_SEC = 5.5
MIN_LINE = 0.8
# err long: slack can be trimmed later, a clipped syllable cannot be restored
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
    score: float = 0.0          # alignment confidence, 0-1
    takes: int = 1              # takes detected in the recording
    coverage: float = 0.0       # share of the heard speech the caption covers
    tightened: bool = False     # tightened from text, else the whole take
    speed: float = 1.0          # voice speed
    src: Path | None = None
    src_in: float = 0.0
    src_len: float = 0.0        # length of the rough clip
    src_short: float = 0.0      # time filled with a freeze frame because the source ran short
    clock: str | None = None    # clock layer
    aside: str | None = None    # aside layer

    @property
    def duration(self) -> float:
        return self.tl_end - self.tl_start


@dataclass
class Timeline:
    reel: Reel
    clips: list[Clip] = field(default_factory=list)
    speed: float = 1.0
    speed_origin: str = ""            # where the speed came from
    voice_source: str | None = None   # CapCut project the voice came from (--from-capcut)

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


def _fit_pool(pool_start: float, pool_len: float, want: float, line) -> tuple[float, float]:
    """Keep `want` seconds of the rough clip's pool, and return the shortfall too."""
    slack = max(pool_len - want, 0.0)
    if line.frm is not None:
        offset = line.frm
    elif line.fit == "start":
        offset = 0.0
    elif line.fit == "end":
        offset = slack
    else:
        offset = slack / 2
    return pool_start + max(0.0, min(offset, slack)), max(0.0, want - pool_len)


def from_capcut(reel: Reel, source: str) -> Timeline:
    """Build the timeline from a CapCut project whose voice is already recorded and sped up.

    Timing is read, not estimated: CapCut's values are used as is, so no whisper
    alignment or speed inference is needed, and TTS works the same way.
    The nth voice segment pairs with the nth line.
    """
    voices = capcut.voice_segments(source)
    if not voices:
        raise ValueError(f"'{source}' has no voice recordings or TTS on its audio tracks")
    if len(voices) != len(reel.lines):
        raise ValueError(
            f"line count mismatch - the yml has {len(reel.lines)} lines but '{source}' has "
            f"{len(voices)} voice segments.\n  Each line needs exactly one voice segment."
        )

    # rough clips on the CapCut video track become the source pools, so the yml needs no paths
    rough = capcut.read(source).video
    if rough and not any(l.src for l in reel.lines) and len(rough) != len(voices):
        raise ValueError(
            f"'{source}' has {len(rough)} video clips but {len(voices)} voice segments.\n"
            f"  Put one rough clip per line on the video track, or give each line a src: in the yml."
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
        if line.src:                                   # the whole file named in the yml is the pool
            clip.src_len = ff.duration(line.src)
            clip.src_in, clip.src_short = _fit_pool(0.0, clip.src_len, clip.duration, line)
        elif i <= len(rough) and rough[i - 1].src:      # the rough clip on the CapCut video track is the pool
            r = rough[i - 1]
            clip.src = r.src
            clip.src_len = r.src_dur or ff.duration(r.src)
            clip.src_in, clip.src_short = _fit_pool(r.src_in, clip.src_len, clip.duration, line)
        tl.clips.append(clip)
    return tl


def build(reel: Reel, *, on_progress=None) -> Timeline:
    tl = Timeline(reel=reel)
    cursor = 0.0
    # Voice speed differs per format. Without one in the yml, use the template's.
    # (--from-capcut reads the speed set in CapCut instead.)
    if reel.speed:
        default_speed, origin = reel.speed, "set in the yml"
    elif reel.template:
        default_speed, origin = capcut.voice_speed(reel.template), f"from template {reel.template}"
    else:
        default_speed, origin = 1.0, "default"
    tl.speed = default_speed
    tl.speed_origin = origin

    for i, line in enumerate(reel.lines, 1):
        if on_progress:
            on_progress(i, len(reel.lines), line.t)

        takes, coverage, tightened = 1, 0.0, False
        speed = line.speed if line.speed else default_speed
        if line.rec:
            # measure the decoded wav, not the ADTS AAC source: a wrong length drifts A/V
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
            # rough clips usually have slack at both ends, so centre by default
            clip.src_len = ff.duration(line.src)
            clip.src_in, clip.src_short = _fit_pool(0.0, clip.src_len, dur, line)

        tl.clips.append(clip)
        cursor = clip.tl_end + (reel.gap if i < len(reel.lines) else 0.0)

    return tl
