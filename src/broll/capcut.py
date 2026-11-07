"""Read and write CapCut drafts (draft_info.json).

CapCut 9.x uses draft_info.json, not draft_content.json, and it is plain JSON.
Writes copy an existing project and replace only the segments. See docs/capcut-draft-format.md.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

US = 1_000_000  # CapCut times are in microseconds
DRAFT_ROOT = Path(os.environ.get(
    "BROLL_CAPCUT_DRAFTS", Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"))
_PLACEHOLDER = "##_draftpath_placeholder"


@dataclass
class Seg:
    start: float          # position on the timeline (s)
    end: float
    src: Path | None = None
    src_in: float = 0.0
    src_dur: float = 0.0  # length consumed from the source (= (end-start) x speed)
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
    raise FileNotFoundError(
        f"CapCut project not found: {name_or_path} (looked in {DRAFT_ROOT}; "
        f"set BROLL_CAPCUT_DRAFTS or pass the project folder path)")


def is_open(name_or_path: str | Path) -> bool:
    """CapCut keeps a .locked file while a project is open, and saves over any write when it closes."""
    return (draft_dir(name_or_path) / ".locked").exists()


def _resolve(raw: str, root: Path) -> Path | None:
    if not raw:
        return None
    if raw.startswith(_PLACEHOLDER):
        # ##_draftpath_placeholder_<UUID>_##/textReading/xxx.wav → <draft>/textReading/xxx.wav
        _, _, rel = raw.partition("_##/")
        return root / rel if rel else None
    return Path(os.path.expanduser(raw))


def _plain_text(content: str) -> str:
    """A text material's content is a JSON string holding the text and its styles."""
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
                src_dur=(sr.get("duration") or 0) / US,
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
_EMPHASIS = re.compile(r"\*([^*]+)\*")


def plain_caption(text: str) -> str:
    """*emphasis* is coloured only in the preview. CapCut's form for multi-colour captions is unverified, so only the asterisks go."""
    return _EMPHASIS.sub(r"\1", text)


def classify_caption(text: str) -> str:
    """Classify a caption into one of three layers: clock, main or aside."""
    t = (text or "").strip()
    if CLOCK_RE.match(t):
        return "clock"
    if t.startswith("(") or t.startswith("（"):
        return "aside"
    return "main"


@dataclass
class CaptionStyle:
    """The look of one caption layer read from the template, carried to ASS for the preview."""
    kind: str
    family: str          # font family for libass
    size: float          # CapCut font size
    color: str           # #RRGGBB
    bold: bool
    stroke: float        # CapCut stroke width (normalised)
    shadow_alpha: float  # 0~1
    y: float             # CapCut transform.y
    scale_x: float
    scale_y: float

    def y_px(self, height: int) -> float:
        # CapCut's transform.y is relative to half the frame height, checked with layers at
        # -0.272/-0.442/-0.589 against where they render.
        return height / 2 * (1 - self.y)


