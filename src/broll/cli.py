"""broll CLI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import assemble, caption, capcut, project, render
from . import ffmpeg as ff

LOW_SCORE = 0.75


def _fmt(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:05.2f}"


def _collect_notes(tl, reverse: bool) -> dict[int, list[str]]:
    """Per-cut things to eyeball, drawn on the storyboard rather than printed."""
    notes: dict[int, list[str]] = {}
    for c in tl.clips:
        flags = []
        if c.src and ff.is_hdr(c.src):
            flags.append("HDR->SDR")
        if any(t and render.strip_emoji(t) != t for t in (c.text, c.clock, c.aside)):
            flags.append("emoji left out of preview")
        if c.src_short > 0.05:
            flags.append(f"source {c.src_short:.1f}s short")
        if not reverse and c.rec and c.score < LOW_SCORE:
            flags.append("weak alignment")
        if flags:
            notes[c.index] = flags
    return notes


def _warn_if_open(project: str) -> None:
    if capcut.is_open(project):
        print(f"⚠ '{project}' is open in CapCut - edits it has not saved yet will not be included",
              file=sys.stderr)


def cmd_build(args: argparse.Namespace) -> int:
    if args.reel:
        reel = project.load(Path(args.reel))
    elif args.from_capcut:
        reel = project.from_draft(args.from_capcut)
        print(f"no yml - building from CapCut project '{args.from_capcut}' alone", file=sys.stderr)
    else:
        print("error: give a reel yml, or use --from-capcut <project>", file=sys.stderr)
        return 1
    work = reel.root / ".broll" / reel.path.stem

    def align_progress(i: int, n: int, text: str) -> None:
        print(f"  [{i}/{n}] aligning  {text[:34]}", file=sys.stderr)

    print(f"reel: {reel.path.name} · {len(reel.lines)} lines · {reel.width}x{reel.height}", file=sys.stderr)
    if args.from_capcut:
        # voice recorded and sped up in CapCut; that project is also the default style template
        reel.template = reel.template or args.from_capcut
        print(f"voice: taken as-is from CapCut '{args.from_capcut}' (no alignment)", file=sys.stderr)
        _warn_if_open(args.from_capcut)
        tl = assemble.from_capcut(reel, args.from_capcut)
    else:
        tl = assemble.build(reel, on_progress=align_progress)

    timeline_json = work / "timeline.json"
    timeline_json.parent.mkdir(parents=True, exist_ok=True)
    timeline_json.write_text(json.dumps(tl.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    reverse = tl.voice_source is not None
    head = "speed" if reverse else "score"
    print(f"\n{'#':>3}  {'timeline':>14}  {'recording':>16}  {head:>5}  caption", file=sys.stderr)
    for c in tl.clips:
        flag = "  " if reverse or not c.rec or c.score >= LOW_SCORE else " ⚠"
        rec = f"{_fmt(c.rec_in)}~{_fmt(c.rec_out)}" if c.rec else "-"
        metric = f"{c.speed:>4.2f}x" if reverse else f"{c.score:>5.2f}"
        print(f"{c.index:>3}  {_fmt(c.tl_start)}~{_fmt(c.tl_end)}  {rec:>16}  "
              f"{metric}{flag}{c.text[:30]}", file=sys.stderr)
    speeds = sorted({round(c.speed, 2) for c in tl.clips})
    where = "as set in CapCut" if reverse else tl.speed_origin
    label = f"{speeds[0]:.2f}x" if len(speeds) == 1 else f"{speeds[0]:.2f}~{speeds[-1]:.2f}x"
    print(f"\ntotal {_fmt(tl.duration)} · speed {label} ({where})", file=sys.stderr)

    if not reverse:
        weak = [c for c in tl.clips if c.rec and c.score < LOW_SCORE]
        if weak:
            print(f"⚠ {len(weak)} line(s) aligned with low confidence: "
                  f"{', '.join(str(c.index) for c in weak)} - the recording may not match the caption",
                  file=sys.stderr)
        loose = [c for c in tl.clips if c.rec and not c.tightened]
        if loose:
            print(f"· whole take kept because the caption differs from what was said: "
                  f"{', '.join(str(c.index) for c in loose)}", file=sys.stderr)
    over = [c for c in tl.clips if len(re.findall(r"\*[^*]+\*", c.text)) > 1]
    if over:
        print(f"⚠ more than one *emphasis* in a line: "
              f"{', '.join(str(c.index) for c in over)} - keep it to one per frame", file=sys.stderr)
    if any("*" in c.text for c in tl.clips):
        print("· *emphasis* is coloured in the preview only - the CapCut project gets plain text",
              file=sys.stderr)
    notes = _collect_notes(tl, reverse)
    short = [c for c in tl.clips if c.src_short > 0.05]
    if short:
        print(f"⚠ source too short, padded with its last frame: "
              f"{', '.join(f'{c.index}({c.src_short:.1f}s)' for c in short)}", file=sys.stderr)

    out_name = args.capcut or (f"{args.from_capcut}-broll" if args.from_capcut else None)
    if out_name:
        args.capcut = out_name
        if not reel.template:
            print("error: the yml needs template: <CapCut project name>", file=sys.stderr)
            return 1
        out_dir = capcut.write_draft(reel.template, args.capcut, tl, overwrite=args.overwrite)
        print(f"\nCapCut project -> {out_dir}", file=sys.stderr)
        if problems := capcut.validate_draft(out_dir):
            for p in problems:
                print(f"  ⚠ {p}", file=sys.stderr)
        else:
            print("  checks passed (references, track continuity, media files)", file=sys.stderr)
        print("  it shows up in CapCut's project list within a few seconds - add music there.",
              file=sys.stderr)

    if args.no_render:
        print(f"timeline -> {timeline_json}", file=sys.stderr)
        return 0

    out = Path(args.out) if args.out else reel.root / f"{reel.path.stem}-preview.mp4"

    def render_progress(stage: str, i: int, n: int) -> None:
        print(f"\r  rendering {stage} {i}/{n}   ", end="", file=sys.stderr, flush=True)

    render.render(tl, out, work=work, on_progress=render_progress)
    board = render.storyboard(tl, out, work / "storyboard.png", notes=notes)
    print(f"\npreview -> {out}", file=sys.stderr)
    print(f"storyboard -> {board}", file=sys.stderr)
    print(f"timeline -> {timeline_json}", file=sys.stderr)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Write a reel yml skeleton from a CapCut project, one line per recording."""
    voices = capcut.voice_segments(args.project)
    if not voices:
        print(f"error: '{args.project}' has no voice recordings or TTS. Record the voice in CapCut first.",
              file=sys.stderr)
        return 1
    draft = capcut.read(args.project)
    # captions already in the project fill the main, clock and aside fields
    existing: dict[float, dict[str, str]] = {}
    for t in draft.texts:
        if t.text:
            existing.setdefault(round(t.start, 2), {}).setdefault(
                capcut.classify_caption(t.text), t.text.strip())

    out = Path(args.out) if args.out else Path("reels") / f"{args.project}.yml"
    if out.exists() and not args.overwrite:
        print(f"error: {out} already exists (use --overwrite to replace it)", file=sys.stderr)
        return 1

    lines = [f"# {args.project} - skeleton read from a CapCut project with the voice already recorded.",
             f"# {len(voices)} lines. Fill in t (caption) and src (video source).",
             f"template: {args.project}",
             'ratio: "9:16"', "", "lines:"]
    for i, v in enumerate(voices, 1):
        layers = existing.get(round(v.start, 2), {})
        q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731
        lines += [
            f"  # {i}. {v.start:5.2f}~{v.end:5.2f}s ({v.end - v.start:.2f}s at {v.speed:.2f}x)",
            f"  - t: {q(layers.get('main', ''))}",
            "    src:            # rough clip for this line, cut a little long",
        ]
        lines.append(f"    clock: {q(layers['clock'])}" if "clock" in layers
                     else '    # clock: "7:30"   # clock layer, if the template has one')
        lines.append(f"    aside: {q(layers['aside'])}" if "aside" in layers
                     else '    # aside: "(note)"  # aside layer, if the template has one')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.truth:
        # regression reference; personal content, so it stays out of the repo and is regenerated
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
        print(f"reference timings -> {tp}", file=sys.stderr)

    print(f"wrote {out}", file=sys.stderr)
    print(f"  {len(voices)} lines · {voices[-1].end:.2f}s total · speed "
          f"{min(v.speed for v in voices):.2f}~{max(v.speed for v in voices):.2f}x", file=sys.stderr)
    print(f"  fill in t and src, then:  broll build {out} --from-capcut {args.project}", file=sys.stderr)
    return 0


