"""从v1格式检查用例迁移，补齐标准OPC并验证新标题/脚本规范及安全边界。"""

import socket
import stat
import struct
import tempfile
import unittest
import warnings
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from app.review.extract_types import ExtractionLimits
from app.review.format_office import inspect_office


def inspect_file(path, profile, *, limits=None):
    return inspect_office(path.read_bytes(), path.name, profile, limits=limits)


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PRIVATE = "PRIVATE_SENTINEL_NEVER_RETURN"


def content_types(files):
    def kind(name):
        if name == "word/document.xml":
            return "wordprocessingml.document.main"
        if name == "word/styles.xml":
            return "wordprocessingml.styles"
        if name == "word/settings.xml":
            return "wordprocessingml.settings"
        if name == "ppt/presentation.xml":
            return "presentationml.presentation.main"
        if name.startswith("ppt/slides/"):
            return "presentationml.slide"
        if name.startswith("ppt/slideLayouts/"):
            return "presentationml.slideLayout"
        if name.startswith("ppt/slideMasters/"):
            return "presentationml.slideMaster"
        if "/theme/" in name:
            return "theme"
        return None

    overrides = "".join(
        f'<Override PartName="/{name}" '
        f'ContentType="application/vnd.openxmlformats-officedocument.{kind(name)}+xml"/>'
        for name in files
        if name.endswith(".xml") and kind(name)
    )
    return (
        f'<Types xmlns="{CONTENT_NS}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>{overrides}</Types>'
    )


def styles(extra="", defaults=""):
    return (
        f'<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr>'
        f"{defaults}</w:rPr></w:rPrDefault></w:docDefaults>"
        '<w:style w:type="paragraph" w:styleId="Normal" w:default="1">'
        '<w:name w:val="Normal"/></w:style>' + extra + "</w:styles>"
    )


def document(content):
    return f'<w:document xmlns:w="{W_NS}"><w:body>{content}</w:body></w:document>'


def paragraph(text="正文测试", props="", pprops=""):
    return f"<w:p><w:pPr>{pprops}</w:pPr><w:r><w:rPr>{props}</w:rPr><w:t>{text}</w:t></w:r></w:p>"


def theme(font="微软雅黑"):
    return (
        f'<a:theme xmlns:a="{A_NS}"><a:themeElements><a:fontScheme name="Test">'
        f'<a:majorFont><a:latin typeface="Arial"/><a:ea typeface="黑体"/></a:majorFont>'
        f'<a:minorFont><a:latin typeface="Arial"/><a:ea typeface="{font}"/></a:minorFont>'
        "</a:fontScheme></a:themeElements></a:theme>"
    )


def relations(items):
    content = "".join(
        f'<Relationship Id="{i}" Type="{R_NS}/{kind}" Target="{target}"{external}/>'
        for i, kind, target, external in items
    )
    return f'<Relationships xmlns="{PKG_NS}">{content}</Relationships>'


def drawing_shape(
    text="正文测试", props="", kind="body", level_defaults="", run_attributes="", identity=2
):
    placeholder = f'<p:ph type="{kind}" idx="1"/>' if kind else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{identity}" name="Test"/><p:cNvSpPr/>'
        f"<p:nvPr>{placeholder}</p:nvPr></p:nvSpPr><p:txBody><a:bodyPr/>"
        f"<a:lstStyle>{level_defaults}</a:lstStyle><a:p><a:r>"
        f"<a:rPr {run_attributes}>{props}</a:rPr>"
        f"<a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
    )


class FormatCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, suffix, files):
        files = dict(files)
        main = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
        files.setdefault("_rels/.rels", relations([("main", "officeDocument", main, "")]))
        files.setdefault("[Content_Types].xml", content_types(files))
        path = self.root / (str(len(list(self.root.iterdir()))) + suffix)
        with ZipFile(path, "w", ZIP_DEFLATED) as zipped:
            for name, value in files.items():
                zipped.writestr(name, value)
        return path

    def docx(self, content, style=None, extra=None):
        files = {"word/document.xml": document(content)}
        if style is not None:
            files["word/styles.xml"] = style
        files.update(extra or {})
        related = []
        for name, kind in [("word/styles.xml", "styles"), ("word/settings.xml", "settings")]:
            if name in files:
                related.append((kind, kind, name.removeprefix("word/"), ""))
        existing = files.get("word/_rels/document.xml.rels")
        if existing is not None:
            generated = relations(related)
            files["word/_rels/document.xml.rels"] = existing.replace(
                "</Relationships>", generated.split(">", 1)[1]
            )
        elif related:
            files["word/_rels/document.xml.rels"] = relations(related)
        return self.package(".docx", files)

    def pptx(self, shapes, extra=None):
        files = {
            "ppt/presentation.xml": (
                f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">'
                '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
                '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": relations(
                [("rId1", "slide", "slides/slide1.xml", "")]
            ),
            "ppt/slides/slide1.xml": (
                f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree>'
                f"{shapes}</p:spTree></p:cSld></p:sld>"
            ),
        }
        files.update(extra or {})
        return self.package(".pptx", files)

    def rules(self, report):
        return [item["rule"] for item in report["findings"]]

    def test_docx_direct_font_and_size_pass_without_modifying_file(self):
        path = self.docx(
            paragraph(props='<w:rFonts w:eastAsia="微软雅黑"/><w:sz w:val="24"/>'), styles()
        )
        original = path.read_bytes()
        before_hash = sha256(original).hexdigest()
        report = inspect_file(path, {"font_name": "Microsoft YaHei", "font_size": 12})
        self.assertEqual(report["status"], "passed")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), before_hash)
        self.assertTrue(report["coverage"])
        self.assertIn("不代表", report["summary"])

    def test_docx_explicit_mismatch_has_location_and_rule(self):
        path = self.docx(paragraph(props='<w:rFonts w:eastAsia="宋体"/>'), styles())
        report = inspect_file(path, {"font_name": "微软雅黑"})
        self.assertEqual(report["status"], "issues")
        self.assertEqual(report["findings"][0]["severity"], "error")
        self.assertIn("第 1 段", report["findings"][0]["location"])
        self.assertIn("font_name", self.rules(report))

    def test_docx_paragraph_style_based_on_and_character_override(self):
        definitions = (
            '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/>'
            '<w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:eastAsia="宋体"/>'
            '<w:sz w:val="24"/></w:rPr></w:style>'
            '<w:style w:type="character" w:styleId="Correct"><w:rPr>'
            '<w:rFonts w:eastAsia="微软雅黑"/></w:rPr></w:style>'
        )
        content = paragraph(
            props='<w:rStyle w:val="Correct"/>', pprops='<w:pStyle w:val="BodyText"/>'
        )
        path = self.docx(content, styles(definitions))
        self.assertEqual(
            inspect_file(path, {"font_name": "微软雅黑", "font_size": 12})["status"], "passed"
        )

    def test_docx_theme_inheritance_from_defaults(self):
        defaults = '<w:rFonts w:eastAsiaTheme="minorEastAsia"/><w:sz w:val="24"/>'
        path = self.docx(
            paragraph(),
            styles(defaults=defaults),
            {
                "word/_rels/document.xml.rels": relations([("t", "theme", "theme/theme1.xml", "")]),
                "word/theme/theme1.xml": theme(),
            },
        )
        self.assertEqual(
            inspect_file(path, {"font_name": "微软雅黑", "font_size": 12})["status"], "passed"
        )

    def test_docx_heading_not_forced_to_body_size(self):
        heading = (
            '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
            '<w:rPr><w:sz w:val="48"/></w:rPr></w:style>'
        )
        content = paragraph(props='<w:sz w:val="24"/>') + paragraph(
            "标题", props='<w:sz w:val="48"/>', pprops='<w:pStyle w:val="Heading1"/>'
        )
        report = inspect_file(self.docx(content, styles(heading)), {"font_size": 12})
        self.assertEqual(report["status"], "passed")

    def test_caption_inheriting_normal_not_mistaken_for_body(self):
        caption = (
            '<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/>'
            '<w:basedOn w:val="Normal"/><w:rPr><w:sz w:val="18"/></w:rPr></w:style>'
        )
        content = paragraph(pprops='<w:pStyle w:val="Caption"/>')
        report = inspect_file(self.docx(content, styles(caption)), {"font_size": 12})
        self.assertIn("body_role_unresolved", self.rules(report))
        self.assertNotIn("font_size", self.rules(report))

    def test_unknown_font_is_warning_not_mismatch(self):
        report = inspect_file(self.docx(paragraph(), styles()), {"font_name": "微软雅黑"})
        self.assertIn("font_unresolved", self.rules(report))
        self.assertNotIn("font_name", self.rules(report))
        self.assertTrue(all(f["severity"] == "warning" for f in report["findings"]))

    def test_no_spec_does_not_invent_font_or_size_requirements(self):
        path = self.docx(paragraph(props='<w:rFonts w:eastAsia="任意字体"/><w:sz w:val="73"/>'))
        self.assertEqual(inspect_file(path, {})["status"], "passed")

    def test_unsupported_file_is_not_passed(self):
        path = self.root / "note.pdf"
        path.write_bytes(b"not important")
        self.assertEqual(inspect_file(path, {})["status"], "unsupported")

    def test_broken_and_missing_office_xml_are_errors(self):
        path = self.root / "broken.docx"
        path.write_bytes(b"not zip")
        self.assertEqual(inspect_file(path, {})["status"], "error")
        missing = self.package(".docx", {"word/styles.xml": styles()})
        self.assertEqual(inspect_file(missing, {})["status"], "error")

    def test_entity_declarations_are_rejected(self):
        unsafe = (
            '<!DOCTYPE x [<!ENTITY a "expanded">]>'
            f'<w:document xmlns:w="{W_NS}"><w:body/></w:document>'
        )
        path = self.package(".docx", {"word/document.xml": unsafe})
        self.assertEqual(inspect_file(path, {})["status"], "error")
        utf16 = self.package(".docx", {"word/document.xml": unsafe.encode("utf-16")})
        self.assertEqual(inspect_file(utf16, {})["status"], "error")

    def test_missing_style_parent_does_not_invent_effective_font(self):
        body = (
            '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/>'
            '<w:basedOn w:val="MissingStyle"/></w:style>'
        )
        path = self.docx(
            paragraph(pprops='<w:pStyle w:val="BodyText"/>'),
            styles(body, '<w:rFonts w:eastAsia="宋体"/>'),
        )
        report = inspect_file(path, {"font_name": "微软雅黑"})
        self.assertIn("font_unresolved", self.rules(report))
        self.assertNotIn("font_name", self.rules(report))

    def test_free_text_and_citation_are_explicitly_not_covered(self):
        path = self.docx(paragraph())
        report = inspect_file(
            path, {"body": "规范", "citation_style": "APA 7", "ppt_template": "校模版"}
        )
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["findings"])
        self.assertTrue(any("引用体例" in item for item in report["coverage"]))
        self.assertTrue(any("自由文本规范" in item for item in report["coverage"]))

    def test_pptx_direct_body_font_and_canvas(self):
        path = self.pptx(drawing_shape(props='<a:ea typeface="微软雅黑"/>'))
        report = inspect_file(
            path, {"font_name": "微软雅黑", "slide_width": 12192000, "slide_height": 6858000}
        )
        self.assertEqual(report["status"], "passed")
        mismatch = inspect_file(path, {"slide_width": 18288000})
        self.assertIn("slide_width", self.rules(mismatch))

    def test_pptx_size_from_shape_list_style_and_title_exclusion(self):
        body = drawing_shape(level_defaults='<a:lvl1pPr><a:defRPr sz="2000"/></a:lvl1pPr>')
        title = drawing_shape(
            "大标题",
            kind="title",
            level_defaults='<a:lvl1pPr><a:defRPr sz="4800"/></a:lvl1pPr>',
            identity=3,
        )
        self.assertEqual(
            inspect_file(self.pptx(body + title), {"font_size": 20})["status"], "passed"
        )

    def test_pptx_master_and_theme_inheritance(self):
        extra = {
            "ppt/slides/_rels/slide1.xml.rels": relations(
                [("l", "slideLayout", "../slideLayouts/slideLayout1.xml", "")]
            ),
            "ppt/slideLayouts/slideLayout1.xml": f'<p:sldLayout xmlns:p="{P_NS}"/>',
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": relations(
                [("m", "slideMaster", "../slideMasters/slideMaster1.xml", "")]
            ),
            "ppt/slideMasters/slideMaster1.xml": (
                f'<p:sldMaster xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:txStyles><p:bodyStyle>'
                '<a:lvl1pPr><a:defRPr sz="2000"><a:ea typeface="+mn-ea"/></a:defRPr></a:lvl1pPr>'
                "</p:bodyStyle></p:txStyles></p:sldMaster>"
            ),
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": relations(
                [("t", "theme", "../theme/theme1.xml", "")]
            ),
            "ppt/theme/theme1.xml": theme(),
        }
        report = inspect_file(
            self.pptx(drawing_shape(), extra), {"font_name": "微软雅黑", "font_size": 20}
        )
        self.assertEqual(report["status"], "passed")

    def test_nonsemantic_ppt_text_boxes_not_forced_to_body_size(self):
        report = inspect_file(self.pptx(drawing_shape(kind=None)), {"font_size": 20})
        self.assertIn("body_role_unresolved", self.rules(report))
        self.assertNotIn("font_size", self.rules(report))

    def test_placeholder_exact_prompt_is_reported_but_normal_sentence_is_not(self):
        report = inspect_file(self.pptx(drawing_shape("请输入标题")), {})
        self.assertIn("placeholder_text", self.rules(report))
        normal = inspect_file(self.docx(paragraph("本节说明如何输入正文内容。")), {})
        self.assertNotIn("placeholder_text", self.rules(normal))

    def test_external_slide_target_not_followed(self):
        extra = {
            "ppt/_rels/presentation.xml.rels": relations(
                [("rId1", "slide", "file:///etc/passwd", ' TargetMode="External"')]
            )
        }
        report = inspect_file(self.pptx("", extra), {})
        self.assertIn("slide_unresolved", self.rules(report))


