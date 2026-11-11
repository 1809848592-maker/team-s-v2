"""从冻结条目确定性读取要求，未知/冲突要求诚实报告，不借范例猜测。"""

import hashlib
from copy import deepcopy
from uuid import uuid4

import pytest
from test_extract_ooxml import (
    W,
    docx_entries,
    package,
    pptx_entries,
    relationship,
    relationships,
)

from app.presets.catalog import preset_catalog
from app.review.format_check import inspect_document
from app.review.format_rules import compile_item
from app.schemas.specs import SpecItem


def item(requirement="文稿中文正文使用宋体、英文正文使用 Arial，均为12磅。", **overrides):
    return {
        "number": "F1",
        "category": "format",
        "check_method": "program",
        "requirement": requirement,
        **overrides,
    }


def word_bytes(*, cn="宋体", en="Times New Roman", size=24, text="正文 English"):
    styles = (
        f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:default="1" '
        'w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'
    )
    body = (
        f'<w:p><w:r><w:rPr><w:rFonts w:eastAsia="{cn}" w:ascii="{en}"/>'
        f'<w:sz w:val="{size}"/></w:rPr><w:t>{text}</w:t></w:r></w:p>'
    )
    return package(
        docx_entries(
            body,
            extras=[
                ("word/styles.xml", styles),
                (
                    "word/_rels/document.xml.rels",
                    relationships(relationship("styles", "styles.xml", "styles")),
                ),
            ],
        )
    )


def preset_fonts():
    return deepcopy(next(p for p in preset_catalog() if p["key"] == "fonts_cn_en")["items"])


def test_existing_font_presets_compile_from_actual_requirement_not_number_or_examples():
    first, second = preset_fonts()
    for spec in (first, second):
        spec["number"] = "arbitrary-renumbered"
        spec["positive_example"] = "正文应为30磅"
        spec["negative_example"] = "正文使用宋体12磅"
    word = compile_item(SpecItem.model_validate(first))
    assert word.formats == {"docx"}
    assert word.profile["font_name"] == {"eastAsia": "宋体", "ascii": "Times New Roman"}
    assert word.profile["font_size"] == {"eastAsia": 12, "ascii": 12}
    ppt = compile_item(SpecItem.model_validate(second))
    assert ppt.formats == {"pptx"}
    assert ppt.profile["title_font_name"] == {"eastAsia": "黑体", "ascii": "Arial"}
    assert ppt.profile["title_font_size"] == {"eastAsia": 28}
    assert ppt.profile["font_size"] == {"eastAsia": 20}


@pytest.mark.parametrize(
    "text,profile,formats",
    [
        (
            "正文使用宋体12磅。",
            {
                "font_name": {"eastAsia": "宋体", "ascii": "宋体"},
                "font_size": {"eastAsia": 12, "ascii": 12},
            },
            {"docx", "pptx"},
        ),
        (
            "中文正文使用宋体、小四号。",
            {"font_name": {"eastAsia": "宋体"}, "font_size": {"eastAsia": 12}},
            {"docx", "pptx"},
        ),
        ("英文正文字体为 Arial。", {"font_name": {"ascii": "Arial"}}, {"docx", "pptx"}),
        ("PPT 中文标题字号为28pt。", {"title_font_size": {"eastAsia": 28}}, {"pptx"}),
        (
            'Word 英文正文使用"Custom Face"，字号为10.5磅。',
            {"font_name": {"ascii": "Custom Face"}, "font_size": {"ascii": 10.5}},
            {"docx"},
        ),
        (
            "PPT 画布尺寸为宽10英寸、高5.625英寸。",
            {"slide_width": 9144000, "slide_height": 5143500},
            {"pptx"},
        ),
        (
            "文稿页面尺寸为宽21厘米、高29.7厘米。",
            {"page_width": 11906, "page_height": 16838},
            {"docx"},
        ),
    ],
)
def test_only_complete_explicit_grammar_is_compiled(text, profile, formats):
    rule = compile_item(SpecItem.model_validate(item(text)))
    assert rule.profile == profile
    assert rule.formats == formats


