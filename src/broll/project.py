"""릴스 정의 파일(yml) 로드와 검증."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Line:
    t: str                      # 자막 텍스트
    rec: Path | None = None     # 이 줄 녹음
    src: Path | None = None     # 이 줄 영상 소스
    frm: float | None = None    # 소스 인점(초). 지정하면 다듬기 없이 이 지점부터
    fit: str = "center"         # 러프컷이 길 때 어디를 남길지: center | start | end
    speed: float | None = None  # 이 줄만 배속 다르게
    hold: float = 0.0           # 줄 끝나고 더 유지할 시간
    clock: str | None = None    # 시각 레이어 (7:30 같은)
    aside: str | None = None    # 괄호 부연 레이어


@dataclass
class Reel:
    path: Path
    width: int = 1080
    height: int = 1920
    fps: int = 30
    template: str | None = None   # 캡컷 템플릿 프로젝트 이름
    # 줄 사이 간격. 기본 0 - 실측한 릴스들은 컷과 음성이 1:1 로 딱 붙어 있었다(틈 0개).
    gap: float = 0.0
    speed: float | None = None    # 목소리 배속. None 이면 템플릿에서 읽는다
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
    """yml 없이 캡컷 프로젝트만으로 릴스를 구성한다.

    녹음·러프컷·자막을 전부 캡컷에서 넣어뒀을 때 쓴다. 목소리 세그먼트 하나가 한 줄이고,
    같은 시각에서 시작하는 자막을 층별로 붙인다.
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
