"""从同一份已发布规范编译明确要求；整句无法识别时不猜测、不借用范例。"""

import math
import re
from dataclasses import dataclass

from app.schemas.specs import SpecItem

CHECKER_VERSION = "program-format/1"

# 非引号字体名仅接受明确名称，避免把“宋体，但……”等条件误吞成字体名称。
_FONT = (
    r"(?:宋体|黑体|微软雅黑|等线|仿宋|楷体|Arial|Times New Roman|Calibri|Cambria|"
    r"SimSun|SimHei|Microsoft YaHei|DengXian|KaiTi|FangSong|"
    r'“[^“”\r\n，,；;。]{1,100}”|"[^"\r\n，,；;。]{1,100}")'
)
_NUM = r"\d{1,8}(?:\.\d{1,8})?"
_WORD = r"(?:文稿|文档|Word|DOCX)"
_PPT = r"(?:PPT|PPTX|幻灯片)"
_UNIT = r"(?:磅|pt)"
_DUAL = re.compile(
    rf"{_WORD}\s*中文正文使用\s*(?P<cn>{_FONT})\s*、英文正文使用\s*"
    rf"(?P<en>{_FONT})\s*，均为\s*(?P<size>{_NUM})\s*{_UNIT}。?",
    re.IGNORECASE,
)
_PPT_COMBINED = re.compile(
    rf"{_PPT}\s*中文标题使用\s*(?P<title>{_FONT})\s*(?P<tsize>{_NUM})\s*{_UNIT}"
    rf"，正文使用\s*(?P<body>{_FONT})\s*(?P<bsize>{_NUM})\s*{_UNIT}"
    rf"，英文使用\s*(?P<en>{_FONT})\s*。?",
    re.IGNORECASE,
)
_SINGLE = re.compile(
    rf"(?P<format>{_WORD}|{_PPT})?\s*(?P<script>中文|英文)?(?P<role>正文|标题)"
    rf"(?:使用|字体为)\s*(?P<font>{_FONT})\s*"
    rf"(?:[，,]\s*字号(?:为)?\s*)?(?P<size>{_NUM})\s*{_UNIT}。?",
    re.IGNORECASE,
)
_SIZE = re.compile(
    rf"(?P<format>{_WORD}|{_PPT})?\s*(?P<script>中文|英文)?(?P<role>正文|标题)"
    rf"字号(?:为|使用)?\s*(?P<size>{_NUM})\s*{_UNIT}。?",
    re.IGNORECASE,
)
_FONT_ONLY = re.compile(
    rf"(?P<format>{_WORD}|{_PPT})?\s*(?P<script>中文|英文)?(?P<role>正文|标题)"
    rf"字体(?:为|使用)\s*(?P<font>{_FONT})\s*。?",
    re.IGNORECASE,
)
_CN_SIZE = re.compile(
    rf"(?P<format>{_WORD}|{_PPT})?\s*(?P<script>中文|英文)?(?P<role>正文|标题)"
    rf"使用\s*(?P<font>{_FONT})\s*、(?P<size>小四|四|小五|五)号。?",
    re.IGNORECASE,
)
_DIMENSION = re.compile(
    rf"(?P<format>{_WORD}|{_PPT})\s*(?:页面|画布)尺寸为\s*宽\s*(?P<width>{_NUM})"
    rf"\s*(?P<wu>厘米|毫米|英寸|cm|mm|in|EMU|twip)\s*[，,、×x]\s*高\s*"
    rf"(?P<height>{_NUM})\s*(?P<hu>厘米|毫米|英寸|cm|mm|in|EMU|twip)\s*。?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CompiledRule:
    formats: frozenset[str]
    profile: dict


def _font(value: str) -> str:
    return value.strip('“”"').strip()


def _positive(value: str) -> float | None:
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _formats(value: str | None) -> frozenset[str]:
    if value is None:
        return frozenset({"docx", "pptx"})
    return frozenset({"pptx" if re.fullmatch(_PPT, value, re.I) else "docx"})


def _compile_item(item: SpecItem) -> CompiledRule | None:
    """没有返回值表示整条未覆盖；正反例、编号、资源ID不会变成另一份规则。"""
    text = item.requirement.strip()
    if match := _DUAL.fullmatch(text):
        if (size := _positive(match["size"])) is None:
            return None
        return CompiledRule(
            frozenset({"docx"}),
            {
                "font_name": {"eastAsia": _font(match["cn"]), "ascii": _font(match["en"])},
                "font_size": {"eastAsia": size, "ascii": size},
            },
        )
    if match := _PPT_COMBINED.fullmatch(text):
        title_size, body_size = _positive(match["tsize"]), _positive(match["bsize"])
        if title_size is None or body_size is None:
            return None
        return CompiledRule(
            frozenset({"pptx"}),
            {
                "font_name": {"eastAsia": _font(match["body"]), "ascii": _font(match["en"])},
                "font_size": {"eastAsia": body_size},
                "title_font_name": {
                    "eastAsia": _font(match["title"]),
                    "ascii": _font(match["en"]),
                },
                "title_font_size": {"eastAsia": title_size},
            },
        )
    for pattern in (_SINGLE, _SIZE, _FONT_ONLY, _CN_SIZE):
        if not (match := pattern.fullmatch(text)):
            continue
        values = match.groupdict()
        formats = _formats(values["format"])
        if values["role"] == "标题" and formats != frozenset({"pptx"}):
            # Word标题层级没有明确指定，不能把全部标题当作同一级。
            return None
        slots = {"中文": ("eastAsia",), "英文": ("ascii",)}.get(
            values["script"], ("eastAsia", "ascii")
        )
        prefix = "title_" if values["role"] == "标题" else ""
        profile = {}
        if values.get("font"):
            profile[prefix + "font_name"] = {slot: _font(values["font"]) for slot in slots}
        if values.get("size"):
            named_size = {"小四": 12, "四": 14, "小五": 9, "五": 10.5}.get(values["size"])
            size = named_size if named_size is not None else _positive(values["size"])
            if size is None:
                return None
            profile[prefix + "font_size"] = dict.fromkeys(slots, size)
        return CompiledRule(formats, profile)
    if match := _DIMENSION.fullmatch(text):
        formats = _formats(match["format"])
        ppt = "pptx" in formats
        inch = 914400 if ppt else 1440
        profile = {}
        for dimension, group in (("width", "wu"), ("height", "hu")):
            unit = match[group].lower()
            if (ppt and unit == "twip") or (not ppt and unit == "emu"):
                return None
            multiplier = {
                "厘米": inch / 2.54,
                "cm": inch / 2.54,
                "毫米": inch / 25.4,
                "mm": inch / 25.4,
                "英寸": inch,
                "in": inch,
                "emu": 1,
                "twip": 1,
            }[unit]
            if (value := _positive(match[dimension])) is None:
                return None
            converted = round(value * multiplier)
            if converted < 1:
                return None
            profile[("slide_" if ppt else "page_") + dimension] = converted
        return CompiledRule(formats, profile)
    return None


def compile_item(item: SpecItem) -> CompiledRule | None:
    rule = _compile_item(item)
    if rule is not None:
        for key, value in rule.profile.items():
            if key.endswith("font_name") and any(not font for font in value.values()):
                return None
    return rule