# Font file name to the family libass looks up, as fc-list reports it.
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
    """Read the look of each caption layer from the template project.

    Hand-picked preview styles never matched in measurements; reading the values CapCut
    holds is the only way that does.
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


def caption_layers(draft: Draft) -> dict[float, dict[str, str]]:
    """Group captions by start time into layers (main, clock, aside)."""
    out: dict[float, dict[str, str]] = {}
    for t in draft.texts:
        if t.text and t.text.strip():
            out.setdefault(round(t.start, 2), {}).setdefault(
                classify_caption(t.text), t.text.strip())
    return out


VOICE_KINDS = ("record", "text_to_audio")


def voice_segments(name_or_path: str | Path) -> list[Seg]:
    """Voice segments (recordings or TTS) of a draft, in time order.

    When the voice was recorded and sped up in CapCut its timing is already final, and
    reading it beats estimating it with whisper.
    """
    draft = read(name_or_path)
    return [s for s in draft.audio if s.kind in VOICE_KINDS]


def voice_passthrough(name_or_path: str | Path) -> tuple[list[dict], dict] | None:
    """Carry the voice tracks over whole, materials and tracks.

    They are copied, not rebuilt: rebuilding TTS voice data, volume, fades and the like
    always misses something.
    """
    root = draft_dir(name_or_path)
    raw = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    auds = {a["id"]: a for a in raw["materials"].get("audios") or []}
    for track in raw["tracks"]:
        if track["type"] != "audio":
            continue
        ids = list(dict.fromkeys(s.get("material_id") for s in track.get("segments") or []))
        mats = [auds[i] for i in ids if i in auds and auds[i].get("type") in VOICE_KINDS]
        if mats and len(mats) == len(ids):
            return mats, track
    return None


def voice_speed(template: str | Path) -> float:
    """Median voice speed in the template.

    Measured formats ranged from 1.10-1.26 up to 1.5-2.0 (TTS), so the value is read
    from the template rather than hardcoded.
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


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def u16len(text: str) -> int:
    """CapCut counts text style ranges in UTF-16 code units.

    An emoji takes two units, so a range from Python's len() falls short and the
    uncovered part renders in the default style, oversized and shifted.
    """
    return len(text.encode("utf-16-le")) // 2


