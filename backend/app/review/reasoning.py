"""按绑定模块做逻辑与衔接轻检；真实正文和上下文分别核验，不作终审结论。"""

import hashlib
import re
from dataclasses import asdict
from uuid import UUID

from app.review.extract_types import ExtractedDocument
from app.review.reviewers.contract import adapt_findings, segment_inputs
from app.review.reviewers.executor import ReviewerExecutor, _safe_json
from app.review.reviewers.instructions import load_reasoning_instruction
from app.review.reviewers.reasoning_types import UpstreamSnapshot
from app.review.reviewers.schemas import HistoricalOpinion
from app.schemas.plans import PlanContent


def _check_plan(plan, module, upstream):
    logic_sources = {
        "module.goal": module.goal,
        "module.core_argument": module.core_argument,
        "plan.argument": plan.argument,
    }
    logic_checks = {
        "goal": {"available": bool(module.goal.strip()), "sources": {"module.goal"}},
        "argument": {
            "available": bool(module.core_argument.strip() or plan.argument.strip()),
            "sources": {"module.core_argument", "plan.argument"},
        },
        "support": {"available": True, "sources": set(logic_sources)},
    }
    linkage_sources = {
        "module.scope_in": module.scope_in,
        "module.scope_out": module.scope_out,
    }
    linkage_checks = {
        "scope": {
            "available": bool(module.scope_in.strip() or module.scope_out.strip()),
            "sources": {"module.scope_in", "module.scope_out"},
        }
    }
    for index, item in enumerate(plan.terms):
        key = f"term:{index}"
        linkage_sources[key] = "\n".join((item.term, item.definition, item.source))
        linkage_checks[key] = {"available": True, "sources": {key}}
    if not plan.terms:
        linkage_checks["terms"] = {"available": False, "sources": set()}
    for index, item in enumerate(plan.data_definitions):
        key = f"data:{index}"
        linkage_sources[key] = "\n".join((item.variable, item.source, item.processing))
        linkage_checks[key] = {
            "available": bool(item.source.strip() or item.processing.strip()),
            "sources": {key},
        }
    if not plan.data_definitions:
        linkage_checks["data_definitions"] = {"available": False, "sources": set()}
    snapshots = {item.module_id: item for item in upstream}
    for dependency in module.upstream:
        key = f"upstream:{dependency.module_id}"
        snapshot = snapshots.get(dependency.module_id)
        text = snapshot.handoff_note if snapshot is not None else ""
        linkage_sources[key] = text
        linkage_checks[key] = {"available": bool(text.strip()), "sources": {key}}
    if not module.upstream:
        linkage_checks["upstream"] = {"available": False, "sources": set()}
    return {
        "logic": (logic_sources, logic_checks),
        "linkage": (linkage_sources, linkage_checks),
    }


def _inputs(
    project_id, job_id, module_id, document, plan, specification, handoff_note, history, upstream
):
    if (
        not all(isinstance(value, UUID) for value in (project_id, job_id, module_id))
        or not isinstance(document, ExtractedDocument)
        or document.format not in {"docx", "pptx", "pdf", "md"}
        or re.fullmatch(r"[a-f0-9]{64}", document.sha256) is None
        or not isinstance(plan, dict)
        or not isinstance(specification, dict)
        or not isinstance(handoff_note, str)
        or len(handoff_note) > 10000
        or "\0" in handoff_note
        or not isinstance(history, (list, tuple))
        or len(history) > 500
        or not isinstance(upstream, list)
        or len(upstream) > 200
    ):
        raise ValueError
    validated_plan = PlanContent.model_validate(plan)
    identifiers = {item.id for item in validated_plan.modules}
    if len(identifiers) != len(validated_plan.modules) or module_id not in identifiers:
        raise ValueError
    module = next(item for item in validated_plan.modules if item.id == module_id)
    dependencies = {item.module_id for item in module.upstream}
    if (
        len(dependencies) != len(module.upstream)
        or module_id in dependencies
        or not dependencies <= identifiers
    ):
        raise ValueError
    snapshots = [
        UpstreamSnapshot.model_validate_json(
            _safe_json(item.model_dump(mode="json") if isinstance(item, UpstreamSnapshot) else item)
        )
        for item in upstream
    ]
    if len({item.module_id for item in snapshots}) != len(snapshots):
        raise ValueError
    if len({item.delivery_id for item in snapshots}) != len(snapshots):
        raise ValueError
    if any(
        item.project_id != project_id or item.module_id not in dependencies for item in snapshots
    ):
        raise ValueError
    if any("\0" in item.handoff_note for item in snapshots):
        raise ValueError
    historical = [
        HistoricalOpinion.model_validate_json(
            _safe_json(
                item.model_dump(mode="json") if isinstance(item, HistoricalOpinion) else item
            )
        )
        for item in history
    ]
    _safe_json(
        {
            "plan": plan,
            "specification": specification,
            "delivery": {**document.to_dict(), "handoff_note": handoff_note},
            "history": [item.model_dump(mode="json") for item in historical],
            "upstream": [item.model_dump(mode="json") for item in snapshots],
        }
    )
    return validated_plan, module, historical, snapshots


