"""固定OOXML样例与损坏包：只读内存，无网络、Office或模型调用。"""

import socket
import stat
import struct
import warnings
import zipfile
from dataclasses import replace
from io import BytesIO

import pytest

from app.review.extract_types import Collector, ExtractionError, ExtractionLimits
from app.review.ooxml_extract import extract_docx, extract_pptx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
PRIVATE = "private-document-marker-never-return-in-error"


def package(entries, *, compression=zipfile.ZIP_DEFLATED):
    stream = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=compression) as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
    return stream.getvalue()


def relationships(*entries):
    return f'<Relationships xmlns="{REL}">{"".join(entries)}</Relationships>'


def relationship(identity, target, kind="slide", *, external=False):
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{identity}" Type="{R}/{kind}" Target="{target}"{mode}/>'


def base_entries(part):
    return [
        ("[Content_Types].xml", f'<Types xmlns="{CONTENT}"/>'),
        ("_rels/.rels", relationships(relationship("main", part, "officeDocument"))),
    ]


def docx_entries(body="", *, extras=()):
    return [
        *base_entries("word/document.xml"),
        (
            "word/document.xml",
            f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:m="{M}">'
            f"<w:body>{body}</w:body></w:document>",
        ),
        *extras,
    ]


def paragraph(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def slide_xml(shapes=""):
    return (
        f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:m="{M}"><p:cSld><p:spTree>'
        f"{shapes}</p:spTree></p:cSld></p:sld>"
    )


def shape(identity, text, *, before=""):
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{identity}" name="fixed"/></p:nvSpPr>'
        f"<p:txBody>{before}<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
    )


def pptx_entries(slides=(), *, extras=()):
    order = "".join(
        f'<p:sldId id="{256 + index}" r:id="r{index}"/>' for index in range(len(slides))
    )
    return [
        *base_entries("ppt/presentation.xml"),
        (
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            f"{order}</p:sldIdLst></p:presentation>",
        ),
        (
            "ppt/_rels/presentation.xml.rels",
            relationships(
                *(
                    relationship(f"r{index}", f"slides/{name}")
                    for index, (name, _) in enumerate(slides)
                )
            ),
        ),
        *((f"ppt/slides/{name}", slide_xml(shapes)) for name, shapes in slides),
        *extras,
    ]


def extract(data, *, pptx=False, **limits):
    collector = Collector(replace(ExtractionLimits(), **limits))
    (extract_pptx if pptx else extract_docx)(data, collector)
    return collector


def codes(collector):
    return {warning.code for warning in collector.warnings}


def test_docx_interleaves_paragraphs_tables_hyperlink_and_nested_table_in_document_order():
    body = (
        paragraph("首段") + "<w:p/>" + "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>单元格 </w:t></w:r>"
        '<w:hyperlink r:id="web"><w:r><w:t>链接文字</w:t></w:r></w:hyperlink>'
        "<w:r><w:tab/><w:t>尾</w:t><w:br/><w:t>换行</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc>" + paragraph("嵌套") + "</w:tc></w:tr></w:tbl>"
        "</w:tc><w:tc>" + paragraph("右格") + "</w:tc></w:tr></w:tbl>" + paragraph("末段")
    )
    collector = extract(package(docx_entries(body)))
    assert [item.text for item in collector.segments] == [
        "首段",
        "单元格 链接文字\t尾\n换行",
        "嵌套",
        "右格",
        "末段",
    ]
    locations = [item.location for item in collector.segments]
    assert [item.paragraph for item in locations] == [1, 3, 4, 5, 6]
    assert [(item.table, item.row, item.column) for item in locations] == [
        (None, None, None),
        (1, 1, 1),
        (2, 1, 1),
        (1, 1, 2),
        (None, None, None),
    ]
    assert all(item.part == "word/document.xml" and item.page is None for item in locations)
    assert collector.warnings == []