def cmd_caption(args: argparse.Namespace) -> int:
    _warn_if_open(args.project)
    source, caps = caption.captions(args.project, args.max_chars, args.source, args.lang)
    where = "the recordings on its audio tracks" if source == "voice" else "the sound of its first video track"
    print(f"transcribed: {where} in CapCut '{args.project}'", file=sys.stderr)
    for start, end, text in caps:
        print(f"  {_fmt(start)}-{_fmt(end)}  {text}", file=sys.stderr)
    if args.dry_run:
        return 0
    name = args.name or f"{args.project}-caption"
    dst = capcut.write_captions(args.project, args.style, name, caps, overwrite=args.overwrite)
    print(f"CapCut '{dst.name}' - {len(caps)} captions styled like '{args.style}'", file=sys.stderr)
    for p in capcut.validate_draft(dst):
        print(f"  ⚠ {p}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="broll", description="Line up clips and captions with your voice, and hand back a CapCut project.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="trim rough clips to the voice and write a CapCut project + preview")
    b.add_argument("reel", nargs="?", help="reel yml (optional with --from-capcut)")
    b.add_argument("-o", "--out", help="preview mp4 path")
    b.add_argument("--no-render", action="store_true", help="compute the timeline, skip the preview")
    b.add_argument("--from-capcut", metavar="PROJECT",
                   help="take timing from the voice already recorded in this CapCut project (no alignment)")
    b.add_argument("--capcut", metavar="NAME",
                   help="write a CapCut project with this name (copies the template: project)")
    b.add_argument("--overwrite", action="store_true", help="replace a CapCut project with the same name")
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("init", help="write a reel yml skeleton from a CapCut project's recordings")
    i.add_argument("project", help="CapCut project with the voice recorded")
    i.add_argument("-o", "--out", help="yml path (default reels/<project>.yml)")
    i.add_argument("--overwrite", action="store_true")
    i.add_argument("--truth", action="store_true",
                   help="also save the project's timings as tests/truth-<project>.json for regression checks")
    i.set_defaults(func=cmd_init)

    c = sub.add_parser("caption", help="transcribe what the timeline says into short caption lines")
    c.add_argument("project", help="CapCut project to caption")
    c.add_argument("--style", required=True, metavar="PROJECT", help="CapCut project to copy the caption look from")
    c.add_argument("--name", help="output project name (default <project>-caption)")
    c.add_argument("--max-chars", type=int, default=caption.MAX_CHARS,
                   help=f"longest line in characters (default {caption.MAX_CHARS}, tuned for Korean)")
    c.add_argument("--lang", default="ko", help="spoken language for whisper, e.g. ko, en, ja, auto (default ko)")
    c.add_argument("--source", choices=["auto", "voice", "video"], default="auto",
                   help="voice: recordings on the audio tracks · video: the first video track · "
                        "auto: voice if there are recordings")
    c.add_argument("--dry-run", action="store_true", help="print the captions without writing a project")
    c.add_argument("--overwrite", action="store_true")
    c.set_defaults(func=cmd_caption)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
