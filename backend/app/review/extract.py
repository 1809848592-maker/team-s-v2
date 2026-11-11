"""本地只读抽取：保留位置及覆盖范围，失败时不返回截断的成功结果。"""

import hashlib
import re

from app.review.extract_types import (
    Collector,
    ExtractedDocument,
    ExtractionError,
    ExtractionLimits,
    TextLocation,
)


def _markdown(data: bytes, collector: Collector) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ExtractionError("invalid_encoding") from None
    if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        raise ExtractionError("invalid_document")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines, start=1):
        collector.add(line, TextLocation(part="markdown", line_start=number, line_end=number))
    if re.search(r"!\[|<img\b|<svg\b|<canvas\b|<video\b|<audio\b", text, re.IGNORECASE):
        collector.warn("visual_content", "Markdown内嵌图片、音视频和图形未作识别，仅保留源文本。")


def extract_text(
    data: bytes,
    filename: str,
    *,
    limits: ExtractionLimits | None = None,
) -> ExtractedDocument:
    """文件名只用于选择格式，原内容不改写、不执行，也不跟随外部链接。"""
    limits = limits if limits is not None else ExtractionLimits()
    if not isinstance(data, bytes) or not isinstance(filename, str):
        raise ExtractionError("invalid_document")
    if len(data) > limits.max_file_bytes:
        raise ExtractionError("limit_exceeded")
    suffix = filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)
    format_name = suffix[-1].lower() if len(suffix) == 2 else ""
    if format_name not in {"docx", "pptx", "pdf", "md"}:
        raise ExtractionError("unsupported_format")
    collector = Collector(limits)
    if format_name == "md":
        _markdown(data, collector)
    elif format_name == "pdf":
        from app.review.pdf_extract import extract_pdf

        extract_pdf(data, collector)
    else:
        from app.review.ooxml_extract import extract_docx, extract_pptx

        (extract_docx if format_name == "docx" else extract_pptx)(data, collector)
    if not collector.segments:
        collector.warn("no_text", "未抽取到可用文本；这不表示文件已检查或审核通过。")
    return ExtractedDocument(
        format=format_name,
        sha256=hashlib.sha256(data).hexdigest(),
        segments=tuple(collector.segments),
        warnings=tuple(collector.warnings),
    )
