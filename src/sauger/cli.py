"""sauger CLI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import assemble, capcut, project, render

LOW_SCORE = 0.75


def _fmt(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:05.2f}"


def cmd_build(args: argparse.Namespace) -> int:
    reel = project.load(Path(args.reel))
    work = reel.root / ".sauger" / reel.path.stem

    def align_progress(i: int, n: int, text: str) -> None:
        print(f"  [{i}/{n}] 정렬  {text[:34]}", file=sys.stderr)

    print(f"릴스: {reel.path.name} · {len(reel.lines)}줄 · {reel.width}x{reel.height}", file=sys.stderr)
    if args.from_capcut:
        # 캡컷에서 녹음·속도까지 끝낸 프로젝트를 받아 쓴다. 스타일 템플릿도 기본은 그쪽.
        reel.template = reel.template or args.from_capcut
        print(f"목소리: 캡컷 '{args.from_capcut}' 에서 그대로 가져옴 (정렬 안 함)", file=sys.stderr)
        tl = assemble.from_capcut(reel, args.from_capcut)
    else:
        tl = assemble.build(reel, on_progress=align_progress)

    timeline_json = work / "timeline.json"
    timeline_json.parent.mkdir(parents=True, exist_ok=True)
    timeline_json.write_text(json.dumps(tl.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    reverse = tl.voice_source is not None
    head = "배속" if reverse else "신뢰"
    print(f"\n{'#':>3}  {'타임라인':>14}  {'녹음 구간':>16}  {head:>5}  자막", file=sys.stderr)
    for c in tl.clips:
        flag = "  " if reverse or not c.rec or c.score >= LOW_SCORE else " ⚠"
        rec = f"{_fmt(c.rec_in)}~{_fmt(c.rec_out)}" if c.rec else "-"
        metric = f"{c.speed:>4.2f}x" if reverse else f"{c.score:>5.2f}"
        print(f"{c.index:>3}  {_fmt(c.tl_start)}~{_fmt(c.tl_end)}  {rec:>16}  "
              f"{metric}{flag}{c.text[:30]}", file=sys.stderr)
    print(f"\n총 길이 {_fmt(tl.duration)}", file=sys.stderr)

    if not reverse:
        weak = [c for c in tl.clips if c.rec and c.score < LOW_SCORE]
        if weak:
            print(f"⚠ 정렬 신뢰도 낮은 줄 {len(weak)}개: "
                  f"{', '.join(str(c.index) for c in weak)} — 녹음/자막이 다를 수 있음", file=sys.stderr)
        loose = [c for c in tl.clips if c.rec and not c.tightened]
        if loose:
            print(f"· 자막이 대사와 달라 테이크를 통째로 쓴 줄: "
                  f"{', '.join(str(c.index) for c in loose)}", file=sys.stderr)
    over = [c for c in tl.clips if len(re.findall(r"\*[^*]+\*", c.text)) > 1]
    if over:
        print(f"⚠ 강조가 한 프레임에 둘 이상인 줄: "
              f"{', '.join(str(c.index) for c in over)} — 스타일 규칙상 하나만", file=sys.stderr)
    short = [c for c in tl.clips if c.src_short > 0.05]
    if short:
        print(f"⚠ 소스가 짧아 마지막 프레임으로 채운 줄: "
              f"{', '.join(f'{c.index}({c.src_short:.1f}s)' for c in short)}", file=sys.stderr)

    out_name = args.capcut or (f"{args.from_capcut}-sauger" if args.from_capcut else None)
    if out_name:
        args.capcut = out_name
        if not reel.template:
            print("오류: yml 에 template: <캡컷 프로젝트 이름> 이 필요하다", file=sys.stderr)
            return 1
        out_dir = capcut.write_draft(reel.template, args.capcut, tl, overwrite=args.overwrite)
        print(f"\n캡컷 드래프트 → {out_dir}", file=sys.stderr)
        print("  캡컷을 껐다 켜면 목록에 뜬다. 음악·TTS 는 거기서 얹으면 돼.", file=sys.stderr)

    if args.no_render:
        print(f"타임라인 → {timeline_json}", file=sys.stderr)
        return 0

    out = Path(args.out) if args.out else reel.root / f"{reel.path.stem}-preview.mp4"

    def render_progress(stage: str, i: int, n: int) -> None:
        print(f"\r  렌더 {stage} {i}/{n}   ", end="", file=sys.stderr, flush=True)

    render.render(tl, out, work=work, on_progress=render_progress)
    board = render.storyboard(tl, out, work / "storyboard.png")
    print(f"\n완료 → {out}", file=sys.stderr)
    print(f"스토리보드 → {board}", file=sys.stderr)
    print(f"타임라인 → {timeline_json}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sauger", description="릴스 조립기")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="릴스 yml → 타임라인 + 미리보기 mp4")
    b.add_argument("reel", help="릴스 정의 yml 경로")
    b.add_argument("-o", "--out", help="출력 mp4 경로")
    b.add_argument("--no-render", action="store_true", help="타임라인만 계산하고 렌더는 생략")
    b.add_argument("--from-capcut", metavar="프로젝트",
                   help="캡컷에서 녹음·속도까지 끝낸 프로젝트에서 타이밍을 가져온다 (정렬 생략)")
    b.add_argument("--capcut", metavar="이름", help="캡컷 드래프트로 내보낸다 (template: 프로젝트를 복제)")
    b.add_argument("--overwrite", action="store_true", help="같은 이름의 캡컷 프로젝트를 덮어쓴다")
    b.set_defaults(func=cmd_build)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
