"""本地 PDF 样例和安全边界；不使用 OCR、网络或模型。

pdf-mixed-pages.pdf 使用 ReportLab 4.4.9 的 Canvas(invariant=True,
pageCompression=1, pagesize=(400, 400)) 生成，作者固定为 Team.s tests。
第1页 Helvetica 12 两行英文，第2页空白，第3页 PIL 80x80 白底黑方块，
第4页使用 UnicodeCIDFont("STSong-Light")，以12号字写下下方固定中文；
不嵌入或分发系统字体。PDF不含真实用户材料；生成和渲染依赖只用于
准备固定fixture，不是运行依赖。CID中文页经macOS Quick Look渲染检查。
畸形和边界文件用 pypdf 的公开对象接口在内存生成，测试只读取生成后的字节。
"""

import logging
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter, get_configuration
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from app.review.extract import extract_text
from app.review.extract_types import ExtractionError, ExtractionLimits

FIXTURE = Path(__file__).parent / "fixtures" / "extract" / "pdf-mixed-pages.pdf"
CHINESE = "中文段落：研究结果与证据。"


def _dictionary(**values):
    return DictionaryObject({NameObject("/" + key): value for key, value in values.items()})


def _font_resources():
    return _dictionary(
        Font=_dictionary(
            F1=_dictionary(
                Type=NameObject("/Font"),
                Subtype=NameObject("/Type1"),
                BaseFont=NameObject("/Helvetica"),
            )
        )
    )


def _stream(data: bytes, *, compressed=False):
    stream = DecodedStreamObject()
    stream.set_data(data)
    return stream.flate_encode() if compressed else stream


def _pdf(*contents: bytes, compressed=False, resources=None) -> bytes:
    writer = PdfWriter()
    for content in contents:
        page = writer.add_blank_page(400, 400)
        page[NameObject("/Resources")] = resources if resources is not None else _font_resources()
        page[NameObject("/Contents")] = _stream(content, compressed=compressed)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _extract(data: bytes, **limits):
    return extract_text(data, "source.pdf", limits=replace(ExtractionLimits(), **limits))


def _assert_error(data, code, **limits):
    with pytest.raises(ExtractionError) as caught:
        _extract(data, **limits)
    assert caught.value.code == code
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    return caught.value


def test_real_pdf_preserves_physical_pages_chinese_and_extracted_line_locations():
    result = _extract(FIXTURE.read_bytes())
    assert [segment.text for segment in result.segments] == [
        "First evidence line.",
        "Second evidence line.",
        CHINESE,
    ]
    assert [
        (
            s.location.part,
            s.location.page,
            s.location.paragraph,
            s.location.line_start,
            s.location.line_end,
        )
        for s in result.segments
    ] == [("pdf", 1, 1, 1, 1), ("pdf", 1, 2, 2, 2), ("pdf", 4, 1, 1, 1)]
    assert result.coverage == "partial"
    assert {(w.code, w.location.page if w.location else None) for w in result.warnings} == {
        ("pdf_layout", None),
        ("pdf_page_no_text", 2),
        ("pdf_page_no_text", 3),
        ("visual_content", 3),
    }


@pytest.mark.parametrize("password", ["", "fixture-password"])
def test_encrypted_pdf_is_refused_even_when_empty_password_would_open_it(password):
    writer = PdfWriter(clone_from=BytesIO(FIXTURE.read_bytes()))
    writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    _assert_error(output.getvalue(), "encrypted_document")


@pytest.mark.parametrize("data", [b"", b"%PDF-1.7\nPRIVATE_SENTINEL", b"not a PDF"])
def test_corrupt_pdf_is_refused_with_safe_error(data, caplog, capsys):
    _assert_error(data, "invalid_document")
    assert "PRIVATE_SENTINEL" not in caplog.text + capsys.readouterr().err


def test_truncated_real_pdf_is_not_reported_as_partial_success():
    _assert_error(FIXTURE.read_bytes()[:-100], "invalid_document")


def test_page_limit_counts_blank_and_image_pages():
    assert len(_extract(FIXTURE.read_bytes(), max_pages=4).segments) == 3
    _assert_error(FIXTURE.read_bytes(), "limit_exceeded", max_pages=3)


def test_character_limit_accepts_exact_boundary_and_refuses_truncation():
    count = len("First evidence line.") + len("Second evidence line.") + len(CHINESE)
    assert len(_extract(FIXTURE.read_bytes(), max_text_chars=count).segments) == 3
    _assert_error(FIXTURE.read_bytes(), "limit_exceeded", max_text_chars=count - 1)


def test_segment_limit_refuses_truncation():
    assert len(_extract(FIXTURE.read_bytes(), max_segments=4).segments) == 3
    # Four warnings also have their own bound; use pure text for this segment check.
    data = _pdf(b"BT /F1 12 Tf (One) Tj 0 -20 Td (Two) Tj ET")
    assert len(_extract(data, max_segments=2).segments) == 2
    _assert_error(data, "limit_exceeded", max_segments=1)


def test_compressed_content_limit_applies_during_decompression():
    payload = b"BT /F1 12 Tf (" + b"A" * 8192 + b") Tj ET"
    data = _pdf(payload, compressed=True)
    assert len(data) < 2048
    _assert_error(data, "limit_exceeded", max_pdf_content_bytes=1024)


