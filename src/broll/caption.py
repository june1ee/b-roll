"""Spoken video to phrase-length captions.

A single take cut up in CapCut has no voice track, so there is nothing to pair captions with 1:1.
Line-by-line vlog captions are the spoken words with endings shortened, so a transcript touched up by hand is faster.
So: transcribe the voice the timeline plays and group it into short phrases.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import asr
from . import capcut as cc
from . import ffmpeg as ff

_AUDIO_REV = "2"  # bump when timeline_audio changes, to invalidate the cache
CLIP_CACHE = Path(os.environ.get("BROLL_CLIP_DIR", Path.home() / ".cache/broll/clips"))

MAX_CHARS = 16         # for Korean; the longest real caption, 19 characters with spaces, still fit one line
PAUSE = 0.35           # a pause this long in the source ends a phrase
# Korean clause endings, where a break follows the speaker's breathing. Other languages split long runs by length only.
CLAUSE_ENDS = ("고", "서", "데", "니까", "면", "요", "다", "까", "며", "만", "게", "죠")
HOLD = 1.5             # how long a caption stays up after speech ends when nothing follows


@dataclass
class Cut:
    tl_start: float
    tl_end: float
    src: Path
    src_in: float
    src_out: float

    def to_tl(self, t: float) -> float:
        rate = (self.tl_end - self.tl_start) / (self.src_out - self.src_in)
        return self.tl_start + (t - self.src_in) * rate


@dataclass
class Spoken:
    text: str
    src_start: float
    src_end: float
    start: float       # on the timeline
    end: float
    cut: int


def voice_cuts(project: str | Path) -> list[Cut]:
    """Line-by-line recordings and TTS on the audio tracks, where a vlog keeps its voice."""
    cuts = []
    for v in cc.voice_segments(project):
        if not v.src or not v.src.exists():
            raise FileNotFoundError(f"recording not found: {v.src}")
        cuts.append(Cut(v.start, v.end, v.src, v.src_in, v.src_in + v.src_dur))
    return cuts


def main_cuts(project: str | Path) -> list[Cut]:
    """The first video track is the talking footage; b-roll on tracks above is assumed silent."""
    root = cc.draft_dir(project)
    raw = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    videos = {v["id"]: v for v in raw["materials"].get("videos") or []}
    track = next((t for t in raw["tracks"] if t["type"] == "video"), None)
    if not track:
        raise RuntimeError(f"{root.name}: no video track")
    cuts = []
    for s in sorted(track["segments"], key=lambda s: s["target_timerange"]["start"]):
        mat = videos.get(s["material_id"])
        src = cc._resolve(mat.get("path", ""), root) if mat else None
        if not src:
            continue
        if not src.exists():
            raise FileNotFoundError(f"source video not found: {src} (if you moved it, relink it in CapCut)")
        tr, sr = s["target_timerange"], s["source_timerange"]
        cuts.append(Cut(tr["start"] / cc.US, (tr["start"] + tr["duration"]) / cc.US, src,
                        sr["start"] / cc.US, (sr["start"] + sr["duration"]) / cc.US))
    return cuts


def timeline_audio(cuts: list[Cut]) -> Path:
    """A wav of only the voice the timeline plays, in place, with speed and trims applied.

    Transcribing the source and filtering by cut drifts word times by up to 1 s and drops edge
    words; transcribing each cut alone makes whisper invent words on 1 s cuts (both measured).
    One pass over what is heard keeps the context, and its times are already timeline times.
    """
    key = _AUDIO_REV + "|".join(f"{c.src.resolve()}:{c.src.stat().st_mtime:.0f}:{c.src_in:.3f}:{c.src_out:.3f}:"
                   f"{c.tl_start:.3f}:{c.tl_end:.3f}" for c in cuts)
    dst = CLIP_CACHE / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.wav"
    if dst.exists():
        return dst
    CLIP_CACHE.mkdir(parents=True, exist_ok=True)
    # CapCut recordings (raw AAC) trim short unless decoded to wav first
    srcs = list(dict.fromkeys(c.src for c in cuts))
    inputs = [arg for src in srcs for arg in ("-i", str(ff.cached_wav(src, rate=16000, channels=1)))]
    parts = []
    for i, c in enumerate(cuts):
        speed = (c.src_out - c.src_in) / (c.tl_end - c.tl_start)
        delay = round(c.tl_start * 1000)
        parts.append(f"[{srcs.index(c.src)}:a]atrim={c.src_in:.3f}:{c.src_out:.3f},"
                     f"asetpts=PTS-STARTPTS,atempo={speed:.4f},adelay={delay}:all=1[a{i}]")
    # mix rather than concatenate, so overlapping recordings stay in place
    parts.append("".join(f"[a{i}]" for i in range(len(cuts)))
                 + f"amix=inputs={len(cuts)}:normalize=0:duration=longest[out]")
    ff.ffmpeg(*inputs, "-filter_complex", ";".join(parts), "-map", "[out]",
              "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst))
    return dst


def spoken_words(cuts: list[Cut], lang: str = "ko") -> list[Spoken]:
    out = []
    for w in asr.words(timeline_audio(cuts), lang):
        # which cut a word came from; a new cut can start a new line
        i = next((k for k, c in enumerate(cuts) if c.tl_start <= (w.start + w.end) / 2 < c.tl_end),
                 min(range(len(cuts)), key=lambda k: abs(cuts[k].tl_start - w.start)))
        out.append(Spoken(w.text, w.start, w.end, w.start, w.end, i))
    return out


def _split(run: list[Spoken], max_chars: int) -> list[list[Spoken]]:
    """Choose break points: lines within max_chars, preferring clause endings and fewer lines."""
    n = len(run)
    best: list[tuple[float, int]] = [(0.0, 0)] + [(float("inf"), 0)] * n
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            text = " ".join(w.text for w in run[i:j])
            if len(text) > max_chars and j - i > 1:
                break
            cost = best[i][0] + 1.0
            last = run[j - 1].text
            if j < n and (len(last) < 2 or not last.endswith(CLAUSE_ENDS)):
                cost += 1.5
            if len(text) < 5:
                cost += 1.0
            if cost < best[j][0]:
                best[j] = (cost, i)
    cuts, j = [], n
    while j > 0:
        i = best[j][1]
        cuts.append(run[i:j])
        j = i
    return cuts[::-1]


def phrases(words: list[Spoken], max_chars: int = MAX_CHARS,
            break_on_cut: bool = True) -> list[list[Spoken]]:
    """Break at sentence ends, pauses and cut boundaries first, then split only the long runs.

    In a single take, cut boundaries are often jump cuts in the middle of a word, so breaking
    there leaves 0.1 s captions. Cut boundaries break lines only for line-by-line recordings.
    """
    out: list[list[Spoken]] = []
    run: list[Spoken] = []
    for w in words:
        if run:
            prev = run[-1]
            if (prev.text[-1] in ".?!,"
                    or (break_on_cut and w.cut != prev.cut)
                    or w.src_start - prev.src_end > PAUSE):
                out.extend(_split(run, max_chars))
                run = []
        run.append(w)
    if run:
        out.extend(_split(run, max_chars))
    return out


def captions(project: str | Path, max_chars: int = MAX_CHARS, source: str = "auto",
             lang: str = "ko") -> tuple[str, list[tuple[float, float, str]]]:
    """source: voice (audio-track recordings) | video (first video track) | auto (voice if any)."""
    if source == "auto":
        source = "voice" if cc.voice_segments(project) else "video"
    cuts = voice_cuts(project) if source == "voice" else main_cuts(project)
    if not cuts:
        raise RuntimeError(f"{project}: nothing to transcribe from the {source} track")
    cuts.sort(key=lambda c: c.tl_start)
    groups = phrases(spoken_words(cuts, lang), max_chars, break_on_cut=source == "voice")
    end_of_video = max(cc.read(project).duration, cuts[-1].tl_end)
    out = []
    for i, g in enumerate(groups):
        # captions drop full stops; question and exclamation marks carry tone, so they stay
        text = " ".join(w.text for w in g).rstrip(".,")
        start = 0.0 if i == 0 and g[0].start < 0.5 else g[0].start
        nxt = groups[i + 1][0].start if i + 1 < len(groups) else end_of_video
        out.append((start, min(nxt, g[-1].end + HOLD), text))
    return source, out
