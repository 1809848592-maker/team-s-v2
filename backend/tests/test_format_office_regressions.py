"""针对独立复核发现的继承歧义与错误覆盖范围回归。"""

import pytest
import test_format_check as authored
from test_format_check import (
    A_NS,
    P_NS,
    drawing_shape,
    paragraph,
    relations,
    styles,
)

from app.review.format_office import inspect_office


@pytest.fixture
def factory():
    helper = authored.FormatCheckTests()
    helper.setUp()
    yield helper
    helper.doCleanups()


def check(path, profile):
    return inspect_office(path.read_bytes(), path.name, profile)


@pytest.mark.parametrize("level", ["bad", "-1", "10"])
def test_invalid_ppt_level_never_falls_back_to_first_list_level(factory, level):
    shape = drawing_shape(level_defaults='<a:lvl1pPr><a:defRPr sz="2000"/></a:lvl1pPr>').replace(
        "<a:p>", f'<a:p><a:pPr lvl="{level}"/>'
    )
    report = check(factory.pptx(shape), {"font_size": 20})
    assert report["status"] == "issues"
    assert any(f["rule"] == "paragraph_level_unresolved" for f in report["findings"])
    assert not any(f["severity"] == "error" for f in report["findings"])


def test_ppt_layout_alternate_content_cannot_prove_visible_inheritance(factory):
    layout = (
        f'<p:sldLayout xmlns:p="{P_NS}" xmlns:a="{A_NS}" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        '<p:cSld><p:spTree><mc:AlternateContent><mc:Choice Requires="unavailable">'
        + drawing_shape(level_defaults='<a:lvl1pPr><a:defRPr sz="2000"/></a:lvl1pPr>')
        + "</mc:Choice><mc:Fallback/></mc:AlternateContent></p:spTree></p:cSld></p:sldLayout>"
    )
    extra = {
        "ppt/slides/_rels/slide1.xml.rels": relations(
            [("l", "slideLayout", "../slideLayouts/layout.xml", "")]
        ),
        "ppt/slideLayouts/layout.xml": layout,
    }
    report = check(factory.pptx(drawing_shape(), extra), {"font_size": 20})
    assert report["status"] == "issues"
    assert any(f["rule"] == "font_size_unresolved" for f in report["findings"])
    assert not any(f["rule"] == "font_size" for f in report["findings"])


@pytest.mark.parametrize("duplicate", ["id", "default"])
def test_duplicate_word_style_identity_or_defaults_fail_safely(factory, duplicate):
    extra = (
        '<w:style w:type="paragraph" w:styleId="Normal"/>'
        if duplicate == "id"
        else '<w:style w:type="paragraph" w:styleId="Other" w:default="1"/>'
    )
    report = check(factory.docx(paragraph(), styles(extra)), {"font_size": 12})
    assert report["status"] == "error"
    assert report["findings"][0]["rule"] == "invalid_office_file"


def test_word_unknown_character_style_blocks_theme_language_fallback(factory):
    theme = (
        f'<a:theme xmlns:a="{A_NS}"><a:themeElements><a:fontScheme name="Test">'
        '<a:minorFont><a:ea typeface=""/><a:font script="Hans" typeface="微软雅黑"/>'
        '<a:font script="Hant" typeface="宋体"/></a:minorFont>'
        "</a:fontScheme></a:themeElements></a:theme>"
    )
    extra = {
        "word/theme/theme.xml": theme,
        "word/settings.xml": (
            '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:themeFontLang w:eastAsia="zh-CN"/></w:settings>'
        ),
        "word/_rels/document.xml.rels": relations(
            [("t", "theme", "theme/theme.xml", ""), ("s", "settings", "settings.xml", "")]
        ),
    }
    path = factory.docx(
        paragraph(props='<w:rStyle w:val="Missing"/><w:rFonts w:eastAsiaTheme="minorEastAsia"/>'),
        styles(),
        extra,
    )
    report = check(path, {"font_name": "微软雅黑"})
    assert report["status"] == "issues"
    assert any(f["rule"] == "font_unresolved" for f in report["findings"])
    assert not any(f["rule"] == "font_name" for f in report["findings"])


@pytest.mark.parametrize("text", ["English é", "English 𠀀"])
def test_uncovered_extended_script_is_never_silently_passed(factory, text):
    path = factory.docx(paragraph(text, props='<w:rFonts w:ascii="Arial"/>'), styles())
    report = check(path, {"font_name": {"ascii": "Arial"}})
    assert report["status"] == "issues"
    assert any(f["rule"] == "script_uncovered" for f in report["findings"])
    assert not any(f["severity"] == "error" for f in report["findings"])
