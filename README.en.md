# sauger

A reel assembler. Give it caption lines, a voice recording per line, and video sources — it lines up the timing, assembles the cut, and renders a preview mp4.

Background and design rationale: [`docs/PLAN.md`](docs/PLAN.md). Caption styling: [`style.yml`](style.yml).

## Usage

### Default — record in CapCut, hand it over (recommended)

```bash
uv run sauger build reels/my-reel.yml --from-capcut 0811
```

1. Record in CapCut and set the pacing there (TTS works too)
2. In the yml, write only the captions and which video source each line uses
3. Run the command → a `0811-sauger` project appears. Add music, ship.

**No timing is estimated here.** The values CapCut already committed to are used verbatim, so alignment error is zero by construction. The *n*-th voice segment pairs 1:1 with the *n*-th line.

### Alternative — hand over recordings and let sauger align them

```bash
uv run sauger build reels/my-reel.yml --capcut my-reel-out
```

Give each line a `rec:` and sauger finds the spoken span with whisper plus silence detection, trims to it, and applies the speed ratio the template was using. Only for recordings not made in CapCut.

### Either way

- **Rough-cut trimming** — clips cut generously get shortened to the voice length (centered by default; `fit: start|end`, or pin it with `from:`)
- **CapCut draft export** — clones the `template:` project and swaps only the segments. The caption layers (main / visual / parenthetical) keep their font, size, stroke, shadow, and position. If CapCut is open, the project shows up in the list within seconds.
- **Preview mp4 + per-cut storyboard PNG** — rough proxies for checking timing. For what the captions actually *look* like, CapCut's render is the source of truth.

**A format is just a CapCut project.** To use a new one, build a project in that style once and name it in `template:`. No style values are hardcoded.

Output lands under `reels/.sauger/<name>/` (`timeline.json`, `storyboard.png`, intermediate segments).

## Design notes

**Alignment splits the problem across two signals.** whisper.cpp timestamps drift at the boundaries by 0.3–1.3s (measured). So *what was said* comes from whisper, and *where it started and stopped* comes from `silencedetect`. Neither signal is trusted for the thing it's bad at.

**Captions summarize speech, they don't transcribe it.** In a real reel of mine, 4 of 8 lines were summaries rather than transcripts. So text is only used to narrow a span when the match is strong (≥0.80 similarity) and covers at least 60% of what was heard; otherwise the first take is used whole. Measured on real recordings, a chunk attached after a long silence was consistently filler.

**Recordings are decoded to wav and cached.** CapCut records raw ADTS AAC, where the container reports a duration that disagrees with the real one — trimming against it silently cuts spans up to 0.9s short.

## Requirements

```bash
brew install whisper-cpp ffmpeg-full   # the default ffmpeg bottle has no libass, so captions can't be burned in
curl -L --fail -o ~/.cache/sauger/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

`ffmpeg-full` is keg-only so it won't be on PATH; sauger locates it on its own (override with `SAUGER_FFMPEG_DIR`).

---

Korean version: [`README.ko.md`](README.ko.md)