def review_reasoning(
    client,
    *,
    project_id: UUID,
    job_id: UUID,
    module_id: UUID,
    document: ExtractedDocument,
    plan: dict,
    specification: dict,
    handoff_note: str,
    history: list[HistoricalOpinion | dict] | tuple[HistoricalOpinion | dict, ...],
    upstream: list[UpstreamSnapshot | dict],
) -> dict:
    """只返回两位审核员的结果；不运行全项目衔接、标注、退回或终审。"""
    report = {
        "status": "incomplete",
        "logic": None,
        "linkage": None,
        "checks": {"logic": [], "linkage": []},
        "finding_links": {"logic": [], "linkage": []},
        "coverage": {"checked": [], "uncovered": []},
        "document_sha256": None,
        "extraction_warnings": [],
        "upstream_sources": [],
        "error_code": None,
    }
    try:
        validated_plan, module, historical, snapshots = _inputs(
            project_id,
            job_id,
            module_id,
            document,
            plan,
            specification,
            handoff_note,
            history,
            upstream,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        report["error_code"] = "input_invalid"
        return report
    report["document_sha256"] = document.sha256
    report["extraction_warnings"] = [asdict(warning) for warning in document.warnings]
    report["upstream_sources"] = [
        {
            **item.model_dump(mode="json", exclude={"handoff_note"}),
            "handoff_sha256": hashlib.sha256(item.handoff_note.encode("utf-8")).hexdigest(),
        }
        for item in snapshots
    ]
    if not any(segment.text.strip() for segment in document.segments):
        report["error_code"] = "source_unreadable"
        report["coverage"]["uncovered"] = ["没有可读交付正文，逻辑及衔接轻检未完成。"]
        return report
    checks_by_reviewer = _check_plan(validated_plan, module, snapshots)
    successful = {}
    notices = ["未核验外部全文、原始数据或视觉内容，意见仅供人工核对。"]
    if document.warnings:
        notices.append("文本抽取存在未覆盖内容，请查看extraction_warnings逐项提示。")
    executor = ReviewerExecutor(client)
    for reviewer in ("logic", "linkage"):
        sources, expected = checks_by_reviewer[reviewer]
        for key, setting in expected.items():
            if not setting["available"]:
                notices.append(f"{reviewer}/{key}：绑定上下文未提供可用依据，此范围未覆盖。")
        overview = (
            {
                "research_question": validated_plan.research_question,
                "argument": validated_plan.argument,
            }
            if reviewer == "logic"
            else {
                "terms": [item.model_dump(mode="json") for item in validated_plan.terms],
                "data_definitions": [
                    item.model_dump(mode="json") for item in validated_plan.data_definitions
                ],
            }
        )
        context = {
            "plan": {
                "overview": overview,
                "module": module.model_dump(mode="json"),
                "context_sources": [{"key": key, "text": text} for key, text in sources.items()],
                "expected_checks": [
                    {"key": key, "available": value["available"]} for key, value in expected.items()
                ],
            },
            "specification": specification,
            "delivery": {
                "format": document.format,
                "sha256": document.sha256,
                "segments": segment_inputs(document, handoff_note),
                "warnings": [asdict(warning) for warning in document.warnings],
            },
        }
        if reviewer == "linkage":
            context["delivery"]["upstream_snapshots"] = [
                item.model_dump(mode="json") for item in snapshots
            ]
        accepted = []

        def adapt(output):
            adapted = adapt_findings(
                output,
                category=reviewer,
                document=document,
                handoff_note=handoff_note,
                expected=expected,
                sources=sources,
                max_findings=20,
            )
            accepted[:] = [adapted]
            return adapted

        result = executor.run_compact(
            project_id=project_id,
            job_id=job_id,
            reviewer=reviewer,
            instruction=load_reasoning_instruction(reviewer),
            context=context,
            history=historical,
            adapt_output=adapt,
        )
        report[reviewer] = result.to_dict()
        if result.status == "completed":
            successful[reviewer] = accepted[0]
            for check in accepted[0].checks:
                if check["status"] == "uncovered":
                    notices.append(f"{reviewer}/{check['key']}：{check['reason']}")
        if result.coverage:
            for kind in ("checked", "uncovered"):
                report["coverage"][kind].extend(getattr(result.coverage, kind))
    for kind in ("checked", "uncovered"):
        values = (
            [*notices, *report["coverage"][kind]]
            if kind == "uncovered"
            else report["coverage"][kind]
        )
        report["coverage"][kind] = list(dict.fromkeys(values))[:100]
    if len(successful) != 2:
        report["error_code"] = "reviewer_incomplete"
        return report
    report["status"] = "completed"
    for reviewer, output in successful.items():
        report["checks"][reviewer] = list(output.checks)
        report["finding_links"][reviewer] = list(output.finding_links)
    return report