def test_docx_preserves_real_spaces_content_controls_and_warns_for_unread_features():
    body = (
        '<w:sdt><w:sdtContent><w:p><w:r><w:t xml:space="preserve">  原始 空格  </w:t>'
        "<w:drawing><w:txbxContent>"
        + paragraph("图中文字不混入正文")
        + "</w:txbxContent></w:drawing></w:r><m:oMath><m:r><m:t>公式</m:t></m:r></m:oMath>"
        "<w:del><w:r><w:delText>删除不保留</w:delText></w:r></w:del>"
        "<w:ins><w:r><w:t>新文字</w:t></w:r></w:ins>"
        '<w:fldSimple w:instr="PAGE"><w:r><w:t>缓存域</w:t></w:r></w:fldSimple>'
        "</w:p></w:sdtContent></w:sdt>"
    )
    extras = [
        ("word/header1.xml", f'<w:hdr xmlns:w="{W}">{paragraph("页眉不混入正文")}</w:hdr>'),
        ("word/footnotes.xml", f'<w:footnotes xmlns:w="{W}"/>'),
        ("word/media/image1.png", b"fixed image bytes"),
        ("word/charts/chart1.xml", '<chart xmlns="urn:fixed"/>'),
    ]
    collector = extract(package(docx_entries(body, extras=extras)))
    assert [item.text for item in collector.segments] == ["  原始 空格  新文字缓存域"]
    assert {
        "embedded_content_unsupported",
        "equations_unsupported",
        "tracked_changes_unsupported",
        "fields_unsupported",
        "supplementary_text_unsupported",
        "charts_unsupported",
    } <= codes(collector)


def test_docx_table_content_controls_preserve_positions_and_do_not_hide_unsupported_content():
    controlled_row = (
        "<w:sdt><w:sdtPr><w:alias w:val='行控件'/></w:sdtPr><w:sdtContent>"
        "<w:tr><w:trPr><w:trStyle w:val='样式'/></w:trPr><w:tc><w:p/></w:tc>"
        "<w:sdt><w:sdtPr/><w:sdtContent><w:tc>"
        + paragraph("控件单元格")
        + "</w:tc></w:sdtContent></w:sdt></w:tr></w:sdtContent></w:sdt>"
    )
    metadata = "<w:tblPr><w:tblStyle w:val='表格样式'/></w:tblPr><w:tblGrid/>"
    clean = extract(package(docx_entries(f"<w:tbl>{metadata}{controlled_row}</w:tbl>")))
    assert [item.text for item in clean.segments] == ["控件单元格"]
    assert clean.warnings == []

    body = (
        f"<w:tbl>{metadata}{controlled_row}"
        "<w:del><w:tr><w:tc>" + paragraph("已删除行") + "</w:tc></w:tr></w:del>"
        "<w:tr><w:trPr><w:del/></w:trPr><w:tc>"
        + paragraph("属性标记删除行")
        + "</w:tc></w:tr><w:ins><w:tr><w:tc>"
        + paragraph("插入行")
        + "</w:tc><w:del><w:tc>"
        + paragraph("已删除格")
        + "</w:tc></w:del><w:sdt><w:sdtContent><w:tc>"
        + paragraph("右侧控件格")
        + "</w:tc></w:sdtContent></w:sdt></w:tr></w:ins>"
        "<w:moveTo><w:tr><w:moveTo><w:tc>"
        + paragraph("移入行格")
        + "</w:tc></w:moveTo><w:unknown>"
        + paragraph("未知格结构不抽取")
        + "</w:unknown></w:tr></w:moveTo><w:unknown>"
        + paragraph("未知表结构不抽取")
        + "</w:unknown></w:tbl>"
    )
    collector = extract(package(docx_entries(body)))
    assert [item.text for item in collector.segments] == [
        "控件单元格",
        "插入行",
        "右侧控件格",
        "移入行格",
    ]
    assert [
        (item.location.table, item.location.row, item.location.column, item.location.paragraph)
        for item in collector.segments
    ] == [(1, 1, 2, 2), (1, 4, 1, 3), (1, 4, 3, 4), (1, 5, 1, 5)]
    assert codes(collector) == {"tracked_changes_unsupported", "table_structure_unsupported"}


