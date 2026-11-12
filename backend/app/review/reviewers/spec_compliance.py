"""逐条对照绑定的AI规范；输出引用必须能回到本次实际输入。"""

import re
from dataclasses import asdict
from uuid import UUID

from pydantic import ValidationError

from app.review.extract_types import ExtractedDocument
from app.review.reviewers.contract import adapt_findings, segment_inputs
from app.review.reviewers.executor import ReviewerExecutor, _safe_json
from app.review.reviewers.instructions import load_spec_instruction
from app.review.reviewers.schemas import HistoricalOpinion
from app.schemas.specs import SpecItem


def review_specification(
    client,
    *,
    project_id: UUID,
    job_id: UUID,
    items: list[dict] | list[SpecItem],
    document: ExtractedDocument,
    handoff_note: str,
    plan: dict,
    history: list[HistoricalOpinion | dict] | tuple[HistoricalOpinion | dict, ...],
) -> dict:
    """可信内部组件，不执行文件下载、修改、退回、人工标注或终审。"""
    report = {
        "status": "incomplete",
        "review": None,
        "checks": [],
        "document_sha256": None,
        "extraction_warnings": [],
        "error_code": None,
    }
    try:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(job_id, UUID)
            or not isinstance(document, ExtractedDocument)
            or document.format not in {"docx", "pptx", "pdf", "md"}
            or re.fullmatch(r"[a-f0-9]{64}", document.sha256) is None
            or not isinstance(items, list)
            or len(items) > 500
            or not isinstance(plan, dict)
            or not isinstance(handoff_note, str)
            or len(handoff_note) > 10000
            or "\0" in handoff_note
            or not isinstance(history, (list, tuple))
            or len(history) > 500
        ):
            raise ValueError
        specs = [
            SpecItem.model_validate(
                item.model_dump(mode="json") if isinstance(item, SpecItem) else item
            )
            for item in items
        ]
        if len({item.number for item in specs}) != len(specs):
            raise ValueError
        # 即使没有AI条目也先验证历史与完整数据，不能静默忽略坏输入。
        historical = [
            record
            if isinstance(record, HistoricalOpinion)
            else HistoricalOpinion.model_validate_json(_safe_json(record))
            for record in history
        ]
        _safe_json(
            {
                "plan": plan,
                "document": document.to_dict(),
                "handoff_note": handoff_note,
                "history": [record.model_dump(mode="json") for record in historical],
            }
        )
    except (ValueError, ValidationError, TypeError, UnicodeError, RecursionError):
        report["error_code"] = "input_invalid"
        return report
    report["document_sha256"] = document.sha256
    report["extraction_warnings"] = [asdict(warning) for warning in document.warnings]
    ai = [item for item in specs if item.check_method == "ai"]
    report["checks"] = [
        {
            **item.model_dump(mode="json"),
            "status": "uncovered" if item.check_method == "ai" else "delegated",
            "reason": "规范审核未完成。"
            if item.check_method == "ai"
            else "此条由程序或人工检查处理。",
        }
        for item in specs
    ]
    if not ai:
        report["status"] = "no_ai_items"
        return report
    context = {
        "plan": plan,
        "specification": {"items": [item.model_dump(mode="json") for item in ai]},
        "delivery": {
            "sha256": document.sha256,
            "format": document.format,
            "segments": segment_inputs(document, handoff_note),
        },
    }
    accepted = {}

    def adapt(output):
        adapted = adapt_findings(
            output,
            category="specification",
            document=document,
            handoff_note=handoff_note,
            expected={
                item.number: {
                    "available": item.category != "template"
                    and bool(document.segments or handoff_note.strip()),
                    "sources": {item.number},
                }
                for item in ai
            },
            sources={item.number: item.requirement for item in ai},
            severities=("blocking", "important", "suggestion"),
            spec_numbers={item.number: item.number for item in ai},
        )
        accepted.update({check["key"]: check for check in adapted.checks})
        return adapted

    result = ReviewerExecutor(client).run_compact(
        project_id=project_id,
        job_id=job_id,
        reviewer="spec_compliance",
        instruction=load_spec_instruction(),
        context=context,
        history=historical,
        adapt_output=adapt,
    )
    report.update(status=result.status, review=result.to_dict(), error_code=result.error_code)
    if result.status == "completed":
        for check in report["checks"]:
            if check["number"] in accepted:
                value = accepted[check["number"]]
                check.update(status=value["status"], reason=value["reason"])
        # 抽取器的实际遗漏由平台保留，模型不能用空uncovered覆盖它。
        if report["extraction_warnings"]:
            coverage = report["review"]["coverage"]["uncovered"]
            message = "文本抽取存在未覆盖内容，请查看extraction_warnings逐项提示。"
            if len(coverage) < 100 and message not in coverage:
                coverage.append(message)
    return report
