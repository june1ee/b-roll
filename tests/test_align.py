from broll.align import _fuzzy_substring, find_span, take_groups
from broll.asr import Char


def chars(text, start, step=0.1):
    return [Char(c, start + i * step, start + (i + 1) * step) for i, c in enumerate(text)]


def test_fuzzy_substring_prefers_the_last_take():
    start, end, score = _fuzzy_substring("안녕", "안녕음안녕")
    assert (start, end, score) == (3, 5, 1.0)


def test_take_groups_splits_on_long_silence_and_keeps_short_tails():
    spans = [(0.0, 1.0), (1.3, 2.0), (3.5, 4.5), (5.4, 5.7)]
    assert take_groups(spans) == [[(0.0, 1.0), (1.3, 2.0)], [(3.5, 4.5), (5.4, 5.7)]]


def test_find_span_keeps_first_take_when_caption_differs():
    spans = [(0.5, 1.5), (3.0, 4.0)]
    span = find_span(chars("전혀다른말", 0.5), "자막", spans)
    assert (span.start, span.end, span.tightened) == (0.5, 3.0, False)


def test_find_span_tightens_on_a_verbatim_caption():
    spans = [(0.5, 1.5)]
    span = find_span(chars("오늘도출근", 0.5, step=0.2), "오늘도 출근", spans)
    assert span.tightened and span.score == 1.0
    assert (span.start, span.end) == (0.5, 1.5)
