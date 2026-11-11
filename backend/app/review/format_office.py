"""Conservative, read-only checks for a small, explicit Office format profile.

``font_name`` and ``font_size`` describe ordinary body text. Headings, tables,
and unclassified slide text boxes are not assumed to be body text. Optional
``slide_width`` and ``slide_height`` are canvas dimensions in OOXML EMUs.
Reports describe covered checks, never a complete visual or academic review.
"""

from __future__ import annotations

import math
import re

from app.review.extract_types import Collector, ExtractionError, ExtractionLimits, TextLocation
from app.review.ooxml_extract import _P, _W, _Package, _resolve_part

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_FINDINGS = 100
_UNKNOWN = object()
_CANONICAL_NAMESPACES = {
    "http://purl.oclc.org/ooxml/wordprocessingml/main": W[1:-1],
    "http://purl.oclc.org/ooxml/drawingml/main": A[1:-1],
    "http://purl.oclc.org/ooxml/presentationml/main": P[1:-1],
    "http://purl.oclc.org/ooxml/officeDocument/relationships": R[1:-1],
}

_PLACEHOLDER = re.compile(
    r"(?:请?输入(?:标题|副标题|正文(?:内容)?|内容|文字)[一二三四五六七八九十\d]*"
    r"|单击(?:此处)?添加(?:标题|副标题|文本|正文)"
    r"|click\s+to\s+(?:add|edit)\s+(?:title|subtitle|text|master\s+title\s+style))",
    re.IGNORECASE,
)
_FONT_ALIASES = {
    "microsoftyahei": "微软雅黑",
    "microsoftyaheiui": "微软雅黑ui",
    "simsun": "宋体",
    "simhei": "黑体",
    "dengxian": "等线",
    "kaiti": "楷体",
    "fangsong": "仿宋",
}


class _Report:
    def __init__(self, limits):
        self.findings = []
        self.coverage = []
        self._seen = set()
        self.truncated = False
        self.maximum = min(MAX_FINDINGS, limits.max_segments)
        self.attempted = set()

    def add(self, severity, location, rule, message):
        location, rule, message = str(location)[:160], str(rule)[:80], str(message)[:512]
        key = (severity, location, rule, message)
        if key in self._seen:
            return
        if len(self.findings) >= self.maximum:
            self.truncated = True
            return
        self._seen.add(key)
        self.findings.append(dict(severity=severity, location=location, rule=rule, message=message))

    def cover(self, message):
        message = str(message)[:512]
        if message not in self.coverage and len(self.coverage) < MAX_FINDINGS:
            self.coverage.append(message)

    def result(self, status=None, summary=None):
        if self.truncated:
            self.cover(f"检查结果超过 {self.maximum} 项，报告已截断，不能视为完整问题清单。")
        if status is None:
            status = "issues" if self.findings or self.truncated else "passed"
        errors = sum(f["severity"] == "error" for f in self.findings)
        warnings = sum(f["severity"] == "warning" for f in self.findings)
        if summary is None:
            summary = (
                f"有限范围检查发现 {errors} 项明确问题、{warnings} 项需人工确认。"
                if self.findings
                else "已覆盖的有限检查项未发现问题。"
            )
            summary += "不代表视觉排版、内容或引用已完整审核。"
        return dict(status=status, findings=self.findings, coverage=self.coverage, summary=summary)


