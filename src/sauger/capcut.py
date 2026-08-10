"""캡컷 드래프트(draft_info.json) 읽기.

CapCut 9.x 는 draft_content.json 이 아니라 draft_info.json 을 쓰고, 평문 JSON이다.
지금은 읽기 전용 — 기존 프로젝트에서 '정답 편집'을 뽑아 비교/템플릿 용도로 쓴다.
쓰기(M2)는 이 파일을 복제해 세그먼트만 교체하는 방식으로 붙인다.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
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


CLOCK_RE = re.compile(r"^\s*\d{1,2}\s*:\s*\d{2}\s*$")


def classify_caption(text: str) -> str:
    """자막 3층 구조 분류. 실측: 시각 / 메인 / 괄호 부연."""
    t = (text or "").strip()
    if CLOCK_RE.match(t):
        return "clock"
    if t.startswith("(") or t.startswith("（"):
        return "aside"
    return "main"


@dataclass
class CaptionStyle:
    """템플릿에서 읽어낸 자막 한 층의 생김새. 미리보기 번인용으로 ASS 에 옮긴다."""
    kind: str
    family: str          # libass 가 찾을 폰트 패밀리
    size: float          # 캡컷 글꼴 크기
    color: str           # #RRGGBB
    bold: bool
    stroke: float        # 캡컷 stroke width (정규화값)
    shadow_alpha: float  # 0~1
    y: float             # 캡컷 transform.y
    scale_x: float
    scale_y: float

    def y_px(self, height: int) -> float:
        # 실측 보정: 캡컷 transform.y 는 화면 '절반' 높이 기준이다.
        # (0806 의 세 레이어 -0.272/-0.442/-0.589 를 렌더 위치와 맞춰 확인)
        return height / 2 * (1 - self.y)


# 폰트 파일명 → libass 가 찾는 패밀리. fc-list 로 확인한 이름.
_FAMILY = {
    "Pretendard-Medium": "Pretendard Medium",
    "Pretendard-Bold": "Pretendard",
    "MemomentKkukkukk": "메모먼트꾹꾹체",
}


def _family_of(font_path: str) -> str:
    stem = Path(font_path or "").stem
    return _FAMILY.get(stem, stem.split("-")[0] or "Pretendard")


def _hex(fill) -> str:
    try:
        r, g, b = (fill or {}).get("content", {}).get("solid", {}).get("color", [1, 1, 1])
    except Exception:
        r = g = b = 1.0
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in (r, g, b))


def caption_styles(template: str | Path) -> dict[str, CaptionStyle]:
    """템플릿 프로젝트의 자막 층별 생김새를 읽는다.

    미리보기에서 스타일을 손으로 정하려 들면 절대 못 맞춘다(실측으로 확인).
    캡컷이 들고 있는 값을 그대로 가져오는 게 유일하게 맞는 방법이다.
    """
    root = draft_dir(template)
    raw = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    texts = {t["id"]: t for t in raw["materials"].get("texts") or []}
    out: dict[str, CaptionStyle] = {}
    for track in raw["tracks"]:
        if track["type"] != "text":
            continue
        for seg in track.get("segments") or []:
            mat = texts.get(seg.get("material_id"))
            if not mat:
                continue
            content = json.loads(mat.get("content") or "{}")
            kind = classify_caption(content.get("text", ""))
            if kind in out:
                continue
            style = (content.get("styles") or [{}])[0]
            strokes = style.get("strokes") or [{}]
            clip = seg.get("clip") or {}
            scale = clip.get("scale") or {}
            out[kind] = CaptionStyle(
                kind=kind,
                family=_family_of(mat.get("font_path", "")),
                size=float(style.get("size") or mat.get("font_size") or 13),
                color=_hex(style.get("fill")),
                bold=bool(style.get("bold")),
                stroke=float(strokes[0].get("width") or 0.017),
                shadow_alpha=float(mat.get("shadow_alpha") or 0.12),
                y=float((clip.get("transform") or {}).get("y") or 0.0),
                scale_x=float(scale.get("x") or 1.0),
                scale_y=float(scale.get("y") or 1.0),
            )
    return out


def voice_speed(template: str | Path) -> float:
    """템플릿이 목소리에 걸어둔 배속(중앙값).

    실측: 0806 은 1.30~1.50(평균 1.34), 0703 은 1.10~1.26, 0718(TTS) 은 1.5~2.0.
    포맷마다 템포가 다르니 값을 박아두지 않고 템플릿에서 읽는다.
    """
    root = draft_dir(template)
    raw = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    auds = {a["id"]: a for a in raw["materials"].get("audios") or []}
    speeds = [
        float(s.get("speed") or 1.0)
        for track in raw["tracks"] if track["type"] == "audio"
        for s in track.get("segments") or []
        if (auds.get(s.get("material_id")) or {}).get("type") in ("record", "text_to_audio")
    ]
    if not speeds:
        return 1.0
    speeds.sort()
    return round(speeds[len(speeds) // 2], 3)


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _prototypes(raw: dict) -> dict:
    """템플릿에서 종류별 '견본' 세그먼트+소재를 뽑는다.

    스타일·효과·위치가 전부 이 견본에 들어있으므로, 복제해서 텍스트와 시간만 갈아끼우면
    June 이 캡컷에서 잡아둔 모양이 그대로 나온다. 직접 재현하려 들면 절대 못 맞춘다.
    """
    mats = raw["materials"]
    texts = {t["id"]: t for t in mats.get("texts") or []}
    protos: dict = {"text": {}, "video": None, "audio": None}
    for track in raw["tracks"]:
        for seg in track.get("segments") or []:
            mid = seg.get("material_id")
            if track["type"] == "text" and mid in texts:
                kind = classify_caption(_plain_text(texts[mid].get("content", "")))
                protos["text"].setdefault(kind, (track, seg, texts[mid]))
            elif track["type"] == "video" and protos["video"] is None:
                mat = next((v for v in mats.get("videos") or [] if v["id"] == mid), None)
                if mat:
                    protos["video"] = (track, seg, mat)
            elif track["type"] == "audio" and protos["audio"] is None:
                mat = next((a for a in mats.get("audios") or []
                            if a["id"] == mid and a.get("type") == "record"), None)
                if mat:
                    protos["audio"] = (track, seg, mat)
    return protos


def _seg_from(proto: dict, material_id: str, start: float, dur: float,
              src_in: float = 0.0, src_dur: float | None = None,
              speed: float = 1.0) -> dict:
    """target 은 타임라인에서 차지하는 시간, source 는 원본에서 소비하는 시간.
    배속을 걸면 source = target × speed 다."""
    seg = copy.deepcopy(proto)
    seg["id"] = _uid()
    seg["material_id"] = material_id
    seg["target_timerange"] = {"start": round(start * US), "duration": round(dur * US)}
    if seg.get("source_timerange") is not None:
        seg["source_timerange"] = {"start": round(src_in * US),
                                   "duration": round((src_dur if src_dur is not None else dur * speed) * US)}
    seg["speed"] = round(speed, 4)
    for key in ("common_keyframes", "keyframe_refs"):
        if isinstance(seg.get(key), list):
            seg[key] = []
    return seg


def write_draft(template: str | Path, name: str, timeline, *, overwrite: bool = False) -> Path:
    """타임라인을 캡컷 드래프트로 쓴다. 템플릿 프로젝트를 복제한 뒤 세그먼트만 교체한다."""
    src_dir = draft_dir(template)
    dst_dir = DRAFT_ROOT / name
    if dst_dir.exists():
        if not overwrite:
            raise FileExistsError(f"이미 있는 캡컷 프로젝트다: {name} (덮어쓰려면 --overwrite)")
        shutil.rmtree(dst_dir)
    # 백업·임시 파일만 뺀다. 나머지는 뭐가 필요한지 확실치 않으니 통째로 가져간다.
    # (캡컷이 켜져 있으면 새 폴더를 몇 초 안에 알아서 목록에 등록한다 — 실측)
    shutil.copytree(src_dir, dst_dir, ignore=shutil.ignore_patterns(
        "draft_info.json.bak", "template-*.tmp"))

    raw = json.loads((src_dir / "draft_info.json").read_text(encoding="utf-8"))
    protos = _prototypes(raw)
    if not protos["video"] or not protos["text"].get("main"):
        raise RuntimeError(f"템플릿 {src_dir.name} 에 영상/자막 견본이 없다")

    mats = raw["materials"]
    videos, texts, audios = [], [], []
    v_track, t_tracks, a_track = [], {}, []

    for c in timeline.clips:
        start, dur = c.tl_start, c.duration

        # 영상
        vt_proto, vs_proto, vm_proto = protos["video"]
        vm = copy.deepcopy(vm_proto)
        vm["id"] = _uid()
        vm["local_material_id"] = str(uuid.uuid4())
        if c.src:
            vm["path"] = str(Path(c.src).resolve())
            vm["material_name"] = Path(c.src).name
            vm["duration"] = round((c.src_len or dur) * US)
        videos.append(vm)
        v_track.append(_seg_from(vs_proto, vm["id"], start, dur, c.src_in))

        # 자막 3층
        for kind, text in (("main", c.text), ("clock", c.clock), ("aside", c.aside)):
            proto = protos["text"].get(kind)
            if not text or not proto:
                continue
            _, ts_proto, tm_proto = proto
            tm = copy.deepcopy(tm_proto)
            tm["id"] = _uid()
            content = json.loads(tm.get("content") or "{}")
            content["text"] = text
            for style in content.get("styles") or []:
                if isinstance(style.get("range"), list) and len(style["range"]) == 2:
                    style["range"] = [0, len(text)]
            tm["content"] = json.dumps(content, ensure_ascii=False)
            texts.append(tm)
            t_tracks.setdefault(kind, []).append(_seg_from(ts_proto, tm["id"], start, dur))

        # 녹음 — 원본 파일을 참조하고 잘라 쓴 구간만 source_timerange 로 지정
        if c.rec and protos["audio"] and c.rec_out > c.rec_in:
            _, as_proto, am_proto = protos["audio"]
            am = copy.deepcopy(am_proto)
            am["id"] = _uid()
            am["local_material_id"] = str(uuid.uuid4())
            am["path"] = str(Path(c.rec).resolve())
            am["name"] = Path(c.rec).stem
            am["duration"] = round(ffprobe_duration(Path(c.rec)) * US)
            audios.append(am)
            spoken = c.rec_out - c.rec_in
            a_track.append(_seg_from(as_proto, am["id"], start, spoken / c.speed,
                                     c.rec_in, spoken, speed=c.speed))

    mats["videos"], mats["texts"] = videos, texts
    if audios:
        mats["audios"] = [a for a in mats.get("audios") or [] if a.get("type") != "record"] + audios

    tracks = [dict(protos["video"][0], segments=v_track, id=_uid())]
    for kind, segs in t_tracks.items():
        tracks.append(dict(protos["text"][kind][0], segments=segs, id=_uid()))
    if a_track:
        tracks.append(dict(protos["audio"][0], segments=a_track, id=_uid()))
    # 음악·효과음 등 우리가 안 건드리는 오디오 트랙은 그대로 살려둔다
    kept_ids = {a["id"] for a in mats.get("audios") or [] if a.get("type") != "record"}
    for track in raw["tracks"]:
        if track["type"] == "audio" and any(
                s.get("material_id") in kept_ids for s in track.get("segments") or []):
            tracks.append(track)
    raw["tracks"] = tracks

    now = int(_mtime(src_dir) * 1_000_000)
    raw["id"] = _uid()
    raw["name"] = name
    raw["path"] = str(dst_dir / "draft_info.json")
    raw["duration"] = round(timeline.duration * US)
    raw["update_time"] = now
    (dst_dir / "draft_info.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    meta_path = dst_dir / "draft_meta_info.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["draft_id"] = raw["id"]
        meta["draft_name"] = name
        meta["draft_fold_path"] = str(dst_dir)
        meta["draft_root_path"] = str(DRAFT_ROOT)
        meta["tm_duration"] = raw["duration"]
        meta["tm_draft_modified"] = now
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return dst_dir


def _mtime(p: Path) -> float:
    return p.stat().st_mtime


def ffprobe_duration(p: Path) -> float:
    from . import ffmpeg as ff
    try:
        return ff.duration(p)
    except Exception:
        return 0.0


def main_caption_track(draft: Draft) -> list[Seg]:
    """자막 트랙이 여러 개일 때, 영상 컷 수와 가장 잘 맞는 트랙을 메인으로 본다."""
    by_start: dict[float, list[Seg]] = {}
    for t in draft.texts:
        by_start.setdefault(round(t.start, 2), []).append(t)
    cuts = {round(v.start, 2) for v in draft.video}
    # 영상 컷 시작과 겹치는 자막만 남기고, 그 중 시작이 같은 게 여럿이면 첫 번째
    picked = [segs[0] for start, segs in sorted(by_start.items()) if start in cuts]
    return picked or sorted(draft.texts, key=lambda x: x.start)
