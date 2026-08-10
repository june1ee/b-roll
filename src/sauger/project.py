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
    # 줄 사이 간격. 기본 0 — June 의 타임라인은 컷과 음성이 1:1 로 딱 붙어 있다(실측 틈 0개).
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
    raise ValueError(f"모르는 ratio: {value} (지원: {', '.join(table)})")


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
        raise ValueError(f"{path.name}: lines 가 비었다")

    for i, item in enumerate(raw_lines, 1):
        if not isinstance(item, dict) or not item.get("t"):
            raise ValueError(f"{path.name}: {i}번째 줄에 t(자막)가 없다")
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
            raise ValueError(f"{path.name}: {i}번째 줄 fit 값이 이상하다 → {line.fit}")
        for label, p in (("rec", line.rec), ("src", line.src)):
            if p is not None and not p.exists():
                raise FileNotFoundError(f"{path.name}: {i}번째 줄 {label} 파일 없음 → {p}")
        reel.lines.append(line)
    return reel
