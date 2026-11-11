"""只读取 PDF 文字层；页码为物理页，行号为该页抽取文本中的行。"""

import logging
from contextvars import ContextVar
from io import BytesIO
from pkgutil import walk_packages

import pypdf
from pypdf import PdfReader, apply_configuration
from pypdf.errors import LimitReachedError
from pypdf.generic import ArrayObject, DictionaryObject, NullObject, StreamObject

from app.review.extract_types import Collector, ExtractionError, TextLocation

_diagnostics: ContextVar[list[bool] | None] = ContextVar("pdf_extraction_diagnostics", default=None)


class _PrivatePdfDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        active = _diagnostics.get()
        if active is None:
            return True
        # pypdf 的修复告警可能带原文或对象名；不格式化、不转发原始记录。
        if record.levelno >= logging.WARNING and not active:
            active.append(True)
        return False


# 一次安装，始终只检查当前 ContextVar；不修改全局级别、handler 或 propagate，
# 不在并发调用结束时移除其他调用仍在使用的 filter。
_diagnostic_filter = _PrivatePdfDiagnostics()
for _module in ["pypdf", *(module.name for module in walk_packages(pypdf.__path__, "pypdf."))]:
    logging.getLogger(_module).addFilter(_diagnostic_filter)


def _resolved(value):
    return value.get_object() if value is not None else None


def _check_diagnostics() -> None:
    if _diagnostics.get():
        raise ExtractionError("invalid_document")


class _ContentBudget:
    def __init__(self, collector: Collector):
        self.maximum = collector.limits.max_pdf_content_bytes
        self.used = 0
        self.streams: set[int] = set()
        self.resources: dict[int, bool] = {}

    def decode(self, stream: StreamObject) -> None:
        identity = id(stream)
        if identity in self.streams:
            return
        remaining = self.maximum - self.used
        if remaining < 1:
            raise ExtractionError("limit_exceeded")
        # 解压器在输出期间限流；解码后再累计文档中不同文字/字体流的总量。
        with apply_configuration(
            zlib_maximum_output_length=remaining,
            lzw_maximum_output_length=remaining,
            run_length_maximum_output_length=remaining,
            brotli_maximum_output_length=remaining,
            array_based_stream_maximum_output_length=remaining,
        ):
            decoded = stream.get_data()
        self.used += len(decoded)
        if self.used > self.maximum:
            raise ExtractionError("limit_exceeded")
        self.streams.add(identity)
        _check_diagnostics()

    def contents(self, value) -> None:
        value = _resolved(value)
        if value is None or isinstance(value, NullObject):
            return
        if isinstance(value, ArrayObject):
            for stream in value:
                self.contents(stream)
        elif isinstance(value, StreamObject):
            self.decode(value)
        else:
            raise ExtractionError("invalid_document")

    def fonts(self, value, visited: set[int]) -> None:
        """预解码字体映射等文字资源，避免 extract_text 内部吞掉解码错误。"""
        value = _resolved(value)
        if id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, StreamObject):
            self.decode(value)
        if isinstance(value, DictionaryObject):
            for child in value.values():
                self.fonts(child, visited)
        elif isinstance(value, ArrayObject):
            for child in value:
                self.fonts(child, visited)

    def inspect_resources(self, value) -> bool:
        value = _resolved(value)
        if value is None or isinstance(value, NullObject):
            return False
        if not isinstance(value, DictionaryObject):
            raise ExtractionError("invalid_document")
        if id(value) in self.resources:
            return self.resources[id(value)]
        self.resources[id(value)] = False
        self.fonts(value.get("/Font"), set())
        objects = _resolved(value.get("/XObject"))
        if objects is None:
            return False
        if not isinstance(objects, DictionaryObject):
            raise ExtractionError("invalid_document")
        images = False
        for reference in objects.values():
            obj = _resolved(reference)
            if not isinstance(obj, StreamObject):
                raise ExtractionError("invalid_document")
            if obj.get("/Subtype") == "/Image":
                # 只检查类型，不读图片像素、不启动外部解码器或 OCR。
                images = True
            else:
                self.decode(obj)
                images = self.inspect_resources(obj.get("/Resources")) or images
        self.resources[id(value)] = images
        return images


def extract_pdf(data: bytes, collector: Collector) -> None:
    """失败时抛出固定安全错误；不把损坏或超限文件作为截断成功返回。"""
    token = _diagnostics.set([])
    maximum = collector.limits.max_pdf_content_bytes
    try:
        if not isinstance(data, bytes):
            raise ExtractionError("invalid_document")
        if len(data) > collector.limits.max_file_bytes:
            raise ExtractionError("limit_exceeded")
        with apply_configuration(
            maximum_declared_stream_length=collector.limits.max_file_bytes,
            zlib_maximum_output_length=maximum,
            lzw_maximum_output_length=maximum,
            run_length_maximum_output_length=maximum,
            brotli_maximum_output_length=maximum,
            array_based_stream_maximum_output_length=maximum,
            image_maximum_buffer_size=maximum,
            stream_decoding_work_maximum_length=4 * collector.limits.max_file_bytes,
            page_tree_maximum_entries=collector.limits.max_pages * 4,
            # pypdf 此预算统计页面字典条目，不是页数；真实页数另行检查。
            page_tree_maximum_page_entries=collector.limits.max_pages * 128,
            jbig2dec_binary=None,
        ):
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise ExtractionError("encrypted_document")
            if len(reader.pages) > collector.limits.max_pages:
                raise ExtractionError("limit_exceeded")
            _check_diagnostics()
            budget = _ContentBudget(collector)
            collector.warn(
                "pdf_layout",
                "PDF仅抽取文字层及近似阅读顺序，未还原版式或识别图形。",
            )
            for number, page in enumerate(reader.pages, start=1):
                location = TextLocation(part="pdf", page=number)
                budget.contents(page.get("/Contents"))
                has_images = budget.inspect_resources(page.get("/Resources"))

                def inspect_operation(operator, operands, current_matrix, text_matrix):
                    nonlocal has_images
                    if operator == b"INLINE IMAGE":
                        has_images = True

                text = page.extract_text(
                    extraction_mode="plain", visitor_operand_before=inspect_operation
                )
                _check_diagnostics()
                if has_images:
                    collector.warn(
                        "visual_content", "本页包含图片，图片中的文字未作OCR识别。", location
                    )
                if not text.strip():
                    collector.warn("pdf_page_no_text", "本页未抽取到文字层文本。", location)
                for line_number, line in enumerate(text.splitlines(), start=1):
                    collector.add(
                        line,
                        TextLocation(
                            part="pdf",
                            page=number,
                            paragraph=line_number,
                            line_start=line_number,
                            line_end=line_number,
                        ),
                    )
    except ExtractionError:
        raise
    except (LimitReachedError, MemoryError, RecursionError):
        raise ExtractionError("limit_exceeded") from None
    except Exception:
        raise ExtractionError("invalid_document") from None
    finally:
        _diagnostics.reset(token)