def test_docx_external_relationship_is_not_followed_but_link_display_text_survives(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("Network attempted"))
    extras = [
        (
            "word/_rels/document.xml.rels",
            relationships(
                relationship("web", "https://example.invalid/private", "hyperlink", external=True)
            ),
        )
    ]
    body = '<w:p><w:hyperlink r:id="web"><w:r><w:t>可见链接</w:t></w:r></w:hyperlink></w:p>'
    collector = extract(package(docx_entries(body, extras=extras)))
    assert collector.segments[0].text == "可见链接"
    assert "external_relationship_ignored" in codes(collector)
    assert all("example.invalid" not in warning.message for warning in collector.warnings)


def test_pptx_uses_relationship_order_not_slide_filename_and_extracts_group_text_and_tables():
    table = (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="77"/></p:nvGraphicFramePr>'
        "<a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p/>"
        "<a:p><a:r><a:t>表格甲</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody>"
        "<a:p><a:r><a:t>表格乙</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )
    group = (
        '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9"/></p:nvGrpSpPr>' + shape("12", "组内") + "</p:grpSp>"
    )
    slides = [
        ("slide8.xml", shape("3", "先显示", before="<a:p/>") + group + table),
        ("slide2.xml", shape("2", "后显示")),
    ]
    collector = extract(package(pptx_entries(slides)), pptx=True)
    assert [item.text for item in collector.segments] == [
        "先显示",
        "组内",
        "表格甲",
        "表格乙",
        "后显示",
    ]
    locations = [item.location for item in collector.segments]
    assert [item.slide for item in locations] == [1, 1, 1, 1, 2]
    assert [item.shape_id for item in locations] == ["3", "12", "77", "77", "2"]
    assert [item.paragraph for item in locations] == [2, 1, 2, 1, 1]
    assert (locations[2].table, locations[2].row, locations[2].column) == (1, 1, 1)
    assert (locations[3].table, locations[3].row, locations[3].column) == (1, 1, 2)
    assert locations[0].part == "ppt/slides/slide8.xml"
    assert all(item.page is None for item in locations)
    assert collector.warnings == []


def test_pptx_notes_images_charts_equations_and_layout_text_warn_without_invented_text():
    text = (
        '<p:sp><p:nvSpPr><p:cNvPr id="1"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>正文</a:t></a:r>'
        "<m:oMath><m:r><m:t>公式</m:t></m:r></m:oMath></a:p></p:txBody></p:sp>"
        '<p:pic><p:nvPicPr><p:cNvPr id="2"/></p:nvPicPr></p:pic>'
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3"/></p:nvGraphicFramePr>'
        "<a:graphic/></p:graphicFrame>"
    )
    extras = [
        (
            "ppt/notesSlides/notesSlide1.xml",
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><a:t>备注不混入</a:t></p:notes>',
        ),
        (
            "ppt/slideLayouts/slideLayout1.xml",
            f'<p:sldLayout xmlns:p="{P}" xmlns:a="{A}"><a:t>版式不混入</a:t></p:sldLayout>',
        ),
        ("ppt/charts/chart1.xml", '<chart xmlns="urn:fixed"/>'),
    ]
    collector = extract(package(pptx_entries([("slide1.xml", text)], extras=extras)), pptx=True)
    assert [item.text for item in collector.segments] == ["正文"]
    assert {
        "notes_ignored",
        "equations_unsupported",
        "embedded_content_unsupported",
        "graphics_unsupported",
        "charts_unsupported",
        "layout_text_unsupported",
    } <= codes(collector)


@pytest.mark.parametrize("pptx", [False, True])
def test_empty_ooxml_body_is_legal_without_fabricated_text(pptx):
    entries = pptx_entries([("slide1.xml", "")]) if pptx else docx_entries("<w:p/>")
    collector = extract(package(entries), pptx=pptx)
    assert collector.segments == []
    assert collector.warnings == []


@pytest.mark.parametrize(
    "name",
    [
        "../bad.xml",
        "/absolute.xml",
        "word/../bad.xml",
        "word\\bad.xml",
        "word//bad.xml",
        "word/%2e%2e/bad.xml",
        "C:/bad.xml",
    ],
)
def test_unsafe_zip_entry_names_are_rejected(name):
    with pytest.raises(ExtractionError) as error:
        extract(package(docx_entries(extras=[(name, "<fixed/>")])))
    assert error.value.code == "invalid_document"


@pytest.mark.parametrize("name", ["word/document.xml", "WORD/DOCUMENT.XML", "word/%64ocument.xml"])
def test_duplicate_and_ambiguous_zip_parts_are_rejected(name):
    with pytest.raises(ExtractionError) as error:
        extract(package(docx_entries(extras=[(name, "<private/>")])))
    assert error.value.code == "invalid_document"


def test_symlink_entries_are_not_opened():
    item = zipfile.ZipInfo("word/external.xml")
    item.create_system = 3
    item.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ExtractionError) as error:
        extract(package(docx_entries(extras=[(item, PRIVATE)])))
    assert error.value.code == "invalid_document"