class _OfficePackage:
    """Reuse M3-04's bounded OPC reader; canonicalize only this in-memory tree."""

    def __init__(self, data, kind, limits):
        self.collector = Collector(limits)
        self.package = _Package(data, self.collector)
        self.main, _ = self.package.main_part(
            _W if kind == "docx" else _P, "document" if kind == "docx" else "presentation"
        )
        self.relations = {}
        self.has_text = False
        for part, root in self.package.xml.items():
            for element in root.iter():
                element.tag = self._canonical(element.tag)
                element.attrib = {
                    self._canonical(key): value for key, value in element.attrib.items()
                }
                if element.tag in (W + "t", A + "t") and element.text and element.text.strip():
                    self.collector.add(element.text, TextLocation(part=part))
        root = self.xml(self.main, required=True)
        if kind == "docx":
            body = root.find(W + "body")
            if body is None:
                raise ExtractionError("invalid_document")
            self.has_text = any((node.text or "").strip() for node in body.iter(W + "t"))
        else:
            order = root.find(P + "sldIdLst")
            if order is not None and len(order) > limits.max_pages:
                raise ExtractionError("limit_exceeded")

    @staticmethod
    def _canonical(name):
        for strict, ordinary in _CANONICAL_NAMESPACES.items():
            if name.startswith("{" + strict + "}"):
                return "{" + ordinary + "}" + name.split("}", 1)[1]
        return name

    def xml(self, name, required=False):
        if name is _UNKNOWN:
            return _UNKNOWN
        if name not in self.package.xml:
            if required:
                raise ExtractionError("invalid_document")
            return None
        return self.package.xml[name]

    def relationships(self, part):
        if part is _UNKNOWN:
            return _UNKNOWN
        if not part:
            return {}
        if part not in self.relations:
            self.relations[part] = self.package.relationships(part)
        return self.relations[part]

    def related(self, part, relation_type):
        relations = self.relationships(part)
        if relations is _UNKNOWN:
            return _UNKNOWN
        matches = [item for item in relations.values() if item.kind == relation_type]
        if len(matches) > 1:
            return _UNKNOWN
        return self.target(part, matches[0]) if matches else None

    @staticmethod
    def target(part, relation):
        if relation.external:
            return None
        return _resolve_part(part, relation.target)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _metric(value, *, name, font, report):
    if value is None or value == "":
        return {}
    items = value.items() if isinstance(value, dict) else [("any", value)]
    result = {}
    for slot, expected in items:
        if slot not in {"eastAsia", "ascii", "any"} or (isinstance(value, dict) and slot == "any"):
            report.add(
                "warning", "项目规范", "invalid_spec", "字体或字号规范包含不支持的文字脚本。"
            )
            continue
        expected = expected.strip() if font and isinstance(expected, str) else expected
        if font:
            valid = isinstance(expected, str) and bool(expected) and len(expected) <= 200
        else:
            expected = _number(expected)
            valid = expected is not None
        if not valid:
            report.add(
                "warning", "项目规范", "invalid_spec", f"{name} 必须为有效字体名称或正数字号。"
            )
            continue
        result[slot] = expected
    if not result:
        report.add("warning", "项目规范", "invalid_spec", "字体或字号规范未提供可检查的值。")
    return result


def _missing_scripts(report, metrics):
    for name, expectations in metrics.items():
        for slot in expectations:
            if (name, slot) not in report.attempted:
                report.add(
                    "warning",
                    "文档",
                    "script_not_found",
                    f"未发现可按 {name} 检查的对应正文或文字脚本，不能确认该要求符合。",
                )


def _font_key(value):
    normalized = re.sub(r"\s+", "", value).casefold()
    return _FONT_ALIASES.get(normalized, normalized)


def _slots(text):
    slots = []
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text):
        slots.append("eastAsia")
    if re.search(r"[A-Za-z0-9]", text):
        slots.append("ascii")
    if any(
        c.isalpha()
        and ord(c) > 127
        and not (
            "\u3040" <= c <= "\u30ff" or "\u3400" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7af"
        )
        for c in text
    ):
        slots.append("unsupported")
    return slots


def _theme_font(theme, ref, slot, language=None):
    if theme is None or theme is _UNKNOWN:
        return None
    if ref.startswith(("major", "+mj")):
        group = "majorFont"
    elif ref.startswith(("minor", "+mn")):
        group = "minorFont"
    else:
        return None
    base = theme.find(".//" + A + "fontScheme/" + A + group)
    if base is None:
        return None
    if ref.endswith(("Ascii", "HAnsi", "-lt")):
        tag = "latin"
    elif ref.endswith(("EastAsia", "-ea")):
        tag = "ea"
    elif ref.endswith(("Bidi", "-cs")):
        tag = "cs"
    else:
        return None
    font = base.find(A + tag)
    if font is not None and font.get("typeface"):
        return font.get("typeface")
    if language is _UNKNOWN:
        return None
    script = {
        "zh-cn": "Hans",
        "zh-sg": "Hans",
        "zh-tw": "Hant",
        "zh-hk": "Hant",
        "zh-mo": "Hant",
        "ja-jp": "Jpan",
        "ko-kr": "Hang",
    }.get((language or "").lower())
    if script and tag == "ea":
        for font in base.findall(A + "font"):
            if font.get("script") == script:
                return font.get("typeface") or None
    return None


def _first_value(layers, child, attribute, namespace):
    for layer in layers:
        if layer is _UNKNOWN:
            return _UNKNOWN
        if layer is not None:
            prop = layer.find(namespace + child)
            if prop is not None and prop.get(namespace + attribute) is not None:
                return prop.get(namespace + attribute)
    return None


def _word_font(layers, slot, theme, language):
    for layer in layers:
        if layer is _UNKNOWN:
            return None
        fonts = layer.find(W + "rFonts") if layer is not None else None
        if fonts is None:
            continue
        actual = fonts.get(W + slot)
        ref = fonts.get(W + slot + "Theme")
        if actual is None and ref is None:
            continue
        themed = _theme_font(theme, ref, slot, language) if ref else None
        # Different applications resolve conflicting font metadata differently;
        # do not guess which visible font a user will see.
        if actual and ref and (not themed or _font_key(actual) != _font_key(themed)):
            return None
        return themed if ref else (actual or None)
    return None