@pytest.fixture
def factory(tmp_path):
    helper = FormatCheckTests()
    helper.root = tmp_path
    return helper


def raw_package(entries, *, compression=ZIP_DEFLATED):
    output = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(output, "w", compression=compression) as archive:
            for name, value in entries:
                archive.writestr(name, value)
    return output.getvalue()


def rewrite_package(data, *, replace_parts=None, remove=(), add=(), compression=ZIP_DEFLATED):
    replace_parts = replace_parts or {}
    with ZipFile(BytesIO(data)) as archive:
        entries = [
            (info.filename, replace_parts.get(info.filename, archive.read(info)))
            for info in archive.infolist()
            if info.filename not in remove
        ]
    return raw_package([*entries, *add], compression=compression)


def strict_package(data):
    replacements = {
        W_NS: "http://purl.oclc.org/ooxml/wordprocessingml/main",
        A_NS: "http://purl.oclc.org/ooxml/drawingml/main",
        P_NS: "http://purl.oclc.org/ooxml/presentationml/main",
        R_NS: "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
    with ZipFile(BytesIO(data)) as archive:
        entries = []
        for name in archive.namelist():
            value = archive.read(name).decode()
            for old, new in replacements.items():
                value = value.replace(old, new)
            entries.append((name, value))
    return raw_package(entries)


def rules(report):
    return [item["rule"] for item in report["findings"]]


def assert_safe_error(report):
    assert report["status"] == "error"
    assert "invalid_office_file" in rules(report)
    assert PRIVATE not in repr(report)
    assert any(item["severity"] == "error" for item in report["findings"])


def body_profile():
    return {"font_name": {"eastAsia": "微软雅黑", "ascii": "Arial"}, "font_size": 12}


def word_run(text, font, size):
    return (
        f'<w:r><w:rPr><w:rFonts w:eastAsia="{font}" w:ascii="{font}"/>'
        f'<w:sz w:val="{size * 2}"/></w:rPr><w:t>{text}</w:t></w:r>'
    )


@pytest.mark.parametrize("pptx", [False, True])
def test_strict_ooxml_fonts_and_sizes_are_checked(factory, pptx):
    if pptx:
        path = factory.pptx(
            drawing_shape(props='<a:ea typeface="微软雅黑"/>', run_attributes='sz="1200"')
        )
    else:
        path = factory.docx(
            paragraph(props='<w:rFonts w:eastAsia="微软雅黑"/><w:sz w:val="24"/>'), styles()
        )
    report = inspect_office(
        strict_package(path.read_bytes()), path.name, {"font_name": "微软雅黑", "font_size": 12}
    )
    assert report["status"] == "passed"
    mismatch = inspect_office(strict_package(path.read_bytes()), path.name, {"font_size": 14})
    assert mismatch["status"] == "issues"
    assert "font_size" in rules(mismatch)


def test_docx_dual_script_font_and_size_profiles_match_separate_runs(factory):
    text = "<w:p>" + word_run("中文", "微软雅黑", 12) + word_run("English", "Arial", 10) + "</w:p>"
    path = factory.docx(text, styles())
    profile = {
        "font_name": {"eastAsia": "Microsoft YaHei", "ascii": "Arial"},
        "font_size": {"eastAsia": 12, "ascii": 10},
    }
    assert inspect_file(path, profile)["status"] == "passed"
    mismatch = inspect_file(path, {**profile, "font_name": {"eastAsia": "宋体", "ascii": "Arial"}})
    errors = [item for item in mismatch["findings"] if item["severity"] == "error"]
    assert len(errors) == 1 and errors[0]["rule"] == "font_name"
    assert "文字片段 1" in errors[0]["location"]


def test_ppt_dual_script_fonts_and_sizes_match_separate_runs(factory):
    shapes = drawing_shape("中文", props='<a:ea typeface="微软雅黑"/>', run_attributes='sz="2000"')
    shapes += drawing_shape(
        "English", props='<a:latin typeface="Arial"/>', run_attributes='sz="1800"', identity=3
    )
    path = factory.pptx(shapes)
    profile = {
        "font_name": {"eastAsia": "微软雅黑", "ascii": "Arial"},
        "font_size": {"eastAsia": 20, "ascii": 18},
    }
    assert inspect_file(path, profile)["status"] == "passed"
    mismatch = inspect_file(path, {**profile, "font_size": {"eastAsia": 20, "ascii": 19}})
    assert "font_size" in rules(mismatch)
    errors = [item for item in mismatch["findings"] if item["severity"] == "error"]
    assert len(errors) == 1 and "文本框 2" in errors[0]["location"]


@pytest.mark.parametrize("pptx", [False, True])
@pytest.mark.parametrize("key,expectation", [("font_name", "Arial"), ("font_size", 12)])
def test_required_but_absent_script_is_uncovered_not_false_mismatch(
    factory, pptx, key, expectation
):
    path = factory.pptx(drawing_shape()) if pptx else factory.docx(paragraph(), styles())
    report = inspect_file(path, {key: {"ascii": expectation}})
    assert report["status"] != "passed"
    assert report["findings"]
    assert all(item["severity"] == "warning" for item in report["findings"])
    assert key not in rules(report)


@pytest.mark.parametrize("pptx", [False, True])
def test_unspecified_script_requirement_is_not_invented(factory, pptx):
    if pptx:
        path = factory.pptx(drawing_shape("English", props='<a:latin typeface="Arial"/>'))
    else:
        path = factory.docx(paragraph("English", props='<w:rFonts w:ascii="Arial"/>'), styles())
    assert inspect_file(path, {"font_name": {"ascii": "Arial"}})["status"] == "passed"


@pytest.mark.parametrize("kind", ["title", "ctrTitle"])
def test_ppt_explicit_title_font_and_size_are_independent_of_body(factory, kind):
    body = drawing_shape(props='<a:ea typeface="微软雅黑"/>', run_attributes='sz="2000"')
    title = drawing_shape(
        "标题", kind=kind, props='<a:ea typeface="黑体"/>', run_attributes='sz="4000"', identity=3
    )
    path = factory.pptx(body + title)
    profile = {
        "font_name": "微软雅黑",
        "font_size": 20,
        "title_font_name": "黑体",
        "title_font_size": 40,
    }
    assert inspect_file(path, profile)["status"] == "passed"
    wrong_size = inspect_file(path, {**profile, "title_font_size": 44})
    assert "title_font_size" in rules(wrong_size)
    assert "font_size" not in rules(wrong_size)
    wrong_font = inspect_file(path, {**profile, "title_font_name": "宋体"})
    assert "title_font_name" in rules(wrong_font)
    assert "font_name" not in rules(wrong_font)


def test_ppt_title_profile_supports_script_mapping(factory):
    title = drawing_shape(
        "中文 Title",
        kind="title",
        props='<a:ea typeface="黑体"/><a:latin typeface="Arial"/>',
        run_attributes='sz="4000"',
    )
    path = factory.pptx(title)
    profile = {
        "title_font_name": {"eastAsia": "黑体", "ascii": "Arial"},
        "title_font_size": {"eastAsia": 40, "ascii": 40},
    }
    assert inspect_file(path, profile)["status"] == "passed"


def test_docx_title_profile_is_explicitly_uncovered(factory):
    title_style = '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    path = factory.docx(paragraph("标题", pprops='<w:pStyle w:val="Title"/>'), styles(title_style))
    report = inspect_file(path, {"title_font_size": 40})
    assert report["status"] != "passed"
    assert all(item["severity"] == "warning" for item in report["findings"])
    assert "title_font_size" not in rules(report)
    assert any("标题" in item for item in report["coverage"])


def test_subtitle_does_not_satisfy_main_title_requirement(factory):
    path = factory.pptx(drawing_shape("副标题", kind="subTitle", run_attributes='sz="2400"'))
    report = inspect_file(path, {"title_font_size": 40})
    assert report["status"] != "passed"
    assert all(item["severity"] == "warning" for item in report["findings"])
    assert "title_font_size" not in rules(report)


@pytest.mark.parametrize("kind", [None, "body"])
def test_title_requirement_without_identified_title_cannot_pass(factory, kind):
    path = factory.pptx(drawing_shape(kind=kind))
    report = inspect_file(path, {"title_font_name": "黑体", "title_font_size": 40})
    assert report["status"] != "passed"
    assert all(item["severity"] == "warning" for item in report["findings"])


def test_docx_page_dimensions_use_twips_and_all_explicit_sections(factory):
    section = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    path = factory.docx(paragraph() + section, styles())
    assert inspect_file(path, {"page_width": 11906, "page_height": 16838})["status"] == "passed"
    mismatch = inspect_file(path, {"page_width": 12240})
    assert "page_width" in rules(mismatch)
    assert "EMU" not in repr(mismatch)
    first_section = (
        '<w:p><w:pPr><w:sectPr><w:pgSz w:w="12240" w:h="16838"/></w:sectPr></w:pPr></w:p>'
    )
    multiple = factory.docx(first_section + paragraph() + section, styles())
    assert "page_width" in rules(inspect_file(multiple, {"page_width": 11906}))


def test_docx_missing_page_dimensions_are_unresolved_not_passed(factory):
    report = inspect_file(factory.docx(paragraph(), styles()), {"page_width": 11906})
    assert report["status"] != "passed"
    assert all(item["severity"] == "warning" for item in report["findings"])
    assert "page_width" not in rules(report)


def test_ppt_locations_follow_relationship_order_not_slide_filename_order(factory):
    presentation = (
        f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}"><p:sldIdLst>'
        '<p:sldId id="256" r:id="second"/><p:sldId id="257" r:id="first"/>'
        "</p:sldIdLst></p:presentation>"
    )
    extra = {
        "ppt/presentation.xml": presentation,
        "ppt/_rels/presentation.xml.rels": relations(
            [
                ("first", "slide", "slides/slide1.xml", ""),
                ("second", "slide", "slides/slide9.xml", ""),
            ]
        ),
        "ppt/slides/slide9.xml": f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree>'
        + drawing_shape(props='<a:ea typeface="宋体"/>')
        + "</p:spTree></p:cSld></p:sld>",
    }
    path = factory.pptx(drawing_shape(props='<a:ea typeface="微软雅黑"/>'), extra)
    report = inspect_file(path, {"font_name": "微软雅黑"})
    errors = [item for item in report["findings"] if item["severity"] == "error"]
    assert len(errors) == 1
    assert errors[0]["rule"] == "font_name" and "第 1 页" in errors[0]["location"]


