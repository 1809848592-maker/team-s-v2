"""抽取结果只描述已读文本及其来源，不表示检查或审核已通过。"""

from dataclasses import asdict, dataclass
from typing import Literal


class ExtractionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            {
                "unsupported_format": "此文件格式尚不支持文本抽取。",
                "invalid_document": "文件内容损坏或不符合声明的格式，文本抽取未完成。",
                "encrypted_document": "文件已加密，文本抽取未完成。",
                "limit_exceeded": "文件超过文本抽取处理上限，文本抽取未完成。",
                "invalid_encoding": "Markdown须为UTF-8编码，文本抽取未完成。",
                "source_unavailable": "交付原文件不可用，文本抽取未完成。",
                "source_mismatch": "交付原文件与记录的内容不一致，文本抽取未完成。",
            }.get(code, "文本抽取未完成。")
        )


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    max_archive_entries: int = 10000
    max_uncompressed_bytes: int = 128 * 1024 * 1024
    max_xml_bytes: int = 16 * 1024 * 1024
    max_xml_depth: int = 128
    max_segments: int = 20000
    max_text_chars: int = 2_000_000
    max_pages: int = 1000
    max_pdf_content_bytes: int = 32 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError("文本抽取上限必须是正整数")


@dataclass(frozen=True, slots=True)
class TextLocation:
    part: str
    page: int | None = None
    slide: int | None = None
    paragraph: int | None = None
    table: int | None = None
    row: int | None = None
    column: int | None = None
    shape_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class TextSegment:
    text: str
    location: TextLocation


@dataclass(frozen=True, slots=True)
class ExtractionWarning:
    code: str
    message: str
    location: TextLocation | None = None


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    format: Literal["docx", "pptx", "pdf", "md"]
    sha256: str
    segments: tuple[TextSegment, ...]
    warnings: tuple[ExtractionWarning, ...]

    @property
    def coverage(self) -> Literal["text_extracted", "partial", "no_text"]:
        if not self.segments:
            return "no_text"
        return "partial" if self.warnings else "text_extracted"

    def to_dict(self) -> dict:
        return {**asdict(self), "coverage": self.coverage}


class Collector:
    def __init__(self, limits: ExtractionLimits):
        self.limits = limits
        self.segments: list[TextSegment] = []
        self.warnings: list[ExtractionWarning] = []
        self._characters = 0
        self._warning_keys: set[ExtractionWarning] = set()

    def add(self, text: str, location: TextLocation) -> None:
        if not text.strip():
            return
        if (
            len(self.segments) >= self.limits.max_segments
            or self._characters + len(text) > self.limits.max_text_chars
        ):
            raise ExtractionError("limit_exceeded")
        self._characters += len(text)
        self.segments.append(TextSegment(text, location))

    def warn(self, code: str, message: str, location: TextLocation | None = None) -> None:
        warning = ExtractionWarning(code, message, location)
        if warning not in self._warning_keys:
            if len(self.warnings) >= self.limits.max_segments:
                raise ExtractionError("limit_exceeded")
            self.warnings.append(warning)
            self._warning_keys.add(warning)