@pytest.mark.parametrize(
    "text",
    [
        "正文使用宋体12磅，但表格除外。",
        "全文使用宋体12磅。",
        "正文至少12磅。",
        "正文使用宋体12磅。还要求行距1.5倍。",
        "正文使用宋体或黑体12磅。",
        "正文使用宋体但图注不用宋体12磅。",
        "正文使用宋体0磅。",
        "正文使用宋体NaN磅。",
        "Word 标题使用宋体12磅。",
        "标题使用宋体12磅。",
        "引用按APA格式。",
        "PPT 画布尺寸为宽100twip、高200twip。",
        "文稿页面尺寸为宽100EMU、高200EMU。",
        "PPT 画布尺寸为宽0厘米、高20厘米。",
        "排版美观，使用模板。",
        '中文正文字体为"   "。',
    ],
)
def test_ambiguous_partial_or_invalid_requirements_remain_uncovered(text):
    spec = item(text, positive_example="正文使用宋体12磅。")
    assert compile_item(SpecItem.model_validate(spec)) is None
    result = inspect_document(word_bytes(), "fixed.docx", [spec])
    assert result["status"] == "partial"
    assert result["checks"][0]["status"] == "uncovered"
    assert result["findings"] == []


def test_read_only_dual_script_check_has_original_basis_and_separate_mismatches():
    first, _ = preset_fonts()
    data = word_bytes()
    digest = hashlib.sha256(data).hexdigest()
    clean = inspect_document(data, "fixed.docx", [first])
    assert clean["status"] == "passed"
    assert clean["sha256"] == digest == hashlib.sha256(data).hexdigest()
    bad = inspect_document(word_bytes(en="Arial"), "fixed.docx", [first])
    assert bad["status"] == "issues"
    finding = next(f for f in bad["findings"] if f["rule"] == "font_name")
    assert finding["blocking"] is True
    assert finding["spec_number"] == first["number"]
    assert finding["requirement"] == first["requirement"]
    assert "Times New Roman" in finding["message"] and "正文第 1 段" in finding["location"]


def test_all_program_categories_are_processed_but_ai_and_manual_are_delegated():
    for category in ("format", "writing", "template", "handoff"):
        result = inspect_document(
            word_bytes(),
            "fixed.docx",
            [
                item("英文正文使用 Arial 12磅。", category=category),
                item("英文正文使用 Arial 12磅。", number="AI1", check_method="ai"),
                item("英文正文使用 Arial 12磅。", number="M1", check_method="manual"),
            ],
        )
        assert result["checks"][0]["status"] == "issues"
        assert [c["status"] for c in result["checks"]][1:] == ["delegated", "delegated"]
        assert all(f["spec_number"] == "F1" for f in result["findings"])


def test_unknown_and_template_requirements_not_lost_alongside_known_rules():
    known, _ = preset_fonts()
    result = inspect_document(
        word_bytes(),
        "fixed.docx",
        [
            known,
            item("页边距须合适。", number="unknown"),
            item(
                "使用选定模板。",
                number="template",
                category="template",
                template_resource_id=str(uuid4()),
            ),
        ],
    )
    assert result["status"] == "partial"
    assert [c["status"] for c in result["checks"]] == ["passed", "uncovered", "uncovered"]
    assert "视觉一致性" in str(result["checks"][-1]["coverage"])


def test_mutually_exclusive_requirements_are_uncovered_not_file_nonconformance():
    specs = [item("英文正文字体为Arial。"), item("英文正文字体为Times New Roman。", number="F2")]
    for ordered in (specs, list(reversed(specs))):
        result = inspect_document(word_bytes(), "fixed.docx", ordered)
        assert result["status"] == "partial" and not result["findings"]
        assert {c["status"] for c in result["checks"]} == {"uncovered"}
        assert all("互斥" in str(c["coverage"]) for c in result["checks"])


