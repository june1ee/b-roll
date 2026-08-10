"""캡컷 드래프트(draft_info.json) 읽기.

CapCut 9.x 는 draft_content.json 이 아니라 draft_info.json 을 쓰고, 평문 JSON이다.
지금은 읽기 전용 — 기존 프로젝트에서 '정답 편집'을 뽑아 비교/템플릿 용도로 쓴다.
쓰기(M2)는 이 파일을 복제해 세그먼트만 교체하는 방식으로 붙인다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

US = 1_000_000  # 캡컷 시간 단위는 마이크로초
DRAFT_ROOT = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
_PLACEHOLDER = "##_draftpath_placeholder"


@dataclass
class Seg:
    start: float          # 타임라인 위치(초)
    end: float
    src: Path | None = None
    src_in: float = 0.0
    speed: float = 1.0
    text: str | None = None
    kind: str = ""        # record | music | sound | text_to_audio | video | text


@dataclass
class Draft:
    path: Path
    width: int
    height: int
    fps: float
    duration: float
    video: list[Seg] = field(default_factory=list)
    texts: list[Seg] = field(default_factory=list)
    audio: list[Seg] = field(default_factory=list)


def draft_dir(name_or_path: str | Path) -> Path:
    p = Path(name_or_path)
    if p.is_dir():
        return p
    cand = DRAFT_ROOT / str(name_or_path)
    if cand.is_dir():
        return cand
    raise FileNotFoundError(f"캡컷 프로젝트를 못 찾음: {name_or_path} (찾은 곳: {DRAFT_ROOT})")


def _resolve(raw: str, root: Path) -> Path | None:
    if not raw:
        return None
    if raw.startswith(_PLACEHOLDER):
        # ##_draftpath_placeholder_<UUID>_##/textReading/xxx.wav → <draft>/textReading/xxx.wav
        _, _, rel = raw.partition("_##/")
        return root / rel if rel else None
    return Path(os.path.expanduser(raw))


def _plain_text(content: str) -> str:
    """텍스트 소재의 content 는 스타일이 섞인 JSON 문자열이다."""
    try:
        return (json.loads(content) or {}).get("text", "") or ""
    except (json.JSONDecodeError, TypeError):
        return content or ""


def read(name_or_path: str | Path) -> Draft:
    root = draft_dir(name_or_path)
    data = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    canvas = data.get("canvas_config") or {}
    draft = Draft(
        path=root,
        width=int(canvas.get("width") or 1080),
        height=int(canvas.get("height") or 1920),
        fps=float(data.get("fps") or 30),
        duration=(data.get("duration") or 0) / US,
    )

    mats = data.get("materials") or {}
    videos = {v["id"]: v for v in mats.get("videos") or []}
    texts = {t["id"]: t for t in mats.get("texts") or []}
    audios = {a["id"]: a for a in mats.get("audios") or []}

    for track in data.get("tracks") or []:
        for s in track.get("segments") or []:
            tr = s.get("target_timerange") or {}
            sr = s.get("source_timerange") or {}
            seg = Seg(
                start=(tr.get("start") or 0) / US,
                end=((tr.get("start") or 0) + (tr.get("duration") or 0)) / US,
                src_in=(sr.get("start") or 0) / US,
                speed=float(s.get("speed") or 1.0),
            )
            mid = s.get("material_id")
            if track["type"] == "video" and mid in videos:
                v = videos[mid]
                seg.src = _resolve(v.get("path", ""), root)
                seg.kind = v.get("type") or "video"
                draft.video.append(seg)
            elif track["type"] == "text" and mid in texts:
                seg.text = _plain_text(texts[mid].get("content", ""))
                seg.kind = "text"
                draft.texts.append(seg)
            elif track["type"] == "audio" and mid in audios:
                a = audios[mid]
                seg.src = _resolve(a.get("path", ""), root)
                seg.kind = a.get("type") or "audio"
                seg.text = a.get("name")
                draft.audio.append(seg)

    for lst in (draft.video, draft.texts, draft.audio):
        lst.sort(key=lambda x: x.start)
    return draft


def main_caption_track(draft: Draft) -> list[Seg]:
    """자막 트랙이 여러 개일 때, 영상 컷 수와 가장 잘 맞는 트랙을 메인으로 본다."""
    by_start: dict[float, list[Seg]] = {}
    for t in draft.texts:
        by_start.setdefault(round(t.start, 2), []).append(t)
    cuts = {round(v.start, 2) for v in draft.video}
    # 영상 컷 시작과 겹치는 자막만 남기고, 그 중 시작이 같은 게 여럿이면 첫 번째
    picked = [segs[0] for start, segs in sorted(by_start.items()) if start in cuts]
    return picked or sorted(draft.texts, key=lambda x: x.start)
