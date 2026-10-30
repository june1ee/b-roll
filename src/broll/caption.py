"""말한 영상 → 구절 단위 자막.

썰 포맷(0814)은 자막이 말한 구절 그대로이고 녹음 트랙도 없어서 1:1로 짝지을 게 없다.
브이로그(0806)도 자막은 말한 그대로를 어미만 줄인 것이라, 받아쓴 걸 깔고 손으로 다듬는 게 빠르다.
타임라인에서 들리는 목소리를 받아쓰고 짧은 구절로 묶기만 하면 된다.
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

_AUDIO_REV = "2"  # timeline_audio 만드는 방식이 바뀌면 올린다 (캐시 무효화용)
CLIP_CACHE = Path(os.environ.get("BROLL_CLIP_DIR", Path.home() / ".cache/broll/clips"))

MAX_CHARS = 16         # 0814 의 가장 긴 줄이 공백 포함 19자. 한 줄로 안 접히는 선
PAUSE = 0.35           # 원본에서 이만큼 쉬면 구절을 끊는다
# 여기서 끊으면 말의 호흡과 맞는다 (연결어미·종결어미)
CLAUSE_ENDS = ("고", "서", "데", "니까", "면", "요", "다", "까", "며", "만", "게", "죠")
HOLD = 1.5             # 말이 끝난 뒤 다음 자막까지 비면 이만큼만 더 띄운다


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
    start: float       # 타임라인
    end: float
    cut: int


def voice_cuts(project: str | Path) -> list[Cut]:
    """오디오 트랙의 줄별 녹음·TTS. 브이로그 포맷은 목소리가 여기 있다."""
    cuts = []
    for v in cc.voice_segments(project):
        if not v.src or not v.src.exists():
            raise FileNotFoundError(f"녹음 파일을 못 찾음: {v.src}")
        cuts.append(Cut(v.start, v.end, v.src, v.src_in, v.src_in + v.src_dur))
    return cuts


def main_cuts(project: str | Path) -> list[Cut]:
    """첫 영상 트랙이 말하는 영상이다. 위 트랙의 b-roll 은 소리를 안 쓴다고 본다."""
    root = cc.draft_dir(project)
    raw = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    videos = {v["id"]: v for v in raw["materials"].get("videos") or []}
    track = next((t for t in raw["tracks"] if t["type"] == "video"), None)
    if not track:
        raise RuntimeError(f"{root.name}: 영상 트랙이 없다")
    cuts = []
    for s in sorted(track["segments"], key=lambda s: s["target_timerange"]["start"]):
        mat = videos.get(s["material_id"])
        src = cc._resolve(mat.get("path", ""), root) if mat else None
        if not src:
            continue
        if not src.exists():
            raise FileNotFoundError(f"원본 영상을 못 찾음: {src} (옮겼다면 캡컷에서 다시 연결)")
        tr, sr = s["target_timerange"], s["source_timerange"]
        cuts.append(Cut(tr["start"] / cc.US, (tr["start"] + tr["duration"]) / cc.US, src,
                        sr["start"] / cc.US, (sr["start"] + sr["duration"]) / cc.US))
    return cuts


def timeline_audio(cuts: list[Cut]) -> Path:
    """타임라인에서 실제로 들리는 목소리만 제자리에 놓은 wav (배속·잘라낸 구간 반영).

    원본을 받아쓰고 컷에 비춰 거르면 어절 시각이 최대 1초씩 밀려 경계의 말이 빠지고,
    컷만 따로 받아쓰면 1초짜리 컷에서 whisper 가 말을 지어낸다(둘 다 실측).
    들리는 그대로를 한 번에 받아쓰면 문맥도 살고 시각도 곧바로 타임라인 시각이다.
    """
    key = _AUDIO_REV + "|".join(f"{c.src.resolve()}:{c.src.stat().st_mtime:.0f}:{c.src_in:.3f}:{c.src_out:.3f}:"
                   f"{c.tl_start:.3f}:{c.tl_end:.3f}" for c in cuts)
    dst = CLIP_CACHE / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.wav"
    if dst.exists():
        return dst
    CLIP_CACHE.mkdir(parents=True, exist_ok=True)
    # 캡컷 녹음(raw AAC)은 그대로 자르면 구간이 짧아지므로 wav 로 풀어서 쓴다
    srcs = list(dict.fromkeys(c.src for c in cuts))
    inputs = [arg for src in srcs for arg in ("-i", str(ff.cached_wav(src, rate=16000, channels=1)))]
    parts = []
    for i, c in enumerate(cuts):
        speed = (c.src_out - c.src_in) / (c.tl_end - c.tl_start)
        delay = round(c.tl_start * 1000)
        parts.append(f"[{srcs.index(c.src)}:a]atrim={c.src_in:.3f}:{c.src_out:.3f},"
                     f"asetpts=PTS-STARTPTS,atempo={speed:.4f},adelay={delay}:all=1[a{i}]")
    # 겹치는 녹음이 있어도 제자리에 놓이도록 이어 붙이지 않고 섞는다
    parts.append("".join(f"[a{i}]" for i in range(len(cuts)))
                 + f"amix=inputs={len(cuts)}:normalize=0:duration=longest[out]")
    ff.ffmpeg(*inputs, "-filter_complex", ";".join(parts), "-map", "[out]",
              "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst))
    return dst


def spoken_words(cuts: list[Cut]) -> list[Spoken]:
    out = []
    for w in asr.words(timeline_audio(cuts)):
        # 어느 컷에서 나온 말인지 - 컷이 바뀌면 줄도 바꾼다
        i = next((k for k, c in enumerate(cuts) if c.tl_start <= (w.start + w.end) / 2 < c.tl_end),
                 min(range(len(cuts)), key=lambda k: abs(cuts[k].tl_start - w.start)))
        out.append(Spoken(w.text, w.start, w.end, w.start, w.end, i))
    return out


def _split(run: list[Spoken], max_chars: int) -> list[list[Spoken]]:
    """끊을 자리를 고른다. 줄은 max_chars 이하, 어미에서 끊을수록, 줄이 적을수록 좋다."""
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
    """문장 끝·쉼·컷 경계에서 먼저 끊고, 긴 덩어리만 다시 나눈다.

    통짜 테이크의 컷 경계는 말 중간을 자른 점프컷이 많아서(실측: "두 / 건이었는데요")
    거기서 끊으면 0.1초짜리 자막이 생긴다. 줄별 녹음일 때만 컷 경계에서 끊는다.
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


def captions(project: str | Path, max_chars: int = MAX_CHARS,
             source: str = "auto") -> tuple[str, list[tuple[float, float, str]]]:
    """source: voice(오디오 트랙 녹음) | video(첫 영상 트랙) | auto(녹음이 있으면 voice)."""
    if source == "auto":
        source = "voice" if cc.voice_segments(project) else "video"
    cuts = voice_cuts(project) if source == "voice" else main_cuts(project)
    if not cuts:
        raise RuntimeError(f"{project}: 받아쓸 {source} 구간이 없다")
    cuts.sort(key=lambda c: c.tl_start)
    groups = phrases(spoken_words(cuts), max_chars, break_on_cut=source == "voice")
    end_of_video = max(cc.read(project).duration, cuts[-1].tl_end)
    out = []
    for i, g in enumerate(groups):
        # 0814 에는 마침표가 없다. 물음표·느낌표는 말투라 남긴다
        text = " ".join(w.text for w in g).rstrip(".,")
        start = 0.0 if i == 0 and g[0].start < 0.5 else g[0].start
        nxt = groups[i + 1][0].start if i + 1 < len(groups) else end_of_video
        out.append((start, min(nxt, g[-1].end + HOLD), text))
    return source, out
