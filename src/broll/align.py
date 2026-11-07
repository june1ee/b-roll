"""Forced alignment: find the span of a recording that belongs to a caption line.

The two signals do different jobs:
  - **what was said** -> whisper (text matching, choosing among takes)
  - **where it starts and stops** -> silence detection (whisper timestamps drift 0.3-1.3 s)

Whisper time is mapped linearly onto the speech range silence detection found, then the
matched span's edges snap to the nearest silence boundary.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .asr import Char

_KEEP = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㆎ]")
# Tightening a span from text can clip speech, so it only happens when the caption is nearly the whole line.
# Tightening at coverage 0.70 clipped the last syllable of a line.
STRONG_MATCH = 0.85
MIN_COVERAGE = 0.85


@dataclass
class Span:
    start: float
    end: float
    score: float
    takes: int = 1          # takes detected; 2+ means a retake or filler
    coverage: float = 0.0   # share of the heard speech the caption covers
    tightened: bool = False # span tightened from text, else the whole take

    @property
    def duration(self) -> float:
        return self.end - self.start


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    return "".join(c for c in text if _KEEP.match(c))


def _norm_chars(chars: list[Char]) -> tuple[str, list[Char]]:
    kept = [c for c in chars if _KEEP.match(unicodedata.normalize("NFC", c.ch).lower())]
    return "".join(unicodedata.normalize("NFC", c.ch).lower() for c in kept), kept


def _fuzzy_substring(exp: str, asr: str) -> tuple[int, int, float]:
    """Span [start, end) of the ASR closest to exp, and its score. Ties go to the later span (last take)."""
    m, n = len(exp), len(asr)
    prev_d = [0] * (n + 1)              # row 0: starting anywhere is free
    prev_s = list(range(n + 1))
    for i in range(1, m + 1):
        cur_d = [i] + [0] * n
        cur_s = [0] + [0] * n
        for j in range(1, n + 1):
            sub = prev_d[j - 1] + (0 if exp[i - 1] == asr[j - 1] else 1)
            dele = prev_d[j] + 1
            ins = cur_d[j - 1] + 1
            best = min(sub, dele, ins)
            cur_d[j] = best
            cur_s[j] = prev_s[j - 1] if best == sub else (prev_s[j] if best == dele else cur_s[j - 1])
        prev_d, prev_s = cur_d, cur_s
    end = min(range(1, n + 1), key=lambda j: (prev_d[j], -j))
    start = min(prev_s[end], end - 1)
    score = max(0.0, 1.0 - prev_d[end] / max(m, 1))
    return start, end, score


def _rescale(t: float, w0: float, w1: float, v0: float, v1: float) -> float:
    """Map whisper time onto the silence-detection time axis."""
    if w1 - w0 < 1e-3:
        return v0
    return v0 + (t - w0) / (w1 - w0) * (v1 - v0)


SNAP = 0.35   # within this, snap to a speech edge; beyond it keep the computed time


def _snap_start(t: float, spans: list[tuple[float, float]]) -> float:
    if t <= spans[0][0]:
        return spans[0][0]
    for i, (s, e) in enumerate(spans):
        if s <= t <= e:
            return s if t - s <= SNAP else t
        if t < s:                       # in a silent gap: move to the next speech start
            return s
        if i == len(spans) - 1:
            return max(s, t)
    return spans[-1][0]


def _snap_end(t: float, spans: list[tuple[float, float]]) -> float:
    if t >= spans[-1][1]:
        return spans[-1][1]
    for i, (s, e) in enumerate(spans):
        if s <= t <= e:
            return e if e - t <= SNAP else t
        if t < s:                       # in a silent gap: move to the previous speech end, never into the next span
            return spans[i - 1][1] if i else s
    return spans[-1][1]


def take_groups(spans: list[tuple[float, float]], *, gap: float = 0.8,
                tail_gap: float = 1.2, tail_len: float = 0.5
                ) -> list[list[tuple[float, float]]]:
    """Group speech into takes separated by long silences.

    Short breaths stay inside a take. A very short fragment after a silence is usually the
    tail of the sentence rather than a new take, so it joins the previous one.
    """
    groups: list[list[tuple[float, float]]] = []
    for span in spans:
        if groups:
            silence = span[0] - groups[-1][-1][1]
            short_tail = silence < tail_gap and (span[1] - span[0]) < tail_len
            if silence < gap or short_tail:
                groups[-1].append(span)
                continue
        groups.append([span])
    return groups


def find_span(chars: list[Char], expected: str,
              spans: list[tuple[float, float]]) -> Span:
    if not spans:
        return Span(0.0, 0.0, 0.0)
    groups = take_groups(spans)
    v0, v1 = spans[0][0], spans[-1][1]
    # Use the first take, extended to just before the next take starts.
    # Hand edits dropped the trailing filler take but kept the silence before it.
    # A speed-up shortens that silence anyway.
    first_end = groups[1][0][0] if len(groups) > 1 else groups[0][-1][1]
    first = Span(groups[0][0][0], first_end, 0.0, takes=len(groups))

    exp = _norm(expected)
    asr, kept = _norm_chars(chars)
    if not exp or not asr:
        return first

    c0, c1, score = _fuzzy_substring(exp, asr)
    coverage = (c1 - c0) / max(len(asr), 1)
    first.score = score
    first.coverage = coverage
    # Tighten from text only when the caption is close to verbatim.
    # At low coverage the text cannot tell which part was spoken, so using the whole first take
    # is safer; in measured recordings the later takes were filler.
    if score < STRONG_MATCH or coverage < MIN_COVERAGE:
        return first

    w0, w1 = kept[0].start, kept[-1].end
    t0 = _rescale(kept[c0].start, w0, w1, v0, v1)
    t1 = _rescale(kept[c1 - 1].end, w0, w1, v0, v1)

    start = _snap_start(t0, spans)
    end = _snap_end(t1, spans)
    if end - start < 0.2:
        return first
    return Span(start, end, score, takes=len(groups), coverage=coverage, tightened=True)