def test_content_budget_is_cumulative_across_pages_and_has_exact_boundary():
    payload = b"BT /F1 12 Tf (Evidence) Tj ET"
    data = _pdf(payload, payload, compressed=True)
    assert len(_extract(data, max_pdf_content_bytes=2 * len(payload)).segments) == 2
    _assert_error(data, "limit_exceeded", max_pdf_content_bytes=2 * len(payload) - 1)


def test_array_of_content_streams_cannot_bypass_cumulative_budget():
    payload = b"BT /F1 12 Tf (Evidence) Tj ET\n"
    writer = PdfWriter(clone_from=BytesIO(_pdf(payload)))
    writer.pages[0][NameObject("/Contents")] = ArrayObject(
        [_stream(payload, compressed=True), _stream(payload, compressed=True)]
    )
    output = BytesIO()
    writer.write(output)
    assert len(_extract(output.getvalue(), max_pdf_content_bytes=2 * len(payload)).segments) == 1
    _assert_error(output.getvalue(), "limit_exceeded", max_pdf_content_bytes=2 * len(payload) - 1)


def test_form_content_is_extracted_and_included_in_decompression_budget():
    content = b"BT /F1 12 Tf (Nested evidence) Tj ET"
    form = _stream(content, compressed=True)
    form.update(_dictionary(Subtype=NameObject("/Form"), Resources=_font_resources()))
    resources = _font_resources()
    resources[NameObject("/XObject")] = _dictionary(Fm0=form)
    invocation = b"/Fm0 Do"
    data = _pdf(invocation, resources=resources)
    assert [s.text for s in _extract(data).segments] == ["Nested evidence"]
    _assert_error(data, "limit_exceeded", max_pdf_content_bytes=len(content) + len(invocation) - 1)


def test_font_mapping_stream_is_included_in_content_budget():
    content = b"BT /F1 12 Tf (Evidence) Tj ET"
    resources = _font_resources()
    resources["/Font"]["/F1"][NameObject("/ToUnicode")] = _stream(b" " * 2048, compressed=True)
    _assert_error(_pdf(content, resources=resources), "limit_exceeded", max_pdf_content_bytes=1024)


def test_unknown_stream_filter_is_failure_not_empty_success():
    writer = PdfWriter(clone_from=BytesIO(_pdf(b"BT /F1 12 Tf (Evidence) Tj ET")))
    stream = _stream(b"PRIVATE_SENTINEL")
    stream[NameObject("/Filter")] = NameObject("/UnsupportedTestFilter")
    writer.pages[0][NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    _assert_error(output.getvalue(), "invalid_document")


def test_swallowed_pypdf_form_error_is_failure_and_never_logs_document_fragments(caplog, capsys):
    caplog.set_level(logging.WARNING)
    data = _pdf(b"/PRIVATE_SENTINEL Do")
    _assert_error(data, "invalid_document")
    assert "PRIVATE_SENTINEL" not in caplog.text + capsys.readouterr().err
    logging.getLogger("pypdf._page").warning("outside extraction context")
    assert "outside extraction context" in caplog.text


def test_context_scoped_configuration_is_restored_after_success_and_failure():
    original = get_configuration()
    _extract(_pdf(b"BT /F1 12 Tf (Evidence) Tj ET"))
    assert get_configuration() == original
    _assert_error(_pdf(b"A" * 2048, compressed=True), "limit_exceeded", max_pdf_content_bytes=1024)
    assert get_configuration() == original


def test_concurrent_extractions_keep_errors_and_limits_isolated():
    valid = _pdf(b"BT /F1 12 Tf (Evidence) Tj ET")
    invalid = _pdf(b"/PRIVATE_SENTINEL Do")
    with ThreadPoolExecutor(max_workers=2) as pool:
        success = pool.submit(_extract, valid)
        failure = pool.submit(_assert_error, invalid, "invalid_document")
        assert success.result().segments[0].text == "Evidence"
        assert failure.result().code == "invalid_document"


def test_image_only_pdf_does_not_decode_image_or_follow_external_links(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("PDF text extraction must stay local")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    image = _stream(b"deliberately-not-jbig2")
    image.update(
        _dictionary(
            Subtype=NameObject("/Image"),
            Filter=NameObject("/JBIG2Decode"),
            Width=NumberObject(10),
            Height=NumberObject(10),
        )
    )
    resources = _font_resources()
    resources[NameObject("/XObject")] = _dictionary(Image0=image)
    writer = PdfWriter(clone_from=BytesIO(_pdf(b"/Image0 Do", resources=resources)))
    writer.pages[0][NameObject("/Annots")] = ArrayObject(
        [
            _dictionary(
                Subtype=NameObject("/Link"),
                A=_dictionary(
                    S=NameObject("/URI"), URI=TextStringObject("https://invalid.example/")
                ),
            )
        ]
    )
    output = BytesIO()
    writer.write(output)
    result = _extract(output.getvalue(), max_pdf_content_bytes=100)
    assert result.segments == ()
    assert result.coverage == "no_text"
    assert {w.code for w in result.warnings} == {
        "pdf_layout",
        "visual_content",
        "pdf_page_no_text",
        "no_text",
    }


def test_fixture_is_real_pdf_with_four_pages():
    assert len(PdfReader(BytesIO(FIXTURE.read_bytes()), strict=True).pages) == 4


def test_inline_image_is_marked_uncovered_without_pixel_decoding():
    content = b"BI /W 1 /H 1 /BPC 8 /CS /RGB ID \x00\x00\x00 EI"
    result = _extract(_pdf(content))
    assert result.segments == ()
    assert "visual_content" in {w.code for w in result.warnings}
