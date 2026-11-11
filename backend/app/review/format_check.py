"""只读程序检查：逐条使用冻结规范，不维护独立规则表，也不调用模型。"""

import hashlib
from pathlib import Path

from pydantic import ValidationError

from app.review.extract_types import ExtractionLimits
from app.review.format_office import OfficeInspector, _font_key
from app.review.format_rules import CHECKER_VERSION, CompiledRule, compile_item
from app.schemas.specs import SpecItem

MAX_FINDINGS = 100


def _conflicts(compiled: dict[int, CompiledRule]) -> set[int]:
    """相同目标的互斥要求不能靠条目先后顺序覆盖，亦不能归咎于文件。"""
    targets: dict[tuple, dict[object, set[int]]] = {}
    for index, rule in compiled.items():
        for key, value in rule.profile.items():
            for slot, actual in value.items() if isinstance(value, dict) else [(None, value)]:
                normalized = _font_key(actual) if key.endswith("font_name") else actual
                targets.setdefault((key, slot), {}).setdefault(normalized, set()).add(index)
    conflicts = set()
    for values in targets.values():
        if len(values) > 1:
            conflicts.update(index for indices in values.values() for index in indices)
    return conflicts


def inspect_document(
    data: bytes,
    filename: str,
    items: list[dict] | list[SpecItem],
    *,
    limits: ExtractionLimits | None = None,
) -> dict:
    """返回条目级覆盖及有限不符合；unknown/未覆盖/不适用均不会伪装成通过。"""
    limits = limits if limits is not None else ExtractionLimits()
    result = {
        "checker_version": CHECKER_VERSION,
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": "partial",
        "checks": [],
        "findings": [],
        "coverage": ["未覆盖：实际视觉渲染、引用真实性、论据支撑与内容衔接。"],
        "summary": "程序检查未覆盖全部规范要求。",
    }
    if not isinstance(items, list) or len(items) > 500:
        result.update(status="error", summary="未执行检查：规范条目结构无效。")
        return result
    try:
        specs = [SpecItem.model_validate(item) for item in items]
        if len({item.number for item in specs}) != len(specs):
            raise ValueError
    except (ValidationError, ValueError, TypeError):
        result.update(status="error", summary="未执行检查：规范条目结构无效。")
        return result
    suffix = Path(filename).suffix.lower().lstrip(".")
    compiled = {}
    for index, item in enumerate(specs):
        if item.check_method == "program":
            if (rule := compile_item(item)) is not None and suffix in rule.formats:
                compiled[index] = rule
    conflicts = _conflicts(compiled)
    truncated = False
    inspector = None
    for index, item in enumerate(specs):
        check = {
            "spec_number": item.number,
            "category": item.category,
            "requirement": item.requirement,
            "check_method": item.check_method,
            "template_resource_id": (
                str(item.template_resource_id) if item.template_resource_id is not None else None
            ),
            "status": "uncovered",
            "coverage": [],
        }
        result["checks"].append(check)
        if item.check_method != "program":
            check.update(status="delegated", coverage=["未覆盖：此条由AI或人工检查处理。"])
            continue
        if suffix not in {"docx", "pptx"}:
            check["coverage"] = ["未覆盖：此文件类型不支持Office结构格式检查。"]
            continue
        rule = compile_item(item)
        if rule is None:
            check["coverage"] = [
                "未覆盖：未能完整解析本条要求；请按原规范人工确认。"
                if item.template_resource_id is None and item.category != "template"
                else "未覆盖：模板的视觉一致性无法仅凭资源关联或Office结构证明。"
            ]
            continue
        if suffix not in rule.formats:
            check.update(status="not_applicable", coverage=["不适用：本条明确要求另一文件类型。"])
            continue
        if index in conflicts:
            check["coverage"] = ["未覆盖：适用条目对同一格式属性提出互斥要求，请核对规范。"]
            continue
        # 将每条完整要求分开检查；未知规则不会吞掉已知规则，互斥规则不会后写覆盖。
        if inspector is None:
            inspector = OfficeInspector(data, filename, limits=limits)
        report = inspector.inspect(rule.profile)
        check.update(status=report["status"], coverage=report["coverage"])
        for finding in report["findings"]:
            if len(result["findings"]) >= MAX_FINDINGS:
                truncated = True
                break
            result["findings"].append(
                {
                    **finding,
                    "spec_number": item.number,
                    "requirement": item.requirement,
                    "blocking": finding["severity"] == "error" and report["status"] != "error",
                }
            )
    if truncated:
        result["coverage"].append("报告超过100条意见，已截断，不能视为完整检查。")
    program = [check for check in result["checks"] if check["check_method"] == "program"]
    states = {check["status"] for check in program}
    if suffix not in {"docx", "pptx"}:
        result.update(status="unsupported", summary="未覆盖：该文件类型不支持程序格式检查。")
    elif "error" in states:
        result.update(status="error", summary="文件或检查未能安全完成，不能判断格式符合。")
    elif truncated or "issues" in states:
        result.update(status="issues", summary="有限检查发现不符合或需人工确认项，请查看条目覆盖。")
    elif not program or "uncovered" in states:
        result.update(status="partial", summary="存在未覆盖要求，不能判断全部程序规范符合。")
    elif states == {"not_applicable"}:
        result.update(
            status="not_applicable", summary="程序条目明确适用于其他文件类型，未作通过判断。"
        )
    elif "passed" in states:
        result.update(
            status="passed", summary="已列明的有限程序检查未发现问题；未覆盖范围仍需其他审核。"
        )
    return result