def _check_text(
    report,
    text,
    layers,
    theme,
    expected_font,
    expected_size,
    location,
    kind,
    language=None,
    role="body",
    size_uncertain=False,
):
    font_rule = "title_font_name" if role == "title" else "font_name"
    size_rule = "title_font_size" if role == "title" else "font_size"
    slots = _slots(text)
    if "unsupported" in slots and (expected_font or expected_size):
        report.add(
            "warning",
            location,
            "script_uncovered",
            "此文字片段含未覆盖的文字脚本或扩展字符，不能确认该部分格式符合。",
        )
        report.cover("未覆盖：其他文字脚本及未识别的扩展字符；不将其套用到已知脚本规范。")
    if _UNKNOWN in layers:
        report.cover("未覆盖：存在缺失或歧义的继承候选，不能从较低优先级默认值猜测有效格式。")
    if expected_font:
        for slot in slots or ["unsupported"]:
            expected = expected_font.get(slot, expected_font.get("any"))
            if expected is None:
                continue
            report.attempted.add((font_rule, "any" if "any" in expected_font else slot))
            if slot == "unsupported":
                actual = None
            elif kind == "word":
                actual = _word_font(layers, slot, theme, language)
            else:
                actual = None
                tag = "ea" if slot == "eastAsia" else "latin"
                for layer in layers:
                    if layer is _UNKNOWN:
                        break
                    font = layer.find(A + tag) if layer is not None else None
                    if font is not None:
                        actual = font.get("typeface") or None
                        if actual and actual.startswith("+"):
                            actual = _theme_font(theme, actual, slot, language)
                        break
            if actual is None:
                report.add(
                    "warning",
                    location,
                    "font_unresolved",
                    f"无法可靠确定该文字的有效字体（{slot}），请人工确认。",
                )
            elif _font_key(actual) != _font_key(expected):
                report.add(
                    "error",
                    location,
                    font_rule,
                    f"该文字脚本（{slot}）的可解析有效字体不符合项目要求“{expected}”。",
                )
    if expected_size:
        checks = (
            [("any", expected_size["any"])]
            if "any" in expected_size
            else [(slot, expected_size[slot]) for slot in slots if slot in expected_size]
        )
        for slot, _ in checks:
            report.attempted.add((size_rule, slot))
        if not checks:
            return
        if size_uncertain:
            report.add(
                "warning",
                location,
                "font_size_unresolved",
                "存在自动适应或缩放，未检查渲染后的有效字号，请人工确认。",
            )
            report.cover("未覆盖：自动适应、变换缩放和渲染后的视觉字号。")
            return
        if kind == "word":
            raw = _first_value(layers, "sz", "val", W)
            actual = _number(raw) if raw is not _UNKNOWN else None
            actual = actual / 2 if actual else None
        else:
            raw = None
            for layer in layers:
                if layer is _UNKNOWN:
                    break
                if layer is not None and layer.get("sz") is not None:
                    raw = layer.get("sz")
                    break
            actual = _number(raw)
            actual = actual / 100 if actual else None
        if actual is None:
            report.add(
                "warning",
                location,
                "font_size_unresolved",
                "无法可靠确定正文有效字号，请人工确认。",
            )
        else:
            for slot, expected in checks:
                if abs(actual - expected) > 0.05:
                    report.add(
                        "error",
                        location,
                        size_rule,
                        f"可解析基础字号（{slot}）为 {actual:g} pt，不符合 {expected:g} pt 要求。",
                    )


def _placeholder(report, text, location):
    if _PLACEHOLDER.fullmatch(text.strip(" \t\r\n：:。.!！")):
        report.add(
            "warning",
            location,
            "placeholder_text",
            "发现疑似未替换的明确模板提示文字，请确认是否应保留。",
        )


def _word_runs(node):
    blocked = {W + name for name in ("drawing", "pict", "object", "txbxContent", "del", "moveFrom")}
    if node.tag in blocked or node.tag.endswith("}AlternateContent"):
        return
    if node.tag == W + "r":
        yield node
        return
    for child in node:
        yield from _word_runs(child)


def _word_paragraphs(node, in_table=False):
    blocked = {W + name for name in ("drawing", "pict", "object", "txbxContent", "del", "moveFrom")}
    if node.tag in blocked or node.tag.endswith("}AlternateContent"):
        return
    if node.tag == W + "p":
        yield node, in_table
        return
    for child in node:
        yield from _word_paragraphs(child, in_table or node.tag == W + "tbl")


