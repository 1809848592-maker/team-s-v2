"""抽取入口及UTF-8源文本定位；不依赖浏览器、Office或模型。"""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from app.review.extract import extract_text
from app.review.extract_types import ExtractionError, ExtractionLimits


def test_markdown_preserves_source_lines_whitespace_markup_and_original_file_hash():
    data = (
        "\ufeff# 研究问题\r\n\r\n  原文 **证据** [引文](https://example.invalid)  "
        "\r\n\t表格\r第五行"
    ).encode()
    result = extract_text(data, "交付.MD")
    assert result.format == "md" and result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.coverage == "text_extracted" and result.warnings == ()
    assert [item.text for item in result.segments] == [
        "# 研究问题",
        "  原文 **证据** [引文](https://example.invalid)  ",
        "\t表格",
        "第五行",
    ]
    assert [item.location.line_start for item in result.segments] == [1, 3, 4, 5]
    assert [item.location.line_end for item in result.segments] == [1, 3, 4, 5]
    assert all(item.location.page is None for item in result.segments)
    assert "研究问题" in json.dumps(result.to_dict(), ensure_ascii=False)
    with pytest.raises(FrozenInstanceError):
        result.segments[0].text = "altered"


def test_markdown_is_inert_source_including_code_html_links_and_image_warning():
    data = b'```sh\ncurl https://never-fetch.invalid\n```\n<img src="file:///private">\n'
    result = extract_text(data, "data.md")
    assert [item.text for item in result.segments] == data.decode().splitlines()
    assert result.coverage == "partial"
    assert [item.code for item in result.warnings] == ["visual_content"]


@pytest.mark.parametrize("data", [b"", b"\xef\xbb\xbf \r\n\t\r\n"])
def test_empty_markdown_reports_no_text_without_pretending_to_have_coverage(data):
    result = extract_text(data, "empty.md")
    assert result.coverage == "no_text" and not result.segments
    assert "no_text" in {item.code for item in result.warnings}


@pytest.mark.parametrize("filename", ["old.doc", "old.ppt", "other.txt", "file", "a.md.exe"])
def test_unsupported_format_has_fixed_error_and_no_filename_in_message(filename):
    with pytest.raises(ExtractionError) as failed:
        extract_text(b"private manuscript", filename)
    assert failed.value.code == "unsupported_format"
    assert "private" not in str(failed.value) and filename not in str(failed.value)


@pytest.mark.parametrize(
    ("data", "code"),
    [(b"\xff", "invalid_encoding"), (b"a\x00b", "invalid_document"), (b"\x1b", "invalid_document")],
)
def test_markdown_rejects_invalid_encoding_and_binary_controls(data, code):
    with pytest.raises(ExtractionError) as failed:
        extract_text(data, "input.md")
    assert failed.value.code == code


@pytest.mark.parametrize(
    ("limits", "data"),
    [
        (replace(ExtractionLimits(), max_file_bytes=3), b"four"),
        (replace(ExtractionLimits(), max_text_chars=3), b"four"),
        (replace(ExtractionLimits(), max_segments=1), b"one\ntwo"),
    ],
)
def test_limits_reject_incomplete_extraction_instead_of_silently_truncating(limits, data):
    with pytest.raises(ExtractionError) as failed:
        extract_text(data, "limit.md", limits=limits)
    assert failed.value.code == "limit_exceeded"


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_limits_require_positive_integer(invalid):
    with pytest.raises(ValueError):
        ExtractionLimits(max_segments=invalid)