def test_ppt_missing_canvas_size_is_unresolved(factory):
    path = factory.pptx(drawing_shape())
    with ZipFile(path) as archive:
        presentation = (
            archive.read("ppt/presentation.xml")
            .decode()
            .replace('<p:sldSz cx="12192000" cy="6858000"/>', "")
        )
    data = rewrite_package(path.read_bytes(), replace_parts={"ppt/presentation.xml": presentation})
    report = inspect_office(data, "sample.pptx", {"slide_width": 12192000})
    assert report["status"] != "passed"
    assert "slide_size_unresolved" in rules(report)


@pytest.mark.parametrize("change", ["normAutofit", "spAutoFit", "fontScale"])
def test_ppt_visual_autofit_does_not_claim_known_rendered_font_size(factory, change):
    content = drawing_shape(run_attributes='sz="2000"')
    if change == "fontScale":
        body = '<a:bodyPr><a:normAutofit fontScale="80000"/></a:bodyPr>'
    else:
        body = f"<a:bodyPr><a:{change}/></a:bodyPr>"
    path = factory.pptx(content.replace("<a:bodyPr/>", body))
    report = inspect_file(path, {"font_size": 20})
    assert report["status"] != "passed"
    assert all(item["severity"] == "warning" for item in report["findings"])
    assert "font_size" not in rules(report)