def _slide_shapes(slide):
    tree = slide.find(P + "cSld/" + P + "spTree")
    if tree is None:
        raise ExtractionError("invalid_document")

    def visit(node, groups=()):
        if node.tag == P + "sp":
            yield node, groups
        elif node.tag == P + "grpSp":
            for child in node:
                yield from visit(child, (*groups, node))

    for child in tree:
        yield from visit(child)


def _inspect_docx(pkg, spec, report, expected_font, expected_size):
    root = pkg.xml(pkg.main, required=True)
    if root.tag != W + "document":
        raise ValueError("DOCX 主文档命名空间或结构不受支持")
    styles_root = pkg.xml(pkg.related(pkg.main, "styles"))
    styles = (
        {}
        if styles_root is None or styles_root is _UNKNOWN
        else {s.get(W + "styleId"): s for s in styles_root.findall(W + "style")}
    )
    if styles_root is not None and styles_root is not _UNKNOWN:
        definitions = styles_root.findall(W + "style")
        if any(not style.get(W + "styleId") for style in definitions) or len(styles) != len(
            definitions
        ):
            raise ExtractionError("invalid_document")
        default_paragraphs = [
            style
            for style in definitions
            if style.get(W + "type") == "paragraph" and style.get(W + "default") in ("1", "true")
        ]
        if len(default_paragraphs) > 1:
            raise ExtractionError("invalid_document")
    default_style = next(
        (
            s
            for s in styles.values()
            if s.get(W + "type") == "paragraph" and s.get(W + "default") in ("1", "true")
        ),
        None,
    )
    defaults = (
        styles_root.find(W + "docDefaults/" + W + "rPrDefault/" + W + "rPr")
        if styles_root is not None and styles_root is not _UNKNOWN
        else _UNKNOWN
    )
    theme_path = pkg.related(pkg.main, "theme")
    theme = pkg.xml(theme_path) if theme_path else None
    settings = pkg.xml(pkg.related(pkg.main, "settings"))
    theme_lang = (
        settings.find(W + "themeFontLang")
        if settings is not None and settings is not _UNKNOWN
        else None
    )
    fallback_lang = theme_lang.get(W + "eastAsia") if theme_lang is not None else None

    def chain(style):
        result, seen = [], set()
        while style is not None and len(result) < 20:
            identifier = style.get(W + "styleId")
            if identifier in seen:
                return result, False
            seen.add(identifier)
            result.append(style)
            parent = style.find(W + "basedOn")
            if parent is not None:
                style = styles.get(parent.get(W + "val"))
                if style is None:
                    return result, False
            else:
                style = None
        return result, style is None

    body = root.find(W + "body")
    if body is None:
        raise ValueError("DOCX 缺少正文区域")
    report.cover("已检查：DOCX 主文档及内容控件中的普通段落和明确占位提示文字。")
    report.cover("未覆盖：修订视图、兼容分支、绘图及文本框；删除内容不作为当前正文检查。")
    if expected_font or expected_size:
        report.cover(
            "已检查：可识别正文段落的直接格式、字符/段落样式继承、文档默认值及可解析主题字体。"
        )
        report.cover("未覆盖：标题、表格、文本框、页眉页脚、脚注、公式及实际渲染效果。")
    body_count = ordinary_text = 0
    _page_sizes(root, spec, report)
    for index, (paragraph, in_table) in enumerate(_word_paragraphs(body), 1):
        if in_table:
            continue
        runs = list(_word_runs(paragraph))
        text = "".join(t.text or "" for run in runs for t in run.findall(W + "t"))
        location = f"正文第 {index} 段"
        _placeholder(report, text, location)
        if text.strip():
            ordinary_text += 1
        if not text.strip() or not (expected_font or expected_size):
            continue
        pstyle = paragraph.find(W + "pPr/" + W + "pStyle")
        style = styles.get(pstyle.get(W + "val")) if pstyle is not None else default_style
        ancestors, paragraph_complete = chain(style)
        names = []
        for item in ancestors:
            name = item.find(W + "name")
            names.append((name.get(W + "val", "") if name is not None else "").casefold())
            names.append(item.get(W + "styleId", "").casefold())
        outlines = [paragraph.find(W + "pPr/" + W + "outlineLvl")] + [
            s.find(W + "pPr/" + W + "outlineLvl") for s in ancestors
        ]
        heading = any(
            n.startswith(("heading", "title", "subtitle", "标题", "副标题")) for n in names
        ) or any(o is not None and o.get(W + "val") != "9" for o in outlines)
        if heading:
            continue
        # A custom caption/quote may inherit Normal too. Only the effective
        # paragraph style's own identity identifies a body paragraph reliably.
        is_body = any(re.fullmatch(r"normal|body\s*text\d*|正文|普通", n) for n in names[:2])
        if not is_body:
            report.add(
                "warning",
                location,
                "body_role_unresolved",
                "无法确定该段是否为普通正文，未将正文字体和字号要求套用到此段。",
            )
            continue
        body_count += 1
        paragraph_layers = [s.find(W + "rPr") for s in ancestors]
        if not paragraph_complete:
            paragraph_layers.append(_UNKNOWN)
        for run_index, run in enumerate(runs, 1):
            run_text = "".join(t.text or "" for t in run.findall(W + "t"))
            if not run_text.strip():
                continue
            rpr = run.find(W + "rPr")
            rstyle = rpr.find(W + "rStyle") if rpr is not None else None
            char_style = styles.get(rstyle.get(W + "val")) if rstyle is not None else None
            chars, char_complete = chain(char_style)
            char_layers = [s.find(W + "rPr") for s in chars]
            if rstyle is not None and (char_style is None or not char_complete):
                char_layers.append(_UNKNOWN)
            layers = [rpr] + char_layers + paragraph_layers + [defaults]
            language = _first_value(layers, "lang", "eastAsia", W)
            if language is None:
                language = fallback_lang
            _check_text(
                report,
                run_text,
                layers,
                theme,
                expected_font,
                expected_size,
                f"{location}，文字片段 {run_index}",
                "word",
                language,
            )
    if (expected_font or expected_size) and not body_count:
        report.add(
            "warning",
            "文档",
            "body_not_identified",
            "未识别到可按正文规范检查的段落，不能确认正文字体和字号符合要求。",
        )
    if not ordinary_text:
        report.add(
            "warning", "文档", "no_body_text", "未发现可检查的普通正文段落，不能确认格式符合。"
        )
    if spec.get("title_font_name") or spec.get("title_font_size"):
        report.add("warning", "文档", "title_not_covered", "DOCX 标题归属及标题字体字号尚未覆盖。")
        report.cover("未覆盖：DOCX 标题字体、字号及标题层级。")