@pytest.mark.parametrize("filename", ["fixed.pdf", "fixed.md", "fixed.exe"])
def test_unsupported_format_is_never_passed(filename):
    result = inspect_document(b"fixed", filename, preset_fonts())
    assert result["status"] == "unsupported"
    assert all(c["status"] == "uncovered" for c in result["checks"])


def test_no_program_checks_and_wrong_format_are_not_passes():
    _, ppt = preset_fonts()
    assert inspect_document(word_bytes(), "fixed.docx", [])["status"] == "partial"
    result = inspect_document(word_bytes(), "fixed.docx", [ppt])
    assert result["status"] == "not_applicable"
    ai = item(check_method="ai")
    assert inspect_document(word_bytes(), "fixed.docx", [ai])["status"] == "partial"


@pytest.mark.parametrize("specs", [{}, [item(), item()], [item(unknown_field="secret")]])
def test_invalid_snapshot_fails_without_echoing_input(specs):
    result = inspect_document(word_bytes(), "fixed.docx", specs)
    assert result["status"] == "error"
    assert "secret" not in str(result)


def test_bad_office_bytes_are_execution_failure_not_blocking_nonconformance():
    result = inspect_document(b"private-marker", "fixed.docx", [preset_fonts()[0]])
    assert result["status"] == "error"
    assert "private-marker" not in str(result)
    assert all(f["blocking"] is False for f in result["findings"])


def test_actual_ppt_preset_checks_title_body_and_english_without_inventing_english_size():
    def shape(role, cn, size, english="Arial"):
        return (
            '<p:sp><p:nvSpPr><p:nvPr><p:ph type="' + role + '"/></p:nvPr></p:nvSpPr>'
            '<p:txBody><a:p><a:r><a:rPr sz="' + str(size) + '">'
            '<a:ea typeface="' + cn + '"/><a:latin typeface="' + english + '"/>'
            '</a:rPr><a:t>中文</a:t></a:r><a:r><a:rPr sz="1600">'
            '<a:latin typeface="' + english + '"/></a:rPr><a:t>English</a:t></a:r>'
            "</a:p></p:txBody></p:sp>"
        )

    _, spec = preset_fonts()
    good = package(
        pptx_entries([("one.xml", shape("title", "黑体", 2800) + shape("body", "微软雅黑", 2000))])
    )
    assert inspect_document(good, "fixed.pptx", [spec])["status"] == "passed"
    bad = package(
        pptx_entries(
            [("one.xml", shape("title", "宋体", 2000, "Calibri") + shape("body", "微软雅黑", 2000))]
        )
    )
    result = inspect_document(bad, "fixed.pptx", [spec])
    assert result["status"] == "issues"
    assert {f["rule"] for f in result["findings"]} >= {"title_font_name", "title_font_size"}
    assert all(f["spec_number"] == spec["number"] for f in result["findings"])


def test_same_named_font_aliases_do_not_create_false_spec_conflict():
    result = inspect_document(
        word_bytes(),
        "fixed.docx",
        [item("中文正文字体为宋体。"), item("中文正文字体为SimSun。", number="F2")],
    )
    assert result["status"] == "passed"


def test_office_package_parsed_once_for_multiple_independent_items(monkeypatch):
    from app.review import format_office

    original = format_office._Package
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(format_office, "_Package", counted)
    result = inspect_document(
        word_bytes(),
        "fixed.docx",
        [
            item("中文正文字体为宋体。"),
            item("英文正文字体为Times New Roman。", number="F2"),
            item("中文正文字号为12磅。", number="F3"),
        ],
    )
    assert result["status"] == "passed"
    assert len(calls) == 1


def test_combined_report_stays_bounded_and_reports_truncation():
    specs = [item("英文正文字体为Arial。", number=f"F{index}") for index in range(110)]
    result = inspect_document(word_bytes(), "fixed.docx", specs)
    assert result["status"] == "issues"
    assert len(result["findings"]) == 100
    assert len(result["checks"]) == 110
    assert "截断" in str(result["coverage"])