def test_docx_conflicting_direct_and_theme_fonts_are_unresolved(factory):
    path = factory.docx(
        paragraph(props='<w:rFonts w:eastAsia="宋体" w:eastAsiaTheme="minorEastAsia"/>'),
        styles(),
        {
            "word/_rels/document.xml.rels": relations([("t", "theme", "theme/theme1.xml", "")]),
            "word/theme/theme1.xml": theme(),
        },
    )
    report = inspect_file(path, {"font_name": "微软雅黑"})
    assert "font_unresolved" in rules(report)
    assert "font_name" not in rules(report)


def test_style_inheritance_cycle_is_unresolved_not_guessed(factory):
    definition = (
        '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/>'
        '<w:basedOn w:val="Loop"/></w:style>'
    )
    definition += (
        '<w:style w:type="paragraph" w:styleId="Loop"><w:basedOn w:val="BodyText"/></w:style>'
    )
    path = factory.docx(paragraph(pprops='<w:pStyle w:val="BodyText"/>'), styles(definition))
    report = inspect_file(path, {"font_name": "微软雅黑"})
    assert "font_unresolved" in rules(report)
    assert "font_name" not in rules(report)


@pytest.mark.parametrize(
    "profile",
    [
        {"font_size": True},
        {"font_size": float("nan")},
        {"font_size": float("inf")},
        {"font_name": 123},
        {"font_name": {"eastAsia": 123}},
        {"font_size": {"ascii": -1}},
        {"title_font_size": 0},
        {"page_width": -1},
    ],
)
def test_invalid_profiles_never_turn_into_passed_checks(factory, profile):
    report = inspect_file(factory.docx(paragraph(), styles()), profile)
    assert report["status"] != "passed"
    assert "invalid_spec" in rules(report)


