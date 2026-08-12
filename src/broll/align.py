"""강제 정렬 - 녹음 안에서 '그 줄에 쓸 구간'을 찾는다.

역할 분담이 핵심이다:
  - **무엇을 말했나** → whisper (텍스트 매칭, 여러 테이크 중 마지막 고르기)
  - **어디서 시작하고 끝났나** → 무음 검출 (whisper 타임스탬프는 실측 0.3~1.3초씩 밀린다)

whisper 시간축을 무음 검출이 잡은 발화 범위에 선형으로 맞춘 뒤, 매칭된 구간의 경계를
가장 가까운 무음 경계로 스냅한다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .asr import Char

_KEEP = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㆎ]")
# 텍스트로 구간을 좁히는 건 말을 잘라먹을 위험이 있어서, 자막이 사실상 대사 전문일 때만 한다.
# 실측: coverage 0.70 에서 좁혔더니 "…출근하는"이 "…출근한"에서 잘렸다.
STRONG_MATCH = 0.85
MIN_COVERAGE = 0.85


@dataclass
class Span:
    start: float
    end: float
    score: float
    takes: int = 1          # 검출된 테이크 수 (2 이상이면 재녹음/군더더기 흔적)
    coverage: float = 0.0   # 자막이 들린 말의 몇 %를 덮는가
    tightened: bool = False # 텍스트로 구간을 좁혔는지 (아니면 테이크 통째)

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
    """ASR 안에서 exp 와 가장 가까운 구간 [start, end) 와 점수. 동점이면 뒤쪽(마지막 테이크)."""
    m, n = len(exp), len(asr)
    prev_d = [0] * (n + 1)              # 행 0: 어디서 시작해도 공짜
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
    """whisper 시간축 → 무음 검출 시간축."""
    if w1 - w0 < 1e-3:
        return v0
    return v0 + (t - w0) / (w1 - w0) * (v1 - v0)


SNAP = 0.35   # 이 안쪽이면 발화 경계에 붙이고, 아니면 계산된 시각을 그대로 쓴다


def _snap_start(t: float, spans: list[tuple[float, float]]) -> float:
    if t <= spans[0][0]:
        return spans[0][0]
    for i, (s, e) in enumerate(spans):
        if s <= t <= e:
            return s if t - s <= SNAP else t
        if t < s:                       # 무음 갭에 떨어졌으면 다음 발화 시작으로
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
        if t < s:                       # 무음 갭 - 직전 발화 끝으로 (다음 구간으로 넘기면 안 된다)
            return spans[i - 1][1] if i else s
    return spans[-1][1]


def take_groups(spans: list[tuple[float, float]], *, gap: float = 0.8,
                tail_gap: float = 1.2, tail_len: float = 0.5
                ) -> list[list[tuple[float, float]]]:
    """긴 침묵으로 끊어 '테이크'로 묶는다.

    짧은 숨은 한 테이크 안. 다만 침묵 뒤에 붙은 아주 짧은 조각은 새 테이크가 아니라
    문장 꼬리('~이에요')인 경우가 많아서 앞 테이크에 붙인다.
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
    # 첫 테이크를 쓰되, 끝은 '다음 테이크가 시작하기 직전'까지 늘린다.
    # 실측(0806): June 은 뒤 군더더기 테이크만 버리고 그 앞 침묵은 남긴다.
    # 어차피 배속을 걸면 그 침묵도 같이 줄어든다.
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
    # 자막이 대사를 거의 그대로 옮긴 경우에만 텍스트로 구간을 좁힌다.
    # 자막이 요약이면(커버리지가 낮으면) 어느 부분을 말한 건지 텍스트로 못 정하므로
    # 첫 테이크를 통째로 쓰는 쪽이 안전하다 - 실측상 뒤 테이크는 군더더기였다.
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