def _page_sizes(root, spec, report):
    requested = [
        (key, attr)
        for key, attr in (("page_width", "w"), ("page_height", "h"))
        if spec.get(key) not in (None, "")
    ]
    if not requested:
        return
    valid = []
    for key, attribute in requested:
        expected = _number(spec[key])
        if expected is None:
            report.add("warning", "项目规范", "invalid_spec", "页面尺寸须为正数，单位为 twip。")
        else:
            valid.append((key, attribute, expected))
    if not valid:
        return
    body = root.find(W + "body")
    sections = []
    for paragraph, in_table in _word_paragraphs(body):
        if not in_table:
            properties = paragraph.find(W + "pPr")
            if properties is not None:
                sections.extend(properties.findall(W + "sectPr"))
    sections.extend(body.findall(W + "sectPr"))
    report.cover("未覆盖：历史修订中的节尺寸；页面要求仅按当前正文的节属性检查。")
    if not sections:
        report.add("warning", "文档", "page_size_unresolved", "无法确定文档各节的页面尺寸。")
    for index, section in enumerate(sections, 1):
        size = section.find(W + "pgSz")
        for key, attribute, expected in valid:
            actual = _number(size.get(W + attribute)) if size is not None else None
            if actual is None:
                report.add(
                    "warning", f"第 {index} 节", "page_size_unresolved", "无法确定该节页面尺寸。"
                )
            else:
                report.cover("已检查：各节明确保存的页面尺寸（twip），不包含页边距或实际分页。")
                if abs(expected - actual) > 1:
                    report.add(
                        "error", f"第 {index} 节", key, "该节页面尺寸不符合项目要求（twip）。"
                    )


def _placeholder_identity(shape):
    if shape is _UNKNOWN:
        return _UNKNOWN
    return shape.find(".//" + P + "nvPr/" + P + "ph") if shape is not None else None


def _matching_shape(root, placeholder, by_type=False):
    if root is _UNKNOWN or placeholder is _UNKNOWN:
        return _UNKNOWN
    if root is None or placeholder is None:
        return None
    candidates, uncertain = _inherited_shapes(root)
    if uncertain:
        return _UNKNOWN
    matches = [
        shape
        for shape in candidates
        if (other := _placeholder_identity(shape)) is not None
        and other.get("idx", "0") == placeholder.get("idx", "0")
        and (
            not other.get("type")
            or not placeholder.get("type")
            or other.get("type") == placeholder.get("type")
        )
    ]
    if len(matches) > 1:
        return _UNKNOWN
    if matches:
        return matches[0]
    if by_type:
        same_type = [
            shape
            for shape in candidates
            if (other := _placeholder_identity(shape)) is not None
            and other.get("type", "obj") == placeholder.get("type", "obj")
        ]
        if len(same_type) == 1:
            return same_type[0]
        if same_type:
            return _UNKNOWN
    return None


