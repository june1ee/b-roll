"""말한 영상 → 구절 단위 자막.

썰 포맷(0814)은 자막이 대사의 요약이 아니라 말한 구절 그대로라서, 정렬할 정답 대본이
없다. whisper 어절을 캡컷 컷 편집(잘라낸 구간·배속)에 비춰 타임라인 시각으로 옮기고
짧은 구절로 묶기만 하면 된다.
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
    """타임라인에서 실제로 들리는 소리를 그대로 이어 붙인 wav (배속·잘라낸 구간 반영).

    원본을 받아쓰고 컷에 비춰 거르면 어절 시각이 최대 1초씩 밀려 경계의 말이 빠지고,
    컷만 따로 받아쓰면 1초짜리 컷에서 whisper 가 말을 지어낸다(둘 다 실측).
    들리는 그대로를 한 번에 받아쓰면 문맥도 살고 시각도 곧바로 타임라인 시각이다.
    """
    key = "|".join(f"{c.src.resolve()}:{c.src.stat().st_mtime:.0f}:{c.src_in:.3f}:{c.src_out:.3f}:"
                   f"{c.tl_start:.3f}:{c.tl_end:.3f}" for c in cuts)
    dst = CLIP_CACHE / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.wav"
    if dst.exists():
        return dst
    CLIP_CACHE.mkdir(parents=True, exist_ok=True)
    srcs = list(dict.fromkeys(c.src for c in cuts))
    inputs = [arg for src in srcs for arg in ("-i", str(src))]
    parts, labels, t = [], [], 0.0
    for i, c in enumerate(cuts):
        if c.tl_start - t > 0.01:
            parts.append(f"aevalsrc=0:d={c.tl_start - t:.3f}:s=16000[g{i}]")
            labels.append(f"[g{i}]")
        speed = (c.src_out - c.src_in) / (c.tl_end - c.tl_start)
        parts.append(f"[{srcs.index(c.src)}:a]atrim={c.src_in:.3f}:{c.src_out:.3f},"
                     f"asetpts=PTS-STARTPTS,atempo={speed:.4f},aresample=16000,"
                     f"aformat=channel_layouts=mono[a{i}]")
        labels.append(f"[a{i}]")
        t = c.tl_end
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    ff.ffmpeg(*inputs, "-filter_complex", ";".join(parts), "-map", "[out]",
              "-c:a", "pcm_s16le", str(dst))
    return dst


def spoken_words(cuts: list[Cut]) -> list[Spoken]:
    out = []
    for w in asr.words(timeline_audio(cuts)):
        i = next((k for k, c in enumerate(cuts) if w.start < c.tl_end), len(cuts) - 1)
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


def phrases(words: list[Spoken], max_chars: int = MAX_CHARS) -> list[list[Spoken]]:
    """문장 끝·쉼·컷 경계에서 먼저 끊고, 긴 덩어리만 다시 나눈다."""
    out: list[list[Spoken]] = []
    run: list[Spoken] = []
    for w in words:
        if run:
            prev = run[-1]
            if (prev.text[-1] in ".?!,"
                    or w.cut != prev.cut
                    or w.src_start - prev.src_end > PAUSE):
                out.extend(_split(run, max_chars))
                run = []
        run.append(w)
    if run:
        out.extend(_split(run, max_chars))
    return out


def captions(project: str | Path, max_chars: int = MAX_CHARS) -> list[tuple[float, float, str]]:
    cuts = main_cuts(project)
    groups = phrases(spoken_words(cuts), max_chars)
    end_of_video = cuts[-1].tl_end if cuts else 0.0
    out = []
    for i, g in enumerate(groups):
        # 0814 에는 마침표가 없다. 물음표·느낌표는 말투라 남긴다
        text = " ".join(w.text for w in g).rstrip(".,")
        start = 0.0 if i == 0 and g[0].start < 0.5 else g[0].start
        nxt = groups[i + 1][0].start if i + 1 < len(groups) else end_of_video
        out.append((start, min(nxt, g[-1].end + HOLD), text))
    return out
