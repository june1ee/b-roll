"""Load and validate reel definition files (yml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Line:
    t: str                      # caption text
    rec: Path | None = None     # recording for this line
    src: Path | None = None     # video source for this line
    frm: float | None = None    # source in-point (s); when set, play from here without fitting
    fit: str = "center"         # which part of a long rough clip to keep: center | start | end
    speed: float | None = None  # voice speed for this line only
    hold: float = 0.0           # extra time to hold after the line
    clock: str | None = None    # clock layer, like 7:30
    aside: str | None = None    # aside layer


@dataclass
class Reel:
    path: Path
    width: int = 1080
    height: int = 1920
    fps: int = 30
    template: str | None = None   # CapCut template project name
    # gap between lines; 0 because measured reels had cuts and voice back to back
    gap: float = 0.0
    speed: float | None = None    # voice speed; None reads it from the template
    lang: str = "ko"
    lines: list[Line] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.path.parent

    def resolve(self, p: Path | str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.root / p)


def _ratio(value: str | None) -> tuple[int, int]:
    table = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}
    if not value:
        return table["9:16"]
    if value in table:
        return table[value]
    raise ValueError(f"unknown ratio: {value} (supported: {', '.join(table)})")


def from_draft(name: str, *, root: Path | None = None) -> Reel:
    """Build a reel from a CapCut project alone, without a yml.

    For when the voice, rough clips and captions are all in CapCut. Each voice segment is a
    line, and captions starting at the same time attach to it by layer.
    """
    from . import capcut

    draft = capcut.read(name)
    voices = capcut.voice_segments(name)
    if not voices:
        raise ValueError(f"'{name}' has no voice recordings or TTS on its audio tracks")
    layers = capcut.caption_layers(draft)

    reel = Reel(
        path=(root or Path("reels")) / f"{name}.yml",
        width=draft.width, height=draft.height, fps=int(draft.fps),
        template=name,
    )
    for v in voices:
        L = layers.get(round(v.start, 2), {})
        reel.lines.append(Line(t=L.get("main", ""), clock=L.get("clock"), aside=L.get("aside")))
    return reel


def load(path: Path) -> Reel:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    w, h = _ratio(data.get("ratio"))
    reel = Reel(
        path=path,
        width=w, height=h,
        fps=int(data.get("fps", 30)),
        template=data.get("template"),
        gap=float(data.get("gap", 0.0)),
        speed=float(data["speed"]) if data.get("speed") else None,
        lang=data.get("lang", "ko"),
    )
    raw_lines = data.get("lines") or []
    if not raw_lines:
        raise ValueError(f"{path.name}: lines is empty")

    for i, item in enumerate(raw_lines, 1):
        if not isinstance(item, dict) or not item.get("t"):
            raise ValueError(f"{path.name}: line {i} has no t (caption)")
        line = Line(
            t=str(item["t"]).strip(),
            rec=reel.resolve(item["rec"]) if item.get("rec") else None,
            src=reel.resolve(item["src"]) if item.get("src") else None,
            frm=float(item["from"]) if item.get("from") is not None else None,
            fit=str(item.get("fit", "center")),
            speed=float(item["speed"]) if item.get("speed") else None,
            hold=float(item.get("hold", 0.0)),
            clock=item.get("clock"),
            aside=item.get("aside"),
        )
        if line.fit not in ("center", "start", "end"):
            raise ValueError(f"{path.name}: line {i} has fit: {line.fit} (use center, start or end)")
        for label, p in (("rec", line.rec), ("src", line.src)):
            if p is not None and not p.exists():
                raise FileNotFoundError(f"{path.name}: line {i} {label} not found: {p}")
        reel.lines.append(line)
    return reel
