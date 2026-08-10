"""미리보기 렌더 — 트림된 녹음 + 영상 구간 + 번인 자막으로 mp4 한 개."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import ffmpeg as ff
from .assemble import Timeline

# 목소리 시작 직전에 '치직' 하는 기계음이 섞일 때가 있다. 페이드인이 리드 패드(0.06s)를
# 완전히 덮도록 잡아서, 그 구간에 뭐가 들어있든 0에서 올라오게 만든다.
FADE_IN = 0.09
FADE_OUT = 0.04
SAMPLE_RATE = 48000
MAX_CHARS_PER_ROW = 16
_EMPH = re.compile(r"\*([^*]+)\*")


@dataclass
class Style:
    font: str = "Pretendard"
    color: str = "#FFFFFF"
    outline: str = "thin"
    shadow: str = "soft"
    margin_v: int = 320
    emphasis: str = "#FFD3F2"
    max_emphasis: int = 1

    @classmethod
    def load(cls, root: Path) -> "Style":
        for path in (root / "style.yml", root.parent / "style.yml", Path.cwd() / "style.yml"):
            if path.exists():
                d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                cap, emp = d.get("caption") or {}, d.get("emphasis") or {}
                return cls(
                    font=cap.get("font", cls.font),
                    color=cap.get("color", cls.color),
                    outline=cap.get("outline", cls.outline),
                    shadow=cap.get("shadow", cls.shadow),
                    margin_v=int(cap.get("margin_v", cls.margin_v)),
                    emphasis=emp.get("color", cls.emphasis),
                    max_emphasis=int(emp.get("max_per_frame", cls.max_emphasis)),
                )
        return cls()


def _ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"


def render(tl: Timeline, out: Path, *, work: Path, on_progress=None) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    reel = tl.reel

    style = Style.load(reel.root)
    if not ff.has_filter("ass"):
        raise RuntimeError(
            "이 ffmpeg 빌드에 자막(ass) 필터가 없다. `brew install ffmpeg-full` 로 libass 포함 빌드를 깔아라."
        )
    audio = _audio_track(tl, work, on_progress)
    video = _video_track(tl, work, on_progress)
    subs = _write_ass(tl, work / "subs.ass", style)

    out.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress("mux", 1, 1)

    ff.ffmpeg(
        "-i", str(video), "-i", str(audio),
        "-filter_complex", f"[0:v]ass=filename={_escape_path(subs)}[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(reel.fps),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(out),
    )
    return out


def storyboard(tl: Timeline, video: Path, dst: Path, *, cols: int = 4, thumb_w: int = 360) -> Path:
    """컷마다 한 프레임씩 뽑아 한 장으로. 영상을 다 안 봐도 '3번이 이상한데'가 판단된다."""
    frames = dst.parent / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.glob("*.png"):
        old.unlink()
    total = ff.duration(video)
    for c in tl.clips:
        at = min(c.tl_start + min(0.35, c.duration / 3), max(total - 0.05, 0.0))
        ff.ffmpeg("-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
                  "-vf", f"scale={thumb_w}:-2", str(frames / f"{c.index:03d}.png"))
    ff.ffmpeg("-pattern_type", "glob", "-i", str(frames / "*.png"),
              "-filter_complex", f"tile={cols}x{-(-len(tl.clips) // cols)}:margin=10:padding=10:color=0x14181F",
              "-frames:v", "1", str(dst))
    return dst


# ---------- 오디오 ----------

def _audio_track(tl: Timeline, work: Path, on_progress) -> Path:
    seg_dir = work / "audio"
    if seg_dir.exists():
        shutil.rmtree(seg_dir)
    seg_dir.mkdir(parents=True)

    parts: list[Path] = []
    for n, clip in enumerate(tl.clips):
        if on_progress:
            on_progress("audio", n + 1, len(tl.clips))
        seg = seg_dir / f"{clip.index:03d}.wav"
        spoken = (clip.rec_out - clip.rec_in) if clip.rec else 0.0

        if spoken > 0.01:
            fade_in = min(FADE_IN, spoken / 3)
            fade_out = min(FADE_OUT, spoken / 3)
            fade_out_at = max(spoken - fade_out, 0.0)
            # -ss 는 -i 앞(입력 시킹)에 둔다. 뒤에 두면 필터에 들어가는 타임스탬프가
            # 0 이 아니라 원본 절대 시각이라, afade 의 st 가 엉뚱한 지점에 걸려
            # 세그먼트가 통째로 무음이 된다(실측). asetpts 로 한 번 더 못 박는다.
            ff.ffmpeg(
                "-ss", f"{clip.rec_in:.3f}", "-t", f"{spoken:.3f}",
                "-i", str(ff.cached_wav(clip.rec)),
                "-af", (f"asetpts=PTS-STARTPTS,"
                        f"afade=t=in:st=0:d={fade_in:.3f},"
                        f"afade=t=out:st={fade_out_at:.3f}:d={fade_out:.3f},"
                        f"aresample={SAMPLE_RATE}"),
                "-ac", "2", "-c:a", "pcm_s16le", str(seg),
            )
            parts.append(seg)
            if (pad := clip.duration - spoken) > 0.01:   # hold 는 무음으로
                parts.append(_silence(pad, seg_dir / f"{clip.index:03d}h.wav"))
        else:
            parts.append(_silence(clip.duration, seg))

        if clip.index < len(tl.clips) and tl.reel.gap > 0.001:
            parts.append(_silence(tl.reel.gap, seg_dir / f"{clip.index:03d}g.wav"))

    return _concat(parts, work / "audio.wav")


def _silence(seconds: float, dst: Path) -> Path:
    ff.ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
              "-t", f"{max(seconds, 0.01):.3f}", "-c:a", "pcm_s16le", str(dst))
    return dst


# ---------- 비디오 ----------

def _video_track(tl: Timeline, work: Path, on_progress) -> Path:
    seg_dir = work / "video"
    if seg_dir.exists():
        shutil.rmtree(seg_dir)
    seg_dir.mkdir(parents=True)
    reel = tl.reel
    w, h, fps = reel.width, reel.height, reel.fps

    parts: list[Path] = []
    for n, clip in enumerate(tl.clips):
        if on_progress:
            on_progress("video", n + 1, len(tl.clips))
        seg = seg_dir / f"{clip.index:03d}.mp4"
        total = clip.duration + (reel.gap if clip.index < len(tl.clips) else 0.0)
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h},setsar=1,fps={fps}")

        if clip.src:
            if clip.src_short > 0.01:
                vf += f",tpad=stop_mode=clone:stop_duration={clip.src_short + reel.gap + 0.1:.3f}"
            ff.ffmpeg("-ss", f"{clip.src_in:.3f}", "-i", str(clip.src),
                      "-t", f"{total:.3f}", "-an", "-vf", vf,
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                      "-pix_fmt", "yuv420p", "-r", str(fps), str(seg))
        else:
            ff.ffmpeg("-f", "lavfi", "-i", f"color=c=0x101418:s={w}x{h}:r={fps}",
                      "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "20", "-pix_fmt", "yuv420p", str(seg))
        parts.append(seg)

    return _concat(parts, work / "video.mp4")


def _concat(parts: list[Path], dst: Path) -> Path:
    listing = dst.with_suffix(dst.suffix + ".txt")
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    ff.ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dst))
    return dst


# ---------- 자막 ----------

def _chunks(text: str) -> list[tuple[str, bool]]:
    """'*강조*' 표기를 (조각, 강조여부) 목록으로."""
    out, pos = [], 0
    for m in _EMPH.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(text, False)]


def _split_at(plain: str) -> int | None:
    """두 줄로 나눌 위치(평문 기준 인덱스). 나눌 필요 없으면 None."""
    if len(plain) <= MAX_CHARS_PER_ROW:
        return None
    spaces = [i for i, c in enumerate(plain) if c == " "]
    if not spaces:
        return None
    mid = len(plain) / 2
    return min(spaces, key=lambda i: abs(i - mid))


def _dialogue_text(text: str, style: Style) -> str:
    """강조 색을 입히고 두 줄로 접은 ASS 텍스트."""
    parts = _chunks(text)
    plain = "".join(p for p, _ in parts)
    split = _split_at(plain)
    white, pink = _ass_color(style.color), _ass_color(style.emphasis)

    out, idx = [], 0
    for chunk, emph in parts:
        piece = ""
        for ch in chunk:
            if split is not None and idx == split and ch == " ":
                piece += r"\N"      # 줄바꿈 자리의 공백은 버린다
            else:
                piece += _escape_text(ch)
            idx += 1
        out.append(f"{{\\c{pink}}}{piece}{{\\c{white}}}" if emph else piece)
    return "".join(out)


def _ts(seconds: float) -> str:
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_OUTLINE = {"none": 0.0, "hairline": 1.5, "thin": 2.2, "medium": 4.0, "thick": 6.5}
_SHADOW = {"none": 0.0, "soft": 3.0, "strong": 5.0}


def _write_ass(tl: Timeline, dst: Path, style: Style) -> Path:
    reel = tl.reel
    size = int(reel.height * 0.039)
    scale = reel.height / 1920
    outline = round(_OUTLINE.get(style.outline, 2.2) * scale, 1)
    shadow = round(_SHADOW.get(style.shadow, 3.0) * scale, 1)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {reel.width}
PlayResY: {reel.height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{style.font},{size},{_ass_color(style.color)},&H000000FF,&H14000000,&HA0000000,1,0,0,0,100,100,0,0,1,{outline},{shadow},2,90,90,{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = [
        f"Dialogue: 0,{_ts(c.tl_start)},{_ts(c.tl_end)},Main,,0,0,0,,{_dialogue_text(c.text, style)}"
        for c in tl.clips
    ]
    dst.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return dst


def _escape_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", r"\N")


def _escape_path(path: Path) -> str:
    """filter 인자용 이스케이프. 따옴표로 감싸면 ffmpeg 가 옵션 이름으로 오인한다."""
    s = str(path.resolve())
    for ch in ("\\", "'", ":", "[", "]", ",", ";"):
        s = s.replace(ch, "\\" + ch)
    return s