@pytest.mark.parametrize("field", ["_rels/.rels", "[Content_Types].xml"])
def test_missing_standard_opc_metadata_is_a_fixed_error(factory, field):
    data = factory.docx(paragraph(), styles()).read_bytes()
    assert_safe_error(inspect_office(rewrite_package(data, remove=[field]), "source.docx", {}))


@pytest.mark.parametrize("pptx", [False, True])
@pytest.mark.parametrize(
    "limit",
    [
        {"max_file_bytes": 40},
        {"max_archive_entries": 2},
        {"max_uncompressed_bytes": 64},
        {"max_xml_bytes": 64},
        {"max_xml_depth": 3},
    ],
)
def test_shared_package_limits_return_error_instead_of_partial_success(factory, pptx, limit):
    path = factory.pptx(drawing_shape()) if pptx else factory.docx(paragraph(), styles())
    report = inspect_file(path, {}, limits=replace(ExtractionLimits(), **limit))
    assert_safe_error(report)


def test_exact_package_budget_boundaries_are_accepted(factory):
    path = factory.docx(paragraph(), styles())
    with ZipFile(path) as archive:
        count = len(archive.infolist())
        uncompressed = sum(item.file_size for item in archive.infolist())
        xml_bytes = sum(
            item.file_size
            for item in archive.infolist()
            if item.filename.endswith((".xml", ".rels"))
        )
    limits = replace(
        ExtractionLimits(),
        max_file_bytes=path.stat().st_size,
        max_archive_entries=count,
        max_uncompressed_bytes=uncompressed,
        max_xml_bytes=xml_bytes,
    )
    assert inspect_file(path, {}, limits=limits)["status"] == "passed"