def _inherited_shapes(root):
    tree = root.find(P + "cSld/" + P + "spTree")
    candidates = []
    uncertain = False

    def visit(node):
        nonlocal uncertain
        if node.tag.endswith("}AlternateContent"):
            uncertain = True
        elif node.tag == P + "sp":
            candidates.append(node)
        elif node.tag == P + "grpSp":
            for child in node:
                visit(child)

    if tree is not None:
        for child in tree:
            visit(child)
    return candidates, uncertain


def _list_layer(shape, level):
    if shape is _UNKNOWN:
        return _UNKNOWN
    if shape is None:
        return None
    choices = shape.findall(P + "txBody/" + A + "lstStyle/" + A + f"lvl{level}pPr/" + A + "defRPr")
    return _UNKNOWN if len(choices) > 1 else (choices[0] if choices else None)


def _paragraph_layer(shape, level):
    """Use a matching inherited paragraph only when it has unambiguous defaults."""
    if shape is _UNKNOWN:
        return _UNKNOWN
    if shape is None:
        return None
    choices = []
    for paragraph in shape.findall(P + "txBody/" + A + "p"):
        prop = paragraph.find(A + "pPr")
        if prop is not None and prop.get("lvl", "0") == str(level - 1):
            choices.append(prop.find(A + "defRPr"))
    return _UNKNOWN if len(choices) > 1 else (choices[0] if choices else None)


def _size_uncertain(*shapes):
    for shape in shapes:
        if shape is _UNKNOWN:
            return True
        if shape is None:
            continue
        body = shape.find(P + "txBody/" + A + "bodyPr")
        if body is not None and (
            body.find(A + "normAutofit") is not None
            or body.find(A + "spAutoFit") is not None
            or body.get("fontScale") is not None
        ):
            return True
        for transform in shape.iter(A + "xfrm"):
            extent = transform.find(A + "ext")
            child_extent = transform.find(A + "chExt")
            if child_extent is not None:
                if extent is None or any(
                    extent.get(attr) != child_extent.get(attr) for attr in ("cx", "cy")
                ):
                    return True
    return False


def _drawing_common(
    paragraph, shape, layout_shape, master_shape, master, presentation, role, report, location
):
    ppr = paragraph.find(A + "pPr")
    local = ppr.find(A + "defRPr") if ppr is not None else None
    try:
        level = int(ppr.get("lvl", "0")) + 1 if ppr is not None else 1
    except ValueError:
        level = None
    if level is None or not 1 <= level <= 9:
        report.add(
            "warning",
            location,
            "paragraph_level_unresolved",
            "段落级别无法确定，未从列表、版式或母版猜测继承格式。",
        )
        return [local, _UNKNOWN]
    style_name = "titleStyle" if role == "title" else "bodyStyle"
    master_style = (
        _UNKNOWN
        if master is _UNKNOWN
        else master.find(
            P + "txStyles/" + P + style_name + "/" + A + f"lvl{level}pPr/" + A + "defRPr"
        )
        if master is not None
        else None
    )
    default = presentation.find(P + "defaultTextStyle/" + A + f"lvl{level}pPr/" + A + "defRPr")
    return [
        local,
        _list_layer(shape, level),
        _paragraph_layer(layout_shape, level),
        _list_layer(layout_shape, level),
        _paragraph_layer(master_shape, level),
        _list_layer(master_shape, level),
        master_style,
        default,
    ]


