"""ffmpeg/ffprobe 호출 얇은 래퍼."""

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


# homebrew 의 기본 ffmpeg 8 병에는 libass 가 빠져 있어 자막을 못 굽는다.
# ffmpeg-full 은 keg-only 라 PATH 에 안 잡히므로 여기서 직접 찾는다. (.zshrc 는 안 건드린다)
_PREFERRED = (
    Path(os.environ["SAUGER_FFMPEG_DIR"]) if os.environ.get("SAUGER_FFMPEG_DIR") else None,
    Path("/opt/homebrew/opt/ffmpeg-full/bin"),
    Path("/usr/local/opt/ffmpeg-full/bin"),
)


@functools.cache
def _bin(name: str) -> str:
    for d in _PREFERRED:
        if d and (cand := d / name).exists():
            return str(cand)
    p = shutil.which(name)
    if not p:
        raise ToolMissing(f"{name} 없음. `brew install ffmpeg-full` 후 다시 시도.")
    return p


def run(args: list[str], *, quiet: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"실패: {' '.join(args[:3])} ...\n{tail}")
    return cp


def ffmpeg(*args: str) -> None:
    run([_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args])


def probe(path: Path) -> dict:
    cp = run([_bin("ffprobe"), "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(path)])
    return json.loads(cp.stdout)


def duration(path: Path) -> float:
    info = probe(path)
    if d := info.get("format", {}).get("duration"):
        return float(d)
    for s in info.get("streams", []):
        if d := s.get("duration"):
            return float(d)
    raise RuntimeError(f"길이를 못 읽음: {path}")


_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}   # PQ / HLG


def is_hdr(path: Path) -> bool:
    """아이폰으로 찍으면 클립마다 HDR 여부가 갈린다(Dolby Vision/HLG). 톤매핑 없이
    변환하면 그 컷만 색이 뜬다."""
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
    cp = subprocess.run([_bin("ffmpeg"), "-hide_banner", "-filters"],
                        capture_output=True, text=True, errors="replace")
    return any(line.split()[1:2] == [name] for line in cp.stdout.splitlines() if line.strip())


def to_wav16k(src: Path, dst: Path) -> Path:
    """whisper.cpp 입력 규격(16kHz 모노 PCM)으로 변환."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst))
    return dst


WAV_CACHE = Path(os.environ.get("SAUGER_WAV_DIR", Path.home() / ".cache/sauger/wav"))


def cached_wav(src: Path, *, rate: int = 48000, channels: int = 2) -> Path:
    """녹음을 PCM wav 로 한 번 디코드해 캐시한다.

    캡컷 녹음은 raw ADTS AAC 라서 컨테이너가 보고하는 길이가 실제 디코드 길이와 다르고,
    그 상태로 -ss/-t 트림을 하면 구간이 조용히 짧게 잘린다(실측 최대 0.9초). wav 로
    풀어놓으면 길이·타임스탬프가 정확해지고 반복 트림도 빨라진다.
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