@pytest.mark.parametrize(
    "name",
    [
        "../outside.xml",
        "/absolute.xml",
        "word/%2e%2e/escape.xml",
        "C:/outside.xml",
        "word\\unsafe.xml",
    ],
)
def test_unsafe_zip_names_do_not_escape_or_leak_paths(factory, name):
    data = factory.docx(paragraph(), styles()).read_bytes()
    report = inspect_office(
        rewrite_package(data, add=[(name, f"<{PRIVATE}/>")]), f"/private/{PRIVATE}.docx", {}
    )
    assert_safe_error(report)


@pytest.mark.parametrize("name", ["word/document.xml", "WORD/DOCUMENT.XML", "word/%64ocument.xml"])
def test_duplicate_or_ambiguous_zip_parts_are_refused(factory, name):
    data = factory.docx(paragraph(), styles()).read_bytes()
    report = inspect_office(rewrite_package(data, add=[(name, f"<{PRIVATE}/>")]), "source.docx", {})
    assert_safe_error(report)


def test_symlink_zip_entry_is_not_followed(factory):
    entry = ZipInfo("word/external.xml")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = factory.docx(paragraph(), styles()).read_bytes()
    assert_safe_error(
        inspect_office(rewrite_package(data, add=[(entry, PRIVATE)]), "source.docx", {})
    )


@pytest.mark.parametrize("ole", [False, True])
def test_encrypted_zip_and_ole_containers_are_fixed_errors(factory, ole):
    data = bytearray(factory.docx(paragraph(), styles()).read_bytes())
    index = data.index(b"PK\x01\x02")
    flag = struct.unpack_from("<H", data, index + 8)[0]
    struct.pack_into("<H", data, index + 8, flag | 1)
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + PRIVATE.encode() if ole else bytes(data)
    assert_safe_error(inspect_office(payload, "source.docx", {}))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_is_rejected_in_ignored_xml_with_any_encoding(factory, encoding, caplog, capsys):
    data = factory.docx(paragraph(), styles()).read_bytes()
    malicious = (
        f'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///private/{PRIVATE}">]><x>&leak;</x>'.encode(
            encoding
        )
    )
    data = rewrite_package(data, add=[("customXml/ignored.xml", malicious)])
    assert_safe_error(inspect_office(data, "source.docx", {}))
    assert PRIVATE not in caplog.text + capsys.readouterr().err


def test_crc_failure_in_unused_binary_part_is_not_silently_ignored(factory):
    data = factory.docx(paragraph(), styles()).read_bytes()
    encoded = bytearray(
        rewrite_package(data, add=[("word/media/unused.bin", PRIVATE)], compression=ZIP_STORED)
    )
    encoded[encoded.index(PRIVATE.encode())] ^= 1
    assert_safe_error(inspect_office(bytes(encoded), "source.docx", {}))


