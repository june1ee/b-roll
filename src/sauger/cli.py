"""sauger CLI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import assemble, capcut, project, render
from . import ffmpeg as ff

LOW_SCORE = 0.75


def _fmt(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:05.2f}"


def _collect_notes(tl, reverse: bool) -> dict[int, list[str]]:
    """컷별로 눈으로 알아야 할 것들. 터미널이 아니라 스토리보드에 박힌다."""
    notes: dict[int, list[str]] = {}
    for c in tl.clips:
        flags = []
        if c.src and ff.is_hdr(c.src):
            flags.append("HDR→SDR")
        if any(t and render.strip_emoji(t) != t for t in (c.text, c.clock, c.aside)):
            flags.append("이모지 미리보기만 제외")
        if c.src_short > 0.05:
            flags.append(f"소스 {c.src_short:.1f}s 부족")
        if not reverse and c.rec and c.score < LOW_SCORE:
            flags.append("정렬 불확실")
        if flags:
            notes[c.index] = flags
    return notes


def cmd_build(args: argparse.Namespace) -> int:
    if args.reel:
        reel = project.load(Path(args.reel))
    elif args.from_capcut:
        reel = project.from_draft(args.from_capcut)
        print(f"yml 없이 캡컷 '{args.from_capcut}' 만으로 구성", file=sys.stderr)
    else:
        print("오류: 릴스 yml 을 주거나 --from-capcut 을 써라", file=sys.stderr)
        return 1
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
    speeds = sorted({round(c.speed, 2) for c in tl.clips})
    where = "캡컷에서 건 값 그대로" if reverse else tl.speed_origin
    label = f"{speeds[0]:.2f}x" if len(speeds) == 1 else f"{speeds[0]:.2f}~{speeds[-1]:.2f}x"
    print(f"\n총 길이 {_fmt(tl.duration)} · 배속 {label} ({where})", file=sys.stderr)

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
    notes = _collect_notes(tl, reverse)
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
        if problems := capcut.validate_draft(out_dir):
            for p in problems:
                print(f"  ⚠ {p}", file=sys.stderr)
        else:
            print("  자체 검사 통과 (참조·트랙 연속성·파일 존재)", file=sys.stderr)
        print("  캡컷을 껐다 켜면 목록에 뜬다. 음악·TTS 는 거기서 얹으면 돼.", file=sys.stderr)

    if args.no_render:
        print(f"타임라인 → {timeline_json}", file=sys.stderr)
        return 0

    out = Path(args.out) if args.out else reel.root / f"{reel.path.stem}-preview.mp4"

    def render_progress(stage: str, i: int, n: int) -> None:
        print(f"\r  렌더 {stage} {i}/{n}   ", end="", file=sys.stderr, flush=True)

    render.render(tl, out, work=work, on_progress=render_progress)
    board = render.storyboard(tl, out, work / "storyboard.png", notes=notes)
    print(f"\n완료 → {out}", file=sys.stderr)
    print(f"스토리보드 → {board}", file=sys.stderr)
    print(f"타임라인 → {timeline_json}", file=sys.stderr)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """캡컷 프로젝트를 읽어 릴스 yml 뼈대를 만든다. 줄 수와 길이는 녹음에서 가져온다."""
    voices = capcut.voice_segments(args.project)
    if not voices:
        print(f"오류: '{args.project}' 에 녹음/TTS 가 없다. 캡컷에서 먼저 녹음해라.", file=sys.stderr)
        return 1
    draft = capcut.read(args.project)
    # 이미 자막을 넣어뒀다면 층(메인/시각/부연)별로 나눠 채워준다
    existing: dict[float, dict[str, str]] = {}
    for t in draft.texts:
        if t.text:
            existing.setdefault(round(t.start, 2), {}).setdefault(
                capcut.classify_caption(t.text), t.text.strip())

    out = Path(args.out) if args.out else Path("reels") / f"{args.project}.yml"
    if out.exists() and not args.overwrite:
        print(f"오류: {out} 가 이미 있다 (--overwrite 로 덮어쓰기)", file=sys.stderr)
        return 1

    lines = [f"# {args.project} — 캡컷에서 녹음·속도까지 끝낸 프로젝트에서 뽑은 뼈대.",
             f"# 줄 {len(voices)}개. t(자막)와 src(영상 소스)만 채우면 된다.",
             f"template: {args.project}",
             'ratio: "9:16"', "", "lines:"]
    for i, v in enumerate(voices, 1):
        layers = existing.get(round(v.start, 2), {})
        q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731
        lines += [
            f"  # {i}. {v.start:5.2f}~{v.end:5.2f}s ({v.end - v.start:.2f}초, {v.speed:.2f}배속)",
            f"  - t: {q(layers.get('main', ''))}",
            "    src:            # 이 줄에 쓸 영상 (러프컷, 넉넉하게)",
        ]
        lines.append(f"    clock: {q(layers['clock'])}" if "clock" in layers
                     else '    # clock: "7:30"   # 시각 레이어 (쓸 때만)')
        lines.append(f"    aside: {q(layers['aside'])}" if "aside" in layers
                     else '    # aside: "(부연)"  # 괄호 부연 (쓸 때만)')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.truth:
        # 회귀 비교용 정답지. 개인 내용이라 저장소에 안 올린다 — 필요할 때 다시 뽑는다.
        truth = [
            {"i": i, "rec_in": round(v.src_in, 3), "rec_out": round(v.src_in + v.src_dur, 3),
             "speed": round(v.speed, 3), "tl": [round(v.start, 3), round(v.end, 3)]}
            for i, v in enumerate(voices, 1)
        ]
        for i, seg in enumerate(draft.video, 1):
            if i <= len(truth):
                truth[i - 1]["src"] = seg.src.name if seg.src else None
                truth[i - 1]["src_in"] = round(seg.src_in, 3)
        tp = out.parent.parent / "tests" / f"truth-{args.project}.json"
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"정답지도 씀 → {tp}", file=sys.stderr)

    print(f"만들었다 → {out}", file=sys.stderr)
    print(f"  줄 {len(voices)}개 · 총 {voices[-1].end:.2f}초 · 배속 "
          f"{min(v.speed for v in voices):.2f}~{max(v.speed for v in voices):.2f}x", file=sys.stderr)
    print(f"  t 와 src 채운 뒤:  sauger build {out} --from-capcut {args.project}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sauger", description="릴스 조립기")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="릴스 yml → 타임라인 + 미리보기 mp4")
    b.add_argument("reel", nargs="?", help="릴스 정의 yml 경로 (--from-capcut 만 쓸 거면 생략 가능)")
    b.add_argument("-o", "--out", help="출력 mp4 경로")
    b.add_argument("--no-render", action="store_true", help="타임라인만 계산하고 렌더는 생략")
    b.add_argument("--from-capcut", metavar="프로젝트",
                   help="캡컷에서 녹음·속도까지 끝낸 프로젝트에서 타이밍을 가져온다 (정렬 생략)")
    b.add_argument("--capcut", metavar="이름", help="캡컷 드래프트로 내보낸다 (template: 프로젝트를 복제)")
    b.add_argument("--overwrite", action="store_true", help="같은 이름의 캡컷 프로젝트를 덮어쓴다")
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("init", help="캡컷 프로젝트 → 릴스 yml 뼈대")
    i.add_argument("project", help="캡컷 프로젝트 이름 (녹음이 들어있는 것)")
    i.add_argument("-o", "--out", help="출력 yml 경로 (기본 reels/<프로젝트>.yml)")
    i.add_argument("--overwrite", action="store_true")
    i.add_argument("--truth", action="store_true",
                   help="회귀 비교용 정답지(tests/truth-<프로젝트>.json)도 같이 뽑는다")
    i.set_defaults(func=cmd_init)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