def _inspect_pptx(pkg, spec, report, expected_font, expected_size):
    presentation = pkg.xml(pkg.main, required=True)
    if presentation.tag != P + "presentation":
        raise ValueError("PPTX 演示文稿命名空间或结构不受支持")
    report.cover("已检查：PPTX 演示文稿结构及幻灯片上的明确占位提示文字。")
    size = presentation.find(P + "sldSz")
    for key, attr in (("slide_width", "cx"), ("slide_height", "cy")):
        if spec.get(key) not in (None, ""):
            expected = _number(spec[key])
            actual = _number(size.get(attr)) if size is not None else None
            if expected is None:
                report.add("warning", "项目规范", "invalid_spec", f"{key} 应为正数，单位为 EMU。")
            elif actual is None:
                report.add("warning", "演示文稿", "slide_size_unresolved", "无法读取画布尺寸。")
            else:
                report.cover("已检查：明确指定的幻灯片画布尺寸（EMU）。")
                if abs(expected - actual) > 1:
                    report.add(
                        "error",
                        "演示文稿",
                        key,
                        f"画布 {key} 为 {actual:g} EMU，要求 {expected:g} EMU。",
                    )
    if expected_font or expected_size:
        report.cover("已检查：正文占位符中的直接格式、段落/列表、版式、母版及可解析主题继承。")
        report.cover("未覆盖：非语义文本框的正文归属、图中文字、字体实际替换、溢出及视觉排版。")
    report.cover(
        "未覆盖：副标题、表格、备注、兼容分支及实际渲染；noAutofit不代表已检查溢出或视觉字号。"
    )
    by_id = pkg.relationships(pkg.main)
    ids = presentation.find(P + "sldIdLst")
    if ids is None or not list(ids):
        report.add("warning", "演示文稿", "no_slides", "未发现可检查的幻灯片。")
        return
    identities, relation_ids = set(), set()
    title_font = spec["_title_font"]
    title_size = spec["_title_size"]
    for number, slide_id in enumerate(ids, 1):
        if (
            slide_id.tag != P + "sldId"
            or not slide_id.get("id")
            or slide_id.get("id") in identities
            or not slide_id.get(R + "id")
            or slide_id.get(R + "id") in relation_ids
        ):
            raise ExtractionError("invalid_document")
        identities.add(slide_id.get("id"))
        relation_ids.add(slide_id.get(R + "id"))
        relation = by_id.get(slide_id.get(R + "id"))
        if relation is not None and relation.kind != "slide":
            raise ExtractionError("invalid_document")
        path = pkg.target(pkg.main, relation) if relation is not None else None
        if path is None:
            report.add(
                "warning",
                f"第 {number} 页",
                "slide_unresolved",
                "无法安全定位该页，未读取外部目标。",
            )
            continue
        slide = pkg.xml(path, required=True)
        if slide.tag != P + "sld":
            raise ExtractionError("invalid_document")
        layout_path = pkg.related(path, "slideLayout")
        layout = pkg.xml(layout_path, required=True) if layout_path else None
        master_path = pkg.related(layout_path, "slideMaster") if layout_path else None
        master = pkg.xml(master_path, required=True) if master_path else None
        theme_path = pkg.related(master_path, "theme") if master_path else None
        theme = pkg.xml(theme_path, required=True) if theme_path else None
        body_count = ordinary_text = 0
        for shape_number, (shape, groups) in enumerate(_slide_shapes(slide), 1):
            txbody = shape.find(P + "txBody")
            if txbody is None:
                continue
            ph = _placeholder_identity(shape)
            layout_shape = _matching_shape(layout, ph)
            layout_ph = _placeholder_identity(layout_shape) if layout_shape is not None else None
            ph_type = ph.get("type") if ph is not None and ph is not _UNKNOWN else None
            ph_type = ph_type or (
                layout_ph.get("type")
                if layout_ph is not None and layout_ph is not _UNKNOWN
                else None
            )
            master_shape = _matching_shape(
                master, layout_ph if layout_ph is not None else ph, by_type=True
            )
            size_uncertain = _size_uncertain(shape, layout_shape, master_shape, *groups)
            for paragraph_number, paragraph in enumerate(txbody.findall(A + "p"), 1):
                text = "".join(t.text or "" for t in paragraph.iter(A + "t"))
                location = f"第 {number} 页，文本框 {shape_number}，第 {paragraph_number} 段"
                _placeholder(report, text, location)
                if not text.strip():
                    continue
                ordinary_text += 1
                role = "title" if ph_type in ("title", "ctrTitle") else "body"
                font, size = (
                    (title_font, title_size) if role == "title" else (expected_font, expected_size)
                )
                if not (font or size):
                    continue
                if ph_type in ("subTitle", "dt", "ftr", "sldNum"):
                    continue
                if role != "title" and ph_type != "body":
                    report.add(
                        "warning",
                        location,
                        "body_role_unresolved",
                        "该文本框未标记为正文占位符，未强行套用正文字体和字号要求。",
                    )
                    continue
                if role == "body":
                    body_count += 1
                common = _drawing_common(
                    paragraph,
                    shape,
                    layout_shape,
                    master_shape,
                    master,
                    presentation,
                    role,
                    report,
                    location,
                )
                for run_number, run in enumerate(list(paragraph), 1):
                    if run.tag not in (A + "r", A + "fld"):
                        continue
                    run_text = "".join(t.text or "" for t in run.findall(A + "t"))
                    if not run_text.strip():
                        continue
                    layers = [run.find(A + "rPr")] + common
                    language = None
                    for layer in layers:
                        if layer is _UNKNOWN:
                            language = _UNKNOWN
                            break
                        if layer is not None and layer.get("lang"):
                            language = layer.get("lang")
                            break
                    _check_text(
                        report,
                        run_text,
                        layers,
                        theme,
                        font,
                        size,
                        f"{location}，文字片段 {run_number}",
                        "drawing",
                        language,
                        role,
                        size_uncertain,
                    )
        if (expected_font or expected_size) and not body_count:
            report.add(
                "warning",
                f"第 {number} 页",
                "body_not_identified",
                "本页未识别到可检查的正文占位符，不能确认正文规范符合要求。",
            )
        if not ordinary_text:
            report.add(
                "warning", f"第 {number} 页", "no_body_text", "本页未发现可检查的文本框文字。"
            )