def test_external_style_and_theme_relationships_are_not_accessed(factory, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Format checks must not access a network target")

    monkeypatch.setattr(socket, "socket", forbidden)
    path = factory.docx(
        paragraph(),
        extra={
            "word/_rels/document.xml.rels": relations(
                [
                    ("s", "styles", f"https://invalid.example/{PRIVATE}", ' TargetMode="External"'),
                    ("t", "theme", f"file:///private/{PRIVATE}", ' TargetMode="External"'),
                ]
            )
        },
    )
    report = inspect_file(path, {"font_name": "微软雅黑"})
    assert report["status"] != "passed"
    assert PRIVATE not in repr(report)


@pytest.mark.parametrize(
    "target",
    [
        "../../outside.xml",
        "file:///private/PRIVATE_SENTINEL_NEVER_RETURN",
        "slides/slide1.xml?query=secret",
        "http://[",
    ],
)
def test_unsafe_internal_relationship_target_is_refused(factory, target):
    path = factory.pptx(
        drawing_shape(),
        {"ppt/_rels/presentation.xml.rels": relations([("rId1", "slide", target, "")])},
    )
    assert_safe_error(inspect_file(path, {}))


def test_unsupported_namespaces_and_malformed_xml_never_pass(factory):
    for xml in [f"<{PRIVATE}>", "<document><body/></document>"]:
        path = factory.package(".docx", {"word/document.xml": xml})
        assert_safe_error(inspect_file(path, {}))


def test_placeholder_reports_use_fixed_messages_without_echoing_source(factory):
    report = inspect_file(factory.pptx(drawing_shape("请输入标题")), {})
    assert "placeholder_text" in rules(report)
    assert "请输入标题" not in repr(report["findings"])


def test_ppt_slide_count_limit_uses_actual_presentation_order(factory):
    path = factory.pptx(drawing_shape())
    with ZipFile(path) as archive:
        xml = (
            archive.read("ppt/presentation.xml")
            .decode()
            .replace("</p:sldIdLst>", '<p:sldId id="257" r:id="rId2"/></p:sldIdLst>')
        )
        slide = archive.read("ppt/slides/slide1.xml")
        types = (
            archive.read("[Content_Types].xml")
            .decode()
            .replace(
                "</Types>",
                '<Override PartName="/ppt/slides/slide9.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                "</Types>",
            )
        )
    data = rewrite_package(
        path.read_bytes(),
        replace_parts={
            "[Content_Types].xml": types,
            "ppt/presentation.xml": xml,
            "ppt/_rels/presentation.xml.rels": relations(
                [
                    ("rId1", "slide", "slides/slide1.xml", ""),
                    ("rId2", "slide", "slides/slide9.xml", ""),
                ]
            ),
        },
        add=[("ppt/slides/slide9.xml", slide)],
    )
    assert (
        inspect_office(data, "source.pptx", {}, limits=replace(ExtractionLimits(), max_pages=2))[
            "status"
        ]
        == "passed"
    )
    report = inspect_office(
        data, "source.pptx", {}, limits=replace(ExtractionLimits(), max_pages=1)
    )
    assert_safe_error(report)


def test_docx_theme_script_font_uses_related_settings_language(factory):
    defaults = '<w:rFonts w:eastAsiaTheme="minorEastAsia"/><w:sz w:val="24"/>'
    themed = theme().replace(
        '<a:ea typeface="微软雅黑"/>',
        '<a:ea typeface=""/><a:font script="Hans" typeface="微软雅黑"/>',
    )
    path = factory.docx(
        paragraph(),
        styles(defaults=defaults),
        {
            "word/_rels/document.xml.rels": relations([("t", "theme", "theme/theme1.xml", "")]),
            "word/theme/theme1.xml": themed,
            "word/settings.xml": (
                f'<w:settings xmlns:w="{W_NS}"><w:themeFontLang w:eastAsia="zh-CN"/></w:settings>'
            ),
        },
    )
    assert inspect_file(path, {"font_name": "微软雅黑", "font_size": 12})["status"] == "passed"


@pytest.mark.parametrize("direct", [False, True])
def test_missing_character_style_never_invents_inherited_effective_font(factory, direct):
    props = '<w:rStyle w:val="MissingCharacterStyle"/>'
    if direct:
        props += '<w:rFonts w:eastAsia="微软雅黑"/>'
    path = factory.docx(paragraph(props=props), styles(defaults='<w:rFonts w:eastAsia="宋体"/>'))
    report = inspect_file(path, {"font_name": "微软雅黑"})
    if direct:
        assert report["status"] == "passed"
    else:
        assert "font_unresolved" in rules(report)
        assert "font_name" not in rules(report)


@pytest.mark.parametrize("profile", [None, [], "PRIVATE_SENTINEL_NEVER_RETURN"])
def test_profile_type_errors_are_fixed_without_echoing_input(factory, profile):
    report = inspect_file(factory.docx(paragraph(), styles()), profile)
    assert report["status"] == "error"
    assert "invalid_spec" in rules(report)
    assert PRIVATE not in repr(report)


@pytest.mark.parametrize("strict", [False, True])
def test_historical_section_properties_do_not_override_current_page_dimensions(factory, strict):
    section = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:sectPrChange w:id="1"><w:sectPr>'
        '<w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:sectPrChange></w:sectPr>'
    )
    path = factory.docx(paragraph() + section, styles())
    payload = strict_package(path.read_bytes()) if strict else path.read_bytes()
    report = inspect_office(payload, "source.docx", {"page_width": 11906, "page_height": 16838})
    assert report["status"] == "passed"
    assert "page_width" not in rules(report) and "page_height" not in rules(report)


@pytest.mark.parametrize("strict", [False, True])
def test_word_content_control_body_is_checked_with_actual_paragraph_position(factory, strict):
    correct = paragraph(props='<w:rFonts w:eastAsia="微软雅黑"/>')
    wrong = paragraph(props='<w:rFonts w:eastAsia="宋体"/>')
    content_control = "<w:sdt><w:sdtPr/><w:sdtContent>" + wrong + "</w:sdtContent></w:sdt>"
    path = factory.docx(correct + content_control + correct, styles())
    payload = strict_package(path.read_bytes()) if strict else path.read_bytes()
    report = inspect_office(payload, "source.docx", {"font_name": "微软雅黑"})
    assert report["status"] == "issues"
    errors = [item for item in report["findings"] if item["severity"] == "error"]
    assert len(errors) == 1 and errors[0]["rule"] == "font_name"
    assert "第 2 段" in errors[0]["location"]
