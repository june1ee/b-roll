from broll.caption import Spoken, phrases


def words(*spec):
    """(text, start, end, cut) 튜플 → Spoken. 원본 시각과 타임라인 시각을 같게 둔다."""
    return [Spoken(t, s, e, s, e, cut) for t, s, e, cut in spec]


def texts(groups):
    return [" ".join(w.text for w in g) for g in groups]


def test_breaks_at_sentence_end_and_pause():
    ws = words(("안녕하세요.", 0.0, 0.5, 0), ("오늘은", 0.6, 0.9, 0),
               ("날씨가", 0.95, 1.2, 0), ("좋네요", 2.0, 2.4, 0))
    assert texts(phrases(ws)) == ["안녕하세요.", "오늘은 날씨가", "좋네요"]


def test_cut_boundary_breaks_only_for_line_recordings():
    ws = words(("결제", 0.0, 0.3, 0), ("시도", 0.35, 0.6, 0), ("두", 0.62, 0.7, 0),
               ("건이었어요", 0.72, 1.2, 1))
    assert texts(phrases(ws, break_on_cut=True)) == ["결제 시도 두", "건이었어요"]
    assert texts(phrases(ws, break_on_cut=False)) == ["결제 시도 두 건이었어요"]


def test_long_run_splits_under_max_chars_at_clause_endings():
    ws = words(("회사를", 0.0, 0.3, 0), ("다니고", 0.32, 0.6, 0), ("있어서", 0.62, 0.9, 0),
               ("주말에만", 0.92, 1.3, 0), ("만들었어요", 1.32, 1.8, 0))
    lines = texts(phrases(ws, max_chars=12))
    assert all(len(line) <= 12 for line in lines)
    assert lines == ["회사를 다니고 있어서", "주말에만 만들었어요"]