def prototypes(raw: dict) -> dict:
    """Pick a sample segment and material of each kind from the template.

    Style, effects and position all live in the sample, so cloning it and swapping only the
    text and timing reproduces the CapCut look. Recreating it by hand never matches.
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


def index_materials(mats: dict) -> dict[str, tuple[str, dict]]:
    """Material id to (the list it lives in, material)."""
    out: dict[str, tuple[str, dict]] = {}
    for key, items in mats.items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and m.get("id"):
                    out[m["id"]] = (key, m)
    return out


def clone_extras(seg: dict, index: dict, mats: dict) -> None:
    """Clone the extra materials a segment refers to (speed, canvas, effects...) and rebind them.

    In real projects every segment owns its extras. Cloning a prototype but keeping its refs
    makes several segments share one material, and the CapCut preview then
    will not play.
    """
    refs = seg.get("extra_material_refs")
    if not isinstance(refs, list):
        return
    fresh = []
    for ref in refs:
        found = index.get(ref)
        if not found:
            fresh.append(ref)
            continue
        key, mat = found
        copied = copy.deepcopy(mat)
        copied["id"] = new_id()
        mats.setdefault(key, []).append(copied)
        index[copied["id"]] = (key, copied)
        fresh.append(copied["id"])
    seg["extra_material_refs"] = fresh


def _seg_from(proto: dict, material_id: str, start: float, dur: float,
              src_in: float = 0.0, src_dur: float | None = None,
              speed: float = 1.0, index: dict | None = None,
              mats: dict | None = None) -> dict:
    """target is the time taken on the timeline, source the time consumed from the file.
    With a speed change, source = target x speed."""
    seg = copy.deepcopy(proto)
    seg["id"] = new_id()
    seg["material_id"] = material_id
    if index is not None and mats is not None:
        clone_extras(seg, index, mats)
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
    """Write the timeline as a CapCut draft: copy the template project, replace only the segments."""
    src_dir = draft_dir(template)
    dst_dir = copy_project(src_dir, name, overwrite)

    raw = json.loads((src_dir / "draft_info.json").read_text(encoding="utf-8"))
    protos = prototypes(raw)
    if not protos["video"] or not protos["text"].get("main"):
        raise RuntimeError(f"template {src_dir.name} needs at least one video clip and one caption to copy from")

    mats = raw["materials"]
    index = index_materials(mats)
    videos, texts, audios = [], [], []
    v_track, t_tracks, a_track = [], {}, []

    for c in timeline.clips:
        start, dur = c.tl_start, c.duration

        # video
        vt_proto, vs_proto, vm_proto = protos["video"]
        vm = copy.deepcopy(vm_proto)
        vm["id"] = new_id()
        vm["local_material_id"] = str(uuid.uuid4())
        if c.src:
            vm["path"] = str(Path(c.src).resolve())
            vm["material_name"] = Path(c.src).name
            vm["duration"] = round((c.src_len or dur) * US)
        videos.append(vm)
        v_track.append(_seg_from(vs_proto, vm["id"], start, dur, c.src_in,
                                 index=index, mats=mats))

        # the three caption layers
        for kind, text in (("main", c.text), ("clock", c.clock), ("aside", c.aside)):
            proto = protos["text"].get(kind)
            if not text or not proto:
                continue
            _, ts_proto, tm_proto = proto
            tm = copy.deepcopy(tm_proto)
            tm["id"] = new_id()
            content = json.loads(tm.get("content") or "{}")
            content["text"] = text = plain_caption(text)
            for style in content.get("styles") or []:
                if isinstance(style.get("range"), list) and len(style["range"]) == 2:
                    style["range"] = [0, u16len(text)]
            tm["content"] = json.dumps(content, ensure_ascii=False)
            texts.append(tm)
            t_tracks.setdefault(kind, []).append(
                _seg_from(ts_proto, tm["id"], start, dur, index=index, mats=mats))

        # voice: refer to the source file and set only the used span as source_timerange
        if c.rec and protos["audio"] and c.rec_out > c.rec_in and not getattr(timeline, "voice_source", None):
            _, as_proto, am_proto = protos["audio"]
            am = copy.deepcopy(am_proto)
            am["id"] = new_id()
            am["local_material_id"] = str(uuid.uuid4())
            am["path"] = str(Path(c.rec).resolve())
            am["name"] = Path(c.rec).stem
            am["duration"] = round(ffprobe_duration(Path(c.rec)) * US)
            audios.append(am)
            spoken = c.rec_out - c.rec_in
            a_track.append(_seg_from(as_proto, am["id"], start, spoken / c.speed,
                                     c.rec_in, spoken, speed=c.speed,
                                     index=index, mats=mats))

    # --from-capcut: carry the voice tracks over from the source project whole, not rebuilt
    passthrough = voice_passthrough(timeline.voice_source) if getattr(timeline, "voice_source", None) else None

    mats["videos"], mats["texts"] = videos, texts
    keep = [a for a in mats.get("audios") or [] if a.get("type") not in VOICE_KINDS]
    if passthrough:
        mats["audios"] = keep + passthrough[0]
    elif audios:
        mats["audios"] = keep + audios

    tracks = [dict(protos["video"][0], segments=v_track, id=new_id())]
    for kind, segs in t_tracks.items():
        tracks.append(dict(protos["text"][kind][0], segments=segs, id=new_id()))
    if passthrough:
        tracks.append(passthrough[1])
    elif a_track:
        tracks.append(dict(protos["audio"][0], segments=a_track, id=new_id()))
    # keep the audio tracks broll does not touch, such as music and sound effects
    kept_ids = {a["id"] for a in keep}
    for track in raw["tracks"]:
        if track["type"] == "audio" and any(
                s.get("material_id") in kept_ids for s in track.get("segments") or []):
            tracks.append(track)
    raw["tracks"] = tracks

    raw["duration"] = round(timeline.duration * US)
    _save(raw, dst_dir, name, int(_mtime(src_dir) * 1_000_000))
    return dst_dir


def write_captions(project: str | Path, style: str | Path, name: str,
                   captions: list[tuple[float, float, str]], *, overwrite: bool = False) -> Path:
    """Copy a project and add one caption track. Video and audio are left alone."""
    src_dir = draft_dir(project)
    style_raw = json.loads((draft_dir(style) / "draft_info.json").read_text(encoding="utf-8"))
    proto = prototypes(style_raw)["text"].get("main")
    if not proto:
        raise RuntimeError(f"style project {style} has no caption to copy the look from")
    track_proto, seg_proto, mat_proto = proto

    raw = json.loads((src_dir / "draft_info.json").read_text(encoding="utf-8"))
    mats = raw["materials"]
    # extra materials are looked up in the style project and cloned into this one
    style_index = index_materials(style_raw["materials"])
    segs = []
    for start, end, text in captions:
        tm = copy.deepcopy(mat_proto)
        tm["id"] = new_id()
        content = json.loads(tm.get("content") or "{}")
        content["text"] = text
        content["styles"] = [dict((content.get("styles") or [{}])[0], range=[0, u16len(text)])]
        tm["content"] = json.dumps(content, ensure_ascii=False)
        mats.setdefault("texts", []).append(tm)
        segs.append(_seg_from(seg_proto, tm["id"], start, end - start,
                              index=style_index, mats=mats))
    raw["tracks"].append(dict(copy.deepcopy(track_proto), segments=segs, id=new_id()))

    dst_dir = copy_project(src_dir, name, overwrite)
    _save(raw, dst_dir, name, int(_mtime(src_dir) * 1_000_000))
    return dst_dir


def copy_project(src: str | Path, name: str, overwrite: bool = False) -> Path:
    src_dir = draft_dir(src)
    dst_dir = DRAFT_ROOT / name
    if dst_dir.exists():
        if not overwrite:
            raise FileExistsError(f"CapCut project already exists: {name} (use --overwrite to replace it)")
        shutil.rmtree(dst_dir)
    # Skip only backup, temp and lock files. It is unclear what else CapCut needs, so copy the rest.
    # (A running CapCut lists a new folder within seconds on its own.)
    shutil.copytree(src_dir, dst_dir, ignore=shutil.ignore_patterns(
        "draft_info.json.bak", "template-*.tmp", ".locked"))
    return dst_dir


def load_raw(name_or_path: str | Path) -> dict:
    return json.loads((draft_dir(name_or_path) / "draft_info.json").read_text(encoding="utf-8"))


def save_in_place(name_or_path: str | Path, raw: dict) -> Path:
    """Write a document from load_raw back to its project, updating both copies and the listed duration."""
    root = draft_dir(name_or_path)
    if is_open(root):
        raise RuntimeError(f"{root.name} is open in CapCut - close it first, "
                           f"or CapCut will save over this edit when it closes")
    _save(raw, root, root.name, int(time.time() * US), fresh_ids=False)
    return root


def _save(raw: dict, dst_dir: Path, name: str, now: int, *, fresh_ids: bool = True) -> None:
    # draft_info.id is the timeline id and must equal main_timeline_id in Timelines/project.json
    # and the Timelines/<id>/ folder name. With a new one CapCut cannot find the main timeline,
    # opens an empty tab and shows a zero duration.
    raw["path"] = str(dst_dir / "draft_info.json")
    raw["update_time"] = now
    body = json.dumps(raw, ensure_ascii=False)
    (dst_dir / "draft_info.json").write_text(body, encoding="utf-8")

    # the sub-timeline document is a separate file with the same content
    project_path = dst_dir / "Timelines" / "project.json"
    if project_path.exists():
        pj = json.loads(project_path.read_text(encoding="utf-8"))
        main_id = pj.get("main_timeline_id")
        sub = dst_dir / "Timelines" / str(main_id) / "draft_info.json"
        if main_id != raw.get("id"):
            raise RuntimeError(
                f"timeline id mismatch: draft_info.id={raw.get('id')} vs main_timeline_id={main_id}")
        if sub.parent.exists():
            sub.write_text(body, encoding="utf-8")
        if fresh_ids:
            pj["id"] = new_id()      # fresh project id; it must not collide with the template's
        pj["update_time"] = now
        project_path.write_text(json.dumps(pj, ensure_ascii=False), encoding="utf-8")

    meta_path = dst_dir / "draft_meta_info.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if fresh_ids:
            meta["draft_id"] = new_id()
            meta["draft_name"] = name
            meta["draft_fold_path"] = str(dst_dir)
            meta["draft_root_path"] = str(DRAFT_ROOT)
        meta["tm_duration"] = raw["duration"]
        meta["tm_draft_modified"] = now
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def validate_draft(name_or_path: str | Path) -> list[str]:
    """Check that a written draft has the shape CapCut accepts.

    Failures seen in practice: extra materials (speed, canvas, effects) shared between segments
    stop the preview from playing. Dangling refs and gaps in the video track are risky too.
    """
    root = draft_dir(name_or_path)
    raw = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))
    ids = set(index_materials(raw.get("materials") or {}))
    problems: list[str] = []

    dangling, seen = 0, {}
    for track in raw["tracks"]:
        for seg in track.get("segments") or []:
            if seg.get("material_id") and seg["material_id"] not in ids:
                dangling += 1
            for ref in seg.get("extra_material_refs") or []:
                if ref not in ids:
                    dangling += 1
                seen[ref] = seen.get(ref, 0) + 1
    if dangling:
        problems.append(f"{dangling} dangling material reference(s)")
    if shared := sum(1 for n in seen.values() if n > 1):
        problems.append(f"{shared} extra material(s) shared between segments - the CapCut preview will not play")

    for track in raw["tracks"]:
        segs = sorted(track.get("segments") or [], key=lambda s: s["target_timerange"]["start"])
        if track["type"] != "video" or len(segs) < 2:
            continue
        holes = sum(
            1 for a, b in zip(segs, segs[1:])
            if abs(b["target_timerange"]["start"]
                   - (a["target_timerange"]["start"] + a["target_timerange"]["duration"])) > 1000
        )
        if holes:
            problems.append(f"{holes} gap(s) in a video track - black frames between cuts")

    for mat in raw["materials"].get("texts") or []:
        try:
            content = json.loads(mat.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        want = u16len(content.get("text", ""))
        for style in content.get("styles") or []:
            rng = style.get("range")
            if isinstance(rng, list) and len(rng) == 2 and rng[1] != want:
                problems.append(
                    f"caption style range {rng} does not cover the text ({want} UTF-16 units): "
                    f"{content.get('text', '')[:16]!r} - that part will render at the wrong size")

    # timeline id chain: draft_info.id == project.json.main_timeline_id == Timelines/<id>/
    project_path = root / "Timelines" / "project.json"
    if project_path.exists():
        pj = json.loads(project_path.read_text(encoding="utf-8"))
        main_id = pj.get("main_timeline_id")
        if main_id != raw.get("id"):
            problems.append(
                f"timeline id mismatch (draft_info.id={str(raw.get('id'))[:8]} vs "
                f"main_timeline_id={str(main_id)[:8]}) - the player will not play")
        sub = root / "Timelines" / str(main_id) / "draft_info.json"
        if not sub.exists():
            problems.append(f"missing sub timeline document: Timelines/{str(main_id)[:8]}…/draft_info.json")
        elif json.loads(sub.read_text(encoding="utf-8")).get("duration") != raw.get("duration"):
            problems.append("root and sub timeline documents disagree - write both")

    for path_key in ("videos", "audios"):
        for mat in raw["materials"].get(path_key) or []:
            p = _resolve(mat.get("path", ""), root)
            if p and not p.exists():
                problems.append(f"missing media: {p.name}")
    return problems


def _mtime(p: Path) -> float:
    return p.stat().st_mtime


def ffprobe_duration(p: Path) -> float:
    from . import ffmpeg as ff
    try:
        return ff.duration(p)
    except Exception:
        return 0.0


def main_caption_track(draft: Draft) -> list[Seg]:
    """With several text tracks, the one that best matches the video cuts is the main one."""
    by_start: dict[float, list[Seg]] = {}
    for t in draft.texts:
        by_start.setdefault(round(t.start, 2), []).append(t)
    cuts = {round(v.start, 2) for v in draft.video}
    # keep captions that start with a video cut; among same-start captions take the first
    picked = [segs[0] for start, segs in sorted(by_start.items()) if start in cuts]
    return picked or sorted(draft.texts, key=lambda x: x.start)
