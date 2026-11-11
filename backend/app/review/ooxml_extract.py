"""只在内存中读取有界OOXML包；关系目标永不作为网络或磁盘路径打开。"""

import posixpath
import stat
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse

from app.review.extract_types import Collector, ExtractionError, TextLocation

_W = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_P = {
    "http://schemas.openxmlformats.org/presentationml/2006/main",
    "http://purl.oclc.org/ooxml/presentationml/main",
}
_A = {
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
}
_R = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
}
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
_MATH = {
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://purl.oclc.org/ooxml/officeDocument/math",
}
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _tag(element: Element) -> tuple[str, str]:
    if element.tag.startswith("{"):
        namespace, name = element.tag[1:].split("}", 1)
        return namespace, name
    return "", element.tag


def _is(element: Element, namespaces: set[str], name: str) -> bool:
    namespace, local = _tag(element)
    return namespace in namespaces and local == name


def _child(element: Element, namespaces: set[str], name: str) -> Element | None:
    return next((item for item in element if _is(item, namespaces, name)), None)


def _attribute(element: Element, namespaces: set[str], name: str) -> str | None:
    return next(
        (
            element.get(f"{{{namespace}}}{name}")
            for namespace in namespaces
            if element.get(f"{{{namespace}}}{name}") is not None
        ),
        None,
    )


def _safe_name(name: str, *, directory: bool = False) -> None:
    candidate = name[:-1] if directory and name.endswith("/") else name
    decoded = unquote(candidate)
    if (
        not candidate
        or candidate.startswith("/")
        or any(character in candidate or character in decoded for character in ("\\", "\0", ":"))
        or decoded.startswith("/")
        or any(piece in {"", ".", ".."} for piece in decoded.split("/"))
    ):
        raise ExtractionError("invalid_document")


def _resolve_part(source: str, target: str) -> str:
    try:
        parsed = urlsplit(target)
    except ValueError:
        raise ExtractionError("invalid_document") from None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in target:
        raise ExtractionError("invalid_document")
    path = unquote(parsed.path)
    if not path or "\0" in path:
        raise ExtractionError("invalid_document")
    result = posixpath.normpath(
        path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(source), path)
    )
    _safe_name(result)
    return result


@dataclass(frozen=True)
class _Relationship:
    kind: str
    target: str
    external: bool


