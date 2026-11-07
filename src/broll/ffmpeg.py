"""Thin wrappers around ffmpeg and ffprobe."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


class ToolMissing(RuntimeError):
    pass


# Homebrew's default ffmpeg 8 bottle lacks libass, so it cannot burn in captions.
# ffmpeg-full is keg-only and not on PATH, so it is looked up here.
_PREFERRED = (
    Path(os.environ["BROLL_FFMPEG_DIR"]) if os.environ.get("BROLL_FFMPEG_DIR") else None,
    Path("/opt/homebrew/opt/ffmpeg-full/bin"),
    Path("/usr/local/opt/ffmpeg-full/bin"),
)


@functools.cache
def binary(name: str) -> str:
    for d in _PREFERRED:
        if d and (cand := d / name).exists():
            return str(cand)
    p = shutil.which(name)
    if not p:
        raise ToolMissing(f"{name} not found. Install it with `brew install ffmpeg-full`.")
    return p


def run(args: list[str], *, quiet: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"failed: {' '.join(args[:3])} ...\n{tail}")
    return cp


def ffmpeg(*args: str) -> None:
    run([binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args])


def probe(path: Path) -> dict:
    cp = run([binary("ffprobe"), "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(path)])
    return json.loads(cp.stdout)


def duration(path: Path) -> float:
    info = probe(path)
    if d := info.get("format", {}).get("duration"):
        return float(d)
    for s in info.get("streams", []):
        if d := s.get("duration"):
            return float(d)
    raise RuntimeError(f"could not read the duration of {path}")


_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}   # PQ / HLG


def is_hdr(path: Path) -> bool:
    """iPhone clips vary between SDR and HDR (Dolby Vision/HLG). Converted without tone
    mapping, the HDR cuts come out washed out."""
    for s in probe(path).get("streams", []):
        if s.get("codec_type") != "video":
            continue
        if s.get("color_transfer") in _HDR_TRANSFERS or s.get("color_primaries") == "bt2020":
            return True
    return False


TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def has_filter(name: str) -> bool:
    cp = subprocess.run([binary("ffmpeg"), "-hide_banner", "-filters"],
                        capture_output=True, text=True, errors="replace")
    return any(line.split()[1:2] == [name] for line in cp.stdout.splitlines() if line.strip())


def to_wav16k(src: Path, dst: Path) -> Path:
    """Convert to whisper.cpp's input format (16 kHz mono PCM)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst))
    return dst


WAV_CACHE = Path(os.environ.get("BROLL_WAV_DIR", Path.home() / ".cache/broll/wav"))


def cached_wav(src: Path, *, rate: int = 48000, channels: int = 2) -> Path:
    """Decode a recording to PCM wav once and cache it.

    CapCut recordings are raw ADTS AAC, so the container's duration differs from the decoded
    length and trimming with -ss/-t silently cuts spans short (up to 0.9 s, measured).
    Decoded, durations and timestamps are exact and repeated trims are faster.
    """
    if src.suffix.lower() == ".wav":
        return src
    st = src.stat()
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}|{rate}|{channels}"
                       .encode()).hexdigest()[:16]
    dst = WAV_CACHE / f"{key}.wav"
    if not dst.exists():
        WAV_CACHE.mkdir(parents=True, exist_ok=True)
        ffmpeg("-i", str(src), "-ar", str(rate), "-ac", str(channels),
               "-c:a", "pcm_s16le", str(dst))
    return dst