def test_encrypted_zip_flag_and_encrypted_office_container_are_rejected():
    data = bytearray(package(docx_entries()))
    central = data.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", data, central + 8)[0]
    struct.pack_into("<H", data, central + 8, flags | 1)
    for payload in (bytes(data), b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + PRIVATE.encode()):
        with pytest.raises(ExtractionError) as error:
            extract(payload)
        assert error.value.code == "encrypted_document"
        assert PRIVATE not in str(error.value)


@pytest.mark.parametrize("extension", ["xml", "rels", "XML"])
def test_dtd_and_entities_are_rejected_even_in_ignored_parts(extension):
    dangerous = f'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///private/{PRIVATE}">]><x>&leak;</x>'
    with pytest.raises(ExtractionError) as error:
        extract(package(docx_entries(extras=[(f"customXml/ignored.{extension}", dangerous)])))
    assert error.value.code == "invalid_document" and PRIVATE not in str(error.value)


def test_zip_crc_is_verified_for_unread_binary_parts():
    data = bytearray(
        package(
            docx_entries(extras=[("word/media/fixed.bin", PRIVATE)]), compression=zipfile.ZIP_STORED
        )
    )
    offset = data.index(PRIVATE.encode())
    data[offset] ^= 1
    with pytest.raises(ExtractionError) as error:
        extract(bytes(data))
    assert error.value.code == "invalid_document" and PRIVATE not in str(error.value)


@pytest.mark.parametrize("payload", [b"not zip", b"PK\x03\x04broken", b""])
def test_malformed_zip_is_a_fixed_document_error(payload):
    with pytest.raises(ExtractionError) as error:
        extract(payload)
    assert error.value.code == "invalid_document"


@pytest.mark.parametrize(
    "limits",
    [
        {"max_file_bytes": 20},
        {"max_archive_entries": 2},
        {"max_uncompressed_bytes": 40},
        {"max_xml_bytes": 40},
        {"max_xml_depth": 3},
        {"max_text_chars": 2},
        {"max_segments": 1},
    ],
)
def test_zip_xml_depth_and_output_limits_are_enforced(limits):
    with pytest.raises(ExtractionError) as error:
        extract(package(docx_entries(paragraph("文本一") + paragraph("文本二"))), **limits)
    assert error.value.code == "limit_exceeded"


def test_pptx_slide_limit_and_missing_or_external_slide_relations_do_not_read_targets(monkeypatch):
    slides = [("slide1.xml", shape("1", "first")), ("slide2.xml", shape("2", "second"))]
    with pytest.raises(ExtractionError) as error:
        extract(package(pptx_entries(slides)), pptx=True, max_pages=1)
    assert error.value.code == "limit_exceeded"
    entries = pptx_entries(slides)
    entries[3] = (
        "ppt/_rels/presentation.xml.rels",
        relationships(
            relationship("r0", "https://example.invalid/slide.xml", external=True),
            relationship("r1", "slides/slide2.xml"),
        ),
    )
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("Network attempted"))
    collector = extract(package(entries), pptx=True)
    assert [item.text for item in collector.segments] == ["second"]
    assert collector.segments[0].location.slide == 2
    assert "external_relationship_ignored" in codes(collector)
    entries[3] = ("ppt/_rels/presentation.xml.rels", relationships())
    with pytest.raises(ExtractionError) as error:
        extract(package(entries), pptx=True)
    assert error.value.code == "invalid_document"


