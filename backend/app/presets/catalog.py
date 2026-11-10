"""范例仅用于起草规范；引用条目明确标为虚构的格式示例。"""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class BuiltinTemplate:
    id: UUID
    filename: str
    size: int
    sha256: str
    path: Path


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
BUILTIN_TEMPLATES = (
    BuiltinTemplate(
        id=UUID("596ff1c1-9757-42c2-802c-e3cdb7400001"),
        filename="潜学成智-上外PPT模板.pptx",
        size=31456440,
        sha256="2fb31f192b48bda2cdda2d17014fa1c46675fe61618907a84c76f65498d0380b",
        path=_TEMPLATE_DIR / "潜学成智-上外PPT模板.pptx",
    ),
    BuiltinTemplate(
        id=UUID("596ff1c1-9757-42c2-802c-e3cdb7400002"),
        filename="笃思成章-上外PPT模板.pptx",
        size=61780355,
        sha256="15890ec541a0827540b53d167eec9c06688bacff7f1d4fbaa330f4ed5ca794c7",
        path=_TEMPLATE_DIR / "笃思成章-上外PPT模板.pptx",
    ),
)

_PRESETS = [
    {
        "key": "citation_gbt7714_2015",
        "name": "GB/T 7714-2015",
        "items": [
            {
                "number": "REF-GB-01",
                "category": "format",
                "requirement": (
                    "引用采用 GB/T 7714-2015 顺序编码制：正文按首次出现顺序标注 [1] 等编号，"
                    "文后按对应编号列出作者、题名、文献类型及相应出版信息。"
                    "以下仅为格式示例（虚构条目），不能作为真实参考文献使用。"
                ),
                "check_method": "ai",
                "positive_example": (
                    "格式示例（虚构条目）：[1] 张三. 科研协作示例[M]. 北京: 示例出版社, 2020."
                ),
                "negative_example": (
                    "格式示例（虚构条目）：科研协作示例，张三，2020。"
                    "（缺少编号、文献类型、出版地和出版社。）"
                ),
                "template_resource_id": None,
            }
        ],
    },
    {
        "key": "citation_apa7",
        "name": "APA 第7版",
        "items": [
            {
                "number": "REF-APA-01",
                "category": "format",
                "requirement": (
                    "引用采用 APA 第7版：正文使用作者-年份标注；文后条目按作者字母顺序排列，"
                    "列出作者、年份、题名与来源。期刊名和卷号使用斜体；"
                    "文献有 DOI 时附其 https://doi.org/ 链接。"
                    "以下仅为格式示例（虚构条目），不能作为真实参考文献使用。"
                ),
                "check_method": "ai",
                "positive_example": (
                    "格式示例（虚构条目；期刊名和卷号应设为斜体）：Chen, X., & Li, Y. (2020). "
                    "Example collaboration study. Example Journal, 12(3), 45–56."
                ),
                "negative_example": (
                    "格式示例（虚构条目）：Chen X. Example collaboration study, 2020. "
                    "（年份位置、作者写法不符，且缺少期刊名、卷期和页码。）"
                ),
                "template_resource_id": None,
            }
        ],
    },
    {
        "key": "fonts_cn_en",
        "name": "常用中英文字体与字号",
        "items": [
            {
                "number": "FMT-FONT-01",
                "category": "format",
                "requirement": "文稿中文正文使用宋体、英文正文使用 Times New Roman，均为12磅。",
                "check_method": "program",
                "positive_example": "中文正文为宋体12磅，英文正文为 Times New Roman 12磅。",
                "negative_example": "中文正文混用多种字体，英文正文字号与中文正文不一致。",
                "template_resource_id": None,
            },
            {
                "number": "FMT-FONT-02",
                "category": "format",
                "requirement": "PPT 中文标题使用黑体28磅，正文使用微软雅黑20磅，英文使用 Arial。",
                "check_method": "program",
                "positive_example": "幻灯片标题为黑体28磅，正文为微软雅黑20磅。",
                "negative_example": "同级标题字号不一致，正文小于规定字号。",
                "template_resource_id": None,
            },
        ],
    },
    {
        "key": "handoff",
        "name": "交接说明",
        "items": [
            {
                "number": "HANDOFF-01",
                "category": "handoff",
                "requirement": (
                    "交付时附上结论摘要、使用的数据及来源、给下游的说明和已知未解决问题。"
                ),
                "check_method": "ai",
                "positive_example": (
                    "结论摘要：……\n数据与来源：……\n给下游的说明：……\n"
                    "已知未解决问题：……（没有时明确写无）。"
                ),
                "negative_example": "仅上传成果文件，未说明结论、依据及下游需要关注的问题。",
                "template_resource_id": None,
            }
        ],
    },
]

for _key, _template in zip(("ppt_sisu_qianxue", "ppt_sisu_dusi"), BUILTIN_TEMPLATES, strict=True):
    _PRESETS.append(
        {
            "key": _key,
            "name": _template.filename.removesuffix(".pptx"),
            "items": [
                {
                    "number": f"TPL-{_key.removeprefix('ppt_').upper()}",
                    "category": "template",
                    "requirement": f"PPT 使用项目选定的《{_template.filename}》模板。",
                    "check_method": "manual",
                    "positive_example": "从所选模板创建幻灯片，保留其版式与母版。",
                    "negative_example": "使用其他模板或将模板截图作为整页背景。",
                    "template_resource_id": str(_template.id),
                }
            ],
        }
    )


def preset_catalog() -> list[dict]:
    return deepcopy(_PRESETS)