class _Package:
    def __init__(self, data: bytes, collector: Collector):
        self.collector = collector
        self.xml: dict[str, Element] = {}
        self.names: set[str] = set()
        if len(data) > collector.limits.max_file_bytes:
            raise ExtractionError("limit_exceeded")
        if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ExtractionError("encrypted_document")
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > collector.limits.max_archive_entries:
                    raise ExtractionError("limit_exceeded")
                total = xml_bytes = 0
                canonical = set()
                for entry in entries:
                    if entry.orig_filename != entry.filename:
                        raise ExtractionError("invalid_document")
                    _safe_name(entry.filename, directory=entry.is_dir())
                    name = unquote(entry.filename).casefold().rstrip("/")
                    if name in canonical or stat.S_ISLNK(entry.external_attr >> 16):
                        raise ExtractionError("invalid_document")
                    canonical.add(name)
                    if entry.flag_bits & 1:
                        raise ExtractionError("encrypted_document")
                    if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise ExtractionError("invalid_document")
                    total += entry.file_size
                    if total > collector.limits.max_uncompressed_bytes:
                        raise ExtractionError("limit_exceeded")
                    if entry.filename.casefold().endswith((".xml", ".rels")):
                        xml_bytes += entry.file_size
                        if xml_bytes > collector.limits.max_xml_bytes:
                            raise ExtractionError("limit_exceeded")
                    self.names.add(entry.filename)
                consumed = 0
                for entry in entries:
                    is_xml = entry.filename.casefold().endswith((".xml", ".rels"))
                    payload = bytearray() if is_xml else None
                    with archive.open(entry) as stream:
                        while chunk := stream.read(65536):
                            consumed += len(chunk)
                            if consumed > collector.limits.max_uncompressed_bytes:
                                raise ExtractionError("limit_exceeded")
                            if payload is not None:
                                payload.extend(chunk)
                                if len(payload) > collector.limits.max_xml_bytes:
                                    raise ExtractionError("limit_exceeded")
                    if payload is not None:
                        self.xml[entry.filename] = self._parse_xml(bytes(payload))
        except ExtractionError:
            raise
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            OSError,
            ValueError,
            RuntimeError,
            NotImplementedError,
            EOFError,
            zlib.error,
        ):
            raise ExtractionError("invalid_document") from None

    def _parse_xml(self, data: bytes) -> Element:
        try:
            parser = iterparse(
                BytesIO(data),
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
            depth = 0
            for event, _ in parser:
                depth += 1 if event == "start" else -1
                if depth > self.collector.limits.max_xml_depth:
                    raise ExtractionError("limit_exceeded")
            return parser.root
        except ExtractionError:
            raise
        except (
            ParseError,
            DefusedXmlException,
            ValueError,
            LookupError,
            UnicodeError,
            RecursionError,
        ):
            raise ExtractionError("invalid_document") from None

    def get(self, part: str) -> Element:
        if part not in self.xml:
            raise ExtractionError("invalid_document")
        return self.xml[part]

    def relationships(self, source: str) -> dict[str, _Relationship]:
        folder, filename = posixpath.split(source)
        part = posixpath.join(folder, "_rels", f"{filename}.rels") if source else "_rels/.rels"
        if part not in self.xml:
            return {}
        root = self.xml[part]
        if root.tag != f"{{{_REL}}}Relationships":
            raise ExtractionError("invalid_document")
        relations = {}
        for item in root:
            if item.tag != f"{{{_REL}}}Relationship":
                raise ExtractionError("invalid_document")
            identity, kind, target = item.get("Id"), item.get("Type"), item.get("Target")
            if not identity or identity in relations or not kind or not target:
                raise ExtractionError("invalid_document")
            mode = item.get("TargetMode", "Internal")
            if mode not in {"Internal", "External"}:
                raise ExtractionError("invalid_document")
            external = mode == "External"
            if external:
                self.collector.warn(
                    "external_relationship_ignored",
                    "外部关系目标未读取或下载。",
                    TextLocation(part=part),
                )
            relations[identity] = _Relationship(kind.rsplit("/", 1)[-1], target, external)
        return relations

    def main_part(self, namespaces: set[str], tag: str) -> tuple[str, Element]:
        content = self.get("[Content_Types].xml")
        if content.tag != f"{{{_CONTENT}}}Types":
            raise ExtractionError("invalid_document")
        main = [item for item in self.relationships("").values() if item.kind == "officeDocument"]
        if len(main) != 1 or main[0].external:
            raise ExtractionError("invalid_document")
        part = _resolve_part("", main[0].target)
        root = self.get(part)
        if not _is(root, namespaces, tag):
            raise ExtractionError("invalid_document")
        # 审查全部关系文件的外部目标，但不打开其目标。
        for name in self.xml:
            if name.endswith(".rels") and name != "_rels/.rels":
                directory, filename = posixpath.split(name)
                if posixpath.basename(directory) != "_rels":
                    raise ExtractionError("invalid_document")
                self.relationships(posixpath.join(posixpath.dirname(directory), filename[:-5]))
        return part, root

    def warn_parts(self, *, word: bool) -> None:
        for name in sorted(self.names):
            location = TextLocation(part=name)
            if ("/media/" in name or "/embeddings/" in name) and not name.endswith("/"):
                self.collector.warn(
                    "embedded_content_unsupported", "图片及嵌入对象内容未抽取。", location
                )
            if "/charts/" in name and name.endswith(".xml"):
                self.collector.warn("charts_unsupported", "图表中的内容未抽取。", location)
            if word and name.startswith("word/") and name.endswith(".xml"):
                base = posixpath.basename(name)
                if base.startswith(("header", "footer", "footnotes", "endnotes", "comments")):
                    self.collector.warn(
                        "supplementary_text_unsupported",
                        "页眉、页脚、脚注、尾注或批注未抽取。",
                        location,
                    )
            if not word and "/notesSlides/" in name and name.endswith(".xml"):
                self.collector.warn("notes_ignored", "演讲者备注未抽取。", location)
            if not word and any(piece in name for piece in ("/slideMasters/", "/slideLayouts/")):
                root = self.xml.get(name)
                if root is not None and any(
                    _is(item, _A, "t") and item.text for item in root.iter()
                ):
                    self.collector.warn(
                        "layout_text_unsupported", "母版及版式中的继承文字未抽取。", location
                    )


def _word_text(node: Element, collector: Collector, location: TextLocation) -> str:
    namespace, name = _tag(node)
    if namespace == _MC and name == "AlternateContent":
        collector.warn(
            "alternate_content_unsupported",
            "兼容分支内容未抽取，避免重复或猜测可见分支。",
            location,
        )
        return ""
    if namespace in _MATH:
        collector.warn("equations_unsupported", "公式内容未抽取。", location)
        return ""
    if namespace in _W:
        if name in {"drawing", "pict", "object", "txbxContent", "altChunk"}:
            collector.warn(
                "embedded_content_unsupported", "绘图、文本框或嵌入对象内容未抽取。", location
            )
            return ""
        if name in {"del", "delText", "moveFrom"}:
            collector.warn(
                "tracked_changes_unsupported", "文档含修订，仅抽取现有非删除文字。", location
            )
            return ""
        if name in {"ins", "moveTo"}:
            collector.warn(
                "tracked_changes_unsupported", "文档含修订，仅抽取现有非删除文字。", location
            )
        if name in {"instrText", "fldChar", "fldSimple"}:
            collector.warn("fields_unsupported", "动态域未计算，仅保留可读取的缓存文字。", location)
            if name != "fldSimple":
                return ""
        if name == "sym":
            collector.warn("symbols_unsupported", "字体映射符号未抽取。", location)
            return ""
        if name == "t":
            return node.text or ""
        if name == "tab":
            return "\t"
        if name in {"br", "cr"}:
            return "\n"
        if name == "noBreakHyphen":
            return "\u2011"
        if name == "softHyphen":
            return "\u00ad"
        if name == "numPr":
            collector.warn("numbering_unsupported", "自动列表编号未生成，保留段落文字。", location)
    return "".join(_word_text(child, collector, location) for child in node)


def extract_docx(data: bytes, collector: Collector) -> None:
    package = _Package(data, collector)
    part, root = package.main_part(_W, "document")
    body = _child(root, _W, "body")
    if body is None:
        raise ExtractionError("invalid_document")
    package.warn_parts(word=True)
    paragraph = table = 0

    def table_children(
        node: Element,
        wanted: str,
        location: TextLocation,
        *,
        deleted: bool = False,
    ):
        for child in node:
            namespace, name = _tag(child)
            if namespace in _W and name == wanted:
                properties = _child(child, _W, "trPr" if wanted == "tr" else "tcPr")
                changes = (
                    {
                        _tag(item)[1]
                        for item in properties
                        if _tag(item)[0] in _W
                        and _tag(item)[1]
                        in {"ins", "moveTo", "del", "moveFrom", "cellIns", "cellDel"}
                    }
                    if properties is not None
                    else set()
                )
                if changes:
                    collector.warn(
                        "tracked_changes_unsupported",
                        "文档含修订，仅抽取现有非删除文字。",
                        location,
                    )
                # 删除内容仍占原XML行/单元格位置，避免后续定位被前移。
                yield child, deleted or bool(changes & {"del", "moveFrom", "cellDel"})
            elif namespace in _W and name in {
                "sdt",
                "sdtContent",
                "customXml",
                "ins",
                "moveTo",
                "del",
                "moveFrom",
            }:
                if name in {"ins", "moveTo", "del", "moveFrom"}:
                    collector.warn(
                        "tracked_changes_unsupported",
                        "文档含修订，仅抽取现有非删除文字。",
                        location,
                    )
                yield from table_children(
                    child,
                    wanted,
                    location,
                    deleted=deleted or name in {"del", "moveFrom"},
                )
            elif namespace in _W and name in {
                "tblPr",
                "tblGrid",
                "trPr",
                "tblPrEx",
                "sdtPr",
                "sdtEndPr",
                "customXmlPr",
                "bookmarkStart",
                "bookmarkEnd",
                "commentRangeStart",
                "commentRangeEnd",
                "permStart",
                "permEnd",
                "proofErr",
            }:
                continue
            elif namespace == _MC and name == "AlternateContent":
                _word_text(child, collector, location)
            else:
                collector.warn(
                    "table_structure_unsupported", "此表格结构中的内容未抽取。", location
                )

    def visit(node: Element, cell: tuple[int, int, int] | None = None) -> None:
        nonlocal paragraph, table
        namespace, name = _tag(node)
        location = TextLocation(part=part)
        if namespace == _MC and name == "AlternateContent":
            collector.warn(
                "alternate_content_unsupported",
                "兼容分支内容未抽取，避免重复或猜测可见分支。",
                location,
            )
            return
        if namespace in _W and name in {"ins", "moveTo"}:
            collector.warn(
                "tracked_changes_unsupported", "文档含修订，仅抽取现有非删除文字。", location
            )
        if namespace in _W and name == "p":
            paragraph += 1
            values = {"table": cell[0], "row": cell[1], "column": cell[2]} if cell else {}
            location = TextLocation(part=part, paragraph=paragraph, **values)
            collector.add(_word_text(node, collector, location), location)
            return
        if namespace in _W and name == "tbl":
            table += 1
            identity = table
            location = TextLocation(part=part, table=identity)
            for row_index, (row, deleted_row) in enumerate(table_children(node, "tr", location), 1):
                if deleted_row:
                    continue
                row_location = TextLocation(part=part, table=identity, row=row_index)
                for column, (entry, deleted_cell) in enumerate(
                    table_children(row, "tc", row_location), 1
                ):
                    if deleted_cell:
                        continue
                    for child in entry:
                        visit(child, (identity, row_index, column))
            return
        if namespace in _W and name in {"del", "moveFrom", "drawing", "pict", "object", "altChunk"}:
            _word_text(node, collector, location)
            return
        if namespace in _MATH:
            _word_text(node, collector, location)
            return
        if namespace in _W and name in {"gridSpan", "vMerge"}:
            collector.warn(
                "merged_table_cells", "合并单元格按原XML单元格位置定位，未推算视觉网格。", location
            )
        for child in node:
            visit(child, cell)

    for child in body:
        visit(child)


def _drawing_text(node: Element, collector: Collector, location: TextLocation) -> str:
    namespace, name = _tag(node)
    if namespace in _MATH:
        collector.warn("equations_unsupported", "公式内容未抽取。", location)
        return ""
    if namespace in _A:
        if name == "t":
            return node.text or ""
        if name == "br":
            return "\n"
        if name == "fld":
            collector.warn("fields_unsupported", "动态域未计算，仅保留可读取的缓存文字。", location)
    return "".join(_drawing_text(child, collector, location) for child in node)


def _shape_id(node: Element) -> str | None:
    for child in node:
        if _tag(child)[1].startswith("nv"):
            identity = _child(child, _P, "cNvPr")
            return identity.get("id") if identity is not None else None
    return None


def extract_pptx(data: bytes, collector: Collector) -> None:
    package = _Package(data, collector)
    presentation_part, root = package.main_part(_P, "presentation")
    package.warn_parts(word=False)
    order = _child(root, _P, "sldIdLst")
    if order is None:
        return
    references = package.relationships(presentation_part)
    slide_ids, relation_ids = set(), set()
    for slide_number, reference in enumerate(order, 1):
        if slide_number > collector.limits.max_pages:
            raise ExtractionError("limit_exceeded")
        identity, relation_id = reference.get("id"), _attribute(reference, _R, "id")
        if (
            not _is(reference, _P, "sldId")
            or not identity
            or identity in slide_ids
            or not relation_id
            or relation_id in relation_ids
            or relation_id not in references
        ):
            raise ExtractionError("invalid_document")
        slide_ids.add(identity)
        relation_ids.add(relation_id)
        relation = references[relation_id]
        if relation.kind != "slide":
            raise ExtractionError("invalid_document")
        if relation.external:
            continue
        part = _resolve_part(presentation_part, relation.target)
        slide = package.get(part)
        if not _is(slide, _P, "sld"):
            raise ExtractionError("invalid_document")
        common = _child(slide, _P, "cSld")
        tree = _child(common, _P, "spTree") if common is not None else None
        if tree is None:
            raise ExtractionError("invalid_document")
        table_number = 0

        def text_body(body: Element, shape: str | None, **values) -> None:
            for paragraph, node in enumerate((item for item in body if _is(item, _A, "p")), 1):
                location = TextLocation(
                    part=part, slide=slide_number, shape_id=shape, paragraph=paragraph, **values
                )
                collector.add(_drawing_text(node, collector, location), location)

        def shape(node: Element) -> None:
            nonlocal table_number
            namespace, name = _tag(node)
            identity = _shape_id(node)
            location = TextLocation(part=part, slide=slide_number, shape_id=identity)
            if namespace in _P and name == "grpSp":
                for child in node:
                    shape(child)
                return
            if namespace in _P and name in {"sp", "cxnSp"}:
                body = _child(node, _P, "txBody")
                if body is not None:
                    text_body(body, identity)
                return
            if namespace in _P and name == "graphicFrame":
                tables = [item for item in node.iter() if _is(item, _A, "tbl")]
                if not tables:
                    collector.warn(
                        "graphics_unsupported", "图表、SmartArt或其他图形内容未抽取。", location
                    )
                for table in tables:
                    table_number += 1
                    for row, entry in enumerate((item for item in table if _is(item, _A, "tr")), 1):
                        for column, cell in enumerate(
                            (item for item in entry if _is(item, _A, "tc")), 1
                        ):
                            body = _child(cell, _A, "txBody")
                            if body is not None:
                                text_body(
                                    body, identity, table=table_number, row=row, column=column
                                )
                return
            if namespace in _P and name in {"pic", "oleObj", "contentPart"}:
                collector.warn(
                    "embedded_content_unsupported", "图片及嵌入对象内容未抽取。", location
                )
                return
            if namespace not in _P or name not in {"nvGrpSpPr", "grpSpPr", "extLst"}:
                collector.warn("shape_unsupported", "此图形中的内容未抽取。", location)

        for node in tree:
            shape(node)