@pytest.mark.parametrize(
    "target",
    [
        "../../outside.xml",
        "file:///private/slide.xml",
        "slides/slide1.xml?private=yes",
        "slides/slide1.xml#private",
    ],
)
def test_pptx_internal_relationship_target_must_stay_inside_the_package(target):
    entries = pptx_entries([("slide1.xml", shape("1", "text"))])
    entries[3] = ("ppt/_rels/presentation.xml.rels", relationships(relationship("r0", target)))
    with pytest.raises(ExtractionError) as error:
        extract(package(entries), pptx=True)
    assert error.value.code == "invalid_document"


def test_declared_docx_cannot_be_an_unrelated_zip_or_wrong_xml_namespace():
    for entries in (
        [("readme.txt", PRIVATE)],
        docx_entries()[:-1] + [("word/document.xml", "<document><body/></document>")],
    ):
        with pytest.raises(ExtractionError) as error:
            extract(package(entries))
        assert error.value.code == "invalid_document" and PRIVATE not in str(error.value)


@pytest.mark.parametrize("pptx", [False, True])
def test_malformed_relationship_url_is_a_fixed_error_without_private_exception(pptx):
    entries = pptx_entries([("slide1.xml", shape("1", "text"))]) if pptx else docx_entries()
    if pptx:
        entries[3] = (
            "ppt/_rels/presentation.xml.rels",
            relationships(relationship("r0", "http://[")),
        )
    else:
        entries[1] = (
            "_rels/.rels",
            relationships(relationship("main", "http://[", "officeDocument")),
        )
    with pytest.raises(ExtractionError) as error:
        extract(package(entries), pptx=pptx)
    assert error.value.code == "invalid_document"
    assert "IPv6" not in str(error.value) and "http" not in str(error.value)


def test_unknown_xml_encoding_cannot_leak_decoder_lookup_error():
    declaration = f'<?xml version="1.0" encoding="{PRIVATE}"?><fixed/>'
    with pytest.raises(ExtractionError) as error:
        extract(package(docx_entries(extras=[("customXml/unknown.xml", declaration)])))
    assert error.value.code == "invalid_document" and PRIVATE not in str(error.value)


def test_docx_alternate_content_is_omitted_with_warning_instead_of_duplicating_branches():
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    branch = (
        f'<mc:AlternateContent xmlns:mc="{mc}"><mc:Choice Requires="fixed">'
        + paragraph("选择分支不猜测")
        + "</mc:Choice><mc:Fallback>"
        + paragraph("回退分支不重复")
        + "</mc:Fallback></mc:AlternateContent>"
    )
    body = paragraph("before") + branch + '<w:ins w:id="1">' + paragraph("inserted") + "</w:ins>"
    body += '<w:moveTo w:id="2">' + paragraph("moved") + "</w:moveTo>"
    collector = extract(package(docx_entries(body)))
    assert [item.text for item in collector.segments] == ["before", "inserted", "moved"]
    assert {"alternate_content_unsupported", "tracked_changes_unsupported"} <= codes(collector)
    inline = "<w:p><w:r><w:t>inline</w:t></w:r>" + branch + "</w:p>"
    collector = extract(package(docx_entries(inline)))
    assert [item.text for item in collector.segments] == ["inline"]
    assert "alternate_content_unsupported" in codes(collector)


@pytest.mark.parametrize("pptx", [False, True])
def test_strict_math_namespace_is_unsupported_explicitly_in_word_and_slides(pptx):
    strict_math = "http://purl.oclc.org/ooxml/officeDocument/math"
    if pptx:
        entries = pptx_entries([("slide1.xml", shape("1", "safe"))])
        name, xml = entries[-1]
        xml = xml.replace(f'xmlns:m="{M}"', f'xmlns:m="{strict_math}"')
        xml = xml.replace("</a:p>", "<m:oMath><m:r><m:t>formula</m:t></m:r></m:oMath></a:p>")
    else:
        entries = docx_entries(paragraph("safe"))
        name, xml = entries[-1]
        xml = xml.replace(f'xmlns:m="{M}"', f'xmlns:m="{strict_math}"')
        xml = xml.replace("</w:p>", "<m:oMath><m:r><m:t>formula</m:t></m:r></m:oMath></w:p>")
    entries[-1] = (name, xml)
    collector = extract(package(entries), pptx=pptx)
    assert [item.text for item in collector.segments] == ["safe"]
    assert "equations_unsupported" in codes(collector)
