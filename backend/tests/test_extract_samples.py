"""独立Office生成器制作的真实文件，通过公共API验证主要来源位置。"""

from pathlib import Path

from app.review.extract import extract_text

FIXTURES = Path(__file__).parent / "fixtures" / "extract"


def test_authored_docx_preserves_body_table_order_and_blank_paragraph_positions():
    path = FIXTURES / "authored-paragraphs-table.docx"
    result = extract_text(path.read_bytes(), path.name)
    assert [segment.text for segment in result.segments] == [
        "DOCX_TITLE_101 中文标题",
        "DOCX_BODY_103 alpha\nDOCX_BODY_103 beta\t中文",
        "DOCX_R1C1_201",
        "DOCX_R1C2_202",
        "DOCX_R2C1_203",
        "DOCX_R2C1_P2_204",
        "DOCX_R2C2_205",
        "DOCX_AFTER_TABLE_301",
    ]
    assert [segment.location.paragraph for segment in result.segments] == [1, 3, 4, 5, 6, 7, 8, 9]
    cells = [segment for segment in result.segments if segment.location.table is not None]
    assert [(cell.location.table, cell.location.row, cell.location.column) for cell in cells] == [
        (1, 1, 1),
        (1, 1, 2),
        (1, 2, 1),
        (1, 2, 1),
        (1, 2, 2),
    ]
    assert all(segment.location.part == "word/document.xml" for segment in result.segments)
    assert all(segment.location.page is None for segment in result.segments)
    assert result.coverage == "partial"
    assert "supplementary_text_unsupported" in {warning.code for warning in result.warnings}
    assert not any("HEADER_EXCLUDED_719" in segment.text for segment in result.segments)


def test_authored_pptx_uses_presentation_order_and_keeps_group_table_locations():
    path = FIXTURES / "authored-reordered-slides.pptx"
    result = extract_text(path.read_bytes(), path.name)
    titles = [segment for segment in result.segments if "_TITLE" in segment.text]
    source_numbers = [12, *range(2, 12), 1]
    assert [segment.text for segment in titles] == [
        f"PPTX_FILE{number:02}_TITLE 中文" for number in source_numbers
    ]
    assert [segment.location.slide for segment in titles] == list(range(1, 13))
    assert [segment.location.part for segment in titles] == [
        f"ppt/slides/slide{number}.xml" for number in source_numbers
    ]
    assert all(segment.location.shape_id is not None for segment in result.segments)
    second_slide = [segment for segment in result.segments if segment.location.slide == 2]
    assert [segment.text for segment in second_slide] == [
        "PPTX_FILE02_TITLE 中文",
        "PPTX_FILE02_BODY_P1",
        "PPTX_FILE02_BODY_P3",
        "PPTX_FILE02_R1C1",
        "PPTX_FILE02_R1C2",
    ]
    body = [segment for segment in second_slide if "_BODY_" in segment.text]
    assert [segment.location.paragraph for segment in body] == [1, 3]
    assert len({segment.location.shape_id for segment in body}) == 1
    cells = [segment for segment in second_slide if segment.location.table is not None]
    assert [(cell.location.table, cell.location.row, cell.location.column) for cell in cells] == [
        (1, 1, 1),
        (1, 1, 2),
    ]
    grouped = next(s for s in result.segments if s.text == "PPTX_FILE10_GROUP_BODY")
    assert grouped.location.slide == 10 and grouped.location.part == "ppt/slides/slide10.xml"
    assert grouped.location.paragraph == 1
    assert not any("NOTES_FILE" in segment.text for segment in result.segments)
    assert result.coverage == "partial"
    assert "notes_ignored" in {warning.code for warning in result.warnings}


def test_authored_markdown_keeps_bom_crlf_source_line_locations_and_literal_markup():
    path = FIXTURES / "authored-bom-crlf.md"
    original = path.read_bytes()
    assert original.startswith(b"\xef\xbb\xbf") and b"\r\n" in original
    result = extract_text(original, path.name)
    assert [segment.text for segment in result.segments] == [
        "# MD_TITLE_001 中文",
        "MD_BODY_003 first line",
        "MD_BODY_004 second line",
        "```python",
        "MD_CODE_007 = 'literal'",
        "```",
        "| Column | Value |",
        "| --- | --- |",
        "| MD_TABLE_012 | 中文值 |",
    ]
    assert [segment.location.line_start for segment in result.segments] == [
        1,
        3,
        4,
        6,
        7,
        8,
        10,
        11,
        12,
    ]
    assert all(
        segment.location.line_start == segment.location.line_end
        and segment.location.part == "markdown"
        and segment.location.page is None
        for segment in result.segments
    )
    assert result.coverage == "text_extracted" and not result.warnings
