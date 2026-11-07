import json

import pytest

from broll import capcut as cc


@pytest.fixture
def project(tmp_path):
    """캡컷 프로젝트의 최소 골격: 루트 문서, 하위 타임라인 문서, project.json, 메타."""
    raw = {
        "id": "TL", "duration": 2_000_000,
        "tracks": [{"type": "video", "segments": [
            {"material_id": "v1", "extra_material_refs": ["s1"],
             "target_timerange": {"start": 0, "duration": 1_000_000}},
            {"material_id": "v2", "extra_material_refs": ["s2"],
             "target_timerange": {"start": 1_000_000, "duration": 1_000_000}},
        ]}],
        "materials": {"videos": [{"id": "v1", "path": ""}, {"id": "v2", "path": ""}],
                      "speeds": [{"id": "s1"}, {"id": "s2"}], "texts": []},
    }
    root = tmp_path / "demo"
    (root / "Timelines" / "TL").mkdir(parents=True)
    body = json.dumps(raw)
    (root / "draft_info.json").write_text(body)
    (root / "Timelines" / "TL" / "draft_info.json").write_text(body)
    (root / "Timelines" / "project.json").write_text(json.dumps({"id": "P", "main_timeline_id": "TL"}))
    (root / "draft_meta_info.json").write_text(json.dumps({"draft_id": "D", "tm_duration": 0}))
    return root


def test_clean_project_validates(project):
    assert cc.validate_draft(project) == []


def test_shared_extra_material_is_reported(project):
    raw = cc.load_raw(project)
    raw["tracks"][0]["segments"][1]["extra_material_refs"] = ["s1"]
    cc.save_in_place(project, raw)
    assert any("shared" in p for p in cc.validate_draft(project))


def test_style_range_is_counted_in_utf16(project):
    raw = cc.load_raw(project)
    text = "화나😡"
    raw["materials"]["texts"].append(
        {"id": "t1", "content": json.dumps({"text": text, "styles": [{"range": [0, len(text)]}]})})
    cc.save_in_place(project, raw)
    assert cc.u16len(text) == 4
    assert any("UTF-16" in p for p in cc.validate_draft(project))


def test_save_in_place_writes_both_documents_and_keeps_ids(project):
    raw = cc.load_raw(project)
    raw["duration"] = 3_000_000
    cc.save_in_place(project, raw)
    sub = json.loads((project / "Timelines" / "TL" / "draft_info.json").read_text())
    pj = json.loads((project / "Timelines" / "project.json").read_text())
    meta = json.loads((project / "draft_meta_info.json").read_text())
    assert sub["duration"] == 3_000_000
    assert (pj["id"], meta["draft_id"], meta["tm_duration"]) == ("P", "D", 3_000_000)
    assert cc.validate_draft(project) == []


def test_save_in_place_refuses_an_open_project(project):
    (project / ".locked").touch()
    assert cc.is_open(project)
    with pytest.raises(RuntimeError, match="open in CapCut"):
        cc.save_in_place(project, cc.load_raw(project))


def test_caption_helpers():
    assert cc.classify_caption("9:43") == "clock"
    assert cc.classify_caption("(부연)") == "aside"
    assert cc.classify_caption("메인 자막") == "main"
    assert cc.plain_caption("여기만 *강조* 해요") == "여기만 강조 해요"