def _safe_failure(report, code):
    messages = {
        "encrypted_document": "Office 文件已加密，未完成格式检查。",
        "limit_exceeded": "Office 文件超过安全处理上限，未完成格式检查。",
    }
    report.add(
        "error",
        "文件",
        "invalid_office_file",
        messages.get(code, "无法安全读取 Office 文件，未完成格式检查。"),
    )
    report.cover("未覆盖：文件未能完整解析，不能判断其格式符合。")
    return report.result("error", "文件损坏、结构不受支持、加密或超出安全限制；未完成格式检查。")


class OfficeInspector:
    """Prepare one safe, read-only package for several independent profiles.

    The original v1 format inheritance algorithms are retained. Only explicitly
    classifiable text and stored OOXML dimensions are checked; this is not an
    Office layout engine. A constructor failure is retained as a safe report,
    never exposed as a parser exception or a partial success.
    """

    def __init__(self, data: bytes, filename: str, *, limits: ExtractionLimits | None = None):
        self.limits = limits if limits is not None else ExtractionLimits()
        self.package = None
        self.failure = None
        self.kind = ""
        try:
            if not isinstance(data, bytes) or not isinstance(filename, str):
                raise ExtractionError("invalid_document")
            parts = filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)
            self.kind = parts[-1].lower() if len(parts) == 2 else ""
            if self.kind in {"docx", "pptx"}:
                self.package = _OfficePackage(data, self.kind, self.limits)
        except ExtractionError as error:
            self.failure = error.code
        except (MemoryError, RecursionError):
            self.failure = "limit_exceeded"
        except Exception:
            self.failure = "invalid_document"

    def inspect(self, profile: dict) -> dict:
        report = _Report(self.limits)
        if self.failure is not None:
            return _safe_failure(report, self.failure)
        if self.kind not in {"docx", "pptx"}:
            report.add("warning", "文件", "unsupported_format", "本次格式检查仅支持 DOCX 和 PPTX。")
            return report.result("unsupported", "该文件类型未被检查，不能判断格式是否符合规范。")
        if not isinstance(profile, dict):
            report.add("error", "项目规范", "invalid_spec", "项目规范必须为字典。")
            return report.result("error", "未执行检查：项目规范结构无效。")
        spec = dict(profile)
        metrics = {
            name: _metric(spec.get(name), name=name, font=name.endswith("name"), report=report)
            for name in ("font_name", "font_size", "title_font_name", "title_font_size")
        }
        spec["_title_font"], spec["_title_size"] = (
            metrics["title_font_name"],
            metrics["title_font_size"],
        )
        for key, label in (
            ("body", "自由文本规范"),
            ("citation_style", "引用体例"),
            ("ppt_template", "学校模板的视觉一致性"),
        ):
            if spec.get(key):
                report.cover(
                    f"未覆盖：{label}；已记录该要求，请人工自查，不将未实现项判为格式错误。"
                )
        report.cover("未覆盖：引用真实性、论据与论断的对应关系及内容衔接。")
        try:
            if self.kind == "docx":
                _inspect_docx(
                    self.package, spec, report, metrics["font_name"], metrics["font_size"]
                )
            else:
                _inspect_pptx(
                    self.package, spec, report, metrics["font_name"], metrics["font_size"]
                )
            if self.package.collector.warnings:
                report.add(
                    "warning",
                    "文件",
                    "external_relationship_ignored",
                    "外部关系目标未读取，相关内容未覆盖。",
                )
                report.cover("未覆盖：外部链接、关系目标及其继承内容；不会读取网络或其他磁盘文件。")
            _missing_scripts(report, metrics)
        except ExtractionError as error:
            return _safe_failure(_Report(self.limits), error.code)
        except (MemoryError, RecursionError):
            return _safe_failure(_Report(self.limits), "limit_exceeded")
        except Exception:
            return _safe_failure(_Report(self.limits), "invalid_document")
        return report.result()


def inspect_office(
    data: bytes, filename: str, profile: dict, *, limits: ExtractionLimits | None = None
) -> dict:
    """Return bounded v1-shaped findings without reading paths or contacting models."""
    return OfficeInspector(data, filename, limits=limits).inspect(profile)
