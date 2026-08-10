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
    frm: float | None = None    # 소스 인점(초). None이면 컷 선택에 맡김 (M3)
    cuts: int | None = None     # 이 줄을 몇 컷으로 (M3)
    hold: float = 0.0           # 줄 끝나고 더 유지할 시간
    prefer: str | None = None   # 컷 선택 힌트 (M3)


@dataclass
class Reel:
    path: Path
    width: int = 1080
    height: int = 1920
    fps: int = 30
    template: str | None = None   # 캡컷 템플릿 프로젝트 이름 (M2)
    gap: float = 0.05             # 줄 사이 간격(초)
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
        gap=float(data.get("gap", 0.05)),
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
            cuts=int(item["cuts"]) if item.get("cuts") else None,
            hold=float(item.get("hold", 0.0)),
            prefer=item.get("prefer"),
        )
        for label, p in (("rec", line.rec), ("src", line.src)):
            if p is not None and not p.exists():
                raise FileNotFoundError(f"{path.name}: {i}번째 줄 {label} 파일 없음 → {p}")
        reel.lines.append(line)
    return reel
